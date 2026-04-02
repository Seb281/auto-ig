"""Tests for agents/reviewer.py — pure logic (no async, no I/O)."""

from agents.reviewer import _check_caption_banned_topics


class TestCheckCaptionBannedTopics:
    def test_no_match(self, make_account_config):
        config = make_account_config()
        violations = _check_caption_banned_topics(
            "A delicious veggie bowl", ["healthy", "organic"], config
        )
        assert violations == []

    def test_single_match_in_caption(self, make_account_config):
        config = make_account_config()
        violations = _check_caption_banned_topics(
            "Pair with a glass of alcohol", ["food"], config
        )
        assert len(violations) == 1
        assert "alcohol" in violations[0]

    def test_match_in_hashtags(self, make_account_config):
        config = make_account_config()
        violations = _check_caption_banned_topics(
            "Great veggie dish", ["tobacco_free", "healthy"], config
        )
        assert len(violations) == 1
        assert "tobacco" in violations[0]

    def test_multiple_matches(self, make_account_config):
        config = make_account_config()
        violations = _check_caption_banned_topics(
            "alcohol and tobacco are bad", [], config
        )
        assert len(violations) == 2

    def test_case_insensitive(self, make_account_config):
        config = make_account_config()
        violations = _check_caption_banned_topics(
            "ALCOHOL content here", [], config
        )
        assert len(violations) == 1

    def test_empty_caption_no_match(self, make_account_config):
        config = make_account_config()
        violations = _check_caption_banned_topics("", [], config)
        assert violations == []
