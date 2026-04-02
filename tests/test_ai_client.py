"""Tests for utils/ai_client.py — AI client wrapper and image file reading."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from utils.ai_client import (
    IMAGE_GEN_MODEL,
    TEXT_MODEL,
    VISION_MODEL,
    generate_image,
    generate_text,
    generate_vision,
    read_image_file,
)


# ---------------------------------------------------------------------------
# read_image_file
# ---------------------------------------------------------------------------

class TestReadImageFile:
    def test_jpeg_magic_bytes(self, tmp_path):
        path = str(tmp_path / "test.jpg")
        with open(path, "wb") as f:
            f.write(b"\xff\xd8\xff" + b"\x00" * 100)
        data, mime = read_image_file(path)
        assert mime == "image/jpeg"
        assert len(data) == 103

    def test_png_magic_bytes(self, tmp_path):
        path = str(tmp_path / "test.png")
        with open(path, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        data, mime = read_image_file(path)
        assert mime == "image/png"

    def test_gif_magic_bytes(self, tmp_path):
        path = str(tmp_path / "test.gif")
        with open(path, "wb") as f:
            f.write(b"GIF8" + b"\x00" * 100)
        data, mime = read_image_file(path)
        assert mime == "image/gif"

    def test_webp_magic_bytes(self, tmp_path):
        path = str(tmp_path / "test.webp")
        with open(path, "wb") as f:
            f.write(b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 100)
        data, mime = read_image_file(path)
        assert mime == "image/webp"

    def test_unknown_defaults_to_jpeg(self, tmp_path):
        path = str(tmp_path / "test.bin")
        with open(path, "wb") as f:
            f.write(b"\x00\x01\x02\x03" + b"\x00" * 100)
        data, mime = read_image_file(path)
        assert mime == "image/jpeg"


# ---------------------------------------------------------------------------
# generate_text
# ---------------------------------------------------------------------------

class TestGenerateText:
    async def test_calls_correct_model(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")

        mock_response = MagicMock()
        mock_response.text = "AI response text"

        mock_generate = AsyncMock(return_value=mock_response)
        mock_models = MagicMock()
        mock_models.generate_content = mock_generate

        mock_aio = MagicMock()
        mock_aio.models = mock_models

        mock_client = MagicMock()
        mock_client.aio = mock_aio

        with patch("utils.ai_client._get_client", return_value=mock_client):
            result = await generate_text("test prompt")

        assert result == "AI response text"
        mock_generate.assert_called_once()
        call_kwargs = mock_generate.call_args
        assert call_kwargs.kwargs["model"] == TEXT_MODEL


# ---------------------------------------------------------------------------
# generate_vision
# ---------------------------------------------------------------------------

class TestGenerateVision:
    async def test_calls_vision_model(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")

        mock_response = MagicMock()
        mock_response.text = "Vision response"

        mock_generate = AsyncMock(return_value=mock_response)
        mock_models = MagicMock()
        mock_models.generate_content = mock_generate

        mock_aio = MagicMock()
        mock_aio.models = mock_models

        mock_client = MagicMock()
        mock_client.aio = mock_aio

        with patch("utils.ai_client._get_client", return_value=mock_client):
            result = await generate_vision(b"image_data", "image/jpeg", "describe")

        assert result == "Vision response"
        call_kwargs = mock_generate.call_args
        assert call_kwargs.kwargs["model"] == VISION_MODEL


# ---------------------------------------------------------------------------
# generate_image
# ---------------------------------------------------------------------------

class TestGenerateImage:
    async def test_returns_image_bytes(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")

        mock_inline_data = MagicMock()
        mock_inline_data.data = b"fake_image_bytes"

        mock_part = MagicMock()
        mock_part.inline_data = mock_inline_data

        mock_content = MagicMock()
        mock_content.parts = [mock_part]

        mock_candidate = MagicMock()
        mock_candidate.content = mock_content

        mock_response = MagicMock()
        mock_response.candidates = [mock_candidate]

        mock_generate = AsyncMock(return_value=mock_response)
        mock_models = MagicMock()
        mock_models.generate_content = mock_generate

        mock_aio = MagicMock()
        mock_aio.models = mock_models

        mock_client = MagicMock()
        mock_client.aio = mock_aio

        with patch("utils.ai_client._get_client", return_value=mock_client):
            result = await generate_image("a beautiful photo")

        assert result == b"fake_image_bytes"
        call_kwargs = mock_generate.call_args
        assert call_kwargs.kwargs["model"] == IMAGE_GEN_MODEL
