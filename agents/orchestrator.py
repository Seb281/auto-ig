"""Pipeline orchestrator — coordinates the full post-creation pipeline."""

import logging
import os

from agents import CaptionResult, ImageResult, PipelineResult, PlannerBrief, ReviewResult, VideoResult
from agents.caption_writer import generate_caption
from agents.content_planner import generate_brief
from agents.image_sourcing import source_image, source_carousel_images
from agents.reviewer import review_post, review_carousel_post, review_reel_post, RETRY_CAPTION, RETRY_IMAGE, RETRY_VIDEO, STATUS_PASS
from agents.video_sourcing import source_video
from utils.config_loader import AccountConfig

logger = logging.getLogger(__name__)

# Maximum number of reviewer retry attempts before escalation
MAX_REVIEW_RETRIES = 2


async def run_pipeline(
    config: AccountConfig,
    db_path: str,
    user_photo_path: str | None = None,
    user_hint: str | None = None,
    dry_run: bool = False,
    stock_only: bool = False,
) -> PipelineResult:
    """Run the content pipeline and return a PipelineResult."""
    if dry_run:
        logger.info("Dry-run mode noted — publish step will be skipped.")

    # Resolve media directory relative to the project root
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    media_dir = os.path.join(base_dir, "storage", "media")

    image: ImageResult | None = None
    images: list[ImageResult] = []
    video: VideoResult | None = None
    try:
        # Step 1: Generate or synthesize a brief
        if user_photo_path is not None:
            logger.info(
                "User-supplied photo detected — skipping planner, forcing single_image."
            )
            brief = PlannerBrief(
                topic=user_hint or "User-supplied photo",
                angle="",
                visual_keywords=[],
                mood="",
                content_pillar="",
                content_type="single_image",
            )
        else:
            logger.info("Generating brief...")
            brief = await generate_brief(config, db_path, user_hint)

        is_carousel = brief.content_type == "carousel" and user_photo_path is None
        is_reel = brief.content_type == "reel" and user_photo_path is None

        # Step 2: Source media
        if is_reel:
            logger.info("Sourcing reel video...")
            video = await source_video(
                config=config,
                brief=brief,
                db_path=db_path,
                media_dir=media_dir,
            )
            logger.info(
                "Video sourced — source: %s, score: %.2f, duration: %.1fs, path: %s",
                video.source,
                video.score,
                video.duration_seconds,
                video.local_path,
            )
        elif is_carousel:
            logger.info("Sourcing carousel images (3-5)...")
            images = await source_carousel_images(
                config=config,
                brief=brief,
                db_path=db_path,
                media_dir=media_dir,
                stock_only=stock_only,
            )
            # Use the first image as the primary for backwards compatibility
            image = images[0] if images else None
            logger.info(
                "Carousel images sourced — %d images.", len(images)
            )
        else:
            logger.info("Sourcing image...")
            image = await source_image(
                config=config,
                brief=brief,
                db_path=db_path,
                media_dir=media_dir,
                user_photo_path=user_photo_path,
                stock_only=stock_only,
            )
            images = [image]
            logger.info(
                "Image sourced — source: %s, score: %.2f, path: %s",
                image.source,
                image.score,
                image.local_path,
            )

        # Step 3: Generate caption
        logger.info("Generating caption...")
        caption: CaptionResult = await generate_caption(config, brief)

        # Step 4: Reviewer with retry logic (up to MAX_REVIEW_RETRIES)
        review: ReviewResult | None = None
        for attempt in range(1, MAX_REVIEW_RETRIES + 1):
            logger.info("Running reviewer (attempt %d/%d)...", attempt, MAX_REVIEW_RETRIES)

            if is_reel:
                review = await review_reel_post(config, brief, video, caption, db_path)
            elif is_carousel:
                review = await review_carousel_post(config, brief, images, caption, db_path)
            else:
                review = await review_post(config, brief, image, caption, db_path)

            if review.status == STATUS_PASS:
                logger.info("Reviewer PASSED on attempt %d.", attempt)
                break

            logger.warning(
                "Reviewer FAILED on attempt %d: %s (retry_type=%s)",
                attempt,
                "; ".join(review.reasons),
                review.retry_type,
            )

            # Don't retry after the last attempt
            if attempt >= MAX_REVIEW_RETRIES:
                logger.warning(
                    "Reviewer failed after %d attempts — escalating.",
                    MAX_REVIEW_RETRIES,
                )
                break

            # Retry the appropriate upstream step
            if review.retry_type == RETRY_VIDEO:
                logger.info("Re-sourcing video for retry...")
                if video is not None and video.local_path and os.path.exists(video.local_path):
                    os.remove(video.local_path)
                video = await source_video(
                    config=config,
                    brief=brief,
                    db_path=db_path,
                    media_dir=media_dir,
                )
                logger.info(
                    "Video re-sourced — source: %s, score: %.2f",
                    video.source,
                    video.score,
                )
            elif review.retry_type == RETRY_IMAGE:
                if is_carousel:
                    logger.info("Re-sourcing carousel images for retry...")
                    # Clean up old images
                    for img in images:
                        if img.local_path and os.path.exists(img.local_path):
                            os.remove(img.local_path)
                    images = await source_carousel_images(
                        config=config,
                        brief=brief,
                        db_path=db_path,
                        media_dir=media_dir,
                        stock_only=stock_only,
                    )
                    image = images[0] if images else None
                else:
                    logger.info("Re-sourcing image for retry...")
                    image = await source_image(
                        config=config,
                        brief=brief,
                        db_path=db_path,
                        media_dir=media_dir,
                        user_photo_path=user_photo_path,
                        stock_only=stock_only,
                    )
                    images = [image]
                    logger.info(
                        "Image re-sourced — source: %s, score: %.2f",
                        image.source,
                        image.score,
                    )
            elif review.retry_type == RETRY_CAPTION:
                logger.info("Regenerating caption for retry...")
                caption = await generate_caption(config, brief)
                logger.info("Caption regenerated.")
            else:
                # No retry_type specified — retry caption as default
                logger.info("No retry_type specified — regenerating caption.")
                caption = await generate_caption(config, brief)

        review_passed = review is not None and review.status == STATUS_PASS

        if not review_passed:
            logger.warning("Reviewer did not pass — content will be escalated.")

        logger.info("Pipeline complete.")
        return PipelineResult(
            success=review_passed,
            post_id=None,
            brief=brief,
            image=image,
            caption=caption,
            review=review,
            error=None,
            skipped=False,
            images=images,
            video=video,
        )

    except Exception as exc:
        logger.error("Pipeline failed: %s", exc, exc_info=True)
        # Clean up images and video on error — no draft will be created to manage them
        all_image_paths = set()
        if image is not None and image.local_path:
            all_image_paths.add(image.local_path)
        for img in images:
            if img.local_path:
                all_image_paths.add(img.local_path)
        if video is not None and video.local_path:
            all_image_paths.add(video.local_path)
        for path in all_image_paths:
            if os.path.exists(path):
                os.remove(path)
                logger.info("Cleaned up media after error: %s", path)
        return PipelineResult(
            success=False,
            post_id=None,
            brief=None,
            image=None,
            caption=None,
            review=None,
            error=str(exc),
            skipped=False,
            images=[],
        )
