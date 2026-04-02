"""Tests for agents/content_planner.py — pure logic."""

from agents.content_planner import _normalize_pillar


class TestNormalizePillar:
    def test_spaces_to_underscores(self):
        assert _normalize_pillar("farm spotlight") == "farm_spotlight"

    def test_hyphens_to_underscores(self):
        assert _normalize_pillar("farm-spotlight") == "farm_spotlight"

    def test_mixed_case(self):
        assert _normalize_pillar("Farm Spotlight") == "farm_spotlight"

    def test_already_normalized(self):
        assert _normalize_pillar("recipes") == "recipes"

    def test_leading_trailing_spaces(self):
        assert _normalize_pillar("  recipes  ") == "recipes"

    def test_mixed_separators(self):
        assert _normalize_pillar("Farm - Spotlight") == "farm___spotlight"
