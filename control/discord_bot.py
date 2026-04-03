"""Discord bot — commands, notifications, photo intake, and auto-publish timer.

Implements the full Discord control interface for auto-ig using discord.py 2.x
(prefix-based commands with ``!``). All state is persisted in SQLite via the
pending_drafts table so the 2h auto-publish timer survives restarts.

Supports multi-account: commands are routed to the correct account based on
the incoming channel_id. Each account's config, db_path, and dry_run flag are
stored in bot.bot_data["accounts"][channel_id].
"""

import asyncio
import io
import json
import logging
import os
import shutil
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

import aiosqlite
import discord
from discord.ext import commands

import httpx

from agents import PipelineResult, PlannerBrief
from agents.orchestrator import run_pipeline
from agents.reviewer import STATUS_FAIL
from publisher.instagram import publish_post, publish_carousel, publish_reel, save_post_record
from publisher.facebook import publish_photo_to_facebook, publish_carousel_to_facebook
from publisher.scheduler import get_next_run_time, pipeline_job_id, schedule_pipeline_job
from utils.ai_client import generate_text, generate_image
from utils.config_loader import AccountConfig
from utils.image_utils import is_duplicate_image
from utils.prompts import adapt_caption_for_platform, build_facebook_caption

logger = logging.getLogger(__name__)

# Valid frequency values
_VALID_FREQUENCIES = {"1d", "2d", "3x", "2x", "1x"}

# Valid content types and platforms for !run / !runstock
_VALID_CONTENT_TYPES = {"single", "carousel", "reel"}
_VALID_PLATFORMS = {"instagram", "facebook"}
_TYPE_MAP = {"single": "single_image", "carousel": "carousel", "reel": "reel"}

# Discord message length limit
_DISCORD_MSG_LIMIT = 2000


def _read_file_bytes(path: str) -> bytes:
    """Read a file's contents as bytes (meant to be called via asyncio.to_thread)."""
    with open(path, "rb") as f:
        return f.read()


# ---------------------------------------------------------------------------
# Multi-account context helpers
# ---------------------------------------------------------------------------

def _get_account_context(bot_data: dict, channel_id: int) -> dict | None:
    """Look up account context (config, db_path, dry_run) by channel_id."""
    accounts = bot_data.get("accounts", {})
    return accounts.get(channel_id)


def _killswitch_key(account_id: str) -> str:
    """Return the bot_data key for an account's killswitch state."""
    return f"killswitch_{account_id}"


def _is_killed(bot_data: dict, config: AccountConfig) -> bool:
    """Check whether the killswitch is active for an account."""
    return bool(bot_data.get(_killswitch_key(config.account_id)))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _format_draft_caption(caption: str, hashtags: list[str]) -> str:
    """Build the full caption text with hashtags appended."""
    tag_line = " ".join(f"#{h}" for h in hashtags)
    return f"{caption}\n\n{tag_line}" if tag_line else caption


def _format_draft_preview(
    caption: str,
    hashtags: list[str],
    content_type: str = "single_image",
    target_platforms: str = "",
) -> str:
    """Build a human-readable draft preview for Discord."""
    full = _format_draft_caption(caption, hashtags)
    if content_type == "carousel":
        type_label = "CAROUSEL"
    elif content_type == "reel":
        type_label = "REEL"
    else:
        type_label = "SINGLE IMAGE"
    header = f"--- DRAFT PREVIEW ({type_label}"
    if target_platforms:
        header += f" \u2192 {target_platforms}"
    header += ") ---"
    lines = [
        header,
        "",
        full,
        "",
        "--- END PREVIEW ---",
        "",
        "Commands: !approve  !skip  !edit <new caption>  !regenerate",
    ]
    return "\n".join(lines)


def _truncate(text: str, limit: int = _DISCORD_MSG_LIMIT) -> str:
    """Truncate text to fit within Discord's message length limit."""
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


async def get_pending_draft(db_path: str, account_id: str) -> dict | None:
    """Return the most recent pending draft as a dict, or None."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM pending_drafts "
            "WHERE account_id = ? AND status = 'pending' "
            "ORDER BY created_at DESC LIMIT 1",
            (account_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return dict(row)


async def _update_draft_status(
    db_path: str, draft_id: int, new_status: str
) -> None:
    """Update the status of a pending draft."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE pending_drafts SET status = ? WHERE id = ?",
            (new_status, draft_id),
        )
        await db.commit()


async def _update_draft_caption(
    db_path: str, draft_id: int, new_caption: str
) -> None:
    """Replace the caption on a pending draft."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE pending_drafts SET caption = ? WHERE id = ?",
            (new_caption, draft_id),
        )
        await db.commit()


async def _save_pending_draft(
    db_path: str,
    account_id: str,
    image_path: str,
    image_phash: str,
    caption: str,
    hashtags: list[str],
    alt_text: str,
    brief: PlannerBrief,
    timeout_hours: int,
    content_type: str = "single_image",
    duration_seconds: float | None = None,
    target_platforms: str = "",
) -> int:
    """Insert a new pending draft and return its row ID."""
    now = datetime.now(timezone.utc)
    publish_at = now + timedelta(hours=timeout_hours)

    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            """
            INSERT INTO pending_drafts
                (account_id, image_path, image_phash, caption, hashtags, alt_text,
                 brief_json, created_at, publish_at, status, content_type, duration_seconds,
                 target_platforms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
            """,
            (
                account_id,
                image_path,
                image_phash,
                caption,
                json.dumps(hashtags),
                alt_text,
                json.dumps(asdict(brief)),
                now.isoformat(),
                publish_at.isoformat(),
                content_type,
                duration_seconds,
                target_platforms,
            ),
        )
        await db.commit()
        draft_id = cursor.lastrowid

    logger.info(
        "Pending draft saved — id=%d, content_type=%s, target_platforms=%s, publish_at=%s",
        draft_id,
        content_type,
        target_platforms or "all",
        publish_at.isoformat(),
    )
    return draft_id


async def _get_schedule_config(
    db_path: str, account_id: str
) -> dict | None:
    """Return the schedule_config row for an account, or None."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM schedule_config WHERE account_id = ?",
            (account_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return dict(row)


async def _upsert_schedule_config(
    db_path: str,
    account_id: str,
    frequency: str | None = None,
    preferred_time: str | None = None,
    paused: int | None = None,
    auto_publish: int | None = None,
    killed: int | None = None,
    timezone: str = "UTC",
) -> None:
    """Insert or update the schedule_config for an account."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM schedule_config WHERE account_id = ?",
            (account_id,),
        )
        existing = await cursor.fetchone()

        if existing is None:
            await db.execute(
                """
                INSERT INTO schedule_config
                    (account_id, frequency, preferred_time, timezone, paused, auto_publish, killed)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    account_id,
                    frequency or "1d",
                    preferred_time or "08:00",
                    timezone,
                    paused if paused is not None else 0,
                    auto_publish if auto_publish is not None else 0,
                    killed if killed is not None else 0,
                ),
            )
        else:
            if frequency is not None:
                await db.execute(
                    "UPDATE schedule_config SET frequency = ? WHERE account_id = ?",
                    (frequency, account_id),
                )
            if preferred_time is not None:
                await db.execute(
                    "UPDATE schedule_config SET preferred_time = ? WHERE account_id = ?",
                    (preferred_time, account_id),
                )
            if paused is not None:
                await db.execute(
                    "UPDATE schedule_config SET paused = ? WHERE account_id = ?",
                    (paused, account_id),
                )
            if auto_publish is not None:
                await db.execute(
                    "UPDATE schedule_config SET auto_publish = ? WHERE account_id = ?",
                    (auto_publish, account_id),
                )
            if killed is not None:
                await db.execute(
                    "UPDATE schedule_config SET killed = ? WHERE account_id = ?",
                    (killed, account_id),
                )

        await db.commit()


# ---------------------------------------------------------------------------
# Auto-publish timer
# ---------------------------------------------------------------------------

def _auto_publish_task_key(account_id: str) -> str:
    """Return the bot_data key for an account's auto-publish asyncio task."""
    return f"auto_publish_task_{account_id}"


async def _auto_publish_draft(
    acct_ctx: dict,
    bot_data: dict,
    draft: dict,
    channel_id: int,
    bot: commands.Bot,
) -> None:
    """Publish a draft after the auto-publish timeout elapses."""
    config: AccountConfig = acct_ctx["config"]
    db_path: str = acct_ctx["db_path"]
    draft_id = draft["id"]

    publish_at = datetime.fromisoformat(draft["publish_at"])
    now = datetime.now(timezone.utc)
    delay = (publish_at - now).total_seconds()

    if delay > 0:
        logger.info(
            "Auto-publish timer started for draft %d (account '%s') — %.0f seconds remaining.",
            draft_id,
            config.account_id,
            delay,
        )
        await asyncio.sleep(delay)

    # Re-check status — user may have already acted
    current = await get_pending_draft(db_path, config.account_id)
    if current is None or current["id"] != draft_id or current["status"] != "pending":
        logger.info(
            "Draft %d is no longer pending — auto-publish cancelled.", draft_id
        )
        return

    # Block auto-publish if killswitch is active
    if _is_killed(bot_data, config):
        logger.info(
            "Killswitch active for '%s' — auto-publish of draft %d blocked.",
            config.account_id,
            draft_id,
        )
        return

    logger.info("Auto-publish timeout reached for draft %d.", draft_id)
    await _do_publish_draft(acct_ctx, bot_data, draft, channel_id, bot, auto=True)


async def _do_publish_draft(
    acct_ctx: dict,
    bot_data: dict,
    draft: dict,
    channel_id: int,
    bot: commands.Bot,
    auto: bool = False,
) -> None:
    """Execute the actual publish for a draft (shared by approve and auto-publish)."""
    config: AccountConfig = acct_ctx["config"]
    db_path: str = acct_ctx["db_path"]
    dry_run: bool = acct_ctx["dry_run"]
    draft_id = draft["id"]

    # Guard: re-check draft status to prevent double-publish race
    current = await get_pending_draft(db_path, config.account_id)
    if current is None or current["id"] != draft_id or current["status"] != "pending":
        logger.info("Draft %d is no longer pending — publish aborted.", draft_id)
        return

    caption = draft["caption"]
    hashtags = json.loads(draft["hashtags"])
    alt_text = draft["alt_text"]
    image_path = draft["image_path"]
    image_phash = draft.get("image_phash", "")
    brief_data = json.loads(draft["brief_json"])
    content_type = draft.get("content_type", "single_image")

    full_caption = _format_draft_caption(caption, hashtags)

    await bot.wait_until_ready()
    channel = bot.get_channel(channel_id)
    if channel is None:
        logger.error("Channel %d not found — cannot publish draft %d.", channel_id, draft_id)
        return

    # Determine image paths — carousel uses JSON array, single uses plain path
    if content_type == "carousel":
        try:
            image_paths = json.loads(image_path)
        except (json.JSONDecodeError, TypeError):
            image_paths = [image_path]
        # Use first image phash for duplicate check
        primary_phash = image_phash.split(",")[0] if image_phash else ""
    else:
        image_paths = [image_path]
        primary_phash = image_phash

    # Guard: block duplicate images at publish time
    if primary_phash and await is_duplicate_image(primary_phash, db_path, config.account_id):
        logger.warning("Draft %d blocked — duplicate image detected at publish time.", draft_id)
        await _update_draft_status(db_path, draft_id, "skipped")
        await channel.send(
            content=f"Draft #{draft_id} blocked — image is too similar to a previous post. Use !run to generate a new one.",
        )
        return

    # Determine which platforms to publish to
    raw_target = draft.get("target_platforms", "")
    if raw_target:
        platforms_to_publish = [p.strip() for p in raw_target.split(",")]
    else:
        platforms_to_publish = list(config.platforms)

    published_platforms: list[str] = []
    media_id: str | None = None
    publish_succeeded = False

    try:
        if dry_run:
            logger.info("[DRY RUN] Skipping publish for draft %d.", draft_id)
            media_id = None
            prefix = "[DRY RUN] "
            published_platforms = list(platforms_to_publish)
        else:
            # Publish to Instagram
            if "instagram" in platforms_to_publish:
                if content_type == "reel":
                    media_id = await publish_reel(
                        config, image_paths[0], full_caption
                    )
                elif content_type == "carousel" and len(image_paths) >= 2:
                    media_id = await publish_carousel(
                        config, image_paths, full_caption, alt_text
                    )
                else:
                    media_id = await publish_post(
                        config, image_paths[0], full_caption, alt_text
                    )
                published_platforms.append("instagram")
            prefix = ""

            # Publish to Facebook if targeted (skip for reels)
            if (
                content_type != "reel"
                and "facebook" in platforms_to_publish
                and config.facebook_page_id
            ):
                try:
                    fb_caption = await adapt_caption_for_platform(caption, hashtags, "facebook")
                    if content_type == "carousel" and len(image_paths) >= 2:
                        fb_post_id = await publish_carousel_to_facebook(
                            config, image_paths, fb_caption
                        )
                    else:
                        fb_post_id = await publish_photo_to_facebook(
                            config, image_paths[0], fb_caption
                        )
                    published_platforms.append("facebook")
                    logger.info("Also published to Facebook — post ID: %s", fb_post_id)
                except Exception as fb_exc:
                    logger.error("Facebook publish failed: %s", fb_exc, exc_info=True)
                    # Don't fail the whole publish if Facebook fails

        await save_post_record(
            db_path=db_path,
            account_id=config.account_id,
            topic=brief_data.get("topic", "Unknown"),
            content_pillar=brief_data.get("content_pillar", ""),
            image_phash=primary_phash,
            caption=full_caption,
            instagram_media_id=media_id,
        )

        await _update_draft_status(db_path, draft_id, "published")
        publish_succeeded = True

        source = "Auto-published" if auto else "Published"
        platform_str = ", ".join(published_platforms) if published_platforms else "none"
        msg = f"{prefix}{source} successfully to: {platform_str}."
        if media_id:
            msg += f"\nInstagram Media ID: {media_id}"
        await channel.send(content=msg)
        logger.info("%s draft %d to %s.", source, draft_id, platform_str)

    except Exception as exc:
        logger.error("Publish failed for draft %d: %s", draft_id, exc, exc_info=True)
        # Keep draft as "pending" so it can be retried with !approve
        await channel.send(
            content=f"Publish failed: {exc}\nDraft kept — use `!approve` to retry or `!skip` to discard."
        )

    finally:
        # Only delete media files after a successful publish
        if publish_succeeded:
            for path in image_paths:
                if path and os.path.exists(path):
                    try:
                        os.remove(path)
                        logger.info("Cleaned up draft image: %s", path)
                    except OSError as exc:
                        logger.warning("Failed to clean up image %s: %s", path, exc)

    task_key = _auto_publish_task_key(config.account_id)
    bot_data.pop(task_key, None)


def _start_auto_publish_timer(
    acct_ctx: dict,
    bot_data: dict,
    draft: dict,
    channel_id: int,
    bot: commands.Bot,
) -> None:
    """Schedule the auto-publish coroutine and store the task handle."""
    config: AccountConfig = acct_ctx["config"]
    task_key = _auto_publish_task_key(config.account_id)

    existing_task = bot_data.get(task_key)
    if existing_task is not None and not existing_task.done():
        existing_task.cancel()
        logger.info("Cancelled previous auto-publish timer for '%s'.", config.account_id)

    task = asyncio.create_task(
        _auto_publish_draft(acct_ctx, bot_data, draft, channel_id, bot)
    )
    bot_data[task_key] = task
    logger.info("Auto-publish timer scheduled for draft %d (account '%s').", draft["id"], config.account_id)


# ---------------------------------------------------------------------------
# Notification helpers
# ---------------------------------------------------------------------------

async def send_draft_for_review(
    bot: commands.Bot,
    channel_id: int,
    result: PipelineResult,
    bot_data: dict,
    target_platforms: str = "",
) -> None:
    """Send a draft (image + caption preview) to Discord and start the auto-publish timer."""
    await bot.wait_until_ready()

    acct_ctx = _get_account_context(bot_data, channel_id)
    if acct_ctx is None:
        logger.error("No account context found for channel_id=%d in send_draft_for_review.", channel_id)
        return

    config: AccountConfig = acct_ctx["config"]
    db_path: str = acct_ctx["db_path"]

    channel = bot.get_channel(channel_id)
    if channel is None:
        logger.error("Channel %d not found — cannot send draft for review.", channel_id)
        return

    if result.caption is None or result.brief is None:
        await channel.send(
            content="Pipeline produced an incomplete result — cannot send draft for review.",
        )
        return

    # Determine content type and media list
    content_type = result.brief.content_type if result.brief else "single_image"
    is_carousel = content_type == "carousel" and len(result.images) > 1
    is_reel = content_type == "reel" and result.video is not None

    # Copy media to draft-specific paths
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    media_dir = os.path.join(base_dir, "storage", "media")
    os.makedirs(media_dir, exist_ok=True)

    draft_image_paths: list[str] = []
    all_phashes: list[str] = []
    duration_seconds: float | None = None

    if is_reel:
        draft_path = os.path.join(
            media_dir, f"draft_reel_{os.path.basename(result.video.local_path)}"
        )
        await asyncio.to_thread(shutil.copy2, result.video.local_path, draft_path)
        draft_image_paths.append(draft_path)
        all_phashes.append(result.video.phash)
        duration_seconds = result.video.duration_seconds
        # Clean up original
        if os.path.exists(result.video.local_path):
            os.remove(result.video.local_path)
    elif is_carousel:
        for i, img in enumerate(result.images):
            draft_path = os.path.join(
                media_dir, f"draft_carousel_{i}_{os.path.basename(img.local_path)}"
            )
            await asyncio.to_thread(shutil.copy2, img.local_path, draft_path)
            draft_image_paths.append(draft_path)
            all_phashes.append(img.phash)
            # Clean up original
            if os.path.exists(img.local_path):
                os.remove(img.local_path)
    else:
        if result.image is None:
            await channel.send(
                content="Pipeline produced no image — cannot send draft for review.",
            )
            return
        draft_path = os.path.join(
            media_dir, f"draft_{os.path.basename(result.image.local_path)}"
        )
        await asyncio.to_thread(shutil.copy2, result.image.local_path, draft_path)
        draft_image_paths.append(draft_path)
        all_phashes.append(result.image.phash)
        # Clean up original
        if os.path.exists(result.image.local_path):
            os.remove(result.image.local_path)

    # For database storage: carousel stores JSON array, single/reel stores plain path
    if is_carousel:
        db_image_path = json.dumps(draft_image_paths)
        db_image_phash = ",".join(all_phashes)
    else:
        db_image_path = draft_image_paths[0]
        db_image_phash = all_phashes[0] if all_phashes else ""

    # Save to pending_drafts
    draft_id = await _save_pending_draft(
        db_path=db_path,
        account_id=config.account_id,
        image_path=db_image_path,
        image_phash=db_image_phash,
        caption=result.caption.caption,
        hashtags=result.caption.hashtags,
        alt_text=result.caption.alt_text,
        brief=result.brief,
        timeout_hours=config.auto_publish_timeout_hours,
        content_type=content_type,
        duration_seconds=duration_seconds,
        target_platforms=target_platforms,
    )

    # Send media to Discord
    try:
        if is_reel:
            video_bytes = await asyncio.to_thread(_read_file_bytes, draft_image_paths[0])
            file = discord.File(io.BytesIO(video_bytes), filename="draft_reel.mp4")
            await channel.send(
                content=f"[{config.account_id}] Draft #{draft_id} ready for review",
                file=file,
            )
        elif is_carousel:
            # Send all carousel images
            for i, path in enumerate(draft_image_paths):
                photo_bytes = await asyncio.to_thread(_read_file_bytes, path)
                file = discord.File(io.BytesIO(photo_bytes), filename=f"draft_slide_{i + 1}.jpg")
                label = f"[{config.account_id}] Draft #{draft_id} — Slide {i + 1}/{len(draft_image_paths)}"
                await channel.send(content=label, file=file)
        else:
            photo_bytes = await asyncio.to_thread(_read_file_bytes, draft_image_paths[0])
            file = discord.File(io.BytesIO(photo_bytes), filename="draft.jpg")
            await channel.send(
                content=f"[{config.account_id}] Draft #{draft_id} ready for review",
                file=file,
            )
    except Exception as exc:
        logger.error("Failed to send draft media: %s", exc)
        await channel.send(
            content=f"[{config.account_id}] Draft #{draft_id} ready (could not send media: {exc})",
        )

    # Send caption preview
    preview = _format_draft_preview(
        result.caption.caption, result.caption.hashtags, content_type, target_platforms
    )

    await channel.send(content=_truncate(preview))

    # Load the draft back so we have the full row
    draft = await get_pending_draft(db_path, config.account_id)
    if draft is not None:
        sched = await _get_schedule_config(db_path, config.account_id)
        if sched and sched["auto_publish"]:
            _start_auto_publish_timer(acct_ctx, bot_data, draft, channel_id, bot)
        else:
            logger.info(
                "Auto-publish is off for '%s' — draft %d awaits manual action.",
                config.account_id,
                draft["id"],
            )


async def send_escalation(
    bot: commands.Bot,
    channel_id: int,
    result: PipelineResult,
) -> None:
    """Send a reviewer-escalation message with failure reasons and options."""
    await bot.wait_until_ready()
    channel = bot.get_channel(channel_id)
    if channel is None:
        return

    reasons = (
        "\n".join(f"  - {r}" for r in result.review.reasons)
        if result.review
        else "  - Unknown"
    )
    msg = (
        "Reviewer FAILED after max retries.\n\n"
        f"Reasons:\n{reasons}\n\n"
        "Commands: !approve_anyway  !regenerate  !skip_today"
    )
    await channel.send(content=_truncate(msg))


async def send_pipeline_error(
    bot: commands.Bot,
    channel_id: int,
    error: str,
) -> None:
    """Send a pipeline failure alert."""
    await bot.wait_until_ready()
    channel = bot.get_channel(channel_id)
    if channel is None:
        return

    await channel.send(content=_truncate(f"Pipeline error: {error}"))


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def cmd_start(ctx: commands.Context) -> None:
    """Handle !start — welcome message."""
    await ctx.send(
        "auto-ig bot is running.\n\n"
        "**Pipeline**\n"
        "`!run [single|carousel|reel] [instagram,facebook]` — trigger a pipeline run\n"
        "`!runstock [single|carousel|reel] [instagram,facebook]` — run using only stock media\n"
        "`!suggest <topic>` — queue a topic hint for the next run\n"
        "\n"
        "**Draft Review**\n"
        "`!approve` — publish the pending draft\n"
        "`!approve_anyway` — publish despite reviewer failure\n"
        "`!skip` — discard draft, generate a new one\n"
        "`!skip_today` — skip today's post entirely\n"
        "`!edit <caption>` — replace the draft caption\n"
        "`!regenerate` — discard and regenerate from scratch\n"
        "\n"
        "**Scheduling**\n"
        "`!pause` — pause the scheduler\n"
        "`!resume` — resume the scheduler\n"
        "`!setfrequency <value>` — change posting schedule (1d, 2d, 3x, 2x, 1x)\n"
        "`!autopublish` — toggle auto-publish on/off\n"
        "`!killswitch` — toggle all bot activity on/off (overrides everything)\n"
        "\n"
        "**Info**\n"
        "`!status` — show schedule, next run, and killswitch state\n"
        "`!check` — ping all external services\n"
        "`!start` — show this help message\n"
        "\n"
        "**Photo Intake**\n"
        "Send an image as an attachment (no command needed) to start a pipeline run with your photo.\n"
    )


async def cmd_check(ctx: commands.Context) -> None:
    """Handle !check — ping all external services and report status."""
    bot_data = ctx.bot.bot_data
    channel_id = ctx.channel.id

    acct_ctx = _get_account_context(bot_data, channel_id)
    if acct_ctx is None:
        await ctx.send("No account is configured for this channel.")
        return

    config: AccountConfig = acct_ctx["config"]
    await ctx.send("Checking services...")
    lines: list[str] = []

    # 1. Gemini text
    try:
        resp = await generate_text("Reply with exactly: OK")
        lines.append(f"Gemini text: OK")
    except Exception as exc:
        lines.append(f"Gemini text: FAIL — {_truncate(str(exc), 120)}")

    # 2. Gemini image generation
    try:
        await generate_image("A solid blue square")
        lines.append("Gemini image gen: OK")
    except Exception as exc:
        lines.append(f"Gemini image gen: FAIL — {_truncate(str(exc), 120)}")

    # 3. Unsplash
    try:
        key = os.getenv("UNSPLASH_ACCESS_KEY")
        if not key:
            lines.append("Unsplash: FAIL — UNSPLASH_ACCESS_KEY not set")
        else:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(
                    "https://api.unsplash.com/search/photos",
                    params={"query": "food", "per_page": 1},
                    headers={"Authorization": f"Client-ID {key}"},
                )
            if r.status_code == 200:
                lines.append("Unsplash: OK")
            else:
                lines.append(f"Unsplash: FAIL — HTTP {r.status_code}")
    except Exception as exc:
        lines.append(f"Unsplash: FAIL — {_truncate(str(exc), 120)}")

    # 4. Pexels
    try:
        key = os.getenv("PEXELS_API_KEY")
        if not key:
            lines.append("Pexels: FAIL — PEXELS_API_KEY not set")
        else:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(
                    "https://api.pexels.com/v1/search",
                    params={"query": "food", "per_page": 1},
                    headers={"Authorization": key},
                )
            if r.status_code == 200:
                lines.append("Pexels: OK")
            else:
                lines.append(f"Pexels: FAIL — HTTP {r.status_code}")
    except Exception as exc:
        lines.append(f"Pexels: FAIL — {_truncate(str(exc), 120)}")

    # 5. Meta Graph API — check permissions
    try:
        token = os.getenv(config.access_token_env)
        if not token:
            lines.append(f"Meta API: FAIL — {config.access_token_env} not set")
        else:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(
                    "https://graph.facebook.com/v25.0/me",
                    params={"fields": "id,name", "access_token": token},
                )
            data = r.json()
            if "error" in data:
                err_msg = data["error"].get("message", "Unknown")
                lines.append(f"Meta API: FAIL — {_truncate(err_msg, 120)}")
            else:
                page_name = data.get("name", "?")
                # Check IG publishing permissions
                async with httpx.AsyncClient(timeout=10.0) as client:
                    r2 = await client.get(
                        f"https://graph.facebook.com/v25.0/{config.instagram_user_id}",
                        params={"fields": "id,username", "access_token": token},
                    )
                ig_data = r2.json()
                if "error" in ig_data:
                    ig_err = ig_data["error"].get("message", "Unknown")
                    lines.append(
                        f"Meta API: token OK (page: {page_name}) — "
                        f"IG permissions: INSUFFICIENT — {_truncate(ig_err, 80)}"
                    )
                else:
                    ig_user = ig_data.get("username", ig_data.get("id", "?"))
                    lines.append(
                        f"Meta API: OK — page: {page_name}, IG: @{ig_user}"
                    )
    except Exception as exc:
        lines.append(f"Meta API: FAIL — {_truncate(str(exc), 120)}")

    # 6. Facebook Pages (if configured)
    if "facebook" in config.platforms and config.facebook_page_token_env:
        try:
            fb_token = os.getenv(config.facebook_page_token_env)
            if not fb_token:
                lines.append(f"Facebook: FAIL — {config.facebook_page_token_env} not set")
            else:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    r = await client.get(
                        f"https://graph.facebook.com/v25.0/{config.facebook_page_id}",
                        params={"fields": "id,name", "access_token": fb_token},
                    )
                fb_data = r.json()
                if "error" in fb_data:
                    fb_err = fb_data["error"].get("message", "Unknown")
                    lines.append(f"Facebook: FAIL — {_truncate(fb_err, 120)}")
                else:
                    fb_name = fb_data.get("name", "?")
                    lines.append(f"Facebook: OK — page: {fb_name}")
        except Exception as exc:
            lines.append(f"Facebook: FAIL — {_truncate(str(exc), 120)}")

    # Show platforms
    lines.append(f"\nPlatforms: {', '.join(config.platforms)}")

    await ctx.send("```\n" + "\n".join(lines) + "\n```")


async def cmd_runstock(ctx: commands.Context, content_type: str = "", platforms: str = "") -> None:
    """Handle !runstock — pipeline run using only stock photos (no AI image gen)."""
    bot = ctx.bot
    bot_data = bot.bot_data
    channel_id = ctx.channel.id

    acct_ctx = _get_account_context(bot_data, channel_id)
    if acct_ctx is None:
        await ctx.send("No account is configured for this channel.")
        return

    config: AccountConfig = acct_ctx["config"]
    db_path: str = acct_ctx["db_path"]
    dry_run: bool = acct_ctx["dry_run"]

    # Validate content type
    force_type = None
    if content_type:
        if content_type not in _VALID_CONTENT_TYPES:
            await ctx.send(f"Unknown content type `{content_type}`. Valid: single, carousel, reel")
            return
        force_type = _TYPE_MAP[content_type]

    # Validate platforms
    target_platforms: list[str] | None = None
    if platforms:
        target_platforms = [p.strip() for p in platforms.split(",")]
        invalid = [p for p in target_platforms if p not in _VALID_PLATFORMS]
        if invalid:
            await ctx.send(f"Unknown platform(s): {', '.join(invalid)}. Valid: instagram, facebook")
            return
        if force_type == "reel" and "facebook" in target_platforms and "instagram" not in target_platforms:
            await ctx.send("Reels can only be published to Instagram, not Facebook alone.")
            return

    if _is_killed(bot_data, config):
        await ctx.send(f"[{config.account_id}] Bot is stopped. Use !killswitch to re-enable.")
        return

    pending = await get_pending_draft(db_path, config.account_id)
    if pending is not None:
        await ctx.send(
            "A draft is already pending. Use !approve, !skip, or wait for auto-publish."
        )
        return

    pipeline_key = f"pipeline_running_{config.account_id}"
    if bot_data.get(pipeline_key):
        await ctx.send("A pipeline run is already in progress.")
        return

    bot_data[pipeline_key] = True
    type_label = f" ({content_type})" if content_type else ""
    await ctx.send(f"[{config.account_id}] Starting pipeline run{type_label} (stock only)...")
    result: PipelineResult | None = None

    try:
        suggest_key = f"suggested_topic_{config.account_id}"
        user_hint = bot_data.pop(suggest_key, None)

        result = await run_pipeline(
            config=config,
            db_path=db_path,
            user_hint=user_hint,
            dry_run=dry_run,
            stock_only=True,
            force_content_type=force_type,
        )

        if result.error:
            await send_pipeline_error(bot, channel_id, result.error)
            return

        target_str = ",".join(target_platforms) if target_platforms else ""
        if result.success:
            await send_draft_for_review(bot, channel_id, result, bot_data, target_platforms=target_str)
        elif result.review and result.review.status == STATUS_FAIL:
            await send_draft_for_review(bot, channel_id, result, bot_data, target_platforms=target_str)
            await send_escalation(bot, channel_id, result)
        else:
            await ctx.send(
                "Pipeline completed but produced no publishable result."
            )

    except Exception as exc:
        logger.error("Pipeline run failed: %s", exc, exc_info=True)
        await send_pipeline_error(bot, channel_id, str(exc))

    finally:
        bot_data[pipeline_key] = False
        # Clean up pipeline media if send_draft_for_review didn't get to them
        if result is not None:
            for img in result.images:
                if img.local_path and os.path.exists(img.local_path):
                    os.remove(img.local_path)
            if result.video and result.video.local_path and os.path.exists(result.video.local_path):
                os.remove(result.video.local_path)


async def cmd_run(ctx: commands.Context, content_type: str = "", platforms: str = "") -> None:
    """Handle !run — trigger a pipeline run."""
    bot = ctx.bot
    bot_data = bot.bot_data
    channel_id = ctx.channel.id

    acct_ctx = _get_account_context(bot_data, channel_id)
    if acct_ctx is None:
        await ctx.send("No account is configured for this channel.")
        return

    config: AccountConfig = acct_ctx["config"]
    db_path: str = acct_ctx["db_path"]
    dry_run: bool = acct_ctx["dry_run"]

    # Validate content type
    force_type = None
    if content_type:
        if content_type not in _VALID_CONTENT_TYPES:
            await ctx.send(f"Unknown content type `{content_type}`. Valid: single, carousel, reel")
            return
        force_type = _TYPE_MAP[content_type]

    # Validate platforms
    target_platforms: list[str] | None = None
    if platforms:
        target_platforms = [p.strip() for p in platforms.split(",")]
        invalid = [p for p in target_platforms if p not in _VALID_PLATFORMS]
        if invalid:
            await ctx.send(f"Unknown platform(s): {', '.join(invalid)}. Valid: instagram, facebook")
            return
        if force_type == "reel" and "facebook" in target_platforms and "instagram" not in target_platforms:
            await ctx.send("Reels can only be published to Instagram, not Facebook alone.")
            return

    if _is_killed(bot_data, config):
        await ctx.send(f"[{config.account_id}] Bot is stopped. Use !killswitch to re-enable.")
        return

    pending = await get_pending_draft(db_path, config.account_id)
    if pending is not None:
        await ctx.send(
            "A draft is already pending. Use !approve, !skip, or wait for auto-publish."
        )
        return

    pipeline_key = f"pipeline_running_{config.account_id}"
    if bot_data.get(pipeline_key):
        await ctx.send("A pipeline run is already in progress.")
        return

    bot_data[pipeline_key] = True
    type_label = f" ({content_type})" if content_type else ""
    await ctx.send(f"[{config.account_id}] Starting pipeline run{type_label}...")
    result: PipelineResult | None = None

    try:
        suggest_key = f"suggested_topic_{config.account_id}"
        user_hint = bot_data.pop(suggest_key, None)

        result = await run_pipeline(
            config=config,
            db_path=db_path,
            user_hint=user_hint,
            dry_run=dry_run,
            force_content_type=force_type,
        )

        if result.error:
            await send_pipeline_error(bot, channel_id, result.error)
            return

        target_str = ",".join(target_platforms) if target_platforms else ""
        if result.success:
            await send_draft_for_review(bot, channel_id, result, bot_data, target_platforms=target_str)
        elif result.review and result.review.status == STATUS_FAIL:
            await send_draft_for_review(bot, channel_id, result, bot_data, target_platforms=target_str)
            await send_escalation(bot, channel_id, result)
        else:
            await ctx.send(
                "Pipeline completed but produced no publishable result."
            )

    except Exception as exc:
        logger.error("Pipeline run failed: %s", exc, exc_info=True)
        await send_pipeline_error(bot, channel_id, str(exc))

    finally:
        bot_data[pipeline_key] = False
        # Clean up pipeline media if send_draft_for_review didn't get to them
        if result is not None:
            for img in result.images:
                if img.local_path and os.path.exists(img.local_path):
                    os.remove(img.local_path)
            if result.video and result.video.local_path and os.path.exists(result.video.local_path):
                os.remove(result.video.local_path)


async def cmd_approve(ctx: commands.Context) -> None:
    """Handle !approve — publish the pending draft immediately."""
    bot = ctx.bot
    bot_data = bot.bot_data
    channel_id = ctx.channel.id

    acct_ctx = _get_account_context(bot_data, channel_id)
    if acct_ctx is None:
        await ctx.send("No account is configured for this channel.")
        return

    config: AccountConfig = acct_ctx["config"]
    db_path: str = acct_ctx["db_path"]

    if _is_killed(bot_data, config):
        await ctx.send(f"[{config.account_id}] Bot is stopped. Use !killswitch to re-enable.")
        return

    draft = await get_pending_draft(db_path, config.account_id)
    if draft is None:
        await ctx.send("No pending draft to approve.")
        return

    task_key = _auto_publish_task_key(config.account_id)
    task = bot_data.get(task_key)
    if task is not None and not task.done():
        task.cancel()
        bot_data.pop(task_key, None)

    await ctx.send("Publishing draft...")
    await _do_publish_draft(acct_ctx, bot_data, draft, channel_id, bot, auto=False)


async def cmd_approve_anyway(ctx: commands.Context) -> None:
    """Handle !approve_anyway — publish despite reviewer failure."""
    await cmd_approve(ctx)


async def cmd_skip(ctx: commands.Context) -> None:
    """Handle !skip — discard draft, trigger a new pipeline run."""
    bot_data = ctx.bot.bot_data
    channel_id = ctx.channel.id

    acct_ctx = _get_account_context(bot_data, channel_id)
    if acct_ctx is None:
        await ctx.send("No account is configured for this channel.")
        return

    config: AccountConfig = acct_ctx["config"]
    db_path: str = acct_ctx["db_path"]

    draft = await get_pending_draft(db_path, config.account_id)
    if draft is None:
        await ctx.send("No pending draft to skip.")
        return

    task_key = _auto_publish_task_key(config.account_id)
    task = bot_data.get(task_key)
    if task is not None and not task.done():
        task.cancel()
        bot_data.pop(task_key, None)

    await _update_draft_status(db_path, draft["id"], "skipped")

    # Clean up image(s)
    _cleanup_draft_images(draft)

    await ctx.send("Draft skipped. Use !run to generate a new one.")


async def cmd_skip_today(ctx: commands.Context) -> None:
    """Handle !skip_today — skip today's post entirely."""
    bot_data = ctx.bot.bot_data
    channel_id = ctx.channel.id

    acct_ctx = _get_account_context(bot_data, channel_id)
    if acct_ctx is None:
        await ctx.send("No account is configured for this channel.")
        return

    config: AccountConfig = acct_ctx["config"]
    db_path: str = acct_ctx["db_path"]

    draft = await get_pending_draft(db_path, config.account_id)
    if draft is not None:
        task_key = _auto_publish_task_key(config.account_id)
        task = bot_data.get(task_key)
        if task is not None and not task.done():
            task.cancel()
            bot_data.pop(task_key, None)

        await _update_draft_status(db_path, draft["id"], "skipped")
        _cleanup_draft_images(draft)

    await ctx.send("Today's post skipped. No regeneration.")


async def cmd_edit(ctx: commands.Context, *, new_caption: str) -> None:
    """Handle !edit <new caption> — replace caption on pending draft, then publish."""
    bot = ctx.bot
    bot_data = bot.bot_data
    channel_id = ctx.channel.id

    acct_ctx = _get_account_context(bot_data, channel_id)
    if acct_ctx is None:
        await ctx.send("No account is configured for this channel.")
        return

    config: AccountConfig = acct_ctx["config"]
    db_path: str = acct_ctx["db_path"]

    draft = await get_pending_draft(db_path, config.account_id)
    if draft is None:
        await ctx.send("No pending draft to edit.")
        return

    task_key = _auto_publish_task_key(config.account_id)
    task = bot_data.get(task_key)
    if task is not None and not task.done():
        task.cancel()
        bot_data.pop(task_key, None)

    await _update_draft_caption(db_path, draft["id"], new_caption)
    draft["caption"] = new_caption

    await ctx.send("Caption updated. Publishing...")
    await _do_publish_draft(acct_ctx, bot_data, draft, channel_id, bot, auto=False)


async def cmd_regenerate(ctx: commands.Context) -> None:
    """Handle !regenerate — discard draft and regenerate from scratch."""
    bot_data = ctx.bot.bot_data
    channel_id = ctx.channel.id

    acct_ctx = _get_account_context(bot_data, channel_id)
    if acct_ctx is None:
        await ctx.send("No account is configured for this channel.")
        return

    config: AccountConfig = acct_ctx["config"]
    db_path: str = acct_ctx["db_path"]

    draft = await get_pending_draft(db_path, config.account_id)
    if draft is not None:
        task_key = _auto_publish_task_key(config.account_id)
        task = bot_data.get(task_key)
        if task is not None and not task.done():
            task.cancel()
            bot_data.pop(task_key, None)

        await _update_draft_status(db_path, draft["id"], "skipped")
        _cleanup_draft_images(draft)

    await ctx.send("Draft discarded. Starting fresh pipeline run...")
    await cmd_run(ctx)


async def cmd_status(ctx: commands.Context) -> None:
    """Handle !status — show last run info and schedule."""
    bot_data = ctx.bot.bot_data
    channel_id = ctx.channel.id

    acct_ctx = _get_account_context(bot_data, channel_id)
    if acct_ctx is None:
        await ctx.send("No account is configured for this channel.")
        return

    config: AccountConfig = acct_ctx["config"]
    db_path: str = acct_ctx["db_path"]

    lines: list[str] = [f"Account: {config.account_id}", ""]

    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT topic, published_at, instagram_media_id FROM post_history "
            "WHERE account_id = ? ORDER BY published_at DESC LIMIT 1",
            (config.account_id,),
        )
        row = await cursor.fetchone()
        if row:
            lines.append(f"Last post: {row[0]}")
            lines.append(f"  Published: {row[1]}")
            lines.append(f"  Media ID: {row[2] or 'N/A (dry run)'}")
        else:
            lines.append("No posts published yet.")

    lines.append("")

    sched = await _get_schedule_config(db_path, config.account_id)
    if sched:
        lines.append(f"Frequency: {sched['frequency']}")
        lines.append(f"Preferred time: {sched['preferred_time']}")
        lines.append(f"Paused: {'Yes' if sched['paused'] else 'No'}")
        lines.append(f"Auto-publish: {'On' if sched.get('auto_publish') else 'Off'}")
        if sched.get("killed"):
            lines.append("Killswitch: ACTIVE (all activity stopped)")
    else:
        lines.append(f"Frequency: {config.post_frequency} (default)")
        lines.append(f"Preferred time: {config.preferred_time} (default)")
        lines.append("Paused: No")
        lines.append("Auto-publish: Off")

    scheduler = bot_data.get("scheduler")
    if scheduler is not None:
        job_id = pipeline_job_id(config.account_id)
        next_run = get_next_run_time(scheduler, job_id)
        if next_run:
            lines.append(f"Next run: {next_run}")
        else:
            lines.append("Next run: N/A (paused or no job)")
    else:
        lines.append("Next run: scheduler not active")

    lines.append("")
    lines.append(f"Platforms: {', '.join(config.platforms)}")

    lines.append("")

    draft = await get_pending_draft(db_path, config.account_id)
    if draft:
        ct = draft.get("content_type", "single_image")
        lines.append(f"Pending draft: #{draft['id']} ({ct}, publish_at: {draft['publish_at']})")
    else:
        lines.append("No pending draft.")

    pipeline_key = f"pipeline_running_{config.account_id}"
    if bot_data.get(pipeline_key):
        lines.append("Pipeline: RUNNING")

    await ctx.send(_truncate("\n".join(lines)))


async def cmd_suggest(ctx: commands.Context, *, topic: str) -> None:
    """Handle !suggest <topic> — queue a topic hint for the next run."""
    bot_data = ctx.bot.bot_data
    channel_id = ctx.channel.id

    acct_ctx = _get_account_context(bot_data, channel_id)
    if acct_ctx is None:
        await ctx.send("No account is configured for this channel.")
        return

    config: AccountConfig = acct_ctx["config"]
    suggest_key = f"suggested_topic_{config.account_id}"
    bot_data[suggest_key] = topic
    await ctx.send(f"[{config.account_id}] Topic suggestion queued: {topic}")


async def cmd_pause(ctx: commands.Context) -> None:
    """Handle !pause — pause the scheduler for this account."""
    bot_data = ctx.bot.bot_data
    channel_id = ctx.channel.id

    acct_ctx = _get_account_context(bot_data, channel_id)
    if acct_ctx is None:
        await ctx.send("No account is configured for this channel.")
        return

    config: AccountConfig = acct_ctx["config"]
    db_path: str = acct_ctx["db_path"]

    await _upsert_schedule_config(db_path, config.account_id, paused=1, timezone=config.timezone)

    scheduler = bot_data.get("scheduler")
    if scheduler is not None:
        job_id = pipeline_job_id(config.account_id)
        job = scheduler.get_job(job_id)
        if job is not None:
            job.pause()
            logger.info("Scheduler job '%s' paused via !pause command.", job_id)

    await ctx.send(f"[{config.account_id}] Scheduler paused.")


async def cmd_resume(ctx: commands.Context) -> None:
    """Handle !resume — resume the scheduler for this account."""
    bot_data = ctx.bot.bot_data
    channel_id = ctx.channel.id

    acct_ctx = _get_account_context(bot_data, channel_id)
    if acct_ctx is None:
        await ctx.send("No account is configured for this channel.")
        return

    config: AccountConfig = acct_ctx["config"]
    db_path: str = acct_ctx["db_path"]

    await _upsert_schedule_config(db_path, config.account_id, paused=0, timezone=config.timezone)

    scheduler = bot_data.get("scheduler")
    if scheduler is not None:
        job_id = pipeline_job_id(config.account_id)
        job = scheduler.get_job(job_id)
        if job is not None:
            job.resume()
            logger.info("Scheduler job '%s' resumed via !resume command.", job_id)

    await ctx.send(f"[{config.account_id}] Scheduler resumed.")


async def cmd_autopublish(ctx: commands.Context) -> None:
    """Handle !autopublish — toggle auto-publish for this account."""
    bot_data = ctx.bot.bot_data
    channel_id = ctx.channel.id

    acct_ctx = _get_account_context(bot_data, channel_id)
    if acct_ctx is None:
        await ctx.send("No account is configured for this channel.")
        return

    config: AccountConfig = acct_ctx["config"]
    db_path: str = acct_ctx["db_path"]

    sched = await _get_schedule_config(db_path, config.account_id)
    current = sched["auto_publish"] if sched else 0
    new_value = 0 if current else 1

    await _upsert_schedule_config(db_path, config.account_id, auto_publish=new_value, timezone=config.timezone)

    state = "enabled" if new_value else "disabled"
    await ctx.send(f"[{config.account_id}] Auto-publish {state}.")


async def _reschedule_from_db(
    bot_data: dict, db_path: str, config: AccountConfig
) -> None:
    """Re-read schedule_config from DB and reschedule the APScheduler job."""
    scheduler = bot_data.get("scheduler")
    run_func_key = f"scheduled_run_func_{config.account_id}"
    job_func = bot_data.get(run_func_key)
    if scheduler is None or job_func is None:
        logger.warning("Scheduler not wired up — cannot reschedule.")
        return

    sched = await _get_schedule_config(db_path, config.account_id)
    if sched is None:
        return

    schedule_pipeline_job(
        scheduler=scheduler,
        job_func=job_func,
        frequency=sched["frequency"],
        preferred_time=sched["preferred_time"],
        timezone_str=sched["timezone"],
        account_id=config.account_id,
    )
    logger.info("Rescheduled pipeline job for '%s' after !setfrequency change.", config.account_id)


async def cmd_setfrequency(ctx: commands.Context, value: str = "") -> None:
    """Handle !setfrequency <value> — change posting schedule."""
    bot_data = ctx.bot.bot_data
    channel_id = ctx.channel.id

    acct_ctx = _get_account_context(bot_data, channel_id)
    if acct_ctx is None:
        await ctx.send("No account is configured for this channel.")
        return

    config: AccountConfig = acct_ctx["config"]
    db_path: str = acct_ctx["db_path"]

    if not value:
        await ctx.send(
            "Usage: !setfrequency <value>\n"
            "Values: 1d, 2d, 3x, 2x, 1x, or HH:MM (time only)"
        )
        return

    # Check if it's a time (HH:MM)
    if ":" in value and len(value) <= 5:
        try:
            parts = value.split(":")
            hour = int(parts[0])
            minute = int(parts[1])
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError("Invalid time")
            formatted_time = f"{hour:02d}:{minute:02d}"
            await _upsert_schedule_config(
                db_path, config.account_id, preferred_time=formatted_time, timezone=config.timezone
            )
            await _reschedule_from_db(bot_data, db_path, config)
            await ctx.send(
                f"[{config.account_id}] Posting time changed to {formatted_time}."
            )
            return
        except (ValueError, IndexError):
            await ctx.send(
                "Invalid time format. Use HH:MM (e.g. 14:30)."
            )
            return

    if value not in _VALID_FREQUENCIES:
        await ctx.send(
            f"Invalid frequency '{value}'. "
            f"Valid: {', '.join(sorted(_VALID_FREQUENCIES))}, or HH:MM for time."
        )
        return

    await _upsert_schedule_config(db_path, config.account_id, frequency=value, timezone=config.timezone)
    await _reschedule_from_db(bot_data, db_path, config)
    await ctx.send(f"[{config.account_id}] Posting frequency changed to {value}.")


async def cmd_killswitch(ctx: commands.Context) -> None:
    """Handle !killswitch — toggle all bot activity on/off for this account."""
    bot = ctx.bot
    bot_data = bot.bot_data
    channel_id = ctx.channel.id

    acct_ctx = _get_account_context(bot_data, channel_id)
    if acct_ctx is None:
        await ctx.send("No account is configured for this channel.")
        return

    config: AccountConfig = acct_ctx["config"]
    db_path: str = acct_ctx["db_path"]
    key = _killswitch_key(config.account_id)

    if _is_killed(bot_data, config):
        # --- RE-ENABLE ---
        bot_data.pop(key, None)
        await _upsert_schedule_config(
            db_path, config.account_id, paused=0, killed=0, timezone=config.timezone
        )

        # Resume scheduler job
        scheduler = bot_data.get("scheduler")
        if scheduler is not None:
            job_id = pipeline_job_id(config.account_id)
            job = scheduler.get_job(job_id)
            if job is not None:
                job.resume()

        await ctx.send(f"[{config.account_id}] Bot re-enabled. Scheduler resumed.")
        logger.info("Killswitch OFF for '%s'.", config.account_id)
    else:
        # --- KILL ---
        bot_data[key] = True
        await _upsert_schedule_config(
            db_path, config.account_id, paused=1, killed=1, timezone=config.timezone
        )

        # Pause scheduler job
        scheduler = bot_data.get("scheduler")
        if scheduler is not None:
            job_id = pipeline_job_id(config.account_id)
            job = scheduler.get_job(job_id)
            if job is not None:
                job.pause()

        # Cancel auto-publish timer
        task_key = _auto_publish_task_key(config.account_id)
        task = bot_data.get(task_key)
        if task is not None and not task.done():
            task.cancel()
            bot_data.pop(task_key, None)

        # Skip any pending draft
        draft = await get_pending_draft(db_path, config.account_id)
        if draft is not None:
            await _update_draft_status(db_path, draft["id"], "skipped")
            _cleanup_draft_images(draft)
            logger.info("Pending draft %d skipped by killswitch.", draft["id"])

        await ctx.send(
            f"[{config.account_id}] Bot stopped. "
            "Scheduler paused, auto-publish cancelled, pending draft discarded. "
            "Use !killswitch to re-enable."
        )
        logger.info("Killswitch ON for '%s'.", config.account_id)


# ---------------------------------------------------------------------------
# Image cleanup helper
# ---------------------------------------------------------------------------

def _cleanup_draft_images(draft: dict) -> None:
    """Clean up image file(s) associated with a draft."""
    image_path = draft.get("image_path", "")
    content_type = draft.get("content_type", "single_image")

    if content_type == "carousel":
        try:
            paths = json.loads(image_path)
        except (json.JSONDecodeError, TypeError):
            paths = [image_path] if image_path else []
    else:
        paths = [image_path] if image_path else []

    for path in paths:
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Photo intake handler (on_message)
# ---------------------------------------------------------------------------

async def _handle_photo_message(bot: commands.Bot, message: discord.Message) -> None:
    """Handle user-sent photos — save and run pipeline with the photo."""
    bot_data = bot.bot_data
    channel_id = message.channel.id

    acct_ctx = _get_account_context(bot_data, channel_id)
    if acct_ctx is None:
        return  # Not a registered channel — ignore silently

    config: AccountConfig = acct_ctx["config"]
    db_path: str = acct_ctx["db_path"]
    dry_run: bool = acct_ctx["dry_run"]

    if _is_killed(bot_data, config):
        await message.channel.send(
            f"[{config.account_id}] Bot is stopped. Use !killswitch to re-enable."
        )
        return

    pending = await get_pending_draft(db_path, config.account_id)
    if pending is not None:
        await message.channel.send(
            "A draft is already pending. Use !approve, !skip, or wait for auto-publish."
        )
        return

    pipeline_key = f"pipeline_running_{config.account_id}"
    if bot_data.get(pipeline_key):
        await message.channel.send("A pipeline run is already in progress.")
        return

    # Find the first image attachment
    attachment = None
    for att in message.attachments:
        if att.content_type and att.content_type.startswith("image/"):
            attachment = att
            break

    if attachment is None:
        return  # No image attachment

    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    media_dir = os.path.join(base_dir, "storage", "media")
    os.makedirs(media_dir, exist_ok=True)
    user_photo_path = os.path.join(media_dir, f"user_dc_{attachment.id}.jpg")
    await attachment.save(user_photo_path)
    logger.info("User photo saved to %s", user_photo_path)

    # Extract hint from message content (if any)
    user_hint = message.content if message.content else None

    bot_data[pipeline_key] = True
    await message.channel.send(
        f"[{config.account_id}] Photo received. Running pipeline with your image..."
    )

    try:
        result: PipelineResult = await run_pipeline(
            config=config,
            db_path=db_path,
            user_photo_path=user_photo_path,
            user_hint=user_hint,
            dry_run=dry_run,
        )

        if result.error:
            await send_pipeline_error(bot, channel_id, result.error)
            return

        if result.success or (result.review and result.review.status == STATUS_FAIL):
            await send_draft_for_review(bot, channel_id, result, bot_data)
            if result.review and result.review.status == STATUS_FAIL:
                await send_escalation(bot, channel_id, result)
        else:
            await message.channel.send(
                "Pipeline completed but produced no publishable result."
            )

    except Exception as exc:
        logger.error("Photo pipeline failed: %s", exc, exc_info=True)
        await send_pipeline_error(bot, channel_id, str(exc))

    finally:
        bot_data[pipeline_key] = False
        if os.path.exists(user_photo_path):
            try:
                os.remove(user_photo_path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Startup: resume overdue drafts
# ---------------------------------------------------------------------------

async def _resume_overdue_drafts(bot: commands.Bot) -> None:
    """On startup, find overdue pending drafts and auto-publish them for all accounts."""
    await bot.wait_until_ready()

    bot_data = bot.bot_data
    accounts = bot_data.get("accounts", {})

    for channel_id, acct_ctx in accounts.items():
        config: AccountConfig = acct_ctx["config"]
        db_path: str = acct_ctx["db_path"]

        # Restore killswitch state from DB on startup
        sched = await _get_schedule_config(db_path, config.account_id)
        if sched and sched.get("killed"):
            bot_data[_killswitch_key(config.account_id)] = True
            logger.info("Killswitch restored for '%s' on startup.", config.account_id)

        if _is_killed(bot_data, config):
            logger.info("Killswitch active for '%s' — skipping draft resume.", config.account_id)
            continue

        draft = await get_pending_draft(db_path, config.account_id)
        if draft is None:
            logger.info("No pending drafts to resume for account '%s'.", config.account_id)
            continue

        publish_at = datetime.fromisoformat(draft["publish_at"])
        now = datetime.now(timezone.utc)

        channel = bot.get_channel(channel_id)
        if channel is None:
            logger.error("Channel %d not found — cannot resume draft for '%s'.", channel_id, config.account_id)
            continue
        auto_pub = sched["auto_publish"] if sched else 0

        if not auto_pub:
            logger.info(
                "Auto-publish is off for '%s' — draft %d still pending.",
                config.account_id,
                draft["id"],
            )
            await channel.send(
                content=f"[{config.account_id}] Draft #{draft['id']} is still pending. Use !approve, !skip, or !regenerate.",
            )
            continue

        if now >= publish_at:
            logger.info(
                "Overdue draft %d found for '%s' (publish_at=%s). Auto-publishing now.",
                draft["id"],
                config.account_id,
                draft["publish_at"],
            )
            await channel.send(
                content=f"[{config.account_id}] Resuming overdue draft #{draft['id']} — auto-publishing now.",
            )
            await _do_publish_draft(acct_ctx, bot_data, draft, channel_id, bot, auto=True)
        else:
            logger.info(
                "Pending draft %d found for '%s' (publish_at=%s). Restarting auto-publish timer.",
                draft["id"],
                config.account_id,
                draft["publish_at"],
            )
            _start_auto_publish_timer(acct_ctx, bot_data, draft, channel_id, bot)


# ---------------------------------------------------------------------------
# Bot factory
# ---------------------------------------------------------------------------

def build_bot(
    accounts: list[tuple[AccountConfig, str, bool]],
) -> commands.Bot:
    """Build and configure the Discord bot for one or more accounts.

    Args:
        accounts: List of (config, db_path, dry_run) tuples. All accounts
                  must share the same Discord bot token.

    Returns:
        Configured Bot instance ready for bot.start(token).
    """
    if not accounts:
        raise ValueError("At least one account must be provided.")

    first_config = accounts[0][0]
    token = os.getenv(first_config.discord_bot_token_env)
    if not token:
        raise ValueError(
            f"Discord bot token env var '{first_config.discord_bot_token_env}' is missing or empty."
        )

    # Configure intents — message_content is required for prefix commands and photo intake
    intents = discord.Intents.default()
    intents.message_content = True

    bot = commands.Bot(command_prefix="!", intents=intents)

    # Attach custom data dict (mirrors Telegram's bot_data pattern)
    bot.bot_data = {}  # type: ignore[attr-defined]

    # Build accounts dict keyed by channel_id for O(1) lookup
    accounts_by_channel_id: dict[int, dict] = {}
    for config, db_path, dry_run in accounts:
        channel_id_str = os.getenv(config.discord_channel_id_env, "0")
        try:
            channel_id = int(channel_id_str)
        except ValueError:
            logger.error(
                "Invalid channel ID '%s' for account '%s' — skipping.",
                channel_id_str,
                config.account_id,
            )
            continue

        if channel_id == 0:
            logger.warning(
                "Channel ID is 0 for account '%s' — env var '%s' may not be set.",
                config.account_id,
                config.discord_channel_id_env,
            )

        accounts_by_channel_id[channel_id] = {
            "config": config,
            "db_path": db_path,
            "dry_run": dry_run,
        }
        logger.info(
            "Registered account '%s' for channel_id=%d.",
            config.account_id,
            channel_id,
        )

    bot.bot_data["accounts"] = accounts_by_channel_id

    # Store token for use by main.py (bot.start needs it)
    bot.bot_data["token"] = token

    # Register command handlers
    bot.command(name="start")(cmd_start)
    bot.command(name="check")(cmd_check)
    bot.command(name="run")(cmd_run)
    bot.command(name="runstock")(cmd_runstock)
    bot.command(name="approve")(cmd_approve)
    bot.command(name="approve_anyway")(cmd_approve_anyway)
    bot.command(name="skip")(cmd_skip)
    bot.command(name="skip_today")(cmd_skip_today)
    bot.command(name="edit")(cmd_edit)
    bot.command(name="regenerate")(cmd_regenerate)
    bot.command(name="status")(cmd_status)
    bot.command(name="suggest")(cmd_suggest)
    bot.command(name="pause")(cmd_pause)
    bot.command(name="resume")(cmd_resume)
    bot.command(name="autopublish")(cmd_autopublish)
    bot.command(name="setfrequency")(cmd_setfrequency)
    bot.command(name="killswitch")(cmd_killswitch)

    # on_ready: resume overdue drafts
    @bot.event
    async def on_ready() -> None:
        logger.info("Discord bot connected as %s.", bot.user)
        await _resume_overdue_drafts(bot)

    # on_message: photo intake + ensure commands still work
    @bot.event
    async def on_message(message: discord.Message) -> None:
        # Ignore messages from the bot itself
        if message.author == bot.user:
            return

        # Check for image attachments (only if no command prefix)
        if message.attachments and not message.content.startswith("!"):
            has_image = any(
                att.content_type and att.content_type.startswith("image/")
                for att in message.attachments
            )
            if has_image:
                await _handle_photo_message(bot, message)
                return

        # Process commands (required when overriding on_message)
        await bot.process_commands(message)

    logger.info(
        "Discord bot built — %d account(s) registered.",
        len(accounts_by_channel_id),
    )
    return bot
