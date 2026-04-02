"""Tests for agents/video_sourcing.py — video sourcing with mocked dependencies."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.video_sourcing import source_video
from utils.stock_search import StockVideo
from utils.video_utils import VideoMetadata


def _mock_stock_videos(n=3):
    return [
        StockVideo(
            url=f"https://example.com/v{i}.mp4", source="pexels",
            photographer="Test", description="Desc",
            duration=15, width=1080, height=1920,
        )
        for i in range(n)
    ]


def _valid_metadata():
    return VideoMetadata(duration_seconds=15.0, width=1080, height=1920, codec="h264")


class TestSourceVideo:
    async def test_happy_path(self, make_account_config, make_planner_brief, tmp_db, tmp_path):
        config = make_account_config()
        brief = make_planner_brief(content_type="reel")
        media_dir = str(tmp_path / "media")

        async def mock_download(url, dest_path):
            with open(dest_path, "wb") as f:
                f.write(b"\x00" * 1000)
            return dest_path

        with patch("agents.video_sourcing.search_pexels_videos", new_callable=AsyncMock, return_value=_mock_stock_videos(1)):
            with patch("agents.video_sourcing.download_video", side_effect=mock_download):
                with patch("agents.video_sourcing.probe_video", new_callable=AsyncMock, return_value=_valid_metadata()):
                    with patch("agents.video_sourcing.validate_reel_specs", return_value=[]):
                        with patch("agents.video_sourcing._score_video_candidate", new_callable=AsyncMock, return_value=0.85):
                            with patch("agents.video_sourcing.compute_video_phash", new_callable=AsyncMock, return_value="abc123"):
                                with patch("agents.video_sourcing.is_duplicate_image", new_callable=AsyncMock, return_value=False):
                                    result = await source_video(config, brief, tmp_db, media_dir)

        assert result.source == "pexels"
        assert result.score == 0.85
        assert result.phash == "abc123"

    async def test_spec_violation_filtered(self, make_account_config, make_planner_brief, tmp_db, tmp_path):
        config = make_account_config()
        brief = make_planner_brief(content_type="reel")
        media_dir = str(tmp_path / "media")

        async def mock_download(url, dest_path):
            with open(dest_path, "wb") as f:
                f.write(b"\x00" * 1000)
            return dest_path

        # All candidates fail spec validation
        with patch("agents.video_sourcing.search_pexels_videos", new_callable=AsyncMock, return_value=_mock_stock_videos(2)):
            with patch("agents.video_sourcing.download_video", side_effect=mock_download):
                with patch("agents.video_sourcing.probe_video", new_callable=AsyncMock, return_value=_valid_metadata()):
                    with patch("agents.video_sourcing.validate_reel_specs", return_value=["Too long"]):
                        with pytest.raises(RuntimeError, match="No valid stock videos"):
                            await source_video(config, brief, tmp_db, media_dir)

    async def test_score_below_threshold_raises(self, make_account_config, make_planner_brief, tmp_db, tmp_path):
        config = make_account_config()
        brief = make_planner_brief(content_type="reel")
        media_dir = str(tmp_path / "media")

        async def mock_download(url, dest_path):
            with open(dest_path, "wb") as f:
                f.write(b"\x00" * 1000)
            return dest_path

        with patch("agents.video_sourcing.search_pexels_videos", new_callable=AsyncMock, return_value=_mock_stock_videos(1)):
            with patch("agents.video_sourcing.download_video", side_effect=mock_download):
                with patch("agents.video_sourcing.probe_video", new_callable=AsyncMock, return_value=_valid_metadata()):
                    with patch("agents.video_sourcing.validate_reel_specs", return_value=[]):
                        with patch("agents.video_sourcing._score_video_candidate", new_callable=AsyncMock, return_value=0.1):
                            with pytest.raises(RuntimeError, match="below threshold"):
                                await source_video(config, brief, tmp_db, media_dir)

    async def test_duplicate_raises(self, make_account_config, make_planner_brief, tmp_db, tmp_path):
        config = make_account_config()
        brief = make_planner_brief(content_type="reel")
        media_dir = str(tmp_path / "media")

        async def mock_download(url, dest_path):
            with open(dest_path, "wb") as f:
                f.write(b"\x00" * 1000)
            return dest_path

        with patch("agents.video_sourcing.search_pexels_videos", new_callable=AsyncMock, return_value=_mock_stock_videos(1)):
            with patch("agents.video_sourcing.download_video", side_effect=mock_download):
                with patch("agents.video_sourcing.probe_video", new_callable=AsyncMock, return_value=_valid_metadata()):
                    with patch("agents.video_sourcing.validate_reel_specs", return_value=[]):
                        with patch("agents.video_sourcing._score_video_candidate", new_callable=AsyncMock, return_value=0.85):
                            with patch("agents.video_sourcing.compute_video_phash", new_callable=AsyncMock, return_value="dup"):
                                with patch("agents.video_sourcing.is_duplicate_image", new_callable=AsyncMock, return_value=True):
                                    with pytest.raises(RuntimeError, match="duplicate"):
                                        await source_video(config, brief, tmp_db, media_dir)

    async def test_no_search_results_raises(self, make_account_config, make_planner_brief, tmp_db, tmp_path):
        config = make_account_config()
        brief = make_planner_brief(content_type="reel")
        media_dir = str(tmp_path / "media")

        with patch("agents.video_sourcing.search_pexels_videos", new_callable=AsyncMock, return_value=[]):
            with pytest.raises(RuntimeError, match="No stock videos found"):
                await source_video(config, brief, tmp_db, media_dir)
