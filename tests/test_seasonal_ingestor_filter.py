"""Unit tests for src/seasonal_ingestor_v2._filter_by_media_type.

Option C (PR #171) widened the seasonal ingest media-type whitelist
from TV-only to {tv, movie, ova, ona, tv_special, special}, so new
seasonal Movies / OVAs / Specials vault natively instead of requiring
a separate popularity-backfill pass (Phase 3 pattern).

The filter is tested as a pure function so these cases run without
spinning up MALClient / JikanClient / Gemini. Full-pipeline integration
coverage for ingest_season remains a gap (never had any), but the
whitelist semantics — the whole point of Option C — are nailed down
here with a known fixture.
"""
import os
import sys

import pytest

TESTS_DIR = os.path.abspath(os.path.dirname(__file__))
ROOT_DIR = os.path.dirname(TESTS_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from src.seasonal_ingestor_v2 import (  # noqa: E402
    SEASONAL_ALLOWED_MEDIA_TYPES,
    _filter_by_media_type,
)


def _entry(mal_id: int, media_type: str) -> dict:
    """Minimal MAL /anime/season/ entry shape — only the fields the
    filter cares about. Real MAL responses carry much more per node
    (title, synopsis, mean, etc.) but the filter is media_type-only."""
    return {"node": {"id": mal_id, "media_type": media_type}}


class TestSeasonalAllowedMediaTypes:
    """Guard the default whitelist composition. If someone silently
    adds Music / CM / PV back in, these tests fail loudly — those were
    deliberately pruned in PR #168 and shouldn't re-enter the vault."""

    def test_default_whitelist_includes_tv(self):
        assert "tv" in SEASONAL_ALLOWED_MEDIA_TYPES

    def test_default_whitelist_includes_non_tv_subtypes(self):
        for subtype in ("movie", "ova", "ona", "tv_special", "special"):
            assert subtype in SEASONAL_ALLOWED_MEDIA_TYPES, (
                f"{subtype} missing from Option C default whitelist"
            )

    def test_default_whitelist_excludes_noise_subtypes(self):
        """Music / CM / PV were pruned in PR #168 and must stay out of
        the seasonal default — otherwise new seasons would reintroduce
        the exact noise we just cleaned up."""
        for noise in ("music", "cm", "pv"):
            assert noise not in SEASONAL_ALLOWED_MEDIA_TYPES, (
                f"Noise subtype {noise} must not be in the seasonal default whitelist"
            )

    def test_default_whitelist_is_frozen(self):
        """Enforced immutability — the whitelist is a module-level
        constant that should never mutate at runtime. frozenset catches
        any accidental .add() calls."""
        assert isinstance(SEASONAL_ALLOWED_MEDIA_TYPES, frozenset)


class TestFilterByMediaType:
    """The pure filter helper. Returns (passed, skipped) where passed
    is entries matching the whitelist and skipped is {mal_id: reason}."""

    def test_empty_page_returns_empty_lists(self):
        passed, skipped = _filter_by_media_type([], SEASONAL_ALLOWED_MEDIA_TYPES)
        assert passed == []
        assert skipped == {}

    def test_all_tv_passes_through(self):
        page = [_entry(1, "tv"), _entry(2, "tv"), _entry(3, "tv")]
        passed, skipped = _filter_by_media_type(page, SEASONAL_ALLOWED_MEDIA_TYPES)
        assert len(passed) == 3
        assert skipped == {}

    def test_mixed_subtypes_split_cleanly(self):
        """The Option C happy path — a modern season has Movies,
        OVAs, ONAs mixed with TV. All should pass the default filter."""
        page = [
            _entry(1, "tv"),
            _entry(2, "movie"),
            _entry(3, "ova"),
            _entry(4, "ona"),
            _entry(5, "tv_special"),
            _entry(6, "special"),
        ]
        passed, skipped = _filter_by_media_type(page, SEASONAL_ALLOWED_MEDIA_TYPES)
        assert len(passed) == 6
        assert skipped == {}

    def test_music_cm_pv_are_filtered(self):
        """Noise subtypes stay excluded — the whole point of keeping
        Music / CM / PV out of the default whitelist is that the
        vault we just pruned (PR #168) shouldn't refill with the same
        noise on the next seasonal run."""
        page = [
            _entry(1, "tv"),
            _entry(100, "music"),
            _entry(101, "cm"),
            _entry(102, "pv"),
        ]
        passed, skipped = _filter_by_media_type(page, SEASONAL_ALLOWED_MEDIA_TYPES)
        assert [e["node"]["id"] for e in passed] == [1]
        assert set(skipped.keys()) == {100, 101, 102}

    def test_unknown_media_type_filtered(self):
        """MAL occasionally returns unknown or novel media_types.
        Default behavior: skip, record in manifest for operator review."""
        page = [_entry(1, "tv"), _entry(99, "unknown"), _entry(100, "future_type")]
        passed, skipped = _filter_by_media_type(page, SEASONAL_ALLOWED_MEDIA_TYPES)
        assert [e["node"]["id"] for e in passed] == [1]
        assert 99 in skipped
        assert 100 in skipped
        assert skipped[99] == "media_type=unknown"
        assert skipped[100] == "media_type=future_type"

    def test_missing_media_type_treated_as_unknown(self):
        """Defensive: if MAL returns a node without media_type at all
        (shouldn't happen but defend anyway), filter as unknown rather
        than crashing."""
        page = [{"node": {"id": 1}}]  # no media_type key
        passed, skipped = _filter_by_media_type(page, SEASONAL_ALLOWED_MEDIA_TYPES)
        assert passed == []
        assert skipped == {1: "media_type=unknown"}

    def test_narrowing_via_parameter(self):
        """Design 3's debugging escape hatch: operator can narrow the
        whitelist to tv-only for a specific run without editing the
        module constant."""
        page = [
            _entry(1, "tv"),
            _entry(2, "movie"),
            _entry(3, "ova"),
        ]
        passed, skipped = _filter_by_media_type(page, frozenset({"tv"}))
        assert [e["node"]["id"] for e in passed] == [1]
        assert set(skipped.keys()) == {2, 3}

    def test_empty_whitelist_rejects_everything(self):
        """Degenerate but shouldn't crash — if an operator passes an
        empty frozenset, every entry gets categorized as skipped."""
        page = [_entry(1, "tv"), _entry(2, "movie")]
        passed, skipped = _filter_by_media_type(page, frozenset())
        assert passed == []
        assert len(skipped) == 2

    def test_skipped_dict_preserves_mal_id_as_key(self):
        """Manifest serialization expects int keys (converted to str
        at JSON write time). Filter must produce int-keyed dict so the
        manifest's existing to_dict() handling continues to work."""
        page = [_entry(42, "music")]
        _, skipped = _filter_by_media_type(page, SEASONAL_ALLOWED_MEDIA_TYPES)
        assert 42 in skipped
        assert isinstance(list(skipped.keys())[0], int)
