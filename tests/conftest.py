"""Shared fixtures for auto-ig test suite."""

import os
import pytest

from agents import (
    CaptionResult,
    ImageResult,
    PipelineResult,
    PlannerBrief,
    ReviewResult,
    VideoResult,
)
from utils.config_loader import AccountConfig, ImageSourcingConfig, init_db


# ---------------------------------------------------------------------------
# Factory fixtures — return callables that produce dataclass instances
# ---------------------------------------------------------------------------

@pytest.fixture
def make_account_config():
    """Factory for AccountConfig with sensible defaults."""

    def _factory(**overrides):
        defaults = dict(
            account_id="test_account",
            instagram_user_id="123456",
            access_token_env="TEST_IG_TOKEN",
            niche="organic food",
            language="en",
            allowed_products=["vegetables", "fruits"],
            banned_topics=["alcohol", "tobacco"],
            tone="warm and educational",
            visual_style="bright, natural food photography",
            post_frequency="1d",
            preferred_time="08:00",
            timezone="America/New_York",
            discord_bot_token_env="TEST_DISCORD_TOKEN",
            discord_channel_id_env="TEST_DISCORD_CHANNEL",
            auto_publish_timeout_hours=4,
            content_pillars=["recipes", "nutrition_tips", "farm_spotlight"],
            image_sourcing=ImageSourcingConfig(
                stock_score_threshold=0.6,
                sources=["unsplash", "pexels"],
                fallback="gemini",
            ),
            temp_http_port=9876,
            platforms=["instagram"],
            facebook_page_id="",
            facebook_page_token_env="",
        )
        defaults.update(overrides)
        return AccountConfig(**defaults)

    return _factory


@pytest.fixture
def make_planner_brief():
    """Factory for PlannerBrief with sensible defaults."""

    def _factory(**overrides):
        defaults = dict(
            topic="Fresh summer salads",
            angle="Easy weeknight prep",
            visual_keywords=["salad", "vegetables", "bowl"],
            mood="bright and energetic",
            content_pillar="recipes",
            content_type="single_image",
        )
        defaults.update(overrides)
        return PlannerBrief(**defaults)

    return _factory


@pytest.fixture
def make_image_result():
    """Factory for ImageResult with sensible defaults."""

    def _factory(**overrides):
        defaults = dict(
            local_path="/tmp/test_image.jpg",
            source="unsplash",
            phash="abcdef1234567890",
            score=0.85,
        )
        defaults.update(overrides)
        return ImageResult(**defaults)

    return _factory


@pytest.fixture
def make_caption_result():
    """Factory for CaptionResult with sensible defaults."""

    def _factory(**overrides):
        defaults = dict(
            caption="Fresh summer salads are the perfect weeknight meal!",
            hashtags=["salad", "healthy", "organic"],
            alt_text="A colorful bowl of fresh summer salad with vegetables.",
        )
        defaults.update(overrides)
        return CaptionResult(**defaults)

    return _factory


@pytest.fixture
def make_video_result():
    """Factory for VideoResult with sensible defaults."""

    def _factory(**overrides):
        defaults = dict(
            local_path="/tmp/test_video.mp4",
            source="pexels",
            phash="1234567890abcdef",
            score=0.80,
            duration_seconds=15.0,
            width=1080,
            height=1920,
        )
        defaults.update(overrides)
        return VideoResult(**defaults)

    return _factory


@pytest.fixture
def make_review_result():
    """Factory for ReviewResult with sensible defaults (PASS)."""

    def _factory(**overrides):
        defaults = dict(
            status="PASS",
            reasons=[],
            retry_type=None,
        )
        defaults.update(overrides)
        return ReviewResult(**defaults)

    return _factory


# ---------------------------------------------------------------------------
# Database fixture — real SQLite via aiosqlite
# ---------------------------------------------------------------------------

@pytest.fixture
async def tmp_db(tmp_path):
    """Create and initialize a real SQLite database in a temp directory."""
    db_path = str(tmp_path / "test.db")
    await init_db(db_path)
    return db_path
