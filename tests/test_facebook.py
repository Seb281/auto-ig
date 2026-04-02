"""Tests for publisher/facebook.py — Facebook Pages publishing with mocked HTTP."""

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from PIL import Image

from publisher.facebook import (
    _get_facebook_token,
    publish_carousel_to_facebook,
    publish_photo_to_facebook,
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


class TestGetFacebookToken:
    def test_missing_config_raises(self, make_account_config):
        config = make_account_config(facebook_page_token_env="")
        with pytest.raises(ValueError, match="not configured"):
            _get_facebook_token(config)

    def test_missing_env_raises(self, make_account_config, monkeypatch):
        config = make_account_config(facebook_page_token_env="FB_TOKEN")
        monkeypatch.delenv("FB_TOKEN", raising=False)
        with pytest.raises(ValueError, match="missing or empty"):
            _get_facebook_token(config)

    def test_success(self, make_account_config, monkeypatch):
        config = make_account_config(facebook_page_token_env="FB_TOKEN")
        monkeypatch.setenv("FB_TOKEN", "the-token")
        assert _get_facebook_token(config) == "the-token"


class TestPublishPhotoToFacebook:
    async def test_success(self, make_account_config, tmp_path, monkeypatch):
        monkeypatch.setenv("FB_TOKEN", "fake-token")
        monkeypatch.setenv("PUBLIC_IP", "127.0.0.1")
        config = make_account_config(
            facebook_page_id="pg_123",
            facebook_page_token_env="FB_TOKEN",
        )

        img_path = str(tmp_path / "test.jpg")
        Image.new("RGB", (200, 200), "red").save(img_path, "JPEG")

        post_resp = _mock_json_response({"post_id": "fb_post_42"})

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=post_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("publisher.facebook.httpx.AsyncClient", return_value=mock_client):
            post_id = await publish_photo_to_facebook(config, img_path, "Test caption")

        assert post_id == "fb_post_42"

    async def test_missing_token_raises(self, make_account_config, tmp_path, monkeypatch):
        monkeypatch.delenv("FB_TOKEN", raising=False)
        config = make_account_config(
            facebook_page_id="pg_123",
            facebook_page_token_env="FB_TOKEN",
        )
        img_path = str(tmp_path / "test.jpg")
        Image.new("RGB", (200, 200), "red").save(img_path, "JPEG")

        with pytest.raises(ValueError):
            await publish_photo_to_facebook(config, img_path, "Caption")


class TestPublishCarouselToFacebook:
    async def test_too_few_images_raises(self, make_account_config, tmp_path, monkeypatch):
        monkeypatch.setenv("FB_TOKEN", "fake-token")
        config = make_account_config(
            facebook_page_id="pg_123",
            facebook_page_token_env="FB_TOKEN",
        )
        img_path = str(tmp_path / "single.jpg")
        Image.new("RGB", (200, 200), "red").save(img_path, "JPEG")

        with pytest.raises(ValueError, match="at least 2"):
            await publish_carousel_to_facebook(config, [img_path], "Caption")

    async def test_success(self, make_account_config, tmp_path, monkeypatch):
        monkeypatch.setenv("FB_TOKEN", "fake-token")
        monkeypatch.setenv("PUBLIC_IP", "127.0.0.1")
        config = make_account_config(
            facebook_page_id="pg_123",
            facebook_page_token_env="FB_TOKEN",
        )

        img_paths = []
        for i in range(3):
            p = str(tmp_path / f"img{i}.jpg")
            Image.new("RGB", (200, 200), "red").save(p, "JPEG")
            img_paths.append(p)

        # First N calls are unpublished photo uploads, last call is feed post
        upload_resps = [_mock_json_response({"id": f"photo_{i}"}) for i in range(3)]
        feed_resp = _mock_json_response({"id": "fb_carousel_42"})

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=upload_resps + [feed_resp])
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("publisher.facebook.httpx.AsyncClient", return_value=mock_client):
            post_id = await publish_carousel_to_facebook(config, img_paths, "Carousel caption")

        assert post_id == "fb_carousel_42"
