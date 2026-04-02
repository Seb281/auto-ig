"""Tests for control/discord_bot.py — build_bot, killswitch, helpers, save_draft."""

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest

from agents import PlannerBrief
from control.discord_bot import (
    _format_draft_preview,
    _is_killed,
    _killswitch_key,
    _save_pending_draft,
    build_bot,
    get_pending_draft,
)


# ---------------------------------------------------------------------------
# build_bot
# ---------------------------------------------------------------------------

class TestBuildBot:
    def test_returns_bot_with_commands(self, make_account_config, monkeypatch):
        monkeypatch.setenv("TEST_DISCORD_TOKEN", "fake-token")
        monkeypatch.setenv("TEST_DISCORD_CHANNEL", "12345")
        config = make_account_config()

        bot = build_bot([(config, "/tmp/test.db", False)])
        command_names = {cmd.name for cmd in bot.commands}

        assert "start" in command_names
        assert "run" in command_names
        assert "approve" in command_names
        assert "status" in command_names
        assert "killswitch" in command_names
        assert "pause" in command_names
        assert "setfrequency" in command_names

    def test_empty_accounts_raises(self):
        with pytest.raises(ValueError, match="At least one account"):
            build_bot([])

    def test_missing_token_raises(self, make_account_config, monkeypatch):
        monkeypatch.delenv("TEST_DISCORD_TOKEN", raising=False)
        config = make_account_config()
        with pytest.raises(ValueError, match="missing or empty"):
            build_bot([(config, "/tmp/test.db", False)])


# ---------------------------------------------------------------------------
# Killswitch helpers
# ---------------------------------------------------------------------------

class TestKillswitchHelpers:
    def test_killswitch_key(self):
        assert _killswitch_key("test_account") == "killswitch_test_account"

    def test_is_killed_false_when_not_set(self, make_account_config):
        config = make_account_config()
        bot_data = {}
        assert _is_killed(bot_data, config) is False

    def test_is_killed_true_when_set(self, make_account_config):
        config = make_account_config()
        bot_data = {"killswitch_test_account": True}
        assert _is_killed(bot_data, config) is True

    def test_is_killed_false_when_explicitly_false(self, make_account_config):
        config = make_account_config()
        bot_data = {"killswitch_test_account": False}
        assert _is_killed(bot_data, config) is False


# ---------------------------------------------------------------------------
# _format_draft_preview
# ---------------------------------------------------------------------------

class TestFormatDraftPreview:
    def test_single_image_label(self):
        result = _format_draft_preview("Caption", ["tag1"], "single_image")
        assert "SINGLE IMAGE" in result
        assert "Caption" in result
        assert "#tag1" in result

    def test_carousel_label(self):
        result = _format_draft_preview("Caption", ["tag1"], "carousel")
        assert "CAROUSEL" in result

    def test_reel_label(self):
        result = _format_draft_preview("Caption", ["tag1"], "reel")
        assert "REEL" in result

    def test_default_type(self):
        result = _format_draft_preview("Caption", [])
        assert "SINGLE IMAGE" in result


# ---------------------------------------------------------------------------
# _save_pending_draft
# ---------------------------------------------------------------------------

class TestSavePendingDraft:
    async def test_inserts_row(self, tmp_db):
        brief = PlannerBrief(
            topic="Test", angle="Angle", visual_keywords=["kw"],
            mood="mood", content_pillar="recipes", content_type="single_image",
        )
        draft_id = await _save_pending_draft(
            db_path=tmp_db,
            account_id="test_account",
            image_path="/tmp/image.jpg",
            image_phash="abc123",
            caption="Test caption",
            hashtags=["tag1", "tag2"],
            alt_text="Alt text",
            brief=brief,
            timeout_hours=4,
        )
        assert draft_id > 0

        async with aiosqlite.connect(tmp_db) as db:
            cursor = await db.execute(
                "SELECT account_id, caption, content_type, status FROM pending_drafts WHERE id = ?",
                (draft_id,),
            )
            row = await cursor.fetchone()

        assert row[0] == "test_account"
        assert row[1] == "Test caption"
        assert row[2] == "single_image"
        assert row[3] == "pending"

    async def test_stores_content_type_and_duration(self, tmp_db):
        brief = PlannerBrief(
            topic="Test", angle="Angle", visual_keywords=["kw"],
            mood="mood", content_pillar="recipes", content_type="reel",
        )
        draft_id = await _save_pending_draft(
            db_path=tmp_db,
            account_id="test_account",
            image_path="/tmp/video.mp4",
            image_phash="vid123",
            caption="Reel caption",
            hashtags=["reel"],
            alt_text="Alt",
            brief=brief,
            timeout_hours=4,
            content_type="reel",
            duration_seconds=15.5,
        )

        async with aiosqlite.connect(tmp_db) as db:
            cursor = await db.execute(
                "SELECT content_type, duration_seconds FROM pending_drafts WHERE id = ?",
                (draft_id,),
            )
            row = await cursor.fetchone()

        assert row[0] == "reel"
        assert row[1] == 15.5


# ---------------------------------------------------------------------------
# get_pending_draft
# ---------------------------------------------------------------------------

class TestGetPendingDraft:
    async def test_returns_none_when_empty(self, tmp_db):
        result = await get_pending_draft(tmp_db, "test_account")
        assert result is None

    async def test_returns_pending_draft(self, tmp_db):
        brief = PlannerBrief(
            topic="Test", angle="Angle", visual_keywords=["kw"],
            mood="mood", content_pillar="recipes", content_type="single_image",
        )
        await _save_pending_draft(
            db_path=tmp_db,
            account_id="test_account",
            image_path="/tmp/image.jpg",
            image_phash="abc",
            caption="Draft caption",
            hashtags=["tag"],
            alt_text="Alt",
            brief=brief,
            timeout_hours=4,
        )

        result = await get_pending_draft(tmp_db, "test_account")
        assert result is not None
        assert result["account_id"] == "test_account"
        assert result["caption"] == "Draft caption"
        assert result["status"] == "pending"
