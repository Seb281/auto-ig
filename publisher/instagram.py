"""Meta Graph API publisher — create container, poll, publish to Instagram."""

import asyncio
import logging
import os
from datetime import datetime, timezone

import aiosqlite
import httpx

from publisher.temp_server import TempImageServer
from utils.config_loader import AccountConfig

logger = logging.getLogger(__name__)

# Meta Graph API base URL and version
_GRAPH_API_BASE = "https://graph.instagram.com/v22.0"

# Polling configuration
_POLL_INTERVAL_SECONDS = 3
_POLL_MAX_SECONDS = 60


async def publish_post(
    config: AccountConfig,
    image_path: str,
    caption_text: str,
    alt_text: str,
) -> str:
    """Publish an image to Instagram via the Meta Graph API and return the media ID."""
    # Retrieve access token from environment
    token = os.getenv(config.access_token_env)
    if not token:
        raise ValueError(
            f"Instagram access token env var '{config.access_token_env}' is missing or empty."
        )

    with TempImageServer(image_path, config.temp_http_port) as public_url:
        logger.info("Image available at %s — starting Meta Graph API publish flow.", public_url)

        async with httpx.AsyncClient(timeout=30.0) as client:
            # Step 1: Create media container
            container_id = await _create_container(
                client, config.instagram_user_id, token, public_url, caption_text, alt_text
            )
            logger.info("Media container created: %s", container_id)

            # Step 2: Poll until container is ready
            await _poll_container_status(client, container_id, token)
            logger.info("Media container ready for publishing.")

            # Step 3: Publish the container
            media_id = await _publish_container(
                client, config.instagram_user_id, token, container_id
            )
            logger.info("Published to Instagram — media ID: %s", media_id)

    return media_id


async def _create_container(
    client: httpx.AsyncClient,
    ig_user_id: str,
    token: str,
    image_url: str,
    caption: str,
    alt_text: str,
) -> str:
    """Create a media container on Instagram and return the container ID."""
    url = f"{_GRAPH_API_BASE}/{ig_user_id}/media"
    params: dict[str, str] = {
        "image_url": image_url,
        "caption": caption,
        "access_token": token,
    }

    # Only include alt_text if non-empty (Meta rejects empty alt_text)
    if alt_text:
        params["alt_text"] = alt_text

    try:
        response = await client.post(url, data=params)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        body = exc.response.text
        raise RuntimeError(
            f"Meta API error creating container (HTTP {exc.response.status_code}): {body}"
        ) from exc

    data = response.json()

    if "error" in data:
        err = data["error"]
        raise RuntimeError(
            f"Meta API error creating container: {err.get('message', 'Unknown')} "
            f"(code {err.get('code', '?')})"
        )

    container_id = data.get("id")
    if not container_id:
        raise RuntimeError(f"Meta API returned no container ID. Response: {data}")

    return container_id


async def _poll_container_status(
    client: httpx.AsyncClient,
    container_id: str,
    token: str,
) -> None:
    """Poll the container status until FINISHED or raise on ERROR/timeout."""
    url = f"{_GRAPH_API_BASE}/{container_id}"
    params = {"fields": "status_code", "access_token": token}

    max_iterations = _POLL_MAX_SECONDS // _POLL_INTERVAL_SECONDS
    for iteration in range(1, max_iterations + 1):
        try:
            response = await client.get(url, params=params)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            body = exc.response.text
            raise RuntimeError(
                f"Meta API error polling container (HTTP {exc.response.status_code}): {body}"
            ) from exc

        data = response.json()

        if "error" in data:
            err = data["error"]
            raise RuntimeError(
                f"Meta API error polling container: {err.get('message', 'Unknown')} "
                f"(code {err.get('code', '?')})"
            )

        status = data.get("status_code", "UNKNOWN")
        logger.debug("Container %s status: %s (poll %d/%d)", container_id, status, iteration, max_iterations)

        if status == "FINISHED":
            return

        if status == "ERROR":
            raise RuntimeError(
                f"Media container {container_id} entered ERROR state. Response: {data}"
            )

        # IN_PROGRESS or any unexpected status — keep polling
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)

    raise TimeoutError(
        f"Media container {container_id} did not reach FINISHED status "
        f"within {_POLL_MAX_SECONDS} seconds."
    )


async def _publish_container(
    client: httpx.AsyncClient,
    ig_user_id: str,
    token: str,
    container_id: str,
) -> str:
    """Publish a ready container and return the Instagram media ID."""
    url = f"{_GRAPH_API_BASE}/{ig_user_id}/media_publish"
    params = {"creation_id": container_id, "access_token": token}

    try:
        response = await client.post(url, data=params)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        body = exc.response.text
        raise RuntimeError(
            f"Meta API error publishing container (HTTP {exc.response.status_code}): {body}"
        ) from exc

    data = response.json()

    if "error" in data:
        err = data["error"]
        raise RuntimeError(
            f"Meta API error publishing: {err.get('message', 'Unknown')} "
            f"(code {err.get('code', '?')})"
        )

    media_id = data.get("id")
    if not media_id:
        raise RuntimeError(f"Meta API returned no media ID. Response: {data}")

    return media_id


async def save_post_record(
    db_path: str,
    account_id: str,
    topic: str,
    content_pillar: str,
    image_phash: str,
    caption: str,
    instagram_media_id: str | None,
) -> None:
    """Insert a row into post_history after publishing or dry-run."""
    caption_snippet = caption[:80]
    published_at = datetime.now(timezone.utc).isoformat()

    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            INSERT INTO post_history
                (account_id, topic, content_pillar, image_phash,
                 caption_snippet, published_at, instagram_media_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account_id,
                topic,
                content_pillar,
                image_phash,
                caption_snippet,
                published_at,
                instagram_media_id,
            ),
        )
        await db.commit()

    logger.info(
        "Post record saved — account=%s, topic=%s, media_id=%s",
        account_id,
        topic,
        instagram_media_id,
    )
