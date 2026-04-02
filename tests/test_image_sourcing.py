"""Tests for agents/image_sourcing.py — image sourcing with mocked dependencies."""

import os
from unittest.mock import AsyncMock, patch

import pytest

from agents.image_sourcing import source_carousel_images, source_image
from utils.stock_search import StockPhoto


def _mock_stock_photos(n=3):
    """Return a list of mock StockPhoto objects."""
    return [
        StockPhoto(url=f"https://example.com/{i}.jpg", source="unsplash",
                   photographer="Test", description="Desc")
        for i in range(n)
    ]


class TestSourceImage:
    async def test_user_photo_path(self, make_account_config, make_planner_brief, tmp_db, tmp_path):
        config = make_account_config()
        brief = make_planner_brief()
        media_dir = str(tmp_path / "media")

        # Create a fake user photo
        user_photo = str(tmp_path / "user.jpg")
        from PIL import Image
        Image.new("RGB", (200, 200), "red").save(user_photo, "JPEG")

        result = await source_image(config, brief, tmp_db, media_dir, user_photo_path=user_photo)

        assert result.source == "user"
        assert result.score == 1.0
        assert os.path.isfile(result.local_path)

    async def test_stock_above_threshold(self, make_account_config, make_planner_brief, tmp_db, tmp_path):
        config = make_account_config()
        brief = make_planner_brief()
        media_dir = str(tmp_path / "media")

        from PIL import Image
        # Mock stock search and download to produce real image files
        async def mock_download(url, dest_path):
            Image.new("RGB", (200, 200), "green").save(dest_path, "JPEG")
            return dest_path

        with patch("agents.image_sourcing._search_stock_photos", new_callable=AsyncMock, return_value=_mock_stock_photos(1)):
            with patch("agents.image_sourcing.download_image", side_effect=mock_download):
                with patch("agents.image_sourcing._score_candidate", new_callable=AsyncMock, return_value=0.9):
                    with patch("agents.image_sourcing.is_duplicate_image", new_callable=AsyncMock, return_value=False):
                        result = await source_image(config, brief, tmp_db, media_dir)

        assert result.source == "unsplash"
        assert result.score == 0.9

    async def test_stock_below_threshold_falls_to_ai(self, make_account_config, make_planner_brief, tmp_db, tmp_path):
        config = make_account_config()
        brief = make_planner_brief()
        media_dir = str(tmp_path / "media")

        from PIL import Image

        async def mock_download(url, dest_path):
            Image.new("RGB", (200, 200), "green").save(dest_path, "JPEG")
            return dest_path

        async def mock_generate_ai(cfg, br, md):
            path = os.path.join(md, "generated.png")
            Image.new("RGB", (200, 200), "blue").save(path, "PNG")
            return path

        with patch("agents.image_sourcing._search_stock_photos", new_callable=AsyncMock, return_value=_mock_stock_photos(1)):
            with patch("agents.image_sourcing.download_image", side_effect=mock_download):
                with patch("agents.image_sourcing._score_candidate", new_callable=AsyncMock, return_value=0.1):
                    with patch("agents.image_sourcing._generate_ai_image", side_effect=mock_generate_ai):
                        result = await source_image(config, brief, tmp_db, media_dir)

        assert result.source == "gemini"

    async def test_stock_only_mode_no_ai(self, make_account_config, make_planner_brief, tmp_db, tmp_path):
        config = make_account_config()
        brief = make_planner_brief()
        media_dir = str(tmp_path / "media")

        from PIL import Image

        async def mock_download(url, dest_path):
            Image.new("RGB", (200, 200), "green").save(dest_path, "JPEG")
            return dest_path

        with patch("agents.image_sourcing._search_stock_photos", new_callable=AsyncMock, return_value=_mock_stock_photos(1)):
            with patch("agents.image_sourcing.download_image", side_effect=mock_download):
                with patch("agents.image_sourcing._score_candidate", new_callable=AsyncMock, return_value=0.5):
                    with patch("agents.image_sourcing.is_duplicate_image", new_callable=AsyncMock, return_value=False):
                        result = await source_image(config, brief, tmp_db, media_dir, stock_only=True)

        # stock_only accepts any score > 0
        assert result.source == "unsplash"


class TestSourceCarouselImages:
    async def test_collects_images(self, make_account_config, make_planner_brief, tmp_db, tmp_path):
        config = make_account_config()
        brief = make_planner_brief(content_type="carousel")
        media_dir = str(tmp_path / "media")

        from PIL import Image

        async def mock_download(url, dest_path):
            Image.new("RGB", (200, 200), "green").save(dest_path, "JPEG")
            return dest_path

        with patch("agents.image_sourcing._search_stock_photos", new_callable=AsyncMock, return_value=_mock_stock_photos(5)):
            with patch("agents.image_sourcing.download_image", side_effect=mock_download):
                with patch("agents.image_sourcing._score_candidate", new_callable=AsyncMock, return_value=0.8):
                    with patch("agents.image_sourcing.is_duplicate_image", new_callable=AsyncMock, return_value=False):
                        results = await source_carousel_images(config, brief, tmp_db, media_dir)

        assert len(results) >= 3
        assert len(results) <= 5

    async def test_ai_fills_shortfall(self, make_account_config, make_planner_brief, tmp_db, tmp_path):
        config = make_account_config()
        brief = make_planner_brief(content_type="carousel")
        media_dir = str(tmp_path / "media")

        from PIL import Image

        async def mock_download(url, dest_path):
            Image.new("RGB", (200, 200), "green").save(dest_path, "JPEG")
            return dest_path

        async def mock_generate_ai(cfg, br, md):
            path = os.path.join(md, f"gen_{os.urandom(4).hex()}.png")
            Image.new("RGB", (200, 200), "blue").save(path, "PNG")
            return path

        # Only 1 stock photo with good enough score
        with patch("agents.image_sourcing._search_stock_photos", new_callable=AsyncMock, return_value=_mock_stock_photos(1)):
            with patch("agents.image_sourcing.download_image", side_effect=mock_download):
                with patch("agents.image_sourcing._score_candidate", new_callable=AsyncMock, return_value=0.8):
                    with patch("agents.image_sourcing.is_duplicate_image", new_callable=AsyncMock, return_value=False):
                        with patch("agents.image_sourcing._generate_ai_image", side_effect=mock_generate_ai):
                            results = await source_carousel_images(config, brief, tmp_db, media_dir)

        assert len(results) >= 3
        # Should include AI-generated images
        sources = [r.source for r in results]
        assert "gemini" in sources
