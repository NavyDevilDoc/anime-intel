"""
Pytest configuration and shared fixtures for the Anime Intelligence Vault test suite.
"""

import os
import sys
import sqlite3
import pytest

# --- PATH ANCHORING ---
TESTS_DIR = os.path.abspath(os.path.dirname(__file__))
ROOT_DIR = os.path.dirname(TESTS_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

# Any Gemini calls that escape mocks during tests should attribute to
# `test` instead of polluting production attribution. The wrapper module
# also short-circuits the Supabase dual-write when TESTING=1, so this
# env var is belt-and-suspenders alongside that.
os.environ.setdefault("ANIME_INTEL_CALL_ORIGIN", "test")

# JikanClient() now fails fast if JIKAN_BASE_URL is unset (public Jikan
# fallback was retired because the upstream is in maintenance mode).
# Dev environments typically have this set to the self-hosted Railway
# URL; tests that don't exercise real Jikan calls get a mock URL that
# won't be hit (the HTTP layer is patched in individual tests).
# `setdefault` means a developer's local Railway URL is preserved if
# already set.
os.environ.setdefault("JIKAN_BASE_URL", "https://test-jikan.example.com/v4")

DB_PATH = os.path.join(ROOT_DIR, "data", "anime_intelligence_v2.db")


# --- AUTH BYPASS UTILITIES ---
# Search/dna/encyclopedia endpoints require auth. Tests that exercise these
# endpoints (but aren't testing auth itself) use authed_client() which injects
# a fake user via dependency override. Tests that verify auth behavior use
# the raw app without overrides.
FAKE_USER = {"sub": "test-user-id", "email": "test@test.com", "role": "authenticated"}


async def _fake_require_auth():
    return FAKE_USER


@pytest.fixture(scope="session")
def db_path():
    """Path to the SQLite database."""
    assert os.path.exists(DB_PATH), f"Database not found at {DB_PATH}"
    return DB_PATH


@pytest.fixture(scope="session")
def db_row_count(db_path):
    """Baseline row count — used to verify no data mutation occurred."""
    with sqlite3.connect(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM anime_info").fetchone()[0]
    assert count > 0, "Database is empty"
    return count


@pytest.fixture
def db_conn(db_path):
    """Open a SQLite connection to the vault DB for the duration of the
    test, then close it. Replaces the try/finally boilerplate around every
    read-only query."""
    conn = sqlite3.connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def engine():
    """Fresh RecommendationEngine with a dummy API key. Every test that
    uses this fixture mocks the LLM plumbing before making a call, so the
    key is never dereferenced. Function-scoped so patch.object mutations
    can't leak across tests."""
    from analysis.recommendation_engine import RecommendationEngine
    return RecommendationEngine(api_key="test-not-used")
