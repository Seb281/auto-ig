"""APScheduler integration — frequency-aware pipeline scheduling.

Wraps AsyncIOScheduler to provide frequency-based automatic pipeline runs.
Schedule configuration is persisted in the schedule_config SQLite table and
can be changed at runtime via the Telegram /setfrequency command.
"""

import logging
from datetime import datetime, timedelta

import aiosqlite
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover — Python < 3.9
    from backports.zoneinfo import ZoneInfo  # type: ignore[no-redef]

from utils.config_loader import AccountConfig

logger = logging.getLogger(__name__)

# Canonical job ID for the pipeline run
PIPELINE_JOB_ID = "pipeline_run"


def create_scheduler() -> AsyncIOScheduler:
    """Create and return a new AsyncIOScheduler (not yet started)."""
    scheduler = AsyncIOScheduler()
    logger.info("AsyncIOScheduler created.")
    return scheduler


async def load_or_init_schedule(
    db_path: str, config: AccountConfig
) -> dict:
    """Load schedule_config from SQLite, inserting defaults if no row exists."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM schedule_config WHERE account_id = ?",
            (config.account_id,),
        )
        row = await cursor.fetchone()

        if row is not None:
            result = dict(row)
            logger.info(
                "Loaded schedule config for '%s': frequency=%s, time=%s, tz=%s, paused=%s",
                config.account_id,
                result["frequency"],
                result["preferred_time"],
                result["timezone"],
                result["paused"],
            )
            return result

        # No row — insert defaults from AccountConfig
        await db.execute(
            """
            INSERT INTO schedule_config
                (account_id, frequency, preferred_time, timezone, paused)
            VALUES (?, ?, ?, ?, 0)
            """,
            (
                config.account_id,
                config.post_frequency,
                config.preferred_time,
                config.timezone,
            ),
        )
        await db.commit()

        result = {
            "account_id": config.account_id,
            "frequency": config.post_frequency,
            "preferred_time": config.preferred_time,
            "timezone": config.timezone,
            "paused": 0,
        }
        logger.info(
            "Initialized schedule config for '%s' with defaults: frequency=%s, time=%s, tz=%s",
            config.account_id,
            result["frequency"],
            result["preferred_time"],
            result["timezone"],
        )
        return result


def _parse_time(preferred_time: str) -> tuple[int, int]:
    """Parse 'HH:MM' into (hour, minute)."""
    parts = preferred_time.split(":")
    return int(parts[0]), int(parts[1])


def _resolve_timezone(timezone_str: str) -> ZoneInfo:
    """Resolve a timezone string to a ZoneInfo, falling back to UTC."""
    try:
        return ZoneInfo(timezone_str)
    except KeyError as exc:
        logger.warning(
            "Invalid timezone '%s' (%s) — falling back to UTC.",
            timezone_str,
            exc,
        )
        return ZoneInfo("UTC")


def frequency_to_trigger(
    frequency: str, preferred_time: str, timezone_str: str
) -> CronTrigger | IntervalTrigger:
    """Convert a frequency string + time + timezone into an APScheduler trigger."""
    hour, minute = _parse_time(preferred_time)
    tz = _resolve_timezone(timezone_str)

    if frequency == "1d":
        return CronTrigger(hour=hour, minute=minute, timezone=tz)

    if frequency == "2d":
        # IntervalTrigger with 2-day interval, starting at the next preferred time
        now = datetime.now(tz)
        today_at_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if today_at_time <= now:
            start_date = today_at_time + timedelta(days=1)
        else:
            start_date = today_at_time
        return IntervalTrigger(days=2, start_date=start_date, timezone=tz)

    if frequency == "3x":
        return CronTrigger(
            day_of_week="mon,wed,fri", hour=hour, minute=minute, timezone=tz
        )

    if frequency == "2x":
        return CronTrigger(
            day_of_week="mon,thu", hour=hour, minute=minute, timezone=tz
        )

    if frequency == "1x":
        return CronTrigger(
            day_of_week="mon", hour=hour, minute=minute, timezone=tz
        )

    # Default to daily if unrecognized
    logger.warning(
        "Unrecognized frequency '%s' — defaulting to daily.", frequency
    )
    return CronTrigger(hour=hour, minute=minute, timezone=tz)


def schedule_pipeline_job(
    scheduler: AsyncIOScheduler,
    job_func,
    frequency: str,
    preferred_time: str,
    timezone_str: str,
    job_id: str = PIPELINE_JOB_ID,
) -> None:
    """Remove any existing pipeline job and add a new one with the given schedule."""
    trigger = frequency_to_trigger(frequency, preferred_time, timezone_str)

    # Remove existing job if present
    existing = scheduler.get_job(job_id)
    if existing is not None:
        scheduler.remove_job(job_id)
        logger.info("Removed existing scheduler job '%s'.", job_id)

    scheduler.add_job(
        job_func,
        trigger=trigger,
        id=job_id,
        name="auto-ig pipeline run",
        replace_existing=True,
        misfire_grace_time=3600,  # 1 hour grace for misfires
    )

    next_run = get_next_run_time(scheduler, job_id)
    logger.info(
        "Scheduled pipeline job '%s': frequency=%s, time=%s, tz=%s, next_run=%s",
        job_id,
        frequency,
        preferred_time,
        timezone_str,
        next_run or "N/A",
    )


def get_next_run_time(
    scheduler: AsyncIOScheduler, job_id: str = PIPELINE_JOB_ID
) -> str | None:
    """Return the next scheduled run time as ISO 8601, or None."""
    job = scheduler.get_job(job_id)
    if job is None:
        return None
    next_run = job.next_run_time
    if next_run is None:
        return None
    return next_run.isoformat()
