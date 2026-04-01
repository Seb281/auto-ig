"""Video validation and thumbnail extraction via ffprobe/ffmpeg."""

import asyncio
import json
import logging
import os
from dataclasses import dataclass

from utils.image_utils import compute_phash

logger = logging.getLogger(__name__)

# Reel spec limits
REEL_MIN_DURATION = 3.0  # seconds
REEL_MAX_DURATION = 90.0
REEL_TARGET_WIDTH = 1080
REEL_TARGET_HEIGHT = 1920


@dataclass
class VideoMetadata:
    """Metadata extracted from a video file via ffprobe."""

    duration_seconds: float
    width: int
    height: int
    codec: str


async def probe_video(video_path: str) -> VideoMetadata:
    """Extract metadata from a video file using ffprobe."""
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_streams", "-show_format", video_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        raise RuntimeError(
            f"ffprobe failed for {video_path}: {stderr.decode(errors='replace')}"
        )

    data = json.loads(stdout.decode())

    # Find the video stream
    video_stream = None
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video":
            video_stream = stream
            break

    if video_stream is None:
        raise RuntimeError(f"No video stream found in {video_path}")

    duration = float(
        data.get("format", {}).get("duration", 0)
        or video_stream.get("duration", 0)
    )
    width = int(video_stream.get("width", 0))
    height = int(video_stream.get("height", 0))
    codec = video_stream.get("codec_name", "unknown")

    return VideoMetadata(
        duration_seconds=duration,
        width=width,
        height=height,
        codec=codec,
    )


async def extract_thumbnail(
    video_path: str,
    output_path: str,
    timestamp: float | None = None,
) -> str:
    """Extract a single frame from a video as a JPEG thumbnail."""
    if timestamp is None:
        meta = await probe_video(video_path)
        timestamp = meta.duration_seconds / 2

    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-ss", str(timestamp), "-i", video_path,
        "-frames:v", "1", "-y", output_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()

    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg thumbnail extraction failed: {stderr.decode(errors='replace')}"
        )

    if not os.path.isfile(output_path):
        raise RuntimeError(f"Thumbnail was not created at {output_path}")

    logger.info("Extracted thumbnail at t=%.1fs -> %s", timestamp, output_path)
    return output_path


def validate_reel_specs(metadata: VideoMetadata) -> list[str]:
    """Return a list of spec violations for a reel (empty means OK)."""
    violations: list[str] = []

    if metadata.duration_seconds < REEL_MIN_DURATION:
        violations.append(
            f"Duration {metadata.duration_seconds:.1f}s is below minimum {REEL_MIN_DURATION}s"
        )
    if metadata.duration_seconds > REEL_MAX_DURATION:
        violations.append(
            f"Duration {metadata.duration_seconds:.1f}s exceeds maximum {REEL_MAX_DURATION}s"
        )

    # Check aspect ratio is roughly 9:16 (allow some tolerance)
    if metadata.width > 0 and metadata.height > 0:
        aspect = metadata.width / metadata.height
        target_aspect = 9 / 16  # 0.5625
        if abs(aspect - target_aspect) > 0.15:
            violations.append(
                f"Aspect ratio {metadata.width}x{metadata.height} "
                f"({aspect:.2f}) is not close to 9:16 ({target_aspect:.2f})"
            )

    return violations


async def compute_video_phash(video_path: str, temp_dir: str) -> str:
    """Extract a thumbnail and compute its perceptual hash."""
    thumb_path = os.path.join(temp_dir, f"phash_thumb_{os.path.basename(video_path)}.jpg")
    try:
        await extract_thumbnail(video_path, thumb_path)
        return compute_phash(thumb_path)
    finally:
        if os.path.exists(thumb_path):
            os.remove(thumb_path)
