"""Entry point for auto-ig — autonomous Instagram post creator."""

import argparse
import asyncio
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from dotenv import load_dotenv

from utils.config_loader import (
    AccountConfig,
    init_db,
    load_account_config,
    validate_env_vars,
)

VERSION = "1.0.0"

logger = logging.getLogger("auto-ig")


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="auto-ig — autonomous Instagram post creator"
    )
    parser.add_argument(
        "--account",
        default="veggie_alternatives",
        help="Account ID matching a directory under accounts/ (default: veggie_alternatives)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the full pipeline but skip actual Instagram publishing",
    )
    parser.add_argument(
        "--log-file",
        default=None,
        help="Path to a log file. Enables RotatingFileHandler alongside stdout logging.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"auto-ig {VERSION}",
    )
    return parser.parse_args()


def setup_logging(log_file: str | None = None) -> None:
    """Configure structured logging for the application."""
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Always log to stdout (captured by systemd journal)
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    root_logger.addHandler(stream_handler)

    # Optionally log to a rotating file
    if log_file is not None:
        log_dir = os.path.dirname(log_file)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)

        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=10 * 1024 * 1024,  # 10 MB
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)


async def main() -> None:
    """Initialize config, database, scheduler, and start the application."""
    args = parse_args()
    setup_logging(log_file=args.log_file)

    logger.info("auto-ig v%s starting...", VERSION)

    load_dotenv()

    # Load account config
    base_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(base_dir, "accounts", args.account, "config.yaml")

    try:
        config: AccountConfig = load_account_config(config_path)
    except FileNotFoundError:
        logger.error("Config file not found: %s", config_path)
        sys.exit(1)
    except ValueError as exc:
        logger.error("Invalid config: %s", exc)
        sys.exit(1)

    # Validate environment variables
    try:
        validate_env_vars(config)
    except ValueError as exc:
        logger.error("%s", exc)
        sys.exit(1)

    # Initialize database
    db_path = os.path.join(base_dir, "accounts", args.account, "post_history.db")
    try:
        await init_db(db_path)
    except Exception as exc:
        logger.error("Database initialization failed: %s", exc)
        sys.exit(1)

    # Ensure storage/media/ directory exists
    media_dir = os.path.join(base_dir, "storage", "media")
    os.makedirs(media_dir, exist_ok=True)

    if args.dry_run:
        logger.info("Dry-run mode enabled — publishing will be skipped.")

    logger.info("auto-ig started for account '%s'", config.account_id)

    # Build and start the Telegram bot
    from control.telegram_bot import (
        build_application,
        send_draft_for_review,
        send_escalation,
        send_pipeline_error,
    )

    application = build_application(
        config=config,
        db_path=db_path,
        dry_run=args.dry_run,
    )

    # --- Scheduler setup ---
    from publisher.scheduler import (
        create_scheduler,
        load_or_init_schedule,
        schedule_pipeline_job,
    )
    from agents.orchestrator import run_pipeline
    from agents import PipelineResult
    from control.telegram_bot import _get_pending_draft

    sched_config = await load_or_init_schedule(db_path, config)
    scheduler = create_scheduler()

    # Lock to protect the pipeline_running check-then-set across coroutines
    pipeline_lock = asyncio.Lock()

    # Define the scheduled pipeline run function
    async def scheduled_run() -> None:
        """Run the pipeline on a schedule — called by APScheduler."""
        bot_data = application.bot_data
        chat_id = int(os.getenv(config.telegram_chat_id_env, "0"))

        # Guard: skip if a draft is already pending
        pending = await _get_pending_draft(db_path, config.account_id)
        if pending is not None:
            logger.info(
                "Scheduled run skipped — a draft is already pending (id=%d).",
                pending["id"],
            )
            return

        # Guard: atomically check-then-set pipeline_running
        async with pipeline_lock:
            if bot_data.get("pipeline_running"):
                logger.info("Scheduled run skipped — pipeline already running.")
                return
            bot_data["pipeline_running"] = True

        logger.info("Scheduled pipeline run starting...")

        try:
            user_hint = bot_data.pop("suggested_topic", None)

            result: PipelineResult = await run_pipeline(
                config=config,
                db_path=db_path,
                user_hint=user_hint,
                dry_run=args.dry_run,
            )

            if result.error:
                await send_pipeline_error(application, chat_id, result.error)
                return

            if result.success:
                await send_draft_for_review(
                    application, chat_id, result, bot_data
                )
            elif result.review and result.review.status == "FAIL":
                await send_draft_for_review(
                    application, chat_id, result, bot_data
                )
                await send_escalation(application, chat_id, result)
            else:
                await application.bot.send_message(
                    chat_id=chat_id,
                    text="Scheduled pipeline completed but produced no publishable result.",
                )

        except Exception as exc:
            logger.error("Scheduled pipeline run failed: %s", exc, exc_info=True)
            try:
                await send_pipeline_error(application, chat_id, str(exc))
            except Exception:
                logger.error("Failed to send error notification.", exc_info=True)

        finally:
            bot_data["pipeline_running"] = False

    # Schedule the pipeline job
    schedule_pipeline_job(
        scheduler=scheduler,
        job_func=scheduled_run,
        frequency=sched_config["frequency"],
        preferred_time=sched_config["preferred_time"],
        timezone_str=sched_config["timezone"],
    )

    # Store scheduler and the run function in bot_data so Telegram commands can access them
    application.bot_data["scheduler"] = scheduler
    application.bot_data["scheduled_run_func"] = scheduled_run

    logger.info("Starting Telegram bot (polling)...")

    await application.initialize()

    # Start the scheduler (shares the asyncio event loop)
    scheduler.start()
    logger.info("APScheduler started.")

    # If schedule is paused in DB, pause the scheduler immediately
    if sched_config.get("paused"):
        scheduler.pause()
        logger.info("Scheduler started in paused state (paused=1 in DB).")

    await application.start()
    await application.updater.start_polling(drop_pending_updates=True)

    logger.info("Telegram bot is running. Press Ctrl+C to stop.")

    # Keep the bot running until interrupted
    stop_event = asyncio.Event()

    def _signal_handler() -> None:
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig_name in ("SIGINT", "SIGTERM"):
        try:
            loop.add_signal_handler(
                getattr(__import__("signal"), sig_name), _signal_handler
            )
        except (NotImplementedError, AttributeError):
            # Signal handling not available on some platforms (e.g. Windows)
            pass

    try:
        await stop_event.wait()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        logger.info("Shutting down...")

        # Shut down the scheduler
        scheduler.shutdown(wait=False)
        logger.info("APScheduler shut down.")

        # Shut down the Telegram bot
        await application.updater.stop()
        await application.stop()
        await application.shutdown()
        logger.info("Telegram bot stopped.")


if __name__ == "__main__":
    asyncio.run(main())
