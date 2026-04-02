"""Tests for utils/stock_search.py — stock photo/video search and download."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from utils.stock_search import (
    _pick_best_video_file,
    download_image,
    download_video,
    search_pexels,
    search_pexels_videos,
    search_unsplash,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_httpx_response(json_data, status_code=200):
    """Create a mock httpx.Response."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=MagicMock(), response=resp
        )
    return resp


# ---------------------------------------------------------------------------
# search_unsplash
# ---------------------------------------------------------------------------

class TestSearchUnsplash:
    async def test_success(self, monkeypatch):
        monkeypatch.setenv("UNSPLASH_ACCESS_KEY", "test-key")
        json_data = {
            "results": [
                {
                    "urls": {"regular": "https://example.com/photo.jpg"},
                    "user": {"name": "Photographer"},
                    "description": "A salad",
                }
            ]
        }
        mock_resp = _mock_httpx_response(json_data)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("utils.stock_search.httpx.AsyncClient", return_value=mock_client):
            results = await search_unsplash(["salad"])

        assert len(results) == 1
        assert results[0].source == "unsplash"
        assert results[0].url == "https://example.com/photo.jpg"

    async def test_rate_limit_returns_empty(self, monkeypatch):
        monkeypatch.setenv("UNSPLASH_ACCESS_KEY", "test-key")
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 429
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("utils.stock_search.httpx.AsyncClient", return_value=mock_client):
            results = await search_unsplash(["salad"])

        assert results == []

    async def test_timeout_returns_empty(self, monkeypatch):
        monkeypatch.setenv("UNSPLASH_ACCESS_KEY", "test-key")
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("utils.stock_search.httpx.AsyncClient", return_value=mock_client):
            results = await search_unsplash(["salad"])

        assert results == []

    async def test_missing_api_key_raises(self, monkeypatch):
        monkeypatch.delenv("UNSPLASH_ACCESS_KEY", raising=False)
        with pytest.raises(ValueError, match="UNSPLASH_ACCESS_KEY"):
            await search_unsplash(["salad"])


# ---------------------------------------------------------------------------
# search_pexels
# ---------------------------------------------------------------------------

class TestSearchPexels:
    async def test_success(self, monkeypatch):
        monkeypatch.setenv("PEXELS_API_KEY", "test-key")
        json_data = {
            "photos": [
                {
                    "src": {"large": "https://example.com/pexels.jpg"},
                    "photographer": "Someone",
                    "alt": "Vegetables",
                }
            ]
        }
        mock_resp = _mock_httpx_response(json_data)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("utils.stock_search.httpx.AsyncClient", return_value=mock_client):
            results = await search_pexels(["vegetables"])

        assert len(results) == 1
        assert results[0].source == "pexels"

    async def test_missing_src_field_skipped(self, monkeypatch):
        monkeypatch.setenv("PEXELS_API_KEY", "test-key")
        json_data = {"photos": [{"src": {}, "photographer": "X", "alt": "Y"}]}
        mock_resp = _mock_httpx_response(json_data)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("utils.stock_search.httpx.AsyncClient", return_value=mock_client):
            results = await search_pexels(["vegetables"])

        assert results == []


# ---------------------------------------------------------------------------
# search_pexels_videos
# ---------------------------------------------------------------------------

class TestSearchPexelsVideos:
    async def test_duration_filtering(self, monkeypatch):
        monkeypatch.setenv("PEXELS_API_KEY", "test-key")
        json_data = {
            "videos": [
                {
                    "duration": 2,  # too short (< 3s default)
                    "video_files": [{"file_type": "video/mp4", "width": 1080, "height": 1920, "quality": "hd", "link": "https://example.com/short.mp4"}],
                    "width": 1080, "height": 1920,
                    "user": {"name": "X"},
                    "title": "Short",
                },
                {
                    "duration": 15,  # valid
                    "video_files": [{"file_type": "video/mp4", "width": 1080, "height": 1920, "quality": "hd", "link": "https://example.com/ok.mp4"}],
                    "width": 1080, "height": 1920,
                    "user": {"name": "Y"},
                    "title": "Good",
                },
            ]
        }
        mock_resp = _mock_httpx_response(json_data)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("utils.stock_search.httpx.AsyncClient", return_value=mock_client):
            results = await search_pexels_videos(["nature"])

        assert len(results) == 1
        assert results[0].duration == 15


# ---------------------------------------------------------------------------
# _pick_best_video_file
# ---------------------------------------------------------------------------

class TestPickBestVideoFile:
    def test_prefers_portrait_hd_mp4(self):
        files = [
            {"file_type": "video/mp4", "width": 1920, "height": 1080, "quality": "hd", "link": "https://landscape.mp4"},
            {"file_type": "video/mp4", "width": 1080, "height": 1920, "quality": "hd", "link": "https://portrait.mp4"},
        ]
        result = _pick_best_video_file(files)
        assert result == "https://portrait.mp4"

    def test_no_mp4_returns_none(self):
        files = [
            {"file_type": "video/webm", "width": 1080, "height": 1920, "quality": "hd", "link": "https://video.webm"},
        ]
        assert _pick_best_video_file(files) is None

    def test_empty_list(self):
        assert _pick_best_video_file([]) is None


# ---------------------------------------------------------------------------
# download_image
# ---------------------------------------------------------------------------

class TestDownloadImage:
    async def test_success(self, tmp_path, monkeypatch):
        dest = str(tmp_path / "downloaded.jpg")
        mock_resp = MagicMock()
        mock_resp.content = b"fake image bytes"
        mock_resp.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("utils.stock_search.httpx.AsyncClient", return_value=mock_client):
            result = await download_image("https://example.com/img.jpg", dest)

        assert result == dest
        assert os.path.isfile(dest)

    async def test_timeout_raises(self, tmp_path):
        dest = str(tmp_path / "downloaded.jpg")
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("utils.stock_search.httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(ConnectionError, match="Timeout"):
                await download_image("https://example.com/img.jpg", dest)


# ---------------------------------------------------------------------------
# download_video
# ---------------------------------------------------------------------------

class TestDownloadVideo:
    async def test_success(self, tmp_path):
        dest = str(tmp_path / "video.mp4")

        # Build an async streaming response mock
        async def fake_aiter_bytes(chunk_size=65536):
            yield b"chunk1"
            yield b"chunk2"

        mock_stream_resp = AsyncMock()
        mock_stream_resp.raise_for_status = MagicMock()
        mock_stream_resp.aiter_bytes = fake_aiter_bytes
        mock_stream_resp.__aenter__ = AsyncMock(return_value=mock_stream_resp)
        mock_stream_resp.__aexit__ = AsyncMock(return_value=False)

        mock_client = AsyncMock()
        mock_client.stream = MagicMock(return_value=mock_stream_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("utils.stock_search.httpx.AsyncClient", return_value=mock_client):
            result = await download_video("https://example.com/v.mp4", dest)

        assert result == dest
        assert os.path.isfile(dest)
        with open(dest, "rb") as f:
            assert f.read() == b"chunk1chunk2"

    async def test_timeout_raises(self, tmp_path):
        dest = str(tmp_path / "video.mp4")
        mock_client = AsyncMock()
        mock_client.stream = MagicMock(side_effect=httpx.TimeoutException("timeout"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("utils.stock_search.httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(ConnectionError, match="Timeout"):
                await download_video("https://example.com/v.mp4", dest)
