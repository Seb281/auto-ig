"""Entry point for auto-ig — autonomous Instagram post creator."""

import argparse
import asyncio
import logging
import os
import sys

from dotenv import load_dotenv

from utils.config_loader import (
    AccountConfig,
    init_db,
    load_account_config,
    validate_env_vars,
)

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
    return parser.parse_args()


def setup_logging() -> None:
    """Configure structured logging for the application."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


async def main() -> None:
    """Initialize config, database, and start the application."""
    setup_logging()
    args = parse_args()

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
    from control.telegram_bot import build_application

    application = build_application(
        config=config,
        db_path=db_path,
        dry_run=args.dry_run,
    )

    logger.info("Starting Telegram bot (polling)...")

    # TODO: Milestone 7 — Wire up AsyncIOScheduler with frequency from schedule_config
    # The scheduler should be stored in application.bot_data["scheduler"] so the
    # /pause and /resume commands can control it.

    await application.initialize()
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
        logger.info("Shutting down Telegram bot...")
        await application.updater.stop()
        await application.stop()
        await application.shutdown()
        logger.info("Telegram bot stopped.")


if __name__ == "__main__":
    asyncio.run(main())
