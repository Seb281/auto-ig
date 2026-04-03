"""Entry point for auto-ig — autonomous Instagram post creator.

Supports running one or more accounts simultaneously. Each account gets
its own scheduler job and pipeline lock; all accounts share a single
Discord bot (they must use the same bot token).
"""

import argparse
import asyncio
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from dotenv import load_dotenv

from agents.reviewer import STATUS_FAIL
from utils.config_loader import (
    AccountConfig,
    init_db,
    load_account_config,
    validate_env_vars,
)

VERSION = "1.2.0"

logger = logging.getLogger("auto-ig")


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="auto-ig — autonomous Instagram post creator"
    )
    parser.add_argument(
        "--account",
        dest="accounts",
        action="append",
        default=None,
        help=(
            "Account ID matching a directory under accounts/. "
            "Can be specified multiple times for multi-account mode "
            "(default: veggie_alternatives)."
        ),
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

    base_dir = os.path.dirname(os.path.abspath(__file__))

    # Resolve account list — default to veggie_alternatives if none specified
    account_ids: list[str] = args.accounts if args.accounts else ["veggie_alternatives"]
    logger.info("Accounts to load: %s", ", ".join(account_ids))

    # ------------------------------------------------------------------
    # Load and validate all account configs
    # ------------------------------------------------------------------
    loaded_accounts: list[tuple[AccountConfig, str]] = []  # (config, db_path)

    for account_id in account_ids:
        config_path = os.path.join(base_dir, "accounts", account_id, "config.yaml")

        try:
            config: AccountConfig = load_account_config(config_path)
        except FileNotFoundError:
            logger.error("Config file not found: %s — skipping account '%s'.", config_path, account_id)
            continue
        except ValueError as exc:
            logger.error("Invalid config for '%s': %s — skipping.", account_id, exc)
            continue

        # Validate environment variables
        try:
            validate_env_vars(config)
        except ValueError as exc:
            logger.error("Env var validation failed for '%s': %s — skipping.", account_id, exc)
            continue

        # Initialize database (use data/ dir for persistence, e.g. Railway volume)
        data_dir = os.path.join(base_dir, "data", account_id)
        db_path = os.path.join(data_dir, "post_history.db")
        try:
            await init_db(db_path)
        except Exception as exc:
            logger.error("DB init failed for '%s': %s — skipping.", account_id, exc)
            continue

        loaded_accounts.append((config, db_path))
        logger.info("Account '%s' loaded successfully.", account_id)

    if not loaded_accounts:
        logger.error("No valid accounts loaded. Exiting.")
        sys.exit(1)

    # Ensure storage/media/ directory exists
    media_dir = os.path.join(base_dir, "storage", "media")
    os.makedirs(media_dir, exist_ok=True)

    if args.dry_run:
        logger.info("Dry-run mode enabled — publishing will be skipped.")

    logger.info(
        "Loaded %d account(s): %s",
        len(loaded_accounts),
        ", ".join(c.account_id for c, _ in loaded_accounts),
    )

    # ------------------------------------------------------------------
    # Build Discord bot with all accounts
    # ------------------------------------------------------------------
    from control.discord_bot import (
        build_bot,
        get_pending_draft,
        send_draft_for_review,
        send_escalation,
        send_pipeline_error,
    )

    accounts_tuples = [
        (config, db_path, args.dry_run)
        for config, db_path in loaded_accounts
    ]

    bot = build_bot(accounts=accounts_tuples)
    bot_data = bot.bot_data

    # ------------------------------------------------------------------
    # Scheduler setup — one shared scheduler, per-account jobs
    # ------------------------------------------------------------------
    from publisher.scheduler import (
        create_scheduler,
        load_or_init_schedule,
        pipeline_job_id,
        schedule_pipeline_job,
    )
    from agents.orchestrator import run_pipeline
    from agents import PipelineResult

    scheduler = create_scheduler()

    # Per-account pipeline locks (prevent concurrent runs for the same account)
    pipeline_locks: dict[str, asyncio.Lock] = {}

    for config, db_path in loaded_accounts:
        sched_config = await load_or_init_schedule(db_path, config)
        pipeline_locks[config.account_id] = asyncio.Lock()

        channel_id = int(os.getenv(config.discord_channel_id_env, "0"))

        # Create a per-account scheduled_run closure
        # Use default args to capture current loop variables
        async def make_scheduled_run(
            _config: AccountConfig = config,
            _db_path: str = db_path,
            _channel_id: int = channel_id,
        ) -> None:
            """Run the pipeline on a schedule for a specific account."""
            acct_lock = pipeline_locks[_config.account_id]
            pipeline_key = f"pipeline_running_{_config.account_id}"

            # Guard: skip if a draft is already pending
            pending = await get_pending_draft(_db_path, _config.account_id)
            if pending is not None:
                logger.info(
                    "Scheduled run for '%s' skipped — a draft is already pending (id=%d).",
                    _config.account_id,
                    pending["id"],
                )
                return

            # Guard: atomically check-then-set pipeline_running
            async with acct_lock:
                if bot_data.get(pipeline_key):
                    logger.info(
                        "Scheduled run for '%s' skipped — pipeline already running.",
                        _config.account_id,
                    )
                    return
                bot_data[pipeline_key] = True

            logger.info("Scheduled pipeline run starting for '%s'...", _config.account_id)

            try:
                suggest_key = f"suggested_topic_{_config.account_id}"
                user_hint = bot_data.pop(suggest_key, None)

                result: PipelineResult = await run_pipeline(
                    config=_config,
                    db_path=_db_path,
                    user_hint=user_hint,
                    dry_run=args.dry_run,
                )

                if result.error:
                    await send_pipeline_error(bot, _channel_id, result.error)
                    return

                if result.success:
                    await send_draft_for_review(
                        bot, _channel_id, result, bot_data
                    )
                elif result.review and result.review.status == STATUS_FAIL:
                    await send_draft_for_review(
                        bot, _channel_id, result, bot_data
                    )
                    await send_escalation(bot, _channel_id, result)
                else:
                    await bot.wait_until_ready()
                    channel = bot.get_channel(_channel_id)
                    if channel is not None:
                        await channel.send(
                            content=f"[{_config.account_id}] Scheduled pipeline completed but produced no publishable result.",
                        )

            except Exception as exc:
                logger.error(
                    "Scheduled pipeline run failed for '%s': %s",
                    _config.account_id,
                    exc,
                    exc_info=True,
                )
                try:
                    await send_pipeline_error(bot, _channel_id, str(exc))
                except Exception:
                    logger.error("Failed to send error notification.", exc_info=True)

            finally:
                bot_data[pipeline_key] = False

        # Schedule the per-account job
        schedule_pipeline_job(
            scheduler=scheduler,
            job_func=make_scheduled_run,
            frequency=sched_config["frequency"],
            preferred_time=sched_config["preferred_time"],
            timezone_str=sched_config["timezone"],
            account_id=config.account_id,
        )

        # Store the run function in bot_data so Discord commands (setfrequency) can reschedule
        run_func_key = f"scheduled_run_func_{config.account_id}"
        bot_data[run_func_key] = make_scheduled_run

        # If schedule is paused in DB, pause the job immediately after scheduler starts
        if sched_config.get("paused"):
            bot_data[f"_paused_on_start_{config.account_id}"] = True

    # Store scheduler in bot_data so Discord commands can access it
    bot_data["scheduler"] = scheduler

    logger.info("Starting Discord bot...")

    # Start the scheduler (shares the asyncio event loop)
    scheduler.start()
    logger.info("APScheduler started.")

    # Pause any accounts that were marked as paused in the DB
    for config, db_path in loaded_accounts:
        paused_key = f"_paused_on_start_{config.account_id}"
        if bot_data.pop(paused_key, False):
            job_id = pipeline_job_id(config.account_id)
            job = scheduler.get_job(job_id)
            if job is not None:
                job.pause()
                logger.info(
                    "Scheduler job '%s' started in paused state (paused=1 in DB).",
                    job_id,
                )

    # Run the Discord bot as a background task
    token = bot_data.pop("token")
    bot_task = asyncio.create_task(bot.start(token))

    logger.info("Discord bot is running. Press Ctrl+C to stop.")

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

        # Shut down the Discord bot
        await bot.close()
        bot_task.cancel()
        try:
            await bot_task
        except asyncio.CancelledError:
            pass
        logger.info("Discord bot stopped.")


if __name__ == "__main__":
    asyncio.run(main())
