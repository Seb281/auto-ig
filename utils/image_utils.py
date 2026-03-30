"""Image processing utilities — resize/crop for Instagram and perceptual hashing."""

import asyncio
import logging
import os
import shutil

import aiosqlite
import imagehash
from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

# Platform dimension registry — (width, height) for optimal image sizing
PLATFORM_DIMENSIONS: dict[str, tuple[int, int]] = {
    "instagram_square": (1080, 1080),
    "instagram_portrait": (1080, 1350),
    "facebook": (1200, 630),
    "pinterest": (1000, 1500),
    "linkedin": (1200, 627),
    "tiktok": (1080, 1920),
    "youtube": (1280, 720),
    "twitter": (1200, 675),
}

# Backward-compatible aliases referencing the registry
INSTAGRAM_SQUARE = PLATFORM_DIMENSIONS["instagram_square"]
INSTAGRAM_PORTRAIT = PLATFORM_DIMENSIONS["instagram_portrait"]


def _resize_sync(image_path: str, output_path: str, target_size: tuple[int, int]) -> str:
    """Synchronous image resize — meant to be called via asyncio.to_thread."""
    if not os.path.isfile(image_path):
        raise FileNotFoundError(f"Input image does not exist: {image_path}")

    out_dir = os.path.dirname(output_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    try:
        img = Image.open(image_path)
        img.load()
    except Exception as exc:
        raise ValueError(f"Cannot open image {image_path}: {exc}") from exc

    if img.mode in ("RGBA", "P", "LA"):
        img = img.convert("RGB")
    elif img.mode != "RGB":
        img = img.convert("RGB")

    img = ImageOps.fit(img, target_size, method=Image.LANCZOS)
    img.save(output_path, format="JPEG", quality=95)
    logger.info(
        "Resized %s -> %s (%dx%d)",
        os.path.basename(image_path),
        os.path.basename(output_path),
        target_size[0],
        target_size[1],
    )
    return output_path


async def resize_for_platform(
    image_path: str,
    output_path: str,
    platform: str,
) -> str:
    """Resize and crop an image to optimal dimensions for the given platform."""
    if platform not in PLATFORM_DIMENSIONS:
        raise ValueError(
            f"Unknown platform '{platform}'. "
            f"Valid platforms: {', '.join(sorted(PLATFORM_DIMENSIONS))}"
        )
    target_size = PLATFORM_DIMENSIONS[platform]
    return await asyncio.to_thread(_resize_sync, image_path, output_path, target_size)


async def resize_for_instagram(
    image_path: str,
    output_path: str,
    aspect: str = "square",
) -> str:
    """Resize and crop an image to Instagram dimensions, saved as JPEG."""
    target_size = INSTAGRAM_PORTRAIT if aspect == "portrait" else INSTAGRAM_SQUARE
    return await asyncio.to_thread(_resize_sync, image_path, output_path, target_size)


def compute_phash(image_path: str) -> str:
    """Compute the perceptual hash of an image file."""
    try:
        img = Image.open(image_path)
        img.load()
    except Exception as exc:
        raise ValueError(f"Cannot open image for hashing {image_path}: {exc}") from exc

    phash_value = imagehash.phash(img)
    return str(phash_value)


async def is_duplicate_image(
    phash: str,
    db_path: str,
    account_id: str,
    threshold: int = 6,
) -> bool:
    """Check if a perceptual hash is too similar to any recent post image."""
    current_hash = imagehash.hex_to_hash(phash)

    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT image_phash FROM post_history WHERE account_id = ?",
            (account_id,),
        )
        rows = await cursor.fetchall()

    for (stored_phash_str,) in rows:
        try:
            stored_hash = imagehash.hex_to_hash(stored_phash_str)
        except Exception:
            logger.warning("Skipping invalid stored phash: %s", stored_phash_str)
            continue

        distance = current_hash - stored_hash
        if distance <= threshold:
            logger.info(
                "Duplicate detected: distance=%d (threshold=%d), stored=%s",
                distance,
                threshold,
                stored_phash_str,
            )
            return True

    return False


async def copy_user_photo(source_path: str, dest_path: str) -> str:
    """Copy a user-supplied photo to the media directory."""
    await asyncio.to_thread(shutil.copy2, source_path, dest_path)
    logger.info("Copied user photo to %s", dest_path)
    return dest_path
