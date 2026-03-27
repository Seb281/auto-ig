"""Content Planner Agent — generates a PlannerBrief from account config and history."""

import logging

import aiosqlite
from agents import PlannerBrief
from utils.config_loader import AccountConfig
from utils.ai_client import generate_text
from utils.prompts import _extract_json, build_planner_prompt

logger = logging.getLogger(__name__)


def _normalize_pillar(raw: str) -> str:
    """Normalize a content pillar value for comparison (lowercase, underscores)."""
    return raw.strip().lower().replace(" ", "_").replace("-", "_")


async def generate_brief(
    config: AccountConfig,
    db_path: str,
    user_hint: str | None = None,
) -> PlannerBrief:
    """Generate a content brief by querying history and calling AI."""
    # Gather recent topics for deduplication
    recent_topics: list[str] = []

    async with aiosqlite.connect(db_path) as db:
        # Published posts from last 30 days
        cursor = await db.execute(
            "SELECT topic FROM post_history "
            "WHERE account_id = ? AND published_at > date('now', '-30 days')",
            (config.account_id,),
        )
        rows = await cursor.fetchall()
        recent_topics.extend(row[0] for row in rows)

        # Pending/approved drafts (avoid collision with in-flight posts)
        cursor = await db.execute(
            "SELECT json_extract(brief_json, '$.topic') FROM pending_drafts "
            "WHERE account_id = ? AND status IN ('pending', 'approved')",
            (config.account_id,),
        )
        rows = await cursor.fetchall()
        recent_topics.extend(row[0] for row in rows if row[0])

    logger.info(
        "Found %d recent topics for deduplication.", len(recent_topics)
    )

    # Build prompt and call AI
    prompt = build_planner_prompt(config, recent_topics, user_hint)
    logger.info("Calling AI for content planning...")

    raw_text = await generate_text(prompt)
    logger.debug("Planner raw response: %s", raw_text)

    # Parse JSON from response
    try:
        data = _extract_json(raw_text)
    except ValueError:
        logger.error("Failed to parse planner response: %s", raw_text[:500])
        raise ValueError(
            f"Failed to parse planner response as JSON: {raw_text[:200]}"
        )

    # Validate content_pillar
    raw_pillar = str(data.get("content_pillar", ""))
    normalized = _normalize_pillar(raw_pillar)
    pillar_map = {_normalize_pillar(p): p for p in config.content_pillars}

    if normalized in pillar_map:
        content_pillar = pillar_map[normalized]
    else:
        logger.warning(
            "AI returned unknown content_pillar '%s'. "
            "Defaulting to '%s'.",
            raw_pillar,
            config.content_pillars[0],
        )
        content_pillar = config.content_pillars[0]

    # Validate visual_keywords
    visual_keywords = data.get("visual_keywords", [])
    if not isinstance(visual_keywords, list) or len(visual_keywords) == 0:
        logger.warning(
            "visual_keywords missing or empty, using topic as fallback."
        )
        visual_keywords = [str(data.get("topic", "food"))]
    visual_keywords = [str(kw) for kw in visual_keywords]

    brief = PlannerBrief(
        topic=str(data.get("topic", "")),
        angle=str(data.get("angle", "")),
        visual_keywords=visual_keywords,
        mood=str(data.get("mood", "")),
        content_pillar=content_pillar,
    )

    logger.info(
        "Brief generated — topic: '%s', pillar: '%s'",
        brief.topic,
        brief.content_pillar,
    )
    return brief
