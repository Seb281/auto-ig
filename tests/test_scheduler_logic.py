"""Tests for publisher/scheduler.py — pure logic (no scheduler instance needed)."""

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from publisher.scheduler import _parse_time, frequency_to_trigger, pipeline_job_id


class TestParseTime:
    def test_morning(self):
        assert _parse_time("08:00") == (8, 0)

    def test_late_night(self):
        assert _parse_time("23:59") == (23, 59)

    def test_midnight(self):
        assert _parse_time("00:00") == (0, 0)


class TestFrequencyToTrigger:
    def test_daily(self):
        trigger = frequency_to_trigger("1d", "08:00", "UTC")
        assert isinstance(trigger, CronTrigger)

    def test_every_two_days(self):
        trigger = frequency_to_trigger("2d", "08:00", "UTC")
        assert isinstance(trigger, IntervalTrigger)

    def test_three_times_weekly(self):
        trigger = frequency_to_trigger("3x", "10:00", "UTC")
        assert isinstance(trigger, CronTrigger)

    def test_two_times_weekly(self):
        trigger = frequency_to_trigger("2x", "10:00", "UTC")
        assert isinstance(trigger, CronTrigger)

    def test_once_weekly(self):
        trigger = frequency_to_trigger("1x", "10:00", "UTC")
        assert isinstance(trigger, CronTrigger)

    def test_unknown_defaults_to_daily(self):
        trigger = frequency_to_trigger("unknown", "08:00", "UTC")
        assert isinstance(trigger, CronTrigger)

    def test_invalid_timezone_falls_back_to_utc(self):
        trigger = frequency_to_trigger("1d", "08:00", "Invalid/Zone")
        assert isinstance(trigger, CronTrigger)


class TestPipelineJobId:
    def test_format(self):
        result = pipeline_job_id("veggie_alternatives")
        assert result == "pipeline_run_veggie_alternatives"

    def test_different_accounts(self):
        assert pipeline_job_id("a") != pipeline_job_id("b")
