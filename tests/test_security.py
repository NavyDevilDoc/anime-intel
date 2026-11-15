"""
Security tests: read-only enforcement and input boundary validation.
These tests run without any API keys — they only touch the local database and code paths.

Note: SQL-injection red-team tests were removed when the SQL-generation path was
replaced by the FAISS RAG pipeline (commit 06cd975). There's no longer a code
path that executes LLM-generated SQL, so the historical injection attack surface
no longer exists. Read-only enforcement below still guards against any future
regression on the parameterized query interface.
"""

import os
import sys
import sqlite3
import pytest

TESTS_DIR = os.path.abspath(os.path.dirname(__file__))
ROOT_DIR = os.path.dirname(TESTS_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import analysis.queries as queries


# =====================================================================
# READ-ONLY ENFORCEMENT
# =====================================================================

class TestReadOnlyEnforcement:
    """Verify that the database connections enforce read-only mode."""

    def test_queries_module_readonly(self):
        """queries.py must open DB in read-only URI mode."""
        conn = queries._get_readonly_connection()
        try:
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                conn.execute("INSERT INTO anime_info (id) VALUES (999999)")
        finally:
            conn.close()

    def test_vector_store_readonly(self):
        """vector_store.py must open DB in read-only URI mode."""
        from analysis.vector_store import _get_readonly_connection
        conn = _get_readonly_connection()
        try:
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                conn.execute("INSERT INTO anime_info (id) VALUES (999999)")
        finally:
            conn.close()


# =====================================================================
# INPUT BOUNDARY TESTING
# =====================================================================

class TestInputBoundaries:
    """Verify that edge-case inputs don't crash the system."""

    def test_empty_query_returns_empty(self):
        """An empty string should not crash — should return empty or no results."""
        result = queries.resolve_show_title("")
        assert isinstance(result, list)

    def test_special_characters_handled(self):
        """SQL metacharacters in input must not cause crashes."""
        dangerous_inputs = [
            "'; DROP TABLE anime_info; --",
            "<script>alert('xss')</script>",
            "' OR '1'='1",
            "Robert'); DROP TABLE Students;--",
            "\x00\x01\x02",  # null bytes
        ]
        for inp in dangerous_inputs:
            result = queries.resolve_show_title(inp)
            assert isinstance(result, list), f"Crashed on input: {repr(inp)}"

    def test_unicode_input_handled(self):
        """Unicode characters (Japanese, emoji) must not cause crashes."""
        unicode_inputs = [
            "進撃の巨人",       # Attack on Titan in Japanese
            "🔥 anime 🔥",
            "Stein's;Gate",     # semicolon in title
        ]
        for inp in unicode_inputs:
            result = queries.resolve_show_title(inp)
            assert isinstance(result, list), f"Crashed on unicode input: {repr(inp)}"

    def test_oversized_input_handled(self):
        """A very long input string must not cause crashes or hangs."""
        long_input = "a" * 10000
        result = queries.resolve_show_title(long_input)
        assert isinstance(result, list)
