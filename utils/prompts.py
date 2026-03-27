"""Reusable AI prompt templates for the auto-ig pipeline."""

import json
import logging
import re

from agents import PlannerBrief
from utils.config_loader import AccountConfig

logger = logging.getLogger(__name__)


def _extract_json(text: str) -> dict:
    """Extract the first valid JSON object from an AI response string."""
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
    """Build the prompt for the Content Planner AI call."""
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
    """Build the prompt for the Caption Writer AI call."""
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


def build_vision_scoring_prompt(
    config: AccountConfig,
    brief: PlannerBrief,
) -> str:
    """Build the prompt for AI vision scoring of a stock photo candidate."""
    keywords = ", ".join(brief.visual_keywords) if brief.visual_keywords else brief.topic
    banned = ", ".join(config.banned_topics)

    lines = [
        f"You are evaluating a stock photo for an Instagram account about {config.niche}.",
        f"Visual style: {config.visual_style}.",
        f"Banned topics (image must NOT depict): {banned}.",
        "",
        "Content brief for this post:",
        f"  Topic: {brief.topic}",
        f"  Angle: {brief.angle}",
        f"  Mood: {brief.mood}",
        f"  Visual keywords: {keywords}",
        "",
        "Score this image from 0.0 to 1.0 based on:",
        "- Relevance to the topic and visual keywords",
        "- Brand fit (natural food photography, bright, farm-fresh aesthetic)",
        "- Quality (resolution, composition, lighting)",
        "- No banned content depicted",
        "",
        "Return your answer as strict JSON with NO extra text:",
        '{"score": 0.0, "reasoning": "..."}',
    ]

    return "\n".join(lines)


def build_image_gen_prompt(
    config: AccountConfig,
    brief: PlannerBrief,
) -> str:
    """Build an AI image generation prompt from the content brief."""
    keywords = ", ".join(brief.visual_keywords) if brief.visual_keywords else brief.topic

    return (
        f"Natural food photography. {keywords}. "
        f"{config.visual_style}. "
        "No text overlays, no people, no watermarks. "
        "Square composition, 1:1 aspect ratio. "
        "High quality, professional food photography with natural lighting."
    )


def build_reviewer_vision_prompt(
    config: AccountConfig,
    brief: PlannerBrief,
    caption: str,
) -> str:
    """Build the prompt for the Reviewer agent's AI vision check."""
    allowed = ", ".join(config.allowed_products)
    banned = ", ".join(config.banned_topics)
    keywords = ", ".join(brief.visual_keywords) if brief.visual_keywords else brief.topic

    lines = [
        f"You are a brand safety reviewer for an Instagram account about {config.niche}.",
        f"Visual style: {config.visual_style}.",
        f"Tone: {config.tone}.",
        f"Allowed products/ingredients: {allowed}.",
        f"Banned topics (must NOT appear in image or caption): {banned}.",
        "",
        "Content brief for this post:",
        f"  Topic: {brief.topic}",
        f"  Angle: {brief.angle}",
        f"  Mood: {brief.mood}",
        f"  Content pillar: {brief.content_pillar}",
        f"  Visual keywords: {keywords}",
        "",
        "Caption to review:",
        caption,
        "",
        "Review the image AND caption for the following:",
        "1. Brand fit: Does the image match the visual style and mood? Is it high quality?",
        "2. Banned content: Does the image or caption reference any banned topics?",
        "3. Caption quality: Is the caption on-brand (tone, no fear-mongering, factual)?",
        "4. Relevance: Does the image match the topic and visual keywords?",
        "",
        "Return your answer as strict JSON with NO extra text:",
        '{"status": "PASS or FAIL", "reasons": ["reason1", "reason2"], "retry_type": "image or caption or null"}',
        "",
        "Rules:",
        '- status must be exactly "PASS" or "FAIL".',
        "- reasons must be an empty array [] if PASS.",
        "- If FAIL, reasons must list specific problems found.",
        '- retry_type: "image" if the image is the problem, "caption" if the caption is the problem, null if PASS.',
        "- If both image and caption have problems, set retry_type to the more severe one.",
    ]

    return "\n".join(lines)


def build_reviewer_text_prompt(
    config: AccountConfig,
    brief: PlannerBrief,
    caption: str,
    hashtags: list[str],
) -> str:
    """Build the prompt for text-only caption review (no image)."""
    allowed = ", ".join(config.allowed_products)
    banned = ", ".join(config.banned_topics)

    hashtag_str = ", ".join(f"#{tag}" for tag in hashtags)

    lines = [
        f"You are a brand safety reviewer for an Instagram account about {config.niche}.",
        f"Tone: {config.tone}.",
        f"Allowed products/ingredients: {allowed}.",
        f"Banned topics (must NOT appear): {banned}.",
        "",
        "Content brief for this post:",
        f"  Topic: {brief.topic}",
        f"  Angle: {brief.angle}",
        f"  Content pillar: {brief.content_pillar}",
        "",
        "Caption to review:",
        caption,
        "",
        f"Hashtags: {hashtag_str}",
        "",
        "Review the caption and hashtags for:",
        "1. Banned content: Any reference to banned topics?",
        "2. Tone: Educational, warm, inspiring? No fear-mongering?",
        "3. Factual claims: Are claims reasonable and not misleading?",
        "4. Hashtag relevance: Are hashtags targeted and appropriate?",
        "",
        "Return your answer as strict JSON with NO extra text:",
        '{"status": "PASS or FAIL", "reasons": ["reason1", "reason2"], "retry_type": "caption or null"}',
        "",
        "Rules:",
        '- status must be exactly "PASS" or "FAIL".',
        "- reasons must be an empty array [] if PASS.",
        "- If FAIL, reasons must list specific problems found.",
        '- retry_type: "caption" if problems found, null if PASS.',
    ]

    return "\n".join(lines)
