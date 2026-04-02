"""Tests for publisher/instagram.py — Meta Graph API publishing with mocked HTTP."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import httpx
import pytest

from publisher.instagram import (
    _poll_container_status,
    publish_post,
    publish_reel,
    save_post_record,
)


def _mock_json_response(data, status_code=200):
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = data
    resp.raise_for_status = MagicMock()
    resp.text = json.dumps(data)
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=MagicMock(), response=resp
        )
    return resp


# ---------------------------------------------------------------------------
# _poll_container_status
# ---------------------------------------------------------------------------

class TestPollContainerStatus:
    async def test_finished_returns(self):
        client = AsyncMock()
        client.get = AsyncMock(return_value=_mock_json_response({"status_code": "FINISHED"}))

        await _poll_container_status(client, "container_123", "token")
        client.get.assert_called_once()

    async def test_error_raises(self):
        client = AsyncMock()
        client.get = AsyncMock(return_value=_mock_json_response({"status_code": "ERROR"}))

        with pytest.raises(RuntimeError, match="ERROR state"):
            await _poll_container_status(client, "container_123", "token")

    async def test_timeout_raises(self):
        client = AsyncMock()
        client.get = AsyncMock(return_value=_mock_json_response({"status_code": "IN_PROGRESS"}))

        with pytest.raises(TimeoutError, match="did not reach FINISHED"):
            await _poll_container_status(client, "container_123", "token", max_seconds=3)


# ---------------------------------------------------------------------------
# publish_post
# ---------------------------------------------------------------------------

class TestPublishPost:
    async def test_happy_path(self, make_account_config, tmp_path, monkeypatch):
        monkeypatch.setenv("TEST_IG_TOKEN", "fake-token")
        monkeypatch.setenv("PUBLIC_IP", "127.0.0.1")
        config = make_account_config()

        # Create a real image file for TempImageServer
        img_path = str(tmp_path / "test.jpg")
        with open(img_path, "wb") as f:
            f.write(b"\xff\xd8\xff" + b"\x00" * 100)

        create_resp = _mock_json_response({"id": "container_1"})
        poll_resp = _mock_json_response({"status_code": "FINISHED"})
        publish_resp = _mock_json_response({"id": "media_42"})

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=[create_resp, publish_resp])
        mock_client.get = AsyncMock(return_value=poll_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("publisher.instagram.httpx.AsyncClient", return_value=mock_client):
            media_id = await publish_post(config, img_path, "Caption", "Alt")

        assert media_id == "media_42"

    async def test_missing_token_raises(self, make_account_config, tmp_path, monkeypatch):
        monkeypatch.delenv("TEST_IG_TOKEN", raising=False)
        config = make_account_config()
        with pytest.raises(ValueError, match="missing or empty"):
            await publish_post(config, "/fake.jpg", "Caption", "Alt")


# ---------------------------------------------------------------------------
# publish_reel
# ---------------------------------------------------------------------------

class TestPublishReel:
    async def test_happy_path(self, make_account_config, tmp_path, monkeypatch):
        monkeypatch.setenv("TEST_IG_TOKEN", "fake-token")
        monkeypatch.setenv("PUBLIC_IP", "127.0.0.1")
        config = make_account_config()

        video_path = str(tmp_path / "test.mp4")
        with open(video_path, "wb") as f:
            f.write(b"\x00" * 100)

        create_resp = _mock_json_response({"id": "reel_container_1"})
        poll_resp = _mock_json_response({"status_code": "FINISHED"})
        publish_resp = _mock_json_response({"id": "reel_42"})

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=[create_resp, publish_resp])
        mock_client.get = AsyncMock(return_value=poll_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("publisher.instagram.httpx.AsyncClient", return_value=mock_client):
            media_id = await publish_reel(config, video_path, "Reel caption")

        assert media_id == "reel_42"


# ---------------------------------------------------------------------------
# save_post_record
# ---------------------------------------------------------------------------

class TestSavePostRecord:
    async def test_inserts_row(self, tmp_db):
        await save_post_record(
            db_path=tmp_db,
            account_id="test_account",
            topic="Test topic",
            content_pillar="recipes",
            image_phash="abc123",
            caption="A test caption for the post that is long enough to test snippet truncation.",
            instagram_media_id="media_42",
        )

        async with aiosqlite.connect(tmp_db) as db:
            cursor = await db.execute(
                "SELECT account_id, topic, instagram_media_id FROM post_history"
            )
            rows = await cursor.fetchall()

        assert len(rows) == 1
        assert rows[0][0] == "test_account"
        assert rows[0][1] == "Test topic"
        assert rows[0][2] == "media_42"
