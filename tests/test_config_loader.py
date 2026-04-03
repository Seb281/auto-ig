"""Tests for utils/config_loader.py — config loading, env validation, DB init."""

import os

import aiosqlite
import pytest
import yaml

from utils.config_loader import (
    AccountConfig,
    ImageSourcingConfig,
    init_db,
    load_account_config,
    validate_env_vars,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _minimal_config_dict():
    """Return a minimal valid config dict for YAML serialization."""
    return {
        "account_id": "test",
        "instagram_user_id_env": "TEST_IG_USER_ID",
        "access_token_env": "TEST_TOKEN",
        "niche": "food",
        "language": "en",
        "allowed_products": ["a"],
        "banned_topics": ["b"],
        "tone": "warm",
        "visual_style": "bright",
        "post_frequency": "1d",
        "preferred_time": "08:00",
        "timezone": "UTC",
        "discord_bot_token_env": "DTOKEN",
        "discord_channel_id_env": "DCHANNEL",
        "auto_publish_timeout_hours": 4,
        "content_pillars": ["recipes"],
        "image_sourcing": {
            "stock_score_threshold": 0.6,
            "sources": ["unsplash"],
            "fallback": "gemini",
        },
        "temp_http_port": 9876,
    }


def _write_yaml(path, data):
    with open(path, "w") as f:
        yaml.dump(data, f)


# ---------------------------------------------------------------------------
# load_account_config
# ---------------------------------------------------------------------------

class TestLoadAccountConfig:
    def test_valid_config(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TEST_IG_USER_ID", "123")
        cfg_path = str(tmp_path / "config.yaml")
        _write_yaml(cfg_path, _minimal_config_dict())
        config = load_account_config(cfg_path)
        assert isinstance(config, AccountConfig)
        assert config.account_id == "test"
        assert config.instagram_user_id == "123"
        assert isinstance(config.image_sourcing, ImageSourcingConfig)

    def test_missing_required_key(self, tmp_path):
        data = _minimal_config_dict()
        del data["niche"]
        cfg_path = str(tmp_path / "config.yaml")
        _write_yaml(cfg_path, data)
        with pytest.raises(ValueError, match="Missing required keys"):
            load_account_config(cfg_path)

    def test_empty_file(self, tmp_path):
        cfg_path = str(tmp_path / "config.yaml")
        with open(cfg_path, "w") as f:
            f.write("")
        with pytest.raises(ValueError, match="empty"):
            load_account_config(cfg_path)

    def test_missing_image_sourcing_keys(self, tmp_path):
        data = _minimal_config_dict()
        data["image_sourcing"] = {"stock_score_threshold": 0.5}
        cfg_path = str(tmp_path / "config.yaml")
        _write_yaml(cfg_path, data)
        with pytest.raises(ValueError, match="Missing image_sourcing keys"):
            load_account_config(cfg_path)

    def test_optional_platforms_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TEST_IG_USER_ID", "123")
        cfg_path = str(tmp_path / "config.yaml")
        _write_yaml(cfg_path, _minimal_config_dict())
        config = load_account_config(cfg_path)
        assert config.platforms == ["instagram"]

    def test_optional_facebook_fields(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TEST_IG_USER_ID", "123")
        data = _minimal_config_dict()
        data["platforms"] = ["instagram", "facebook"]
        data["facebook_page_id"] = "pg_123"
        data["facebook_page_token_env"] = "FB_TOKEN"
        cfg_path = str(tmp_path / "config.yaml")
        _write_yaml(cfg_path, data)
        config = load_account_config(cfg_path)
        assert "facebook" in config.platforms
        assert config.facebook_page_id == "pg_123"


# ---------------------------------------------------------------------------
# validate_env_vars
# ---------------------------------------------------------------------------

class TestValidateEnvVars:
    def test_all_present(self, make_account_config, monkeypatch):
        config = make_account_config()
        monkeypatch.setenv("GEMINI_API_KEY", "x")
        monkeypatch.setenv("UNSPLASH_ACCESS_KEY", "x")
        monkeypatch.setenv("PEXELS_API_KEY", "x")
        monkeypatch.setenv("TEST_IG_TOKEN", "x")
        monkeypatch.setenv("TEST_DISCORD_TOKEN", "x")
        monkeypatch.setenv("TEST_DISCORD_CHANNEL", "x")
        validate_env_vars(config)  # should not raise

    def test_missing_global_var(self, make_account_config, monkeypatch):
        config = make_account_config()
        # Set everything except GEMINI_API_KEY
        monkeypatch.setenv("UNSPLASH_ACCESS_KEY", "x")
        monkeypatch.setenv("PEXELS_API_KEY", "x")
        monkeypatch.setenv("TEST_IG_TOKEN", "x")
        monkeypatch.setenv("TEST_DISCORD_TOKEN", "x")
        monkeypatch.setenv("TEST_DISCORD_CHANNEL", "x")
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        with pytest.raises(ValueError, match="GEMINI_API_KEY"):
            validate_env_vars(config)

    def test_facebook_token_required_when_enabled(self, make_account_config, monkeypatch):
        config = make_account_config(
            platforms=["instagram", "facebook"],
            facebook_page_token_env="FB_PAGE_TOKEN",
        )
        monkeypatch.setenv("GEMINI_API_KEY", "x")
        monkeypatch.setenv("UNSPLASH_ACCESS_KEY", "x")
        monkeypatch.setenv("PEXELS_API_KEY", "x")
        monkeypatch.setenv("TEST_IG_TOKEN", "x")
        monkeypatch.setenv("TEST_DISCORD_TOKEN", "x")
        monkeypatch.setenv("TEST_DISCORD_CHANNEL", "x")
        monkeypatch.delenv("FB_PAGE_TOKEN", raising=False)
        with pytest.raises(ValueError, match="FB_PAGE_TOKEN"):
            validate_env_vars(config)


# ---------------------------------------------------------------------------
# init_db
# ---------------------------------------------------------------------------

class TestInitDb:
    async def test_tables_created(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        await init_db(db_path)
        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
            tables = {row[0] for row in await cursor.fetchall()}
        assert "post_history" in tables
        assert "pending_drafts" in tables
        assert "schedule_config" in tables

    async def test_migrations_idempotent(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        await init_db(db_path)
        await init_db(db_path)  # should not raise

    async def test_migration_columns_exist(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        await init_db(db_path)
        async with aiosqlite.connect(db_path) as db:
            # Check schedule_config has killed column
            cursor = await db.execute("PRAGMA table_info(schedule_config)")
            cols = {row[1] for row in await cursor.fetchall()}
            assert "auto_publish" in cols
            assert "killed" in cols

            # Check pending_drafts has content_type and duration_seconds
            cursor = await db.execute("PRAGMA table_info(pending_drafts)")
            cols = {row[1] for row in await cursor.fetchall()}
            assert "content_type" in cols
            assert "duration_seconds" in cols

            # Check post_history has published_platforms
            cursor = await db.execute("PRAGMA table_info(post_history)")
            cols = {row[1] for row in await cursor.fetchall()}
            assert "published_platforms" in cols
