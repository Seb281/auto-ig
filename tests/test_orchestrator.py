"""Tests for agents/orchestrator.py — pipeline orchestration with mocked agents."""

import os
from unittest.mock import AsyncMock, patch

import pytest

from agents import CaptionResult, ImageResult, PlannerBrief, ReviewResult, VideoResult
from agents.orchestrator import run_pipeline
from agents.reviewer import RETRY_CAPTION, RETRY_IMAGE, RETRY_VIDEO, STATUS_FAIL, STATUS_PASS


def _brief(content_type="single_image"):
    return PlannerBrief(
        topic="Test topic", angle="Angle", visual_keywords=["kw"],
        mood="mood", content_pillar="recipes", content_type=content_type,
    )


def _image(tmp_path, name="img.jpg"):
    path = str(tmp_path / name)
    with open(path, "wb") as f:
        f.write(b"\xff\xd8\xff" + b"\x00" * 100)
    return ImageResult(local_path=path, source="unsplash", phash="abc", score=0.9)


def _video(tmp_path, name="video.mp4"):
    path = str(tmp_path / name)
    with open(path, "wb") as f:
        f.write(b"\x00" * 100)
    return VideoResult(local_path=path, source="pexels", phash="vid", score=0.8,
                       duration_seconds=15.0, width=1080, height=1920)


def _caption():
    return CaptionResult(caption="Test caption", hashtags=["tag"], alt_text="Alt")


def _review_pass():
    return ReviewResult(status=STATUS_PASS, reasons=[], retry_type=None)


def _review_fail(retry_type=RETRY_CAPTION):
    return ReviewResult(status=STATUS_FAIL, reasons=["Problem"], retry_type=retry_type)


class TestRunPipelineSingleImage:
    async def test_happy_path(self, make_account_config, tmp_db, tmp_path):
        config = make_account_config()
        img = _image(tmp_path)

        with patch("agents.orchestrator.generate_brief", new_callable=AsyncMock, return_value=_brief()):
            with patch("agents.orchestrator.source_image", new_callable=AsyncMock, return_value=img):
                with patch("agents.orchestrator.generate_caption", new_callable=AsyncMock, return_value=_caption()):
                    with patch("agents.orchestrator.review_post", new_callable=AsyncMock, return_value=_review_pass()):
                        result = await run_pipeline(config, tmp_db)

        assert result.success is True
        assert result.brief is not None
        assert result.image is not None
        assert result.caption is not None

    async def test_user_photo_forces_single_image(self, make_account_config, tmp_db, tmp_path):
        config = make_account_config()
        user_photo = str(tmp_path / "user.jpg")
        with open(user_photo, "wb") as f:
            f.write(b"\xff\xd8\xff" + b"\x00" * 100)
        img = _image(tmp_path)

        with patch("agents.orchestrator.source_image", new_callable=AsyncMock, return_value=img) as mock_src:
            with patch("agents.orchestrator.generate_caption", new_callable=AsyncMock, return_value=_caption()):
                with patch("agents.orchestrator.review_post", new_callable=AsyncMock, return_value=_review_pass()):
                    result = await run_pipeline(config, tmp_db, user_photo_path=user_photo)

        assert result.success is True
        # Should have called source_image with the user photo
        call_kwargs = mock_src.call_args.kwargs
        assert call_kwargs["user_photo_path"] == user_photo


class TestRunPipelineCarousel:
    async def test_happy_path(self, make_account_config, tmp_db, tmp_path):
        config = make_account_config()
        imgs = [_image(tmp_path, f"img{i}.jpg") for i in range(3)]

        with patch("agents.orchestrator.generate_brief", new_callable=AsyncMock, return_value=_brief("carousel")):
            with patch("agents.orchestrator.source_carousel_images", new_callable=AsyncMock, return_value=imgs):
                with patch("agents.orchestrator.generate_caption", new_callable=AsyncMock, return_value=_caption()):
                    with patch("agents.orchestrator.review_carousel_post", new_callable=AsyncMock, return_value=_review_pass()):
                        result = await run_pipeline(config, tmp_db)

        assert result.success is True
        assert len(result.images) == 3


class TestRunPipelineReel:
    async def test_happy_path(self, make_account_config, tmp_db, tmp_path):
        config = make_account_config()
        vid = _video(tmp_path)

        with patch("agents.orchestrator.generate_brief", new_callable=AsyncMock, return_value=_brief("reel")):
            with patch("agents.orchestrator.source_video", new_callable=AsyncMock, return_value=vid):
                with patch("agents.orchestrator.generate_caption", new_callable=AsyncMock, return_value=_caption()):
                    with patch("agents.orchestrator.review_reel_post", new_callable=AsyncMock, return_value=_review_pass()):
                        result = await run_pipeline(config, tmp_db)

        assert result.success is True
        assert result.video is not None


class TestRunPipelineRetries:
    async def test_retry_image(self, make_account_config, tmp_db, tmp_path):
        config = make_account_config()
        img1 = _image(tmp_path, "img1.jpg")
        img2 = _image(tmp_path, "img2.jpg")

        with patch("agents.orchestrator.generate_brief", new_callable=AsyncMock, return_value=_brief()):
            with patch("agents.orchestrator.source_image", new_callable=AsyncMock, side_effect=[img1, img2]):
                with patch("agents.orchestrator.generate_caption", new_callable=AsyncMock, return_value=_caption()):
                    with patch("agents.orchestrator.review_post", new_callable=AsyncMock,
                               side_effect=[_review_fail(RETRY_IMAGE), _review_pass()]):
                        result = await run_pipeline(config, tmp_db)

        assert result.success is True

    async def test_retry_caption(self, make_account_config, tmp_db, tmp_path):
        config = make_account_config()
        img = _image(tmp_path)

        with patch("agents.orchestrator.generate_brief", new_callable=AsyncMock, return_value=_brief()):
            with patch("agents.orchestrator.source_image", new_callable=AsyncMock, return_value=img):
                with patch("agents.orchestrator.generate_caption", new_callable=AsyncMock,
                           side_effect=[_caption(), _caption()]):
                    with patch("agents.orchestrator.review_post", new_callable=AsyncMock,
                               side_effect=[_review_fail(RETRY_CAPTION), _review_pass()]):
                        result = await run_pipeline(config, tmp_db)

        assert result.success is True

    async def test_retry_video(self, make_account_config, tmp_db, tmp_path):
        config = make_account_config()
        vid1 = _video(tmp_path, "v1.mp4")
        vid2 = _video(tmp_path, "v2.mp4")

        with patch("agents.orchestrator.generate_brief", new_callable=AsyncMock, return_value=_brief("reel")):
            with patch("agents.orchestrator.source_video", new_callable=AsyncMock, side_effect=[vid1, vid2]):
                with patch("agents.orchestrator.generate_caption", new_callable=AsyncMock, return_value=_caption()):
                    with patch("agents.orchestrator.review_reel_post", new_callable=AsyncMock,
                               side_effect=[_review_fail(RETRY_VIDEO), _review_pass()]):
                        result = await run_pipeline(config, tmp_db)

        assert result.success is True

    async def test_max_retries_exhausted(self, make_account_config, tmp_db, tmp_path):
        config = make_account_config()
        img = _image(tmp_path)

        with patch("agents.orchestrator.generate_brief", new_callable=AsyncMock, return_value=_brief()):
            with patch("agents.orchestrator.source_image", new_callable=AsyncMock, return_value=img):
                with patch("agents.orchestrator.generate_caption", new_callable=AsyncMock, return_value=_caption()):
                    with patch("agents.orchestrator.review_post", new_callable=AsyncMock,
                               return_value=_review_fail(RETRY_CAPTION)):
                        result = await run_pipeline(config, tmp_db)

        assert result.success is False
        assert result.review is not None
        assert result.review.status == STATUS_FAIL


class TestRunPipelineErrors:
    async def test_exception_returns_error(self, make_account_config, tmp_db):
        config = make_account_config()

        with patch("agents.orchestrator.generate_brief", new_callable=AsyncMock,
                   side_effect=RuntimeError("AI is down")):
            result = await run_pipeline(config, tmp_db)

        assert result.success is False
        assert result.error is not None
        assert "AI is down" in result.error

    async def test_dry_run_flag(self, make_account_config, tmp_db, tmp_path):
        config = make_account_config()
        img = _image(tmp_path)

        with patch("agents.orchestrator.generate_brief", new_callable=AsyncMock, return_value=_brief()):
            with patch("agents.orchestrator.source_image", new_callable=AsyncMock, return_value=img):
                with patch("agents.orchestrator.generate_caption", new_callable=AsyncMock, return_value=_caption()):
                    with patch("agents.orchestrator.review_post", new_callable=AsyncMock, return_value=_review_pass()):
                        result = await run_pipeline(config, tmp_db, dry_run=True)

        assert result.success is True
