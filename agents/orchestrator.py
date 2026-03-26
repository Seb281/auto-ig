"""Pipeline orchestrator — coordinates the full post-creation pipeline."""

import logging
import os

from agents import CaptionResult, ImageResult, PipelineResult, PlannerBrief, ReviewResult
from agents.caption_writer import generate_caption
from agents.content_planner import generate_brief
from agents.image_sourcing import source_image
from agents.reviewer import review_post
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
) -> PipelineResult:
    """Run the content pipeline and return a PipelineResult."""
    if dry_run:
        logger.info("Dry-run mode noted — publish step will be skipped.")

    # Resolve media directory relative to the project root
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    media_dir = os.path.join(base_dir, "storage", "media")

    try:
        # Step 1: Generate or synthesize a brief
        if user_photo_path is not None:
            logger.info(
                "User-supplied photo detected — skipping planner."
            )
            brief = PlannerBrief(
                topic=user_hint or "User-supplied photo",
                angle="",
                visual_keywords=[],
                mood="",
                content_pillar="",
            )
        else:
            logger.info("Generating brief...")
            brief = await generate_brief(config, db_path, user_hint)

        # Step 2: Source an image
        logger.info("Sourcing image...")
        image: ImageResult = await source_image(
            config=config,
            brief=brief,
            db_path=db_path,
            media_dir=media_dir,
            user_photo_path=user_photo_path,
        )
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
            review = await review_post(config, brief, image, caption, db_path)

            if review.status == "PASS":
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
            if review.retry_type == "image":
                logger.info("Re-sourcing image for retry...")
                image = await source_image(
                    config=config,
                    brief=brief,
                    db_path=db_path,
                    media_dir=media_dir,
                    user_photo_path=user_photo_path,
                )
                logger.info(
                    "Image re-sourced — source: %s, score: %.2f",
                    image.source,
                    image.score,
                )
            elif review.retry_type == "caption":
                logger.info("Regenerating caption for retry...")
                caption = await generate_caption(config, brief)
                logger.info("Caption regenerated.")
            else:
                # No retry_type specified — retry caption as default
                logger.info("No retry_type specified — regenerating caption.")
                caption = await generate_caption(config, brief)

        # TODO: Milestone 5 — Publisher (temp server + Meta Graph API)

        logger.info("Pipeline complete.")
        return PipelineResult(
            success=review.status == "PASS" if review else False,
            post_id=None,
            brief=brief,
            image=image,
            caption=caption,
            review=review,
            error=None,
            skipped=False,
        )

    except Exception as exc:
        logger.error("Pipeline failed: %s", exc, exc_info=True)
        return PipelineResult(
            success=False,
            post_id=None,
            brief=None,
            image=None,
            caption=None,
            review=None,
            error=str(exc),
            skipped=False,
        )
