"""Tests for utils/video_utils.py — async parts (ffprobe/ffmpeg mocking)."""

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from utils.video_utils import compute_video_phash, extract_thumbnail, probe_video


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_subprocess(stdout_data: bytes, returncode: int = 0, stderr_data: bytes = b""):
    """Create a mock for asyncio.create_subprocess_exec."""
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(stdout_data, stderr_data))
    mock_proc.returncode = returncode
    return mock_proc


# ---------------------------------------------------------------------------
# probe_video
# ---------------------------------------------------------------------------

class TestProbeVideo:
    async def test_valid_video(self):
        ffprobe_output = json.dumps({
            "streams": [
                {"codec_type": "video", "width": 1080, "height": 1920, "codec_name": "h264", "duration": "15.5"}
            ],
            "format": {"duration": "15.5"},
        }).encode()

        mock_proc = _mock_subprocess(ffprobe_output)
        with patch("utils.video_utils.asyncio.create_subprocess_exec", return_value=mock_proc):
            meta = await probe_video("/tmp/test.mp4")

        assert meta.width == 1080
        assert meta.height == 1920
        assert meta.duration_seconds == 15.5
        assert meta.codec == "h264"

    async def test_no_video_stream_raises(self):
        ffprobe_output = json.dumps({
            "streams": [{"codec_type": "audio"}],
            "format": {"duration": "15.5"},
        }).encode()

        mock_proc = _mock_subprocess(ffprobe_output)
        with patch("utils.video_utils.asyncio.create_subprocess_exec", return_value=mock_proc):
            with pytest.raises(RuntimeError, match="No video stream"):
                await probe_video("/tmp/test.mp4")

    async def test_ffprobe_failure_raises(self):
        mock_proc = _mock_subprocess(b"", returncode=1, stderr_data=b"ffprobe error")
        with patch("utils.video_utils.asyncio.create_subprocess_exec", return_value=mock_proc):
            with pytest.raises(RuntimeError, match="ffprobe failed"):
                await probe_video("/tmp/test.mp4")


# ---------------------------------------------------------------------------
# extract_thumbnail
# ---------------------------------------------------------------------------

class TestExtractThumbnail:
    async def test_success(self, tmp_path):
        out_path = str(tmp_path / "thumb.jpg")
        # Create the file to simulate ffmpeg output
        with open(out_path, "wb") as f:
            f.write(b"\xff\xd8\xff" + b"\x00" * 100)

        mock_proc = _mock_subprocess(b"", returncode=0)
        with patch("utils.video_utils.asyncio.create_subprocess_exec", return_value=mock_proc):
            # Mock probe_video since extract_thumbnail calls it when timestamp is None
            with patch("utils.video_utils.probe_video") as mock_probe:
                mock_probe.return_value = MagicMock(duration_seconds=10.0)
                result = await extract_thumbnail("/tmp/video.mp4", out_path)

        assert result == out_path

    async def test_failure_raises(self, tmp_path):
        out_path = str(tmp_path / "thumb.jpg")
        mock_proc = _mock_subprocess(b"", returncode=1, stderr_data=b"ffmpeg error")
        with patch("utils.video_utils.asyncio.create_subprocess_exec", return_value=mock_proc):
            with pytest.raises(RuntimeError, match="ffmpeg thumbnail extraction failed"):
                await extract_thumbnail("/tmp/video.mp4", out_path, timestamp=5.0)

    async def test_explicit_timestamp_skips_probe(self, tmp_path):
        out_path = str(tmp_path / "thumb.jpg")
        with open(out_path, "wb") as f:
            f.write(b"\xff\xd8\xff" + b"\x00" * 100)

        mock_proc = _mock_subprocess(b"", returncode=0)
        with patch("utils.video_utils.asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            result = await extract_thumbnail("/tmp/video.mp4", out_path, timestamp=3.0)

        assert result == out_path
        # Verify ffmpeg was called with the explicit timestamp
        call_args = mock_exec.call_args[0]
        assert "3.0" in [str(a) for a in call_args]


# ---------------------------------------------------------------------------
# compute_video_phash
# ---------------------------------------------------------------------------

class TestComputeVideoPhash:
    async def test_extracts_and_hashes(self, tmp_path):
        temp_dir = str(tmp_path)
        with patch("utils.video_utils.extract_thumbnail") as mock_extract:
            mock_extract.return_value = "/tmp/thumb.jpg"
            with patch("utils.video_utils.compute_phash", return_value="abc123"):
                with patch("os.path.exists", return_value=True):
                    with patch("os.remove"):
                        result = await compute_video_phash("/tmp/video.mp4", temp_dir)

        assert result == "abc123"
