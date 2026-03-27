"""Account configuration loader and database initialization for auto-ig."""

import logging
import os
from dataclasses import dataclass

import aiosqlite
import yaml

logger = logging.getLogger(__name__)


@dataclass
class ImageSourcingConfig:
    """Configuration for image sourcing behavior."""

    stock_score_threshold: float
    sources: list[str]
    fallback: str


@dataclass
class AccountConfig:
    """Canonical account configuration loaded from YAML."""

    account_id: str
    instagram_user_id: str
    access_token_env: str

    niche: str
    language: str
    allowed_products: list[str]
    banned_topics: list[str]
    tone: str
    visual_style: str

    post_frequency: str
    preferred_time: str
    timezone: str

    discord_bot_token_env: str
    discord_channel_id_env: str
    auto_publish_timeout_hours: int

    content_pillars: list[str]
    image_sourcing: ImageSourcingConfig
    temp_http_port: int


_REQUIRED_ACCOUNT_KEYS = [
    "account_id",
    "instagram_user_id",
    "access_token_env",
    "niche",
    "language",
    "allowed_products",
    "banned_topics",
    "tone",
    "visual_style",
    "post_frequency",
    "preferred_time",
    "timezone",
    "discord_bot_token_env",
    "discord_channel_id_env",
    "auto_publish_timeout_hours",
    "content_pillars",
    "image_sourcing",
    "temp_http_port",
]

_REQUIRED_IMAGE_SOURCING_KEYS = ["stock_score_threshold", "sources", "fallback"]

_GLOBAL_ENV_VARS = [
    "GEMINI_API_KEY",
    "UNSPLASH_ACCESS_KEY",
    "PEXELS_API_KEY",
]


def load_account_config(config_path: str) -> AccountConfig:
    """Load an AccountConfig dataclass from a YAML file."""
    with open(config_path, "r") as f:
        raw = yaml.safe_load(f)

    if raw is None:
        raise ValueError(f"Config file is empty: {config_path}")

    missing = [k for k in _REQUIRED_ACCOUNT_KEYS if k not in raw]
    if missing:
        raise ValueError(
            f"Missing required keys in {config_path}: {', '.join(missing)}"
        )

    img_raw = raw["image_sourcing"]
    if not isinstance(img_raw, dict):
        raise ValueError(
            f"image_sourcing must be a mapping in {config_path}"
        )

    missing_img = [k for k in _REQUIRED_IMAGE_SOURCING_KEYS if k not in img_raw]
    if missing_img:
        raise ValueError(
            f"Missing image_sourcing keys in {config_path}: {', '.join(missing_img)}"
        )

    image_sourcing = ImageSourcingConfig(
        stock_score_threshold=float(img_raw["stock_score_threshold"]),
        sources=list(img_raw["sources"]),
        fallback=str(img_raw["fallback"]),
    )

    return AccountConfig(
        account_id=str(raw["account_id"]),
        instagram_user_id=str(raw["instagram_user_id"]),
        access_token_env=str(raw["access_token_env"]),
        niche=str(raw["niche"]),
        language=str(raw["language"]),
        allowed_products=list(raw["allowed_products"]),
        banned_topics=list(raw["banned_topics"]),
        tone=str(raw["tone"]),
        visual_style=str(raw["visual_style"]),
        post_frequency=str(raw["post_frequency"]),
        preferred_time=str(raw["preferred_time"]),
        timezone=str(raw["timezone"]),
        discord_bot_token_env=str(raw["discord_bot_token_env"]),
        discord_channel_id_env=str(raw["discord_channel_id_env"]),
        auto_publish_timeout_hours=int(raw["auto_publish_timeout_hours"]),
        content_pillars=list(raw["content_pillars"]),
        image_sourcing=image_sourcing,
        temp_http_port=int(raw["temp_http_port"]),
    )


def validate_env_vars(config: AccountConfig) -> None:
    """Validate that all required environment variables are set and non-empty."""
    required = list(_GLOBAL_ENV_VARS) + [
        config.access_token_env,
        config.discord_bot_token_env,
        config.discord_channel_id_env,
    ]

    missing = [var for var in required if not os.getenv(var)]

    if missing:
        raise ValueError(
            f"Missing or empty environment variables: {', '.join(missing)}"
        )

    logger.info("All required environment variables are present.")


async def init_db(db_path: str) -> None:
    """Create all SQLite tables if they do not already exist."""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS post_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id TEXT NOT NULL,
                topic TEXT NOT NULL,
                content_pillar TEXT NOT NULL,
                image_phash TEXT NOT NULL,
                caption_snippet TEXT NOT NULL,
                published_at TEXT NOT NULL,
                instagram_media_id TEXT
            )
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_drafts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id TEXT NOT NULL,
                image_path TEXT NOT NULL,
                image_phash TEXT NOT NULL DEFAULT '',
                caption TEXT NOT NULL,
                hashtags TEXT NOT NULL,
                alt_text TEXT NOT NULL,
                brief_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                publish_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
            )
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS schedule_config (
                account_id TEXT PRIMARY KEY,
                frequency TEXT NOT NULL DEFAULT '1d',
                preferred_time TEXT NOT NULL DEFAULT '08:00',
                timezone TEXT NOT NULL DEFAULT 'America/New_York',
                paused INTEGER NOT NULL DEFAULT 0
            )
            """
        )

        await db.commit()

    logger.info("Database initialized at %s", db_path)
