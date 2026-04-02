"""Tests for utils/image_utils.py — resize, phash, duplicate detection."""

import os

import aiosqlite
import pytest
from PIL import Image

from utils.image_utils import (
    PLATFORM_DIMENSIONS,
    _resize_sync,
    compute_phash,
    copy_user_photo,
    is_duplicate_image,
    resize_for_instagram,
    resize_for_platform,
)


def _create_test_image(path, size=(200, 200), color="red", mode="RGB"):
    """Create a test image file."""
    img = Image.new(mode, size, color)
    img.save(path, format="JPEG")
    return path


# ---------------------------------------------------------------------------
# _resize_sync
# ---------------------------------------------------------------------------

class TestResizeSync:
    def test_creates_jpeg_at_target_size(self, tmp_path):
        src = _create_test_image(str(tmp_path / "src.jpg"))
        out = str(tmp_path / "out.jpg")
        _resize_sync(src, out, (1080, 1080))
        assert os.path.isfile(out)
        img = Image.open(out)
        assert img.size == (1080, 1080)

    def test_rgba_to_rgb_conversion(self, tmp_path):
        src = str(tmp_path / "src.png")
        img = Image.new("RGBA", (200, 200), (255, 0, 0, 128))
        img.save(src, format="PNG")
        out = str(tmp_path / "out.jpg")
        _resize_sync(src, out, (100, 100))
        result = Image.open(out)
        assert result.mode == "RGB"

    def test_missing_input_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            _resize_sync("/nonexistent/file.jpg", str(tmp_path / "out.jpg"), (100, 100))


# ---------------------------------------------------------------------------
# resize_for_instagram
# ---------------------------------------------------------------------------

class TestResizeForInstagram:
    async def test_square(self, tmp_path):
        src = _create_test_image(str(tmp_path / "src.jpg"))
        out = str(tmp_path / "out.jpg")
        await resize_for_instagram(src, out, aspect="square")
        img = Image.open(out)
        assert img.size == (1080, 1080)

    async def test_portrait(self, tmp_path):
        src = _create_test_image(str(tmp_path / "src.jpg"))
        out = str(tmp_path / "out.jpg")
        await resize_for_instagram(src, out, aspect="portrait")
        img = Image.open(out)
        assert img.size == (1080, 1350)


# ---------------------------------------------------------------------------
# resize_for_platform
# ---------------------------------------------------------------------------

class TestResizeForPlatform:
    async def test_facebook(self, tmp_path):
        src = _create_test_image(str(tmp_path / "src.jpg"))
        out = str(tmp_path / "out.jpg")
        await resize_for_platform(src, out, "facebook")
        img = Image.open(out)
        assert img.size == PLATFORM_DIMENSIONS["facebook"]

    async def test_unknown_platform_raises(self, tmp_path):
        src = _create_test_image(str(tmp_path / "src.jpg"))
        out = str(tmp_path / "out.jpg")
        with pytest.raises(ValueError, match="Unknown platform"):
            await resize_for_platform(src, out, "myspace")


# ---------------------------------------------------------------------------
# compute_phash
# ---------------------------------------------------------------------------

class TestComputePhash:
    def test_returns_hex_string(self, tmp_path):
        src = _create_test_image(str(tmp_path / "src.jpg"))
        phash = compute_phash(src)
        assert isinstance(phash, str)
        assert len(phash) == 16  # 64-bit phash = 16 hex chars

    def test_deterministic(self, tmp_path):
        src = _create_test_image(str(tmp_path / "src.jpg"))
        assert compute_phash(src) == compute_phash(src)

    def test_different_images_different_hashes(self, tmp_path):
        # Use patterned images — solid colors hash identically
        src1 = str(tmp_path / "a.jpg")
        img1 = Image.new("RGB", (200, 200), "red")
        for x in range(0, 200, 2):
            for y in range(200):
                img1.putpixel((x, y), (0, 0, 0))
        img1.save(src1, format="JPEG")

        src2 = str(tmp_path / "b.jpg")
        img2 = Image.new("RGB", (200, 200), "blue")
        for y in range(0, 200, 2):
            for x in range(200):
                img2.putpixel((x, y), (255, 255, 0))
        img2.save(src2, format="JPEG")

        assert compute_phash(src1) != compute_phash(src2)


# ---------------------------------------------------------------------------
# is_duplicate_image
# ---------------------------------------------------------------------------

class TestIsDuplicateImage:
    async def test_no_history_returns_false(self, tmp_db):
        result = await is_duplicate_image("abcdef1234567890", tmp_db, "test_account")
        assert result is False

    async def test_exact_match_returns_true(self, tmp_db):
        # Insert a matching record
        async with aiosqlite.connect(tmp_db) as db:
            await db.execute(
                "INSERT INTO post_history (account_id, topic, content_pillar, image_phash, caption_snippet, published_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("test_account", "topic", "pillar", "abcdef1234567890", "caption", "2024-01-01"),
            )
            await db.commit()
        result = await is_duplicate_image("abcdef1234567890", tmp_db, "test_account")
        assert result is True

    async def test_distant_hash_returns_false(self, tmp_db):
        async with aiosqlite.connect(tmp_db) as db:
            await db.execute(
                "INSERT INTO post_history (account_id, topic, content_pillar, image_phash, caption_snippet, published_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("test_account", "topic", "pillar", "0000000000000000", "caption", "2024-01-01"),
            )
            await db.commit()
        result = await is_duplicate_image("ffffffffffffffff", tmp_db, "test_account")
        assert result is False


# ---------------------------------------------------------------------------
# copy_user_photo
# ---------------------------------------------------------------------------

class TestCopyUserPhoto:
    async def test_file_is_copied(self, tmp_path):
        src = _create_test_image(str(tmp_path / "original.jpg"))
        dest = str(tmp_path / "copy.jpg")
        await copy_user_photo(src, dest)
        assert os.path.isfile(dest)
        assert os.path.getsize(dest) == os.path.getsize(src)
