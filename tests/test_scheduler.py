"""Tests for publisher/scheduler.py — APScheduler integration."""

import aiosqlite
import pytest

from publisher.scheduler import (
    create_scheduler,
    get_next_run_time,
    load_or_init_schedule,
    schedule_pipeline_job,
)


class TestCreateScheduler:
    def test_returns_scheduler(self):
        scheduler = create_scheduler()
        assert scheduler is not None
        assert hasattr(scheduler, "add_job")


class TestSchedulePipelineJob:
    async def test_adds_job(self):
        scheduler = create_scheduler()
        scheduler.start()
        try:
            async def dummy():
                pass

            schedule_pipeline_job(
                scheduler, dummy, "1d", "08:00", "UTC", account_id="test"
            )
            job = scheduler.get_job("pipeline_run_test")
            assert job is not None
        finally:
            scheduler.shutdown(wait=False)

    async def test_replaces_existing_job(self):
        scheduler = create_scheduler()
        scheduler.start()
        try:
            async def dummy():
                pass

            schedule_pipeline_job(scheduler, dummy, "1d", "08:00", "UTC", account_id="test")
            schedule_pipeline_job(scheduler, dummy, "2d", "10:00", "UTC", account_id="test")
            # Should still have exactly one job with that ID
            job = scheduler.get_job("pipeline_run_test")
            assert job is not None
        finally:
            scheduler.shutdown(wait=False)


class TestGetNextRunTime:
    async def test_returns_iso_string(self):
        scheduler = create_scheduler()
        scheduler.start()
        try:
            async def dummy():
                pass

            schedule_pipeline_job(scheduler, dummy, "1d", "08:00", "UTC", account_id="test")
            nrt = get_next_run_time(scheduler, "pipeline_run_test")
            assert nrt is not None
            assert "T" in nrt  # ISO 8601 format
        finally:
            scheduler.shutdown(wait=False)

    def test_missing_job_returns_none(self):
        scheduler = create_scheduler()
        assert get_next_run_time(scheduler, "nonexistent") is None


class TestLoadOrInitSchedule:
    async def test_inserts_defaults(self, make_account_config, tmp_db):
        config = make_account_config()
        result = await load_or_init_schedule(tmp_db, config)
        assert result["account_id"] == "test_account"
        assert result["frequency"] == "1d"
        assert result["paused"] == 0

    async def test_loads_existing(self, make_account_config, tmp_db):
        config = make_account_config()
        # First call inserts
        await load_or_init_schedule(tmp_db, config)

        # Modify the DB directly
        async with aiosqlite.connect(tmp_db) as db:
            await db.execute(
                "UPDATE schedule_config SET frequency = '2d' WHERE account_id = ?",
                (config.account_id,),
            )
            await db.commit()

        # Second call should load the modified value
        result = await load_or_init_schedule(tmp_db, config)
        assert result["frequency"] == "2d"
