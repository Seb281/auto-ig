"""Thin AI client — wraps Google Gemini SDK calls."""

import logging
import os

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

# Model constants — single place to change when switching providers
TEXT_MODEL = "gemini-2.5-flash-lite"       # 1,000 RPD free tier
VISION_MODEL = "gemini-2.5-flash"          # 250 RPD free tier (multimodal)
IMAGE_GEN_MODEL = "gemini-2.5-flash-image" # ~500 RPD free tier


def _get_client() -> genai.Client:
    """Return a configured Gemini client."""
    return genai.Client(api_key=os.getenv("GEMINI_API_KEY"))


async def generate_text(prompt: str) -> str:
    """Send a text prompt to Gemini and return the response text."""
    client = _get_client()
    response = await client.aio.models.generate_content(
        model=TEXT_MODEL,
        contents=prompt,
    )
    return response.text


async def generate_vision(image_bytes: bytes, mime_type: str, prompt: str) -> str:
    """Send an image + text prompt to Gemini vision and return the response text."""
    client = _get_client()
    response = await client.aio.models.generate_content(
        model=VISION_MODEL,
        contents=[
            types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
            prompt,
        ],
    )
    return response.text


async def generate_image(prompt: str) -> bytes:
    """Generate an image from a text prompt and return the raw image bytes."""
    client = _get_client()
    response = await client.aio.models.generate_content(
        model=IMAGE_GEN_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_modalities=["IMAGE"],
            image_config=types.ImageConfig(aspect_ratio="1:1"),
        ),
    )
    for part in response.candidates[0].content.parts:
        if part.inline_data:
            return part.inline_data.data
    raise RuntimeError("Gemini image generation returned no image data.")


def read_image_file(file_path: str) -> tuple[bytes, str]:
    """Read an image file and return (raw_bytes, mime_type)."""
    with open(file_path, "rb") as f:
        data = f.read()

    if data[:3] == b"\xff\xd8\xff":
        mime = "image/jpeg"
    elif data[:8] == b"\x89PNG\r\n\x1a\n":
        mime = "image/png"
    elif data[:4] == b"GIF8":
        mime = "image/gif"
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        mime = "image/jpeg"

    return data, mime
