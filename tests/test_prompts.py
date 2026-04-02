"""Tests for utils/prompts.py — JSON extraction and prompt builders."""

import pytest
from utils.prompts import (
    _extract_json,
    build_caption_prompt,
    build_facebook_caption,
    build_planner_prompt,
    build_platform_caption_prompt,
    build_reviewer_vision_prompt,
    build_vision_scoring_prompt,
)


# ---------------------------------------------------------------------------
# _extract_json
# ---------------------------------------------------------------------------

class TestExtractJson:
    def test_clean_json(self):
        result = _extract_json('{"key": "value"}')
        assert result == {"key": "value"}

    def test_markdown_fenced(self):
        text = '```json\n{"score": 0.8}\n```'
        assert _extract_json(text) == {"score": 0.8}

    def test_nested_braces(self):
        text = '{"outer": {"inner": 1}}'
        result = _extract_json(text)
        assert result == {"outer": {"inner": 1}}

    def test_json_with_surrounding_text(self):
        text = 'Here is the result: {"topic": "salads"} hope this helps!'
        assert _extract_json(text) == {"topic": "salads"}

    def test_unbalanced_braces_raises(self):
        with pytest.raises(ValueError, match="Unbalanced braces"):
            _extract_json('{"key": "value"')

    def test_no_json_raises(self):
        with pytest.raises(ValueError, match="No JSON object found"):
            _extract_json("no json here at all")

    def test_empty_string_raises(self):
        with pytest.raises(ValueError):
            _extract_json("")

    def test_array_values(self):
        text = '{"tags": ["a", "b", "c"]}'
        result = _extract_json(text)
        assert result["tags"] == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# build_planner_prompt
# ---------------------------------------------------------------------------

class TestBuildPlannerPrompt:
    def test_includes_niche_and_pillars(self, make_account_config):
        config = make_account_config()
        prompt = build_planner_prompt(config, [])
        assert "organic food" in prompt
        assert "recipes" in prompt
        assert "nutrition_tips" in prompt

    def test_includes_banned_topics(self, make_account_config):
        config = make_account_config()
        prompt = build_planner_prompt(config, [])
        assert "alcohol" in prompt
        assert "tobacco" in prompt

    def test_includes_recent_topics(self, make_account_config):
        config = make_account_config()
        prompt = build_planner_prompt(config, ["topic_a", "topic_b"])
        assert "topic_a" in prompt
        assert "topic_b" in prompt
        assert "do NOT repeat" in prompt.lower() or "NOT repeat" in prompt

    def test_no_recent_topics(self, make_account_config):
        config = make_account_config()
        prompt = build_planner_prompt(config, [])
        assert "creative freedom" in prompt.lower() or "Full creative freedom" in prompt

    def test_user_hint_included(self, make_account_config):
        config = make_account_config()
        prompt = build_planner_prompt(config, [], user_hint="summer berries")
        assert "summer berries" in prompt

    def test_content_type_options_listed(self, make_account_config):
        config = make_account_config()
        prompt = build_planner_prompt(config, [])
        assert "single_image" in prompt
        assert "carousel" in prompt
        assert "reel" in prompt


# ---------------------------------------------------------------------------
# build_caption_prompt
# ---------------------------------------------------------------------------

class TestBuildCaptionPrompt:
    def test_single_image_prompt(self, make_account_config, make_planner_brief):
        config = make_account_config()
        brief = make_planner_brief(content_type="single_image")
        prompt = build_caption_prompt(config, brief)
        assert "organic food" in prompt
        assert "carousel" not in prompt.lower() or "swipe" not in prompt.lower()

    def test_carousel_prompt_mentions_swipe(self, make_account_config, make_planner_brief):
        config = make_account_config()
        brief = make_planner_brief(content_type="carousel")
        prompt = build_caption_prompt(config, brief)
        assert "carousel" in prompt.lower()
        assert "swipe" in prompt.lower()

    def test_reel_prompt_mentions_video(self, make_account_config, make_planner_brief):
        config = make_account_config()
        brief = make_planner_brief(content_type="reel")
        prompt = build_caption_prompt(config, brief)
        assert "reel" in prompt.lower() or "video" in prompt.lower()
        assert "hook" in prompt.lower()

    def test_includes_banned_topics(self, make_account_config, make_planner_brief):
        config = make_account_config()
        brief = make_planner_brief()
        prompt = build_caption_prompt(config, brief)
        assert "alcohol" in prompt


# ---------------------------------------------------------------------------
# build_vision_scoring_prompt
# ---------------------------------------------------------------------------

class TestBuildVisionScoringPrompt:
    def test_includes_keywords_and_banned(self, make_account_config, make_planner_brief):
        config = make_account_config()
        brief = make_planner_brief()
        prompt = build_vision_scoring_prompt(config, brief)
        assert "salad" in prompt
        assert "alcohol" in prompt
        assert "0.0 to 1.0" in prompt

    def test_empty_keywords_uses_topic(self, make_account_config, make_planner_brief):
        config = make_account_config()
        brief = make_planner_brief(visual_keywords=[])
        prompt = build_vision_scoring_prompt(config, brief)
        assert brief.topic in prompt


# ---------------------------------------------------------------------------
# build_reviewer_vision_prompt
# ---------------------------------------------------------------------------

class TestBuildReviewerVisionPrompt:
    def test_includes_caption_and_allowed(self, make_account_config, make_planner_brief):
        config = make_account_config()
        brief = make_planner_brief()
        prompt = build_reviewer_vision_prompt(config, brief, "Test caption text")
        assert "Test caption text" in prompt
        assert "vegetables" in prompt
        assert "alcohol" in prompt

    def test_includes_pass_fail_instructions(self, make_account_config, make_planner_brief):
        config = make_account_config()
        brief = make_planner_brief()
        prompt = build_reviewer_vision_prompt(config, brief, "caption")
        assert "PASS" in prompt
        assert "FAIL" in prompt


# ---------------------------------------------------------------------------
# build_facebook_caption
# ---------------------------------------------------------------------------

class TestBuildFacebookCaption:
    def test_no_hashtags(self):
        result = build_facebook_caption("Hello world", [])
        assert result == "Hello world"

    def test_truncates_to_two_hashtags(self):
        result = build_facebook_caption("Hello", ["a", "b", "c", "d"])
        assert "#a" in result
        assert "#b" in result
        assert "#c" not in result

    def test_single_hashtag(self):
        result = build_facebook_caption("Hello", ["tag"])
        assert "#tag" in result


# ---------------------------------------------------------------------------
# build_platform_caption_prompt
# ---------------------------------------------------------------------------

class TestBuildPlatformCaptionPrompt:
    def test_valid_platform(self):
        prompt = build_platform_caption_prompt("Test caption", ["tag1"], "facebook")
        assert "facebook" in prompt.lower()
        assert "Test caption" in prompt

    def test_invalid_platform_raises(self):
        with pytest.raises(ValueError, match="No caption guidelines"):
            build_platform_caption_prompt("caption", [], "myspace")
