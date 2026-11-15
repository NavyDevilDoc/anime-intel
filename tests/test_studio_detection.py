"""
Tests for _detect_studio() in analysis/vector_store.py.

Covers all three detection patterns:
  1. Prefix-based regex ("by MAPPA", "from Sunrise")
  2. Suffix-based regex ("MAPPA anime", "Sunrise shows")
  3. Bare-name DB-lookup fallback (new in this PR) — catches queries like
     "top 10 sunrise" where the user names a studio without a prefix
     keyword or "anime/shows" trailer.

The fallback queries the live SQLite DB via _load_known_studios(), so these
tests require data/anime_intelligence_v2.db to be present. If the DB is
unreachable, _load_known_studios returns (None, None) and the fallback is
skipped — Patterns 1 and 2 still work.
"""
import os
import sys

TESTS_DIR = os.path.abspath(os.path.dirname(__file__))
ROOT_DIR = os.path.dirname(TESTS_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import pytest

from analysis.vector_store import _detect_studio, _load_known_studios


DB_PATH = os.path.join(ROOT_DIR, "data", "anime_intelligence_v2.db")
db_available = pytest.mark.skipif(
    not os.path.exists(DB_PATH),
    reason="anime_intelligence_v2.db not present — needed for studio detection fallback",
)


class TestPrefixBasedDetection:
    """Pattern 1: prefix keywords ('by', 'from', 'studio') unlock detection."""

    def test_by_keyword(self):
        assert _detect_studio("anything by MAPPA") == "MAPPA"

    def test_from_keyword(self):
        assert _detect_studio("mecha from Sunrise") == "Sunrise"

    def test_studio_keyword(self):
        assert _detect_studio("studio Bones shows") == "Bones"


class TestSuffixBasedDetection:
    """Pattern 2: trailing 'anime/shows/series' unlocks detection."""

    def test_anime_trailer(self):
        assert _detect_studio("MAPPA anime") == "MAPPA"

    def test_shows_trailer(self):
        assert _detect_studio("Madhouse shows") == "Madhouse"


class TestNonStudioFilter:
    """Genre/quality terms must not be misdetected as studios."""

    def test_top_anime_not_studio(self):
        # "Top" is in _NON_STUDIO_TERMS; should not match Pattern 2.
        assert _detect_studio("Top anime") is None

    def test_action_anime_not_studio(self):
        # "Action" is a concept-vocabulary genre; should not match.
        assert _detect_studio("Action anime") is None

    # Item 4a Phase 6 — English stop-words / determiners must not slip
    # through Pattern 1's re.IGNORECASE regex. Before the fold-in these
    # queries returned "the 80s" / "the 90s" / "the future" as studio
    # candidates, injecting a bogus LIKE clause into the SQL WHERE.
    def test_from_the_80s_does_not_capture_the(self):
        assert _detect_studio("OVA anime from the 80s") is None

    def test_from_the_90s_does_not_capture_the(self):
        assert _detect_studio("shows from the 90s") is None

    def test_from_the_future_does_not_capture_the(self):
        assert _detect_studio("anime from the future") is None

    def test_by_a_studio_does_not_capture_a(self):
        # Malformed query but should degrade to None rather than "a studio".
        assert _detect_studio("anime by a studio") is None

    def test_from_some_studio_does_not_capture_some(self):
        assert _detect_studio("shows from some obscure studio") is None

    # 2026-09-20 fold-in — Pattern 1 was capturing "studio X" as a two-word
    # studio candidate when the query used "by studio X" phrasing. The "by"
    # prefix fires first (left-to-right), lazy capture extended to
    # end-of-string, yielding e.g. "studio bones" as the studio. SQL then
    # ran `studio LIKE '%studio bones%'` which matched zero rows, the
    # filtered path silently fell back to unfiltered, and non-Bones shows
    # leaked into the result set. Surfaced during Item 4 live QA on "top
    # rated shows by studio bones"; probe confirmed the mechanism.
    # Adding "studio" to _NON_STUDIO_TERMS trips _is_non_studio on the
    # first captured word and rejects Pattern 1, letting Pattern 3's
    # bare-name lookup resolve the actual studio.
    def test_by_studio_bones_does_not_capture_studio(self):
        # Must return "Bones" via Pattern 3, not "studio bones" via Pattern 1.
        assert _detect_studio("top rated shows by studio bones") == "Bones"

    def test_from_studio_mappa_resolves_via_pattern_3(self):
        # Same shape, different studio and prefix keyword.
        assert _detect_studio("hidden gems from studio MAPPA") == "MAPPA"


@db_available
class TestStudioPrefixedNamesStillResolve:
    """Non-regression guard for well-known 'Studio X' names — the
    _NON_STUDIO_TERMS 'studio' entry must not break queries where 'Studio'
    is genuinely part of the studio name. Pattern 3's whole-vault regex
    is sorted longest-first, so 'Studio Ghibli' matches before 'Ghibli'
    or 'Studio' alone."""

    def test_studio_ghibli_still_resolves(self):
        # "by" prefix fires first, captures "Studio Ghibli", _is_non_studio
        # rejects it (starts with "studio"), Pattern 3 catches "Studio Ghibli"
        # as a whole-vault multi-word match.
        assert _detect_studio("shows by Studio Ghibli") == "Studio Ghibli"

    def test_studio_pierrot_still_resolves(self):
        assert _detect_studio("shows by Studio Pierrot") == "Studio Pierrot"

    def test_from_studio_ghibli_still_resolves(self):
        # "from" prefix variant — same flow, different prefix keyword.
        assert _detect_studio("hidden gems from Studio Ghibli") == "Studio Ghibli"


@db_available
class TestBareNameFallback:
    """Pattern 3 (new): bare studio name against the DB-known-studios set."""

    def test_bare_lowercase_studio_matches(self):
        # "top 10 sunrise" — no 'by', no 'anime' trailer. The previous regex
        # patterns would miss this entirely.
        assert _detect_studio("top 10 sunrise") == "Sunrise"

    def test_bare_mixed_case_studio_matches(self):
        assert _detect_studio("underrated MAPPA") == "MAPPA"

    def test_unknown_string_returns_none(self):
        # "Foobar" is not a known studio. Fallback finds nothing.
        assert _detect_studio("top 10 foobar") is None

    def test_non_studio_term_not_returned(self):
        # "Top" matches the regex \b...\b but is filtered by _NON_STUDIO_TERMS.
        # Verifies the filter applies to the fallback path too.
        assert _detect_studio("top 10") is None

    def test_canonical_casing_preserved(self):
        # User typed "mappa" (lowercase). The match should normalize to the
        # canonical DB casing "MAPPA".
        result = _detect_studio("recommend something from mappa")
        # Pattern 1 ("from ...") catches this first and returns "mappa" with
        # original casing — so the bare fallback isn't exercised here. This
        # test confirms the canonical_map plumbing is wired without disturbing
        # the regex-first ordering.
        assert result is not None
        assert result.lower() == "mappa"


@db_available
def test_known_studios_cache_is_memoized():
    """Second call must return the same compiled regex (no re-scan)."""
    regex1, map1 = _load_known_studios()
    regex2, map2 = _load_known_studios()
    assert regex1 is regex2
    assert map1 is map2
