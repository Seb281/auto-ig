"""Image Sourcing Agent — finds or generates an image for the post."""

import base64
import logging
import os
import uuid

import anthropic
import openai

from agents import ImageResult, PlannerBrief
from utils.config_loader import AccountConfig
from utils.image_utils import (
    compute_phash,
    copy_user_photo,
    is_duplicate_image,
    resize_for_instagram,
)
from utils.prompts import (
    _extract_json,
    build_dalle_prompt,
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

# Map source name to search function
_SEARCH_FUNCTIONS = {
    "unsplash": search_unsplash,
    "pexels": search_pexels,
}


def _detect_media_type(file_path: str) -> str:
    """Detect the media type of an image file from its content."""
    with open(file_path, "rb") as f:
        header = f.read(16)

    if header[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    elif header[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    elif header[:4] == b"GIF8":
        return "image/gif"
    elif header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"

    # Default to JPEG
    return "image/jpeg"


async def _score_candidate(
    image_path: str,
    config: AccountConfig,
    brief: PlannerBrief,
) -> float:
    """Score a single candidate image using Claude vision."""
    prompt_text = build_vision_scoring_prompt(config, brief)

    with open(image_path, "rb") as f:
        image_data = base64.standard_b64encode(f.read()).decode("utf-8")

    media_type = _detect_media_type(image_path)

    client = anthropic.AsyncAnthropic()
    try:
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=256,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": image_data,
                            },
                        },
                        {
                            "type": "text",
                            "text": prompt_text,
                        },
                    ],
                }
            ],
        )

        raw_text = response.content[0].text
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


async def _generate_dalle_image(
    config: AccountConfig,
    brief: PlannerBrief,
    media_dir: str,
) -> str:
    """Generate an image using DALL-E 3 and return the local file path."""
    prompt = build_dalle_prompt(config, brief)
    logger.info("Generating DALL-E 3 image with prompt: %s", prompt[:120])

    client = openai.AsyncOpenAI()
    response = await client.images.generate(
        model="dall-e-3",
        prompt=prompt,
        size="1024x1024",
        quality="standard",
        n=1,
    )

    image_url = response.data[0].url
    if not image_url:
        raise RuntimeError("DALL-E 3 returned no image URL.")

    # Download the generated image (URL expires quickly)
    dest_filename = f"dalle_{uuid.uuid4().hex[:8]}.png"
    dest_path = os.path.join(media_dir, dest_filename)
    await download_image(image_url, dest_path)

    logger.info("DALL-E 3 image saved to %s", dest_path)
    return dest_path


async def source_image(
    config: AccountConfig,
    brief: PlannerBrief,
    db_path: str,
    media_dir: str,
    user_photo_path: str | None = None,
) -> ImageResult:
    """Source an image following priority: user photo -> stock -> DALL-E 3."""
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

        # Use stock photo if it meets the threshold
        if (
            best_path is not None
            and best_score >= config.image_sourcing.stock_score_threshold
        ):
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
                    "Stock photo is a duplicate. Falling through to DALL-E 3."
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

        # Priority 3: DALL-E 3 fallback
        logger.info("Falling back to DALL-E 3 image generation.")
        dalle_path = await _generate_dalle_image(config, brief, media_dir)
        temp_files.append(dalle_path)

        resized_filename = f"resized_dalle_{uuid.uuid4().hex[:8]}.jpg"
        resized_path = os.path.join(media_dir, resized_filename)
        await resize_for_instagram(dalle_path, resized_path)

        phash = compute_phash(resized_path)

        # Clean up the raw DALL-E file
        if os.path.exists(dalle_path):
            os.remove(dalle_path)

        # Remove all other temp files
        for tf in temp_files:
            if tf != dalle_path and os.path.exists(tf):
                os.remove(tf)
                logger.debug("Cleaned up temp file: %s", tf)
        temp_files.clear()

        return ImageResult(
            local_path=resized_path,
            source="dalle3",
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
