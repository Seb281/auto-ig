"""Video Sourcing Agent — finds stock video for reel posts."""

import asyncio
import logging
import os
import uuid

from agents import PlannerBrief, VideoResult
from utils.ai_client import generate_vision, read_image_file
from utils.config_loader import AccountConfig
from utils.image_utils import is_duplicate_image
from utils.prompts import _extract_json, build_vision_scoring_prompt
from utils.stock_search import StockVideo, download_video, search_pexels_videos
from utils.video_utils import (
    compute_video_phash,
    extract_thumbnail,
    probe_video,
    validate_reel_specs,
)

logger = logging.getLogger(__name__)

# Maximum number of video candidates to score with vision
MAX_VIDEO_CANDIDATES_TO_SCORE = 3


async def _score_video_candidate(
    video_path: str,
    config: AccountConfig,
    brief: PlannerBrief,
    media_dir: str,
) -> float:
    """Score a video candidate by extracting a thumbnail and running AI vision."""
    thumb_path = os.path.join(
        media_dir, f"score_thumb_{uuid.uuid4().hex[:8]}.jpg"
    )
    try:
        await extract_thumbnail(video_path, thumb_path)

        prompt_text = build_vision_scoring_prompt(config, brief)
        image_bytes, mime_type = await asyncio.to_thread(read_image_file, thumb_path)

        raw_text = await generate_vision(image_bytes, mime_type, prompt_text)
        data = _extract_json(raw_text)
        score = float(data.get("score", 0.0))
        reasoning = data.get("reasoning", "")
        logger.info(
            "Video vision score for %s: %.2f — %s",
            os.path.basename(video_path),
            score,
            reasoning[:100],
        )
        return max(0.0, min(1.0, score))

    except Exception as exc:
        logger.warning(
            "Video vision scoring failed for %s: %s. Assigning score 0.0.",
            os.path.basename(video_path),
            exc,
        )
        return 0.0

    finally:
        if os.path.exists(thumb_path):
            os.remove(thumb_path)


async def source_video(
    config: AccountConfig,
    brief: PlannerBrief,
    db_path: str,
    media_dir: str,
) -> VideoResult:
    """Source a stock video for a reel post."""
    os.makedirs(media_dir, exist_ok=True)
    temp_files: list[str] = []

    try:
        # Step 1: Search Pexels for videos
        stock_results = await search_pexels_videos(brief.visual_keywords)
        if not stock_results:
            raise RuntimeError(
                "No stock videos found for keywords: "
                + ", ".join(brief.visual_keywords)
            )

        logger.info("Found %d video results from Pexels.", len(stock_results))

        # Step 2: Download and validate top candidates
        candidates = stock_results[:MAX_VIDEO_CANDIDATES_TO_SCORE]
        valid_candidates: list[tuple[str, StockVideo]] = []

        for i, video in enumerate(candidates):
            filename = f"video_candidate_{i}_{uuid.uuid4().hex[:8]}.mp4"
            candidate_path = os.path.join(media_dir, filename)

            try:
                await download_video(video.url, candidate_path)
                temp_files.append(candidate_path)
            except ConnectionError as exc:
                logger.warning("Failed to download video candidate %d: %s", i, exc)
                continue

            # Validate reel specs
            try:
                metadata = await probe_video(candidate_path)
            except RuntimeError as exc:
                logger.warning("Failed to probe video candidate %d: %s", i, exc)
                continue

            violations = validate_reel_specs(metadata)
            if violations:
                logger.info(
                    "Video candidate %d failed spec check: %s",
                    i,
                    "; ".join(violations),
                )
                continue

            valid_candidates.append((candidate_path, video))

        if not valid_candidates:
            raise RuntimeError("No valid stock videos found after spec validation.")

        # Step 3: Score valid candidates via thumbnail + AI vision
        best_score = 0.0
        best_path: str | None = None
        best_video: StockVideo | None = None

        for candidate_path, video in valid_candidates:
            score = await _score_video_candidate(
                candidate_path, config, brief, media_dir
            )
            if score > best_score:
                best_score = score
                best_path = candidate_path
                best_video = video

        if best_path is None or best_video is None:
            raise RuntimeError("No stock videos scored above 0.0.")

        logger.info(
            "Best video score: %.2f (threshold: %.2f)",
            best_score,
            config.image_sourcing.stock_score_threshold,
        )

        # Step 4: Compute phash and check for duplicates
        phash = await compute_video_phash(best_path, media_dir)

        is_dup = await is_duplicate_image(phash, db_path, config.account_id)
        if is_dup:
            raise RuntimeError("Best video is a duplicate of a previously published post.")

        # Step 5: Get final metadata
        final_meta = await probe_video(best_path)

        # Clean up non-selected candidates
        for tf in temp_files:
            if tf != best_path and os.path.exists(tf):
                os.remove(tf)
                logger.debug("Cleaned up video temp file: %s", tf)
        temp_files.clear()

        return VideoResult(
            local_path=best_path,
            source=best_video.source,
            phash=phash,
            score=best_score,
            duration_seconds=final_meta.duration_seconds,
            width=final_meta.width,
            height=final_meta.height,
        )

    except Exception:
        # Clean up all temp files on failure
        for tf in temp_files:
            if os.path.exists(tf):
                try:
                    os.remove(tf)
                    logger.debug("Cleaned up video temp file on error: %s", tf)
                except OSError:
                    pass
        raise
