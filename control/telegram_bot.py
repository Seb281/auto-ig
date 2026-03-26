"""Telegram bot — commands, notifications, photo intake, and auto-publish timer.

Implements the full Telegram control interface for auto-ig using
python-telegram-bot v20+ (async/polling). All state is persisted in SQLite
via the pending_drafts table so the 2h auto-publish timer survives restarts.
"""

import asyncio
import json
import logging
import os
import shutil
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

import aiosqlite
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from agents import PipelineResult, PlannerBrief
from agents.orchestrator import run_pipeline
from publisher.scheduler import get_next_run_time, schedule_pipeline_job
from utils.config_loader import AccountConfig

logger = logging.getLogger(__name__)

# Valid frequency values
_VALID_FREQUENCIES = {"1d", "2d", "3x", "2x", "1x"}


def _read_file_bytes(path: str) -> bytes:
    """Read a file's contents as bytes (meant to be called via asyncio.to_thread)."""
    with open(path, "rb") as f:
        return f.read()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _format_draft_caption(caption: str, hashtags: list[str]) -> str:
    """Build the full caption text with hashtags appended."""
    tag_line = " ".join(f"#{h}" for h in hashtags)
    return f"{caption}\n\n{tag_line}" if tag_line else caption


def _format_draft_preview(caption: str, hashtags: list[str]) -> str:
    """Build a human-readable draft preview for Telegram."""
    full = _format_draft_caption(caption, hashtags)
    lines = [
        "--- DRAFT PREVIEW ---",
        "",
        full,
        "",
        "--- END PREVIEW ---",
        "",
        "Commands: /approve  /skip  /edit <new caption>  /regenerate",
    ]
    return "\n".join(lines)


async def _get_pending_draft(db_path: str, account_id: str) -> dict | None:
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
    caption: str,
    hashtags: list[str],
    alt_text: str,
    brief: PlannerBrief,
    timeout_hours: int,
) -> int:
    """Insert a new pending draft and return its row ID."""
    now = datetime.now(timezone.utc)
    publish_at = now + timedelta(hours=timeout_hours)

    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            """
            INSERT INTO pending_drafts
                (account_id, image_path, caption, hashtags, alt_text,
                 brief_json, created_at, publish_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')
            """,
            (
                account_id,
                image_path,
                caption,
                json.dumps(hashtags),
                alt_text,
                json.dumps(asdict(brief)),
                now.isoformat(),
                publish_at.isoformat(),
            ),
        )
        await db.commit()
        draft_id = cursor.lastrowid

    logger.info(
        "Pending draft saved — id=%d, publish_at=%s",
        draft_id,
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
            # Insert with defaults
            await db.execute(
                """
                INSERT INTO schedule_config
                    (account_id, frequency, preferred_time, timezone, paused)
                VALUES (?, ?, ?, 'America/New_York', ?)
                """,
                (
                    account_id,
                    frequency or "1d",
                    preferred_time or "08:00",
                    paused if paused is not None else 0,
                ),
            )
        else:
            # Update only provided fields
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

        await db.commit()


# ---------------------------------------------------------------------------
# Auto-publish timer
# ---------------------------------------------------------------------------

async def _auto_publish_draft(
    bot_data: dict,
    draft: dict,
    chat_id: int,
    application: Application,
) -> None:
    """Publish a draft after the auto-publish timeout elapses."""
    config: AccountConfig = bot_data["config"]
    db_path: str = bot_data["db_path"]
    dry_run: bool = bot_data["dry_run"]
    draft_id = draft["id"]

    # Calculate wait time from now until publish_at
    publish_at = datetime.fromisoformat(draft["publish_at"])
    now = datetime.now(timezone.utc)
    delay = (publish_at - now).total_seconds()

    if delay > 0:
        logger.info(
            "Auto-publish timer started for draft %d — %.0f seconds remaining.",
            draft_id,
            delay,
        )
        await asyncio.sleep(delay)

    # Re-check status — user may have already acted
    current = await _get_pending_draft(db_path, config.account_id)
    if current is None or current["id"] != draft_id or current["status"] != "pending":
        logger.info(
            "Draft %d is no longer pending — auto-publish cancelled.", draft_id
        )
        return

    # Publish
    logger.info("Auto-publish timeout reached for draft %d.", draft_id)
    await _do_publish_draft(bot_data, draft, chat_id, application, auto=True)


async def _do_publish_draft(
    bot_data: dict,
    draft: dict,
    chat_id: int,
    application: Application,
    auto: bool = False,
) -> None:
    """Execute the actual publish for a draft (shared by approve and auto-publish)."""
    config: AccountConfig = bot_data["config"]
    db_path: str = bot_data["db_path"]
    dry_run: bool = bot_data["dry_run"]
    draft_id = draft["id"]

    caption = draft["caption"]
    hashtags = json.loads(draft["hashtags"])
    alt_text = draft["alt_text"]
    image_path = draft["image_path"]
    brief_data = json.loads(draft["brief_json"])

    full_caption = _format_draft_caption(caption, hashtags)

    try:
        if dry_run:
            logger.info("[DRY RUN] Skipping publish for draft %d.", draft_id)
            media_id = None
            prefix = "[DRY RUN] "
        else:
            from publisher.instagram import publish_post

            media_id = await publish_post(
                config, image_path, full_caption, alt_text
            )
            prefix = ""

        # Save post record
        from publisher.instagram import save_post_record

        await save_post_record(
            db_path=db_path,
            account_id=config.account_id,
            topic=brief_data.get("topic", "Unknown"),
            content_pillar=brief_data.get("content_pillar", ""),
            image_phash=brief_data.get("image_phash", ""),
            caption=full_caption,
            instagram_media_id=media_id,
        )

        await _update_draft_status(db_path, draft_id, "published")

        source = "Auto-published" if auto else "Published"
        msg = f"{prefix}{source} successfully."
        if media_id:
            msg += f"\nMedia ID: {media_id}"
        await application.bot.send_message(chat_id=chat_id, text=msg)
        logger.info("%s draft %d.", source, draft_id)

    except Exception as exc:
        logger.error("Publish failed for draft %d: %s", draft_id, exc, exc_info=True)
        await application.bot.send_message(
            chat_id=chat_id,
            text=f"Publish failed: {exc}",
        )

    finally:
        # Clean up image file
        if image_path and os.path.exists(image_path):
            try:
                os.remove(image_path)
                logger.info("Cleaned up draft image: %s", image_path)
            except OSError as exc:
                logger.warning("Failed to clean up image %s: %s", image_path, exc)

    # Clear the auto-publish task reference
    bot_data.pop("auto_publish_task", None)


def _start_auto_publish_timer(
    bot_data: dict,
    draft: dict,
    chat_id: int,
    application: Application,
) -> None:
    """Schedule the auto-publish coroutine and store the task handle."""
    # Cancel any existing timer
    existing_task = bot_data.get("auto_publish_task")
    if existing_task is not None and not existing_task.done():
        existing_task.cancel()
        logger.info("Cancelled previous auto-publish timer.")

    task = asyncio.create_task(
        _auto_publish_draft(bot_data, draft, chat_id, application)
    )
    bot_data["auto_publish_task"] = task
    logger.info("Auto-publish timer scheduled for draft %d.", draft["id"])


# ---------------------------------------------------------------------------
# Notification helpers
# ---------------------------------------------------------------------------

async def send_draft_for_review(
    application: Application,
    chat_id: int,
    result: PipelineResult,
    bot_data: dict,
) -> None:
    """Send a draft (image + caption preview) to Telegram and start the auto-publish timer."""
    config: AccountConfig = bot_data["config"]
    db_path: str = bot_data["db_path"]

    if result.image is None or result.caption is None or result.brief is None:
        await application.bot.send_message(
            chat_id=chat_id,
            text="Pipeline produced an incomplete result — cannot send draft for review.",
        )
        return

    # Copy image to a draft-specific path so the orchestrator's cleanup doesn't affect us
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    media_dir = os.path.join(base_dir, "storage", "media")
    os.makedirs(media_dir, exist_ok=True)
    draft_image_path = os.path.join(
        media_dir, f"draft_{os.path.basename(result.image.local_path)}"
    )
    await asyncio.to_thread(shutil.copy2, result.image.local_path, draft_image_path)

    # Save to pending_drafts
    draft_id = await _save_pending_draft(
        db_path=db_path,
        account_id=config.account_id,
        image_path=draft_image_path,
        caption=result.caption.caption,
        hashtags=result.caption.hashtags,
        alt_text=result.caption.alt_text,
        brief=result.brief,
        timeout_hours=config.auto_publish_timeout_hours,
    )

    # Send image
    try:
        photo_bytes = await asyncio.to_thread(_read_file_bytes, draft_image_path)
        await application.bot.send_photo(
            chat_id=chat_id,
            photo=photo_bytes,
            caption=f"Draft #{draft_id} ready for review",
        )
    except Exception as exc:
        logger.error("Failed to send draft photo: %s", exc)
        await application.bot.send_message(
            chat_id=chat_id,
            text=f"Draft #{draft_id} ready (could not send image: {exc})",
        )

    # Send caption preview
    preview = _format_draft_preview(result.caption.caption, result.caption.hashtags)
    await application.bot.send_message(chat_id=chat_id, text=preview)

    # Load the draft back so we have the full row
    draft = await _get_pending_draft(db_path, config.account_id)
    if draft is not None:
        _start_auto_publish_timer(bot_data, draft, chat_id, application)


async def send_escalation(
    application: Application,
    chat_id: int,
    result: PipelineResult,
) -> None:
    """Send a reviewer-escalation message with failure reasons and options."""
    reasons = (
        "\n".join(f"  - {r}" for r in result.review.reasons)
        if result.review
        else "  - Unknown"
    )
    msg = (
        "Reviewer FAILED after max retries.\n\n"
        f"Reasons:\n{reasons}\n\n"
        "Commands: /approve_anyway  /regenerate  /skip_today"
    )
    await application.bot.send_message(chat_id=chat_id, text=msg)


async def send_pipeline_error(
    application: Application,
    chat_id: int,
    error: str,
) -> None:
    """Send a pipeline failure alert."""
    await application.bot.send_message(
        chat_id=chat_id,
        text=f"Pipeline error: {error}",
    )


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start — welcome message."""
    await update.message.reply_text(
        "auto-ig bot is running.\n\n"
        "Commands:\n"
        "/run — trigger a pipeline run\n"
        "/status — show last run and schedule\n"
        "/approve — publish the pending draft\n"
        "/skip — discard draft, generate a new one\n"
        "/edit <caption> — replace caption and publish\n"
        "/regenerate — discard and regenerate from scratch\n"
        "/approve_anyway — publish despite reviewer failure\n"
        "/skip_today — skip today's post entirely\n"
        "/suggest <topic> — queue a topic for the next run\n"
        "/pause — pause the scheduler\n"
        "/resume — resume the scheduler\n"
        "/setfrequency <value> — change posting schedule\n"
    )


async def cmd_run(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /run — trigger a pipeline run."""
    bot_data = context.application.bot_data
    config: AccountConfig = bot_data["config"]
    db_path: str = bot_data["db_path"]
    dry_run: bool = bot_data["dry_run"]
    chat_id = int(os.getenv(config.telegram_chat_id_env, "0"))

    # Check for existing pending draft
    pending = await _get_pending_draft(db_path, config.account_id)
    if pending is not None:
        await update.message.reply_text(
            "A draft is already pending. Use /approve, /skip, or wait for auto-publish."
        )
        return

    # Check if a pipeline is already running
    if bot_data.get("pipeline_running"):
        await update.message.reply_text("A pipeline run is already in progress.")
        return

    bot_data["pipeline_running"] = True
    await update.message.reply_text("Starting pipeline run...")

    try:
        user_hint = bot_data.pop("suggested_topic", None)

        result: PipelineResult = await run_pipeline(
            config=config,
            db_path=db_path,
            user_hint=user_hint,
            dry_run=dry_run,
        )

        if result.error:
            await send_pipeline_error(context.application, chat_id, result.error)
            return

        if result.success:
            # Review passed — send draft for human review before publishing
            await send_draft_for_review(
                context.application, chat_id, result, bot_data
            )
        elif result.review and result.review.status == "FAIL":
            # Reviewer failed after retries — escalate
            # Still send the draft so user can approve_anyway
            await send_draft_for_review(
                context.application, chat_id, result, bot_data
            )
            await send_escalation(context.application, chat_id, result)
        else:
            await update.message.reply_text(
                "Pipeline completed but produced no publishable result."
            )

    except Exception as exc:
        logger.error("Pipeline run failed: %s", exc, exc_info=True)
        await send_pipeline_error(
            context.application, chat_id, str(exc)
        )

    finally:
        bot_data["pipeline_running"] = False


async def cmd_approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /approve — publish the pending draft immediately."""
    bot_data = context.application.bot_data
    config: AccountConfig = bot_data["config"]
    db_path: str = bot_data["db_path"]
    chat_id = int(os.getenv(config.telegram_chat_id_env, "0"))

    draft = await _get_pending_draft(db_path, config.account_id)
    if draft is None:
        await update.message.reply_text("No pending draft to approve.")
        return

    # Cancel auto-publish timer
    task = bot_data.get("auto_publish_task")
    if task is not None and not task.done():
        task.cancel()
        bot_data.pop("auto_publish_task", None)

    await update.message.reply_text("Publishing draft...")
    await _do_publish_draft(bot_data, draft, chat_id, context.application, auto=False)


async def cmd_approve_anyway(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /approve_anyway — publish despite reviewer failure."""
    # Same as /approve — the draft is already in pending_drafts regardless of review status
    await cmd_approve(update, context)


async def cmd_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /skip — discard draft, trigger a new pipeline run."""
    bot_data = context.application.bot_data
    config: AccountConfig = bot_data["config"]
    db_path: str = bot_data["db_path"]

    draft = await _get_pending_draft(db_path, config.account_id)
    if draft is None:
        await update.message.reply_text("No pending draft to skip.")
        return

    # Cancel auto-publish timer
    task = bot_data.get("auto_publish_task")
    if task is not None and not task.done():
        task.cancel()
        bot_data.pop("auto_publish_task", None)

    await _update_draft_status(db_path, draft["id"], "skipped")

    # Clean up image file
    image_path = draft.get("image_path", "")
    if image_path and os.path.exists(image_path):
        try:
            os.remove(image_path)
        except OSError:
            pass

    await update.message.reply_text(
        "Draft skipped. Use /run to generate a new one."
    )


async def cmd_skip_today(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /skip_today — skip today's post entirely."""
    bot_data = context.application.bot_data
    config: AccountConfig = bot_data["config"]
    db_path: str = bot_data["db_path"]

    draft = await _get_pending_draft(db_path, config.account_id)
    if draft is not None:
        # Cancel auto-publish timer
        task = bot_data.get("auto_publish_task")
        if task is not None and not task.done():
            task.cancel()
            bot_data.pop("auto_publish_task", None)

        await _update_draft_status(db_path, draft["id"], "skipped")

        # Clean up image file
        image_path = draft.get("image_path", "")
        if image_path and os.path.exists(image_path):
            try:
                os.remove(image_path)
            except OSError:
                pass

    await update.message.reply_text("Today's post skipped. No regeneration.")


async def cmd_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /edit <new caption> — replace caption on pending draft, then publish."""
    bot_data = context.application.bot_data
    config: AccountConfig = bot_data["config"]
    db_path: str = bot_data["db_path"]
    chat_id = int(os.getenv(config.telegram_chat_id_env, "0"))

    if not context.args:
        await update.message.reply_text("Usage: /edit <new caption text>")
        return

    new_caption = " ".join(context.args)

    draft = await _get_pending_draft(db_path, config.account_id)
    if draft is None:
        await update.message.reply_text("No pending draft to edit.")
        return

    # Cancel auto-publish timer
    task = bot_data.get("auto_publish_task")
    if task is not None and not task.done():
        task.cancel()
        bot_data.pop("auto_publish_task", None)

    await _update_draft_caption(db_path, draft["id"], new_caption)
    # Refresh draft with new caption
    draft["caption"] = new_caption

    await update.message.reply_text("Caption updated. Publishing...")
    await _do_publish_draft(bot_data, draft, chat_id, context.application, auto=False)


async def cmd_regenerate(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /regenerate — discard draft and regenerate from scratch."""
    bot_data = context.application.bot_data
    config: AccountConfig = bot_data["config"]
    db_path: str = bot_data["db_path"]

    draft = await _get_pending_draft(db_path, config.account_id)
    if draft is not None:
        # Cancel auto-publish timer
        task = bot_data.get("auto_publish_task")
        if task is not None and not task.done():
            task.cancel()
            bot_data.pop("auto_publish_task", None)

        await _update_draft_status(db_path, draft["id"], "skipped")

        # Clean up image file
        image_path = draft.get("image_path", "")
        if image_path and os.path.exists(image_path):
            try:
                os.remove(image_path)
            except OSError:
                pass

    await update.message.reply_text("Draft discarded. Starting fresh pipeline run...")

    # Trigger a new /run
    await cmd_run(update, context)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /status — show last run info and schedule."""
    bot_data = context.application.bot_data
    config: AccountConfig = bot_data["config"]
    db_path: str = bot_data["db_path"]

    lines: list[str] = []

    # Last published post
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

    # Schedule config
    sched = await _get_schedule_config(db_path, config.account_id)
    if sched:
        lines.append(f"Frequency: {sched['frequency']}")
        lines.append(f"Preferred time: {sched['preferred_time']}")
        lines.append(f"Paused: {'Yes' if sched['paused'] else 'No'}")
    else:
        lines.append(f"Frequency: {config.post_frequency} (default)")
        lines.append(f"Preferred time: {config.preferred_time} (default)")
        lines.append("Paused: No")

    # Next scheduled run
    scheduler = bot_data.get("scheduler")
    if scheduler is not None:
        next_run = get_next_run_time(scheduler)
        if next_run:
            lines.append(f"Next run: {next_run}")
        else:
            lines.append("Next run: N/A (paused or no job)")
    else:
        lines.append("Next run: scheduler not active")

    lines.append("")

    # Pending draft
    draft = await _get_pending_draft(db_path, config.account_id)
    if draft:
        lines.append(f"Pending draft: #{draft['id']} (publish_at: {draft['publish_at']})")
    else:
        lines.append("No pending draft.")

    # Pipeline running
    if bot_data.get("pipeline_running"):
        lines.append("Pipeline: RUNNING")

    await update.message.reply_text("\n".join(lines))


async def cmd_suggest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /suggest <topic> — queue a topic hint for the next run."""
    if not context.args:
        await update.message.reply_text("Usage: /suggest <topic or hint>")
        return

    topic = " ".join(context.args)
    context.application.bot_data["suggested_topic"] = topic
    await update.message.reply_text(f"Topic suggestion queued: {topic}")


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /pause — pause the scheduler."""
    bot_data = context.application.bot_data
    config: AccountConfig = bot_data["config"]
    db_path: str = bot_data["db_path"]

    await _upsert_schedule_config(db_path, config.account_id, paused=1)

    # Pause the APScheduler if it is wired up
    scheduler = bot_data.get("scheduler")
    if scheduler is not None:
        scheduler.pause()
        logger.info("Scheduler paused via /pause command.")

    await update.message.reply_text("Scheduler paused.")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /resume — resume the scheduler."""
    bot_data = context.application.bot_data
    config: AccountConfig = bot_data["config"]
    db_path: str = bot_data["db_path"]

    await _upsert_schedule_config(db_path, config.account_id, paused=0)

    # Resume the APScheduler if it is wired up
    scheduler = bot_data.get("scheduler")
    if scheduler is not None:
        scheduler.resume()
        logger.info("Scheduler resumed via /resume command.")

    await update.message.reply_text("Scheduler resumed.")



async def _reschedule_from_db(
    bot_data: dict, db_path: str, config: AccountConfig
) -> None:
    """Re-read schedule_config from DB and reschedule the APScheduler job."""
    scheduler = bot_data.get('scheduler')
    job_func = bot_data.get('scheduled_run_func')
    if scheduler is None or job_func is None:
        logger.warning('Scheduler not wired up — cannot reschedule.')
        return

    sched = await _get_schedule_config(db_path, config.account_id)
    if sched is None:
        return

    schedule_pipeline_job(
        scheduler=scheduler,
        job_func=job_func,
        frequency=sched['frequency'],
        preferred_time=sched['preferred_time'],
        timezone_str=sched['timezone'],
    )
    logger.info('Rescheduled pipeline job after /setfrequency change.')


async def cmd_setfrequency(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /setfrequency <value> — change posting schedule."""
    bot_data = context.application.bot_data
    config: AccountConfig = bot_data["config"]
    db_path: str = bot_data["db_path"]

    if not context.args:
        await update.message.reply_text(
            "Usage: /setfrequency <value>\n"
            "Values: 1d, 2d, 3x, 2x, 1x, or HH:MM (time only)"
        )
        return

    value = context.args[0].strip()

    # Check if it's a time (HH:MM)
    if ":" in value and len(value) <= 5:
        try:
            # Validate HH:MM format
            parts = value.split(":")
            hour = int(parts[0])
            minute = int(parts[1])
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError("Invalid time")
            formatted_time = f"{hour:02d}:{minute:02d}"
            await _upsert_schedule_config(
                db_path, config.account_id, preferred_time=formatted_time
            )
            # Reschedule the APScheduler job with the new time
            await _reschedule_from_db(bot_data, db_path, config)
            await update.message.reply_text(
                f"Posting time changed to {formatted_time}."
            )
            return
        except (ValueError, IndexError):
            await update.message.reply_text(
                "Invalid time format. Use HH:MM (e.g. 14:30)."
            )
            return

    if value not in _VALID_FREQUENCIES:
        await update.message.reply_text(
            f"Invalid frequency '{value}'. "
            f"Valid: {', '.join(sorted(_VALID_FREQUENCIES))}, or HH:MM for time."
        )
        return

    await _upsert_schedule_config(db_path, config.account_id, frequency=value)
    # Reschedule the APScheduler job with the new frequency
    await _reschedule_from_db(bot_data, db_path, config)
    await update.message.reply_text(f"Posting frequency changed to {value}.")


# ---------------------------------------------------------------------------
# Photo intake handler
# ---------------------------------------------------------------------------

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle user-sent photos — save and run pipeline with the photo."""
    bot_data = context.application.bot_data
    config: AccountConfig = bot_data["config"]
    db_path: str = bot_data["db_path"]
    dry_run: bool = bot_data["dry_run"]
    chat_id = int(os.getenv(config.telegram_chat_id_env, "0"))

    # Check for existing pending draft
    pending = await _get_pending_draft(db_path, config.account_id)
    if pending is not None:
        await update.message.reply_text(
            "A draft is already pending. Use /approve, /skip, or wait for auto-publish."
        )
        return

    if bot_data.get("pipeline_running"):
        await update.message.reply_text("A pipeline run is already in progress.")
        return

    # Get the highest resolution photo
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)

    # Save to storage/media/
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    media_dir = os.path.join(base_dir, "storage", "media")
    os.makedirs(media_dir, exist_ok=True)
    user_photo_path = os.path.join(media_dir, f"user_tg_{photo.file_unique_id}.jpg")
    await file.download_to_drive(user_photo_path)
    logger.info("User photo saved to %s", user_photo_path)

    # Extract hint from caption (if any)
    user_hint = update.message.caption

    bot_data["pipeline_running"] = True
    await update.message.reply_text(
        "Photo received. Running pipeline with your image..."
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
            await send_pipeline_error(context.application, chat_id, result.error)
            return

        if result.success or (result.review and result.review.status == "FAIL"):
            await send_draft_for_review(
                context.application, chat_id, result, bot_data
            )
            if result.review and result.review.status == "FAIL":
                await send_escalation(context.application, chat_id, result)
        else:
            await update.message.reply_text(
                "Pipeline completed but produced no publishable result."
            )

    except Exception as exc:
        logger.error("Photo pipeline failed: %s", exc, exc_info=True)
        await send_pipeline_error(context.application, chat_id, str(exc))

    finally:
        bot_data["pipeline_running"] = False
        # Clean up user photo (pipeline makes its own copy)
        if os.path.exists(user_photo_path):
            try:
                os.remove(user_photo_path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Startup: resume overdue drafts
# ---------------------------------------------------------------------------

async def _resume_overdue_drafts(application: Application) -> None:
    """On startup, find overdue pending drafts and auto-publish them."""
    bot_data = application.bot_data
    config: AccountConfig = bot_data["config"]
    db_path: str = bot_data["db_path"]
    chat_id = int(os.getenv(config.telegram_chat_id_env, "0"))

    draft = await _get_pending_draft(db_path, config.account_id)
    if draft is None:
        logger.info("No pending drafts to resume on startup.")
        return

    publish_at = datetime.fromisoformat(draft["publish_at"])
    now = datetime.now(timezone.utc)

    if now >= publish_at:
        logger.info(
            "Overdue draft %d found (publish_at=%s). Auto-publishing now.",
            draft["id"],
            draft["publish_at"],
        )
        await application.bot.send_message(
            chat_id=chat_id,
            text=f"Resuming overdue draft #{draft['id']} — auto-publishing now.",
        )
        await _do_publish_draft(bot_data, draft, chat_id, application, auto=True)
    else:
        logger.info(
            "Pending draft %d found (publish_at=%s). Restarting auto-publish timer.",
            draft["id"],
            draft["publish_at"],
        )
        _start_auto_publish_timer(bot_data, draft, chat_id, application)


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------

def build_application(
    config: AccountConfig,
    db_path: str,
    dry_run: bool = False,
) -> Application:
    """Build and configure the Telegram bot Application."""
    token = os.getenv(config.telegram_bot_token_env)
    if not token:
        raise ValueError(
            f"Telegram bot token env var '{config.telegram_bot_token_env}' is missing or empty."
        )

    application = (
        Application.builder()
        .token(token)
        .post_init(_resume_overdue_drafts)
        .build()
    )

    # Store shared state in bot_data (populated before initialize() is called,
    # so post_init callback can access it)
    application.bot_data["config"] = config
    application.bot_data["db_path"] = db_path
    application.bot_data["dry_run"] = dry_run
    application.bot_data["pipeline_running"] = False

    # Register command handlers
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("run", cmd_run))
    application.add_handler(CommandHandler("approve", cmd_approve))
    application.add_handler(CommandHandler("approve_anyway", cmd_approve_anyway))
    application.add_handler(CommandHandler("skip", cmd_skip))
    application.add_handler(CommandHandler("skip_today", cmd_skip_today))
    application.add_handler(CommandHandler("edit", cmd_edit))
    application.add_handler(CommandHandler("regenerate", cmd_regenerate))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("suggest", cmd_suggest))
    application.add_handler(CommandHandler("pause", cmd_pause))
    application.add_handler(CommandHandler("resume", cmd_resume))
    application.add_handler(CommandHandler("setfrequency", cmd_setfrequency))

    # Photo handler (lowest priority — after commands)
    application.add_handler(
        MessageHandler(filters.PHOTO, handle_photo)
    )

    logger.info("Telegram bot application built and configured.")
    return application
