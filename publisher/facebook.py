"""Facebook Pages API publisher — photo and multi-photo posts."""

import logging
import os

import httpx

from publisher.temp_server import TempImageServer
from utils.config_loader import AccountConfig

logger = logging.getLogger(__name__)

# Meta Graph API base URL and version (shared with Instagram)
_GRAPH_API_BASE = "https://graph.facebook.com/v25.0"


async def publish_photo_to_facebook(
    config: AccountConfig,
    image_path: str,
    caption_text: str,
) -> str:
    """Publish a single photo to a Facebook Page and return the post ID."""
    token = _get_facebook_token(config)
    page_id = config.facebook_page_id

    with TempImageServer(image_path, config.temp_http_port) as public_url:
        logger.info("Image available at %s — publishing to Facebook Page.", public_url)

        async with httpx.AsyncClient(timeout=30.0) as client:
            url = f"{_GRAPH_API_BASE}/{page_id}/photos"
            params: dict[str, str] = {
                "url": public_url,
                "message": caption_text,
                "access_token": token,
            }

            try:
                response = await client.post(url, data=params)
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                body = exc.response.text
                raise RuntimeError(
                    f"Facebook API error publishing photo (HTTP {exc.response.status_code}): {body}"
                ) from exc

            data = response.json()
            if "error" in data:
                err = data["error"]
                raise RuntimeError(
                    f"Facebook API error: {err.get('message', 'Unknown')} "
                    f"(code {err.get('code', '?')})"
                )

            post_id = data.get("post_id") or data.get("id")
            if not post_id:
                raise RuntimeError(f"Facebook API returned no post ID. Response: {data}")

            logger.info("Published photo to Facebook — post ID: %s", post_id)
            return post_id


async def publish_carousel_to_facebook(
    config: AccountConfig,
    image_paths: list[str],
    caption_text: str,
) -> str:
    """Publish a multi-photo post to a Facebook Page and return the post ID."""
    token = _get_facebook_token(config)
    page_id = config.facebook_page_id

    if len(image_paths) < 2:
        raise ValueError("Facebook carousel requires at least 2 images.")

    server = TempImageServer(image_paths, config.temp_http_port)
    with server:
        logger.info(
            "Serving %d images — publishing carousel to Facebook Page.", len(image_paths)
        )

        async with httpx.AsyncClient(timeout=30.0) as client:
            # Step 1: Upload each photo as unpublished
            photo_ids: list[str] = []
            for i, img_path in enumerate(image_paths):
                image_url = server.get_url(img_path)
                photo_id = await _upload_unpublished_photo(
                    client, page_id, token, image_url
                )
                logger.info(
                    "Facebook carousel photo %d/%d uploaded (unpublished): %s",
                    i + 1, len(image_paths), photo_id,
                )
                photo_ids.append(photo_id)

            # Step 2: Create multi-photo post with attached_media
            url = f"{_GRAPH_API_BASE}/{page_id}/feed"
            params: dict[str, str] = {
                "message": caption_text,
                "access_token": token,
            }
            for j, pid in enumerate(photo_ids):
                params[f"attached_media[{j}]"] = f'{{"media_fbid":"{pid}"}}'

            try:
                response = await client.post(url, data=params)
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                body = exc.response.text
                raise RuntimeError(
                    f"Facebook API error creating multi-photo post "
                    f"(HTTP {exc.response.status_code}): {body}"
                ) from exc

            data = response.json()
            if "error" in data:
                err = data["error"]
                raise RuntimeError(
                    f"Facebook API error: {err.get('message', 'Unknown')} "
                    f"(code {err.get('code', '?')})"
                )

            post_id = data.get("id")
            if not post_id:
                raise RuntimeError(
                    f"Facebook API returned no post ID. Response: {data}"
                )

            logger.info("Published carousel to Facebook — post ID: %s", post_id)
            return post_id


async def _upload_unpublished_photo(
    client: httpx.AsyncClient,
    page_id: str,
    token: str,
    image_url: str,
) -> str:
    """Upload a photo to a Facebook Page as unpublished and return its ID."""
    url = f"{_GRAPH_API_BASE}/{page_id}/photos"
    params: dict[str, str] = {
        "url": image_url,
        "published": "false",
        "access_token": token,
    }

    try:
        response = await client.post(url, data=params)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        body = exc.response.text
        raise RuntimeError(
            f"Facebook API error uploading unpublished photo "
            f"(HTTP {exc.response.status_code}): {body}"
        ) from exc

    data = response.json()
    if "error" in data:
        err = data["error"]
        raise RuntimeError(
            f"Facebook API error uploading photo: {err.get('message', 'Unknown')} "
            f"(code {err.get('code', '?')})"
        )

    photo_id = data.get("id")
    if not photo_id:
        raise RuntimeError(
            f"Facebook API returned no photo ID. Response: {data}"
        )

    return photo_id


def _get_facebook_token(config: AccountConfig) -> str:
    """Retrieve the Facebook Page access token from environment."""
    if not config.facebook_page_token_env:
        raise ValueError(
            "facebook_page_token_env is not configured for this account."
        )
    token = os.getenv(config.facebook_page_token_env)
    if not token:
        raise ValueError(
            f"Facebook Page token env var '{config.facebook_page_token_env}' is missing or empty."
        )
    return token
