"""Reviewer Agent — brand rules, vision check, and duplicate detection."""

import asyncio
import base64
import logging
import os

import anthropic

from agents import CaptionResult, ImageResult, PlannerBrief, ReviewResult
from utils.config_loader import AccountConfig
from utils.image_utils import is_duplicate_image
from utils.prompts import _extract_json, build_reviewer_vision_prompt

logger = logging.getLogger(__name__)

# Valid status values
STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"

# Valid retry types
RETRY_IMAGE = "image"
RETRY_CAPTION = "caption"


def _detect_media_type_sync(file_path: str) -> str:
    """Detect the media type of an image file from its content (sync)."""
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

    return "image/jpeg"


def _read_and_encode_sync(file_path: str) -> tuple[str, str]:
    """Read a file and return (base64_data, media_type) — sync, for use with to_thread."""
    with open(file_path, "rb") as f:
        raw = f.read()
    media_type = _detect_media_type_sync(file_path)
    return base64.standard_b64encode(raw).decode("utf-8"), media_type


def _check_caption_banned_topics(
    caption: str,
    hashtags: list[str],
    config: AccountConfig,
) -> list[str]:
    """Check caption and hashtags for banned topic references."""
    violations: list[str] = []
    combined_text = (caption + " " + " ".join(hashtags)).lower()

    for banned in config.banned_topics:
        # Check for the banned topic as a substring (case-insensitive)
        if banned.lower() in combined_text:
            violations.append(
                f"Caption or hashtags reference banned topic: '{banned}'"
            )

    return violations


async def _vision_review(
    image_path: str,
    config: AccountConfig,
    brief: PlannerBrief,
    caption: str,
) -> ReviewResult:
    """Run Claude vision on the image + caption and return a ReviewResult."""
    prompt_text = build_reviewer_vision_prompt(config, brief, caption)

    image_data, media_type = await asyncio.to_thread(
        _read_and_encode_sync, image_path
    )

    client = anthropic.AsyncAnthropic()
    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
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
    logger.debug("Reviewer vision raw response: %s", raw_text)

    try:
        data = _extract_json(raw_text)
    except ValueError:
        logger.error("Failed to parse reviewer response: %s", raw_text[:500])
        # Treat parse failures as FAIL with caption retry (safer to regenerate)
        return ReviewResult(
            status=STATUS_FAIL,
            reasons=["Reviewer response could not be parsed as JSON."],
            retry_type=RETRY_CAPTION,
        )

    status = str(data.get("status", STATUS_FAIL)).upper().strip()
    if status not in (STATUS_PASS, STATUS_FAIL):
        logger.warning(
            "Reviewer returned unexpected status '%s', treating as FAIL.",
            status,
        )
        status = STATUS_FAIL

    reasons = data.get("reasons", [])
    if not isinstance(reasons, list):
        reasons = [str(reasons)] if reasons else []
    reasons = [str(r) for r in reasons if r]

    raw_retry = data.get("retry_type")
    if raw_retry is None or str(raw_retry).lower() in ("null", "none", ""):
        retry_type = None
    else:
        retry_type = str(raw_retry).lower().strip()
        if retry_type not in (RETRY_IMAGE, RETRY_CAPTION):
            logger.warning(
                "Reviewer returned unknown retry_type '%s', defaulting to 'caption'.",
                retry_type,
            )
            retry_type = RETRY_CAPTION

    # If status is FAIL but retry_type is None, default to caption
    if status == STATUS_FAIL and retry_type is None:
        retry_type = RETRY_CAPTION

    # If status is PASS, ensure reasons is empty and retry_type is None
    if status == STATUS_PASS:
        reasons = []
        retry_type = None

    return ReviewResult(
        status=status,
        reasons=reasons,
        retry_type=retry_type,
    )


async def review_post(
    config: AccountConfig,
    brief: PlannerBrief,
    image: ImageResult,
    caption: CaptionResult,
    db_path: str,
) -> ReviewResult:
    """Review a post for brand compliance, image quality, and duplicates."""
    all_reasons: list[str] = []
    retry_type: str | None = None

    # Check 1: Duplicate image detection via perceptual hash
    logger.info("Checking for duplicate images...")
    try:
        is_dup = await is_duplicate_image(
            image.phash, db_path, config.account_id
        )
    except Exception as exc:
        logger.warning("Duplicate check failed: %s. Proceeding anyway.", exc)
        is_dup = False

    if is_dup:
        all_reasons.append(
            "Image is too similar to a previously published post (perceptual hash match)."
        )
        retry_type = RETRY_IMAGE
        logger.info("Duplicate image detected — will recommend image retry.")

    # Check 2: Text-based banned topic check (fast, no API call)
    logger.info("Checking caption for banned topics...")
    banned_violations = _check_caption_banned_topics(
        caption.caption, caption.hashtags, config
    )
    if banned_violations:
        all_reasons.extend(banned_violations)
        # Caption issues take precedence only if no image issue already found
        if retry_type is None:
            retry_type = RETRY_CAPTION
        logger.info(
            "Banned topic violations found: %s", "; ".join(banned_violations)
        )

    # Check 3: Claude vision review (image + caption together)
    logger.info("Running Claude vision review on image + caption...")
    if os.path.exists(image.local_path):
        try:
            vision_result = await _vision_review(
                image.local_path, config, brief, caption.caption
            )

            if vision_result.status == STATUS_FAIL:
                all_reasons.extend(vision_result.reasons)
                # Vision-detected retry_type takes precedence if we don't already
                # have an image retry (which is more severe)
                if retry_type != RETRY_IMAGE and vision_result.retry_type is not None:
                    retry_type = vision_result.retry_type

        except Exception as exc:
            logger.warning(
                "Vision review failed: %s. Proceeding without vision check.", exc
            )
    else:
        logger.warning(
            "Image file not found at %s — skipping vision review.",
            image.local_path,
        )

    # Final verdict
    if all_reasons:
        status = STATUS_FAIL
        # Ensure retry_type is set
        if retry_type is None:
            retry_type = RETRY_CAPTION
        logger.info(
            "Review FAILED with %d reason(s). Retry type: %s",
            len(all_reasons),
            retry_type,
        )
    else:
        status = STATUS_PASS
        retry_type = None
        logger.info("Review PASSED — no issues found.")

    return ReviewResult(
        status=status,
        reasons=all_reasons,
        retry_type=retry_type,
    )
