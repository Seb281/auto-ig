"""Reusable Claude prompt templates for the auto-ig pipeline."""

import json
import logging
import re

from agents import PlannerBrief
from utils.config_loader import AccountConfig

logger = logging.getLogger(__name__)


def _extract_json(text: str) -> dict:
    """Extract the first valid JSON object from a Claude response string."""
    # Strip markdown code fences if present
    stripped = re.sub(r"```(?:json)?\s*", "", text)
    stripped = re.sub(r"```\s*$", "", stripped, flags=re.MULTILINE)
    stripped = stripped.strip()

    # Try parsing the whole stripped text first
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    # Fall back: find the first { ... } block via brace matching
    start = stripped.find("{")
    if start == -1:
        raise ValueError(f"No JSON object found in response: {text[:200]}")

    depth = 0
    for i in range(start, len(stripped)):
        if stripped[i] == "{":
            depth += 1
        elif stripped[i] == "}":
            depth -= 1
            if depth == 0:
                candidate = stripped[start : i + 1]
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    raise ValueError(
                        f"Found JSON-like block but failed to parse: {candidate[:200]}"
                    )

    raise ValueError(f"Unbalanced braces in response: {text[:200]}")


def build_planner_prompt(
    config: AccountConfig,
    recent_topics: list[str],
    user_hint: str | None = None,
) -> str:
    """Build the prompt for the Content Planner Claude call."""
    pillars = ", ".join(config.content_pillars)
    products = ", ".join(config.allowed_products)
    banned = ", ".join(config.banned_topics)

    lines = [
        f"You are a content planner for an Instagram account about {config.niche}.",
        f"Tone: {config.tone}.",
        f"Visual style: {config.visual_style}.",
        f"Content pillars (choose exactly one): {pillars}.",
        f"Allowed products/ingredients: {products}.",
        f"Banned topics (never reference these): {banned}.",
        "",
    ]

    if recent_topics:
        topic_list = "\n".join(f"  - {t}" for t in recent_topics)
        lines.append("Recent topics from the last 30 days (do NOT repeat any of these):")
        lines.append(topic_list)
    else:
        lines.append("No recent posts yet. Full creative freedom.")

    lines.append("")

    if user_hint is not None:
        lines.append(
            f"The user has suggested this topic/angle: {user_hint}. "
            "Use it as primary inspiration."
        )
        lines.append("")

    lines.extend([
        "Generate a content brief for the next Instagram post.",
        "",
        "Requirements:",
        "- content_pillar must be exactly one of: " + pillars,
        "- visual_keywords must contain 3–5 concrete, photographable nouns suitable for stock photo search",
        "",
        "Return your answer as strict JSON with NO extra text:",
        '{"topic": "...", "angle": "...", "visual_keywords": ["...", "..."], "mood": "...", "content_pillar": "..."}',
    ])

    return "\n".join(lines)


def build_caption_prompt(config: AccountConfig, brief: PlannerBrief) -> str:
    """Build the prompt for the Caption Writer Claude call."""
    banned = ", ".join(config.banned_topics)
    keywords = ", ".join(brief.visual_keywords) if brief.visual_keywords else "N/A"

    lines = [
        f"You write Instagram captions for an account about {config.niche}.",
        f"Tone: {config.tone}.",
        f"Language: {config.language}.",
        f"Banned topics (never reference these): {banned}.",
        "",
        "Content brief for this post:",
        f"  Topic: {brief.topic}",
        f"  Angle: {brief.angle}",
        f"  Mood: {brief.mood}",
        f"  Content pillar: {brief.content_pillar}",
        f"  Visual keywords: {keywords}",
        "",
        "Instructions:",
        "- Write a caption with: a compelling hook line, body (2–4 sentences), and a call-to-action.",
        "- Do NOT include hashtags inside the caption text.",
        "- Return exactly 3–5 targeted hashtags (without the # symbol) as a JSON array.",
        "- Write alt_text: a concise visual description of what the image likely shows "
        "(based on the visual keywords), for accessibility. 1–2 sentences.",
        "",
        "Return your answer as strict JSON with NO extra text:",
        '{"caption": "...", "hashtags": ["...", "..."], "alt_text": "..."}',
    ]

    return "\n".join(lines)
