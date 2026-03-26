"""Caption Writer Agent — generates a CaptionResult from a PlannerBrief."""

import logging

import anthropic

from agents import CaptionResult, PlannerBrief
from utils.config_loader import AccountConfig
from utils.prompts import _extract_json, build_caption_prompt

logger = logging.getLogger(__name__)


async def generate_caption(
    config: AccountConfig,
    brief: PlannerBrief,
) -> CaptionResult:
    """Call Claude with the brief to produce caption, hashtags, and alt_text."""
    prompt = build_caption_prompt(config, brief)

    client = anthropic.AsyncAnthropic()
    logger.info("Calling Claude for caption writing...")

    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )

    raw_text = response.content[0].text
    logger.debug("Caption writer raw response: %s", raw_text)

    # Parse JSON from response
    try:
        data = _extract_json(raw_text)
    except ValueError:
        logger.error("Failed to parse caption response: %s", raw_text[:500])
        raise ValueError(
            f"Failed to parse caption response as JSON: {raw_text[:200]}"
        )

    # Validate caption
    caption = str(data.get("caption", "")).strip()
    if not caption:
        raise ValueError("Caption is empty in Claude response.")

    # Validate alt_text
    alt_text = str(data.get("alt_text", "")).strip()
    if not alt_text:
        raise ValueError("alt_text is empty in Claude response.")

    # Validate and clean hashtags
    raw_hashtags = data.get("hashtags", [])
    if not isinstance(raw_hashtags, list):
        logger.warning("hashtags is not a list, wrapping: %s", raw_hashtags)
        raw_hashtags = [str(raw_hashtags)]

    # Strip leading '#' if Claude included them
    hashtags = [str(tag).lstrip("#").strip() for tag in raw_hashtags]
    # Remove any empty strings after stripping
    hashtags = [tag for tag in hashtags if tag]

    if len(hashtags) > 5:
        logger.warning(
            "Got %d hashtags, clamping to first 5.", len(hashtags)
        )
        hashtags = hashtags[:5]
    elif len(hashtags) < 3:
        logger.warning(
            "Got only %d hashtags (expected 3–5). Proceeding anyway.",
            len(hashtags),
        )

    result = CaptionResult(
        caption=caption,
        hashtags=hashtags,
        alt_text=alt_text,
    )

    logger.info(
        "Caption generated — %d chars, %d hashtags.",
        len(result.caption),
        len(result.hashtags),
    )
    return result
