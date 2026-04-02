"""Tests for agents/content_planner.py — brief generation with mocked AI + DB."""

import json
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest

from agents.content_planner import generate_brief


class TestGenerateBrief:
    async def test_valid_response(self, make_account_config, tmp_db):
        config = make_account_config()
        ai_response = json.dumps({
            "topic": "Heirloom tomatoes",
            "angle": "Best varieties for summer",
            "visual_keywords": ["tomato", "garden", "heirloom"],
            "mood": "warm",
            "content_pillar": "recipes",
            "content_type": "single_image",
        })

        with patch("agents.content_planner.generate_text", new_callable=AsyncMock, return_value=ai_response):
            brief = await generate_brief(config, tmp_db)

        assert brief.topic == "Heirloom tomatoes"
        assert brief.content_pillar == "recipes"
        assert brief.content_type == "single_image"
        assert len(brief.visual_keywords) == 3

    async def test_unknown_pillar_defaults_to_first(self, make_account_config, tmp_db):
        config = make_account_config()
        ai_response = json.dumps({
            "topic": "Topic",
            "angle": "Angle",
            "visual_keywords": ["kw"],
            "mood": "mood",
            "content_pillar": "nonexistent_pillar",
            "content_type": "single_image",
        })

        with patch("agents.content_planner.generate_text", new_callable=AsyncMock, return_value=ai_response):
            brief = await generate_brief(config, tmp_db)

        # Should default to the first pillar in config
        assert brief.content_pillar == config.content_pillars[0]

    async def test_unknown_content_type_defaults(self, make_account_config, tmp_db):
        config = make_account_config()
        ai_response = json.dumps({
            "topic": "Topic",
            "angle": "Angle",
            "visual_keywords": ["kw"],
            "mood": "mood",
            "content_pillar": "recipes",
            "content_type": "story",  # invalid
        })

        with patch("agents.content_planner.generate_text", new_callable=AsyncMock, return_value=ai_response):
            brief = await generate_brief(config, tmp_db)

        assert brief.content_type == "single_image"

    async def test_missing_visual_keywords_fallback(self, make_account_config, tmp_db):
        config = make_account_config()
        ai_response = json.dumps({
            "topic": "Smoothies",
            "angle": "Angle",
            "visual_keywords": [],
            "mood": "mood",
            "content_pillar": "recipes",
            "content_type": "single_image",
        })

        with patch("agents.content_planner.generate_text", new_callable=AsyncMock, return_value=ai_response):
            brief = await generate_brief(config, tmp_db)

        assert brief.visual_keywords == ["Smoothies"]

    async def test_deduplication_query(self, make_account_config, tmp_db):
        config = make_account_config()

        # Insert a recent topic
        async with aiosqlite.connect(tmp_db) as db:
            await db.execute(
                "INSERT INTO post_history (account_id, topic, content_pillar, image_phash, caption_snippet, published_at) "
                "VALUES (?, ?, ?, ?, ?, date('now'))",
                ("test_account", "Old topic", "recipes", "hash", "caption"),
            )
            await db.commit()

        ai_response = json.dumps({
            "topic": "New topic",
            "angle": "Angle",
            "visual_keywords": ["kw"],
            "mood": "mood",
            "content_pillar": "recipes",
            "content_type": "single_image",
        })

        with patch("agents.content_planner.generate_text", new_callable=AsyncMock, return_value=ai_response) as mock_ai:
            brief = await generate_brief(config, tmp_db)

        # The prompt should include the recent topic for deduplication
        prompt_arg = mock_ai.call_args[0][0]
        assert "Old topic" in prompt_arg

    async def test_user_hint_passed_to_prompt(self, make_account_config, tmp_db):
        config = make_account_config()
        ai_response = json.dumps({
            "topic": "Berries",
            "angle": "Angle",
            "visual_keywords": ["berry"],
            "mood": "mood",
            "content_pillar": "recipes",
            "content_type": "single_image",
        })

        with patch("agents.content_planner.generate_text", new_callable=AsyncMock, return_value=ai_response) as mock_ai:
            await generate_brief(config, tmp_db, user_hint="summer berries")

        prompt_arg = mock_ai.call_args[0][0]
        assert "summer berries" in prompt_arg
