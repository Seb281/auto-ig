"""Tests for agents/reviewer.py — async review functions with mocked AI + DB."""

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.reviewer import (
    RETRY_CAPTION,
    RETRY_IMAGE,
    RETRY_VIDEO,
    STATUS_FAIL,
    STATUS_PASS,
    review_carousel_post,
    review_post,
    review_reel_post,
)


def _mock_vision_pass():
    """Return a mock for _vision_review that returns PASS."""
    from agents import ReviewResult
    return AsyncMock(return_value=ReviewResult(status=STATUS_PASS, reasons=[], retry_type=None))


def _mock_vision_fail(retry_type=RETRY_CAPTION):
    """Return a mock for _vision_review that returns FAIL."""
    from agents import ReviewResult
    return AsyncMock(return_value=ReviewResult(
        status=STATUS_FAIL,
        reasons=["Image quality is poor"],
        retry_type=retry_type,
    ))


# ---------------------------------------------------------------------------
# review_post
# ---------------------------------------------------------------------------

class TestReviewPost:
    async def test_pass_flow(self, make_account_config, make_planner_brief,
                              make_image_result, make_caption_result, tmp_db, tmp_path):
        config = make_account_config()
        brief = make_planner_brief()
        img_path = str(tmp_path / "test.jpg")
        with open(img_path, "wb") as f:
            f.write(b"\xff\xd8\xff" + b"\x00" * 100)
        image = make_image_result(local_path=img_path)
        caption = make_caption_result()

        with patch("agents.reviewer.is_duplicate_image", new_callable=AsyncMock, return_value=False):
            with patch("agents.reviewer._vision_review", _mock_vision_pass()):
                result = await review_post(config, brief, image, caption, tmp_db)

        assert result.status == STATUS_PASS
        assert result.reasons == []
        assert result.retry_type is None

    async def test_duplicate_detected(self, make_account_config, make_planner_brief,
                                       make_image_result, make_caption_result, tmp_db, tmp_path):
        config = make_account_config()
        brief = make_planner_brief()
        img_path = str(tmp_path / "test.jpg")
        with open(img_path, "wb") as f:
            f.write(b"\xff\xd8\xff" + b"\x00" * 100)
        image = make_image_result(local_path=img_path)
        caption = make_caption_result()

        with patch("agents.reviewer.is_duplicate_image", new_callable=AsyncMock, return_value=True):
            with patch("agents.reviewer._vision_review", _mock_vision_pass()):
                result = await review_post(config, brief, image, caption, tmp_db)

        assert result.status == STATUS_FAIL
        assert result.retry_type == RETRY_IMAGE

    async def test_banned_topic_in_caption(self, make_account_config, make_planner_brief,
                                            make_image_result, make_caption_result, tmp_db, tmp_path):
        config = make_account_config()
        brief = make_planner_brief()
        img_path = str(tmp_path / "test.jpg")
        with open(img_path, "wb") as f:
            f.write(b"\xff\xd8\xff" + b"\x00" * 100)
        image = make_image_result(local_path=img_path)
        caption = make_caption_result(caption="Try this with alcohol!")

        with patch("agents.reviewer.is_duplicate_image", new_callable=AsyncMock, return_value=False):
            with patch("agents.reviewer._vision_review", _mock_vision_pass()):
                result = await review_post(config, brief, image, caption, tmp_db)

        assert result.status == STATUS_FAIL
        assert result.retry_type == RETRY_CAPTION

    async def test_vision_fail_sets_retry_type(self, make_account_config, make_planner_brief,
                                                make_image_result, make_caption_result, tmp_db, tmp_path):
        config = make_account_config()
        brief = make_planner_brief()
        img_path = str(tmp_path / "test.jpg")
        with open(img_path, "wb") as f:
            f.write(b"\xff\xd8\xff" + b"\x00" * 100)
        image = make_image_result(local_path=img_path)
        caption = make_caption_result()

        with patch("agents.reviewer.is_duplicate_image", new_callable=AsyncMock, return_value=False):
            with patch("agents.reviewer._vision_review", _mock_vision_fail(RETRY_IMAGE)):
                result = await review_post(config, brief, image, caption, tmp_db)

        assert result.status == STATUS_FAIL
        assert result.retry_type == RETRY_IMAGE


# ---------------------------------------------------------------------------
# review_carousel_post
# ---------------------------------------------------------------------------

class TestReviewCarouselPost:
    async def test_pass_flow(self, make_account_config, make_planner_brief,
                              make_image_result, make_caption_result, tmp_db, tmp_path):
        config = make_account_config()
        brief = make_planner_brief(content_type="carousel")
        caption = make_caption_result()

        images = []
        for i in range(3):
            path = str(tmp_path / f"img{i}.jpg")
            with open(path, "wb") as f:
                f.write(b"\xff\xd8\xff" + b"\x00" * 100)
            images.append(make_image_result(local_path=path))

        with patch("agents.reviewer.is_duplicate_image", new_callable=AsyncMock, return_value=False):
            with patch("agents.reviewer._vision_review", _mock_vision_pass()):
                result = await review_carousel_post(config, brief, images, caption, tmp_db)

        assert result.status == STATUS_PASS

    async def test_per_slide_duplicate(self, make_account_config, make_planner_brief,
                                        make_image_result, make_caption_result, tmp_db, tmp_path):
        config = make_account_config()
        brief = make_planner_brief(content_type="carousel")
        caption = make_caption_result()

        images = []
        for i in range(3):
            path = str(tmp_path / f"img{i}.jpg")
            with open(path, "wb") as f:
                f.write(b"\xff\xd8\xff" + b"\x00" * 100)
            images.append(make_image_result(local_path=path))

        # Second image is a duplicate
        dup_returns = [False, True, False]
        with patch("agents.reviewer.is_duplicate_image", new_callable=AsyncMock, side_effect=dup_returns):
            with patch("agents.reviewer._vision_review", _mock_vision_pass()):
                result = await review_carousel_post(config, brief, images, caption, tmp_db)

        assert result.status == STATUS_FAIL
        assert result.retry_type == RETRY_IMAGE


# ---------------------------------------------------------------------------
# review_reel_post
# ---------------------------------------------------------------------------

class TestReviewReelPost:
    async def test_pass_flow(self, make_account_config, make_planner_brief,
                              make_video_result, make_caption_result, tmp_db, tmp_path):
        config = make_account_config()
        brief = make_planner_brief(content_type="reel")
        video_path = str(tmp_path / "test.mp4")
        with open(video_path, "wb") as f:
            f.write(b"\x00" * 100)
        video = make_video_result(local_path=video_path)
        caption = make_caption_result()

        with patch("agents.reviewer.is_duplicate_image", new_callable=AsyncMock, return_value=False):
            with patch("agents.reviewer.extract_thumbnail", new_callable=AsyncMock, return_value=str(tmp_path / "thumb.jpg")):
                # Create the thumb file for the vision review
                thumb_path = video_path + ".review_thumb.jpg"
                with open(thumb_path, "wb") as f:
                    f.write(b"\xff\xd8\xff" + b"\x00" * 100)
                with patch("agents.reviewer._vision_review", _mock_vision_pass()):
                    result = await review_reel_post(config, brief, video, caption, tmp_db)

        assert result.status == STATUS_PASS

    async def test_duplicate_video_retry_video(self, make_account_config, make_planner_brief,
                                                 make_video_result, make_caption_result, tmp_db, tmp_path):
        config = make_account_config()
        brief = make_planner_brief(content_type="reel")
        video_path = str(tmp_path / "test.mp4")
        with open(video_path, "wb") as f:
            f.write(b"\x00" * 100)
        video = make_video_result(local_path=video_path)
        caption = make_caption_result()

        with patch("agents.reviewer.is_duplicate_image", new_callable=AsyncMock, return_value=True):
            with patch("agents.reviewer.extract_thumbnail", new_callable=AsyncMock):
                thumb_path = video_path + ".review_thumb.jpg"
                with open(thumb_path, "wb") as f:
                    f.write(b"\xff\xd8\xff" + b"\x00" * 100)
                with patch("agents.reviewer._vision_review", _mock_vision_pass()):
                    result = await review_reel_post(config, brief, video, caption, tmp_db)

        assert result.status == STATUS_FAIL
        assert result.retry_type == RETRY_VIDEO

    async def test_vision_image_retry_remapped_to_video(self, make_account_config, make_planner_brief,
                                                          make_video_result, make_caption_result, tmp_db, tmp_path):
        config = make_account_config()
        brief = make_planner_brief(content_type="reel")
        video_path = str(tmp_path / "test.mp4")
        with open(video_path, "wb") as f:
            f.write(b"\x00" * 100)
        video = make_video_result(local_path=video_path)
        caption = make_caption_result()

        with patch("agents.reviewer.is_duplicate_image", new_callable=AsyncMock, return_value=False):
            with patch("agents.reviewer.extract_thumbnail", new_callable=AsyncMock):
                thumb_path = video_path + ".review_thumb.jpg"
                with open(thumb_path, "wb") as f:
                    f.write(b"\xff\xd8\xff" + b"\x00" * 100)
                # Vision review says RETRY_IMAGE
                with patch("agents.reviewer._vision_review", _mock_vision_fail(RETRY_IMAGE)):
                    result = await review_reel_post(config, brief, video, caption, tmp_db)

        assert result.status == STATUS_FAIL
        # Should be remapped to RETRY_VIDEO for reels
        assert result.retry_type == RETRY_VIDEO
