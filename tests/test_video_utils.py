"""Tests for utils/video_utils.py — pure validation logic."""

from utils.video_utils import VideoMetadata, validate_reel_specs


class TestValidateReelSpecs:
    def test_valid_reel(self):
        meta = VideoMetadata(duration_seconds=15.0, width=1080, height=1920, codec="h264")
        assert validate_reel_specs(meta) == []

    def test_too_short(self):
        meta = VideoMetadata(duration_seconds=1.0, width=1080, height=1920, codec="h264")
        violations = validate_reel_specs(meta)
        assert len(violations) == 1
        assert "below minimum" in violations[0]

    def test_too_long(self):
        meta = VideoMetadata(duration_seconds=120.0, width=1080, height=1920, codec="h264")
        violations = validate_reel_specs(meta)
        assert len(violations) == 1
        assert "exceeds maximum" in violations[0]

    def test_wrong_aspect_ratio(self):
        meta = VideoMetadata(duration_seconds=15.0, width=1920, height=1080, codec="h264")
        violations = validate_reel_specs(meta)
        assert len(violations) == 1
        assert "Aspect ratio" in violations[0]

    def test_multiple_violations(self):
        meta = VideoMetadata(duration_seconds=1.0, width=1920, height=1080, codec="h264")
        violations = validate_reel_specs(meta)
        assert len(violations) == 2

    def test_boundary_min_duration(self):
        meta = VideoMetadata(duration_seconds=3.0, width=1080, height=1920, codec="h264")
        assert validate_reel_specs(meta) == []

    def test_boundary_max_duration(self):
        meta = VideoMetadata(duration_seconds=90.0, width=1080, height=1920, codec="h264")
        assert validate_reel_specs(meta) == []

    def test_close_aspect_ratio_passes(self):
        # 9:16 = 0.5625, tolerance is 0.15 => anything in ~0.41-0.71 passes
        meta = VideoMetadata(duration_seconds=15.0, width=720, height=1280, codec="h264")
        assert validate_reel_specs(meta) == []

    def test_zero_dimensions_skips_aspect_check(self):
        meta = VideoMetadata(duration_seconds=15.0, width=0, height=0, codec="h264")
        assert validate_reel_specs(meta) == []
