"""Async stock photo search clients for Unsplash and Pexels APIs."""

import logging
import os
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

# API endpoints
UNSPLASH_SEARCH_URL = "https://api.unsplash.com/search/photos"
PEXELS_SEARCH_URL = "https://api.pexels.com/v1/search"

# Timeout for API and download requests (seconds)
HTTP_TIMEOUT = 30.0


@dataclass
class StockPhoto:
    """A candidate stock photo from a search result."""

    url: str
    source: str  # "unsplash" | "pexels"
    photographer: str
    description: str


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

    with open(dest_path, "wb") as f:
        f.write(response.content)

    logger.info("Downloaded image to %s (%d bytes).", dest_path, len(response.content))
    return dest_path
