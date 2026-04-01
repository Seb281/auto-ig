"""Async stock photo and video search clients for Unsplash and Pexels APIs."""

import asyncio
import logging
import os
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

# API endpoints
UNSPLASH_SEARCH_URL = "https://api.unsplash.com/search/photos"
PEXELS_SEARCH_URL = "https://api.pexels.com/v1/search"
PEXELS_VIDEO_SEARCH_URL = "https://api.pexels.com/videos/search"

# Timeout for API and download requests (seconds)
HTTP_TIMEOUT = 30.0
VIDEO_DOWNLOAD_TIMEOUT = 60.0


@dataclass
class StockPhoto:
    """A candidate stock photo from a search result."""

    url: str
    source: str  # "unsplash" | "pexels"
    photographer: str
    description: str


@dataclass
class StockVideo:
    """A candidate stock video from a search result."""

    url: str
    source: str  # "pexels"
    photographer: str
    description: str
    duration: int  # seconds
    width: int
    height: int


async def search_unsplash(
    keywords: list[str],
    per_page: int = 5,
) -> list[StockPhoto]:
    """Search Unsplash for photos matching the given keywords."""
    api_key = os.getenv("UNSPLASH_ACCESS_KEY")
    if not api_key:
        raise ValueError("UNSPLASH_ACCESS_KEY environment variable is not set.")

    query = " ".join(keywords)
    params = {"query": query, "per_page": per_page, "orientation": "squarish"}
    headers = {"Authorization": f"Client-ID {api_key}"}

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            response = await client.get(
                UNSPLASH_SEARCH_URL, params=params, headers=headers
            )

        if response.status_code == 429:
            logger.warning("Unsplash rate limit hit (429). Returning empty results.")
            return []

        response.raise_for_status()
        data = response.json()

    except httpx.TimeoutException:
        logger.warning("Unsplash search timed out for query '%s'.", query)
        return []
    except httpx.HTTPStatusError as exc:
        logger.warning("Unsplash API error: %s", exc)
        return []
    except httpx.HTTPError as exc:
        logger.warning("Unsplash network error: %s", exc)
        return []

    results: list[StockPhoto] = []
    for item in data.get("results", []):
        urls = item.get("urls", {})
        # Prefer 'regular' (1080w) for good quality without excessive size
        url = urls.get("regular") or urls.get("full") or urls.get("small", "")
        if not url:
            continue

        photographer = item.get("user", {}).get("name", "Unknown")
        description = item.get("description") or item.get("alt_description") or ""

        results.append(
            StockPhoto(
                url=url,
                source="unsplash",
                photographer=photographer,
                description=description,
            )
        )

    logger.info("Unsplash returned %d results for '%s'.", len(results), query)
    return results


async def search_pexels(
    keywords: list[str],
    per_page: int = 5,
) -> list[StockPhoto]:
    """Search Pexels for photos matching the given keywords."""
    api_key = os.getenv("PEXELS_API_KEY")
    if not api_key:
        raise ValueError("PEXELS_API_KEY environment variable is not set.")

    query = " ".join(keywords)
    params = {"query": query, "per_page": per_page}
    headers = {"Authorization": api_key}

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            response = await client.get(
                PEXELS_SEARCH_URL, params=params, headers=headers
            )

        if response.status_code == 429:
            logger.warning("Pexels rate limit hit (429). Returning empty results.")
            return []

        response.raise_for_status()
        data = response.json()

    except httpx.TimeoutException:
        logger.warning("Pexels search timed out for query '%s'.", query)
        return []
    except httpx.HTTPStatusError as exc:
        logger.warning("Pexels API error: %s", exc)
        return []
    except httpx.HTTPError as exc:
        logger.warning("Pexels network error: %s", exc)
        return []

    results: list[StockPhoto] = []
    for item in data.get("photos", []):
        src = item.get("src", {})
        # Prefer 'large' (940px wide) for good quality
        url = src.get("large") or src.get("original") or src.get("medium", "")
        if not url:
            continue

        photographer = item.get("photographer", "Unknown")
        description = item.get("alt") or ""

        results.append(
            StockPhoto(
                url=url,
                source="pexels",
                photographer=photographer,
                description=description,
            )
        )

    logger.info("Pexels returned %d results for '%s'.", len(results), query)
    return results


def _write_bytes_sync(path: str, data: bytes) -> None:
    """Write raw bytes to a file (sync, for use with to_thread)."""
    with open(path, "wb") as f:
        f.write(data)


async def download_image(url: str, dest_path: str) -> str:
    """Download an image from a URL and save it to the destination path."""
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
    except httpx.TimeoutException:
        raise ConnectionError(f"Timeout downloading image from {url}")
    except httpx.HTTPError as exc:
        raise ConnectionError(f"Failed to download image from {url}: {exc}") from exc

    dest_dir = os.path.dirname(dest_path)
    if dest_dir:
        os.makedirs(dest_dir, exist_ok=True)

    await asyncio.to_thread(_write_bytes_sync, dest_path, response.content)

    logger.info("Downloaded image to %s (%d bytes).", dest_path, len(response.content))
    return dest_path


def _pick_best_video_file(video_files: list[dict]) -> str | None:
    """Pick the best video file URL from a Pexels video result (prefer 9:16, HD, mp4)."""
    best_url: str | None = None
    best_score = -1

    for vf in video_files:
        file_type = vf.get("file_type", "")
        if "mp4" not in file_type:
            continue

        width = vf.get("width", 0)
        height = vf.get("height", 0)
        quality = vf.get("quality", "")

        score = 0
        # Prefer portrait (9:16 ish)
        if height > width:
            score += 10
        # Prefer HD quality
        if quality == "hd":
            score += 5
        elif quality == "sd":
            score += 2
        # Prefer closer to 1080 width
        if 720 <= width <= 1080:
            score += 3

        link = vf.get("link", "")
        if link and score > best_score:
            best_score = score
            best_url = link

    return best_url


async def search_pexels_videos(
    keywords: list[str],
    per_page: int = 10,
    min_duration: int = 3,
    max_duration: int = 60,
) -> list[StockVideo]:
    """Search Pexels for videos matching the given keywords."""
    api_key = os.getenv("PEXELS_API_KEY")
    if not api_key:
        raise ValueError("PEXELS_API_KEY environment variable is not set.")

    query = " ".join(keywords)
    params: dict[str, str | int] = {
        "query": query,
        "per_page": per_page,
        "orientation": "portrait",
    }
    headers = {"Authorization": api_key}

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            response = await client.get(
                PEXELS_VIDEO_SEARCH_URL, params=params, headers=headers
            )

        if response.status_code == 429:
            logger.warning("Pexels video rate limit hit (429). Returning empty results.")
            return []

        response.raise_for_status()
        data = response.json()

    except httpx.TimeoutException:
        logger.warning("Pexels video search timed out for query '%s'.", query)
        return []
    except httpx.HTTPStatusError as exc:
        logger.warning("Pexels video API error: %s", exc)
        return []
    except httpx.HTTPError as exc:
        logger.warning("Pexels video network error: %s", exc)
        return []

    results: list[StockVideo] = []
    for item in data.get("videos", []):
        duration = item.get("duration", 0)
        if duration < min_duration or duration > max_duration:
            continue

        video_files = item.get("video_files", [])
        url = _pick_best_video_file(video_files)
        if not url:
            continue

        width = item.get("width", 0)
        height = item.get("height", 0)
        photographer = item.get("user", {}).get("name", "Unknown")
        description = item.get("url", "")

        results.append(
            StockVideo(
                url=url,
                source="pexels",
                photographer=photographer,
                description=description,
                duration=duration,
                width=width,
                height=height,
            )
        )

    logger.info("Pexels video search returned %d results for '%s'.", len(results), query)
    return results


async def download_video(url: str, dest_path: str) -> str:
    """Download a video from a URL and save it to the destination path."""
    try:
        async with httpx.AsyncClient(timeout=VIDEO_DOWNLOAD_TIMEOUT, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
    except httpx.TimeoutException:
        raise ConnectionError(f"Timeout downloading video from {url}")
    except httpx.HTTPError as exc:
        raise ConnectionError(f"Failed to download video from {url}: {exc}") from exc

    dest_dir = os.path.dirname(dest_path)
    if dest_dir:
        os.makedirs(dest_dir, exist_ok=True)

    await asyncio.to_thread(_write_bytes_sync, dest_path, response.content)

    logger.info("Downloaded video to %s (%d bytes).", dest_path, len(response.content))
    return dest_path
