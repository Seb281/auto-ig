"""Tests for agents/caption_writer.py — caption generation with mocked AI."""

import json
from unittest.mock import AsyncMock, patch

import pytest

from agents.caption_writer import generate_caption


class TestGenerateCaption:
    async def test_valid_response(self, make_account_config, make_planner_brief):
        config = make_account_config()
        brief = make_planner_brief()
        ai_response = json.dumps({
            "caption": "Fresh salads are amazing!",
            "hashtags": ["salad", "healthy", "organic"],
            "alt_text": "A bowl of fresh salad.",
        })

        with patch("agents.caption_writer.generate_text", new_callable=AsyncMock, return_value=ai_response):
            result = await generate_caption(config, brief)

        assert result.caption == "Fresh salads are amazing!"
        assert result.hashtags == ["salad", "healthy", "organic"]
        assert result.alt_text == "A bowl of fresh salad."

    async def test_empty_caption_raises(self, make_account_config, make_planner_brief):
        config = make_account_config()
        brief = make_planner_brief()
        ai_response = json.dumps({
            "caption": "",
            "hashtags": ["tag"],
            "alt_text": "desc",
        })

        with patch("agents.caption_writer.generate_text", new_callable=AsyncMock, return_value=ai_response):
            with pytest.raises(ValueError, match="Caption is empty"):
                await generate_caption(config, brief)

    async def test_hashtag_clamping_to_five(self, make_account_config, make_planner_brief):
        config = make_account_config()
        brief = make_planner_brief()
        ai_response = json.dumps({
            "caption": "Good caption",
            "hashtags": ["a", "b", "c", "d", "e", "f", "g"],
            "alt_text": "Alt text",
        })

        with patch("agents.caption_writer.generate_text", new_callable=AsyncMock, return_value=ai_response):
            result = await generate_caption(config, brief)

        assert len(result.hashtags) == 5

    async def test_hashtag_hash_stripping(self, make_account_config, make_planner_brief):
        config = make_account_config()
        brief = make_planner_brief()
        ai_response = json.dumps({
            "caption": "Good caption",
            "hashtags": ["#salad", "#healthy", "#food"],
            "alt_text": "Alt text",
        })

        with patch("agents.caption_writer.generate_text", new_callable=AsyncMock, return_value=ai_response):
            result = await generate_caption(config, brief)

        assert all(not tag.startswith("#") for tag in result.hashtags)

    async def test_non_list_hashtags_wrapped(self, make_account_config, make_planner_brief):
        config = make_account_config()
        brief = make_planner_brief()
        ai_response = json.dumps({
            "caption": "Good caption",
            "hashtags": "single_tag",
            "alt_text": "Alt text",
        })

        with patch("agents.caption_writer.generate_text", new_callable=AsyncMock, return_value=ai_response):
            result = await generate_caption(config, brief)

        assert isinstance(result.hashtags, list)

    async def test_parse_failure_raises(self, make_account_config, make_planner_brief):
        config = make_account_config()
        brief = make_planner_brief()

        with patch("agents.caption_writer.generate_text", new_callable=AsyncMock, return_value="not json at all"):
            with pytest.raises(ValueError, match="Failed to parse"):
                await generate_caption(config, brief)
