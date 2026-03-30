"""Image Sourcing Agent — finds or generates an image for the post."""

import asyncio
import logging
import os
import uuid

from agents import ImageResult, PlannerBrief
from utils.ai_client import generate_image, generate_vision, read_image_file
from utils.config_loader import AccountConfig
from utils.image_utils import (
    compute_phash,
    copy_user_photo,
    is_duplicate_image,
    resize_for_instagram,
)
from utils.prompts import (
    _extract_json,
    build_image_gen_prompt,
    build_vision_scoring_prompt,
)
from utils.stock_search import (
    StockPhoto,
    download_image,
    search_pexels,
    search_unsplash,
)

logger = logging.getLogger(__name__)

# Maximum number of stock photo candidates to score with vision
MAX_CANDIDATES_TO_SCORE = 3

# Carousel image count range
CAROUSEL_MIN_IMAGES = 3
CAROUSEL_MAX_IMAGES = 5

# Map source name to search function
_SEARCH_FUNCTIONS = {
    "unsplash": search_unsplash,
    "pexels": search_pexels,
}


def _write_bytes_sync(path: str, data: bytes) -> None:
    """Write raw bytes to a file (sync, for use with to_thread)."""
    with open(path, "wb") as f:
        f.write(data)


async def _score_candidate(
    image_path: str,
    config: AccountConfig,
    brief: PlannerBrief,
) -> float:
    """Score a single candidate image using AI vision."""
    prompt_text = build_vision_scoring_prompt(config, brief)

    image_bytes, mime_type = await asyncio.to_thread(read_image_file, image_path)

    try:
        raw_text = await generate_vision(image_bytes, mime_type, prompt_text)
        data = _extract_json(raw_text)
        score = float(data.get("score", 0.0))
        reasoning = data.get("reasoning", "")
        logger.info(
            "Vision score for %s: %.2f — %s",
            os.path.basename(image_path),
            score,
            reasoning[:100],
        )
        return max(0.0, min(1.0, score))

    except Exception as exc:
        logger.warning(
            "Vision scoring failed for %s: %s. Assigning score 0.0.",
            os.path.basename(image_path),
            exc,
        )
        return 0.0


async def _search_stock_photos(
    config: AccountConfig,
    brief: PlannerBrief,
) -> list[StockPhoto]:
    """Search all configured stock photo sources in priority order."""
    all_results: list[StockPhoto] = []

    for source_name in config.image_sourcing.sources:
        search_fn = _SEARCH_FUNCTIONS.get(source_name)
        if search_fn is None:
            logger.warning("Unknown stock source '%s', skipping.", source_name)
            continue

        try:
            results = await search_fn(brief.visual_keywords)
            all_results.extend(results)
            logger.info(
                "Got %d results from %s.", len(results), source_name
            )
        except Exception as exc:
            logger.warning(
                "Stock search failed for %s: %s", source_name, exc
            )

    return all_results


async def _generate_ai_image(
    config: AccountConfig,
    brief: PlannerBrief,
    media_dir: str,
) -> str:
    """Generate an image using Gemini and return the local file path."""
    prompt = build_image_gen_prompt(config, brief)
    logger.info("Generating AI image with prompt: %s", prompt[:120])

    image_bytes = await generate_image(prompt)

    dest_filename = f"generated_{uuid.uuid4().hex[:8]}.png"
    dest_path = os.path.join(media_dir, dest_filename)
    await asyncio.to_thread(_write_bytes_sync, dest_path, image_bytes)

    logger.info("AI-generated image saved to %s", dest_path)
    return dest_path


async def source_image(
    config: AccountConfig,
    brief: PlannerBrief,
    db_path: str,
    media_dir: str,
    user_photo_path: str | None = None,
    stock_only: bool = False,
) -> ImageResult:
    """Source an image following priority: user photo -> stock -> AI generation."""
    os.makedirs(media_dir, exist_ok=True)
    temp_files: list[str] = []

    try:
        # Priority 1: User-supplied photo
        if user_photo_path is not None:
            logger.info("Using user-supplied photo: %s", user_photo_path)
            dest_filename = f"user_{uuid.uuid4().hex[:8]}.jpg"
            dest_path = os.path.join(media_dir, dest_filename)
            await copy_user_photo(user_photo_path, dest_path)

            resized_path = os.path.join(
                media_dir, f"resized_{dest_filename}"
            )
            await resize_for_instagram(dest_path, resized_path)
            temp_files.append(dest_path)

            phash = compute_phash(resized_path)
            return ImageResult(
                local_path=resized_path,
                source="user",
                phash=phash,
                score=1.0,
            )

        # Priority 2: Stock photo search
        stock_results = await _search_stock_photos(config, brief)
        best_score = 0.0
        best_path: str | None = None
        best_source: str = ""

        if stock_results:
            # Download and score top candidates
            candidates = stock_results[:MAX_CANDIDATES_TO_SCORE]
            for i, photo in enumerate(candidates):
                candidate_filename = f"candidate_{i}_{uuid.uuid4().hex[:8]}.jpg"
                candidate_path = os.path.join(media_dir, candidate_filename)

                try:
                    await download_image(photo.url, candidate_path)
                    temp_files.append(candidate_path)
                except ConnectionError as exc:
                    logger.warning("Failed to download candidate %d: %s", i, exc)
                    continue

                score = await _score_candidate(candidate_path, config, brief)
                if score > best_score:
                    best_score = score
                    best_path = candidate_path
                    best_source = photo.source

            logger.info(
                "Best stock photo score: %.2f (threshold: %.2f)",
                best_score,
                config.image_sourcing.stock_score_threshold,
            )

        # In stock_only mode, accept any stock photo; otherwise require threshold
        score_ok = (
            best_score >= config.image_sourcing.stock_score_threshold
            if not stock_only
            else best_score > 0.0
        )

        # Use stock photo if it meets the threshold (or stock_only forces it)
        if best_path is not None and score_ok:
            resized_filename = f"resized_{uuid.uuid4().hex[:8]}.jpg"
            resized_path = os.path.join(media_dir, resized_filename)
            await resize_for_instagram(best_path, resized_path)

            phash = compute_phash(resized_path)

            # Check for duplicate
            is_dup = await is_duplicate_image(
                phash, db_path, config.account_id
            )
            if is_dup:
                logger.info(
                    "Stock photo is a duplicate. Falling through to AI generation."
                )
            else:
                # Remove unused candidate temp files (keep only the resized one)
                for tf in temp_files:
                    if tf != best_path and os.path.exists(tf):
                        os.remove(tf)
                        logger.debug("Cleaned up temp file: %s", tf)
                # Remove the original candidate too (we have the resized version)
                if os.path.exists(best_path):
                    os.remove(best_path)
                temp_files.clear()

                return ImageResult(
                    local_path=resized_path,
                    source=best_source,
                    phash=phash,
                    score=best_score,
                )

        # Priority 3: AI image generation fallback
        if stock_only:
            raise RuntimeError(
                "No suitable stock photo found and AI image generation is disabled (stock_only mode)."
            )
        logger.info("Falling back to AI image generation.")
        generated_path = await _generate_ai_image(config, brief, media_dir)
        temp_files.append(generated_path)

        resized_filename = f"resized_generated_{uuid.uuid4().hex[:8]}.jpg"
        resized_path = os.path.join(media_dir, resized_filename)
        await resize_for_instagram(generated_path, resized_path)

        phash = compute_phash(resized_path)

        # Clean up the raw generated file
        if os.path.exists(generated_path):
            os.remove(generated_path)

        # Remove all other temp files
        for tf in temp_files:
            if tf != generated_path and os.path.exists(tf):
                os.remove(tf)
                logger.debug("Cleaned up temp file: %s", tf)
        temp_files.clear()

        return ImageResult(
            local_path=resized_path,
            source="gemini",
            phash=phash,
            score=best_score if best_score > 0 else 0.5,
        )

    except Exception:
        # Clean up all temp files on failure
        for tf in temp_files:
            if os.path.exists(tf):
                try:
                    os.remove(tf)
                    logger.debug("Cleaned up temp file on error: %s", tf)
                except OSError:
                    pass
        raise


async def source_carousel_images(
    config: AccountConfig,
    brief: PlannerBrief,
    db_path: str,
    media_dir: str,
    stock_only: bool = False,
) -> list[ImageResult]:
    """Source 3-5 images for a carousel post, each scored independently."""
    os.makedirs(media_dir, exist_ok=True)
    temp_files: list[str] = []
    accepted_images: list[ImageResult] = []

    try:
        # Search for stock photos (request more results for carousel)
        stock_results = await _search_stock_photos(config, brief)

        # Score and collect candidates — we need more than for single image
        max_candidates = CAROUSEL_MAX_IMAGES * 2  # Score extra to have fallbacks
        candidates_to_score = stock_results[:max_candidates]

        scored_candidates: list[tuple[str, float, str]] = []  # (path, score, source)

        for i, photo in enumerate(candidates_to_score):
            candidate_filename = f"carousel_candidate_{i}_{uuid.uuid4().hex[:8]}.jpg"
            candidate_path = os.path.join(media_dir, candidate_filename)

            try:
                await download_image(photo.url, candidate_path)
                temp_files.append(candidate_path)
            except ConnectionError as exc:
                logger.warning("Failed to download carousel candidate %d: %s", i, exc)
                continue

            score = await _score_candidate(candidate_path, config, brief)

            # In stock_only mode, accept any photo; otherwise require threshold
            threshold = (
                config.image_sourcing.stock_score_threshold
                if not stock_only
                else 0.0
            )
            if score >= threshold:
                scored_candidates.append((candidate_path, score, photo.source))

        # Sort by score descending and take the top CAROUSEL_MAX_IMAGES
        scored_candidates.sort(key=lambda x: x[1], reverse=True)
        top_candidates = scored_candidates[:CAROUSEL_MAX_IMAGES]

        for candidate_path, score, source in top_candidates:
            resized_filename = f"carousel_resized_{uuid.uuid4().hex[:8]}.jpg"
            resized_path = os.path.join(media_dir, resized_filename)
            await resize_for_instagram(candidate_path, resized_path)

            phash = compute_phash(resized_path)

            # Check for duplicate against post_history
            is_dup = await is_duplicate_image(phash, db_path, config.account_id)
            if is_dup:
                logger.info("Carousel candidate is a duplicate — skipping.")
                if os.path.exists(resized_path):
                    os.remove(resized_path)
                continue

            accepted_images.append(
                ImageResult(
                    local_path=resized_path,
                    source=source,
                    phash=phash,
                    score=score,
                )
            )

        # Fill remaining slots with AI-generated images if needed
        if not stock_only:
            while len(accepted_images) < CAROUSEL_MIN_IMAGES:
                logger.info(
                    "Carousel has %d images, need %d — generating AI image.",
                    len(accepted_images),
                    CAROUSEL_MIN_IMAGES,
                )
                generated_path = await _generate_ai_image(config, brief, media_dir)
                temp_files.append(generated_path)

                resized_filename = f"carousel_gen_resized_{uuid.uuid4().hex[:8]}.jpg"
                resized_path = os.path.join(media_dir, resized_filename)
                await resize_for_instagram(generated_path, resized_path)

                phash = compute_phash(resized_path)

                # Clean up raw generated file
                if os.path.exists(generated_path):
                    os.remove(generated_path)

                accepted_images.append(
                    ImageResult(
                        local_path=resized_path,
                        source="gemini",
                        phash=phash,
                        score=0.5,
                    )
                )

        if len(accepted_images) < CAROUSEL_MIN_IMAGES:
            raise RuntimeError(
                f"Could not source enough images for carousel: "
                f"got {len(accepted_images)}, need at least {CAROUSEL_MIN_IMAGES}."
            )

        # Clean up temp files that are not part of accepted images
        accepted_paths = {img.local_path for img in accepted_images}
        for tf in temp_files:
            if tf not in accepted_paths and os.path.exists(tf):
                os.remove(tf)
                logger.debug("Cleaned up carousel temp file: %s", tf)

        logger.info("Carousel sourcing complete: %d images.", len(accepted_images))
        return accepted_images

    except Exception:
        # Clean up all temp files and accepted images on failure
        accepted_paths = {img.local_path for img in accepted_images}
        for tf in temp_files:
            if os.path.exists(tf):
                try:
                    os.remove(tf)
                except OSError:
                    pass
        for img in accepted_images:
            if os.path.exists(img.local_path):
                try:
                    os.remove(img.local_path)
                except OSError:
                    pass
        raise
