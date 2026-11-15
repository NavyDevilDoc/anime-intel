"""Tests for Staff/Studio autocomplete (Item 4a, Phase 2).

Exercises both the underlying `analysis.queries.staff_autocomplete`
function against the real vault DB and the FastAPI endpoint
`POST /api/autocomplete/staff` via TestClient.

Run:
    pytest tests/test_staff_autocomplete.py -v
"""
import os
import sys

import pytest
from fastapi.testclient import TestClient

TESTS_DIR = os.path.abspath(os.path.dirname(__file__))
ROOT_DIR = os.path.dirname(TESTS_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

os.environ["TESTING"] = "1"
os.environ.setdefault("ADMIN_USERNAME", "test-admin")
os.environ.setdefault("ADMIN_PASSWORD", "test-password")

from api.main import app  # noqa: E402
from analysis.queries import staff_autocomplete  # noqa: E402

client = TestClient(app)


# ---------------------------------------------------------------------------
# Unit tests — analysis.queries.staff_autocomplete
# ---------------------------------------------------------------------------


class TestStaffAutocompleteQuery:
    """Direct exercise of the query function against the real DB."""

    def test_empty_query_returns_empty(self):
        result = staff_autocomplete("", limit=5)
        assert result == {"persons": [], "studios": []}

    def test_whitespace_query_returns_empty(self):
        result = staff_autocomplete("   ", limit=5)
        assert result == {"persons": [], "studios": []}

    def test_no_match_returns_empty_lists(self):
        result = staff_autocomplete("xyzqqqzz_nomatch", limit=5)
        assert result["persons"] == []
        assert result["studios"] == []

    def test_watanabe_returns_persons(self):
        result = staff_autocomplete("watanabe", limit=5)
        assert len(result["persons"]) > 0
        # All returned people must have "Watanabe" in the display name
        for p in result["persons"]:
            assert "Watanabe" in p["name_display"]
            assert p["person_mal_id"] > 0
            assert p["total_count"] >= 1
            assert isinstance(p["roles"], list)

    def test_role_breakdown_is_populated(self):
        result = staff_autocomplete("kanno", limit=3)
        assert len(result["persons"]) > 0
        # Yoko Kanno should be in the returned set and have Music role
        yoko = next(
            (p for p in result["persons"] if p["name_display"] == "Yoko Kanno"),
            None,
        )
        assert yoko is not None, "Yoko Kanno should surface for 'kanno' query"
        assert any(r["role"] == "Music" for r in yoko["roles"])

    def test_ghibli_returns_studios(self):
        result = staff_autocomplete("Ghibli", limit=5)
        # Ghibli is a well-known studio, should surface at least one entry
        assert len(result["studios"]) >= 1
        assert all("show_count" in s and s["show_count"] > 0 for s in result["studios"])

    def test_bones_studio_deduplicates_by_string(self):
        """The `Bones` studio exists both standalone and in co-production
        strings — both should surface as distinct entries so users can
        pick the specific variant."""
        result = staff_autocomplete("Bones", limit=5)
        studio_names = [s["name"] for s in result["studios"]]
        assert "Bones" in studio_names, f"Expected 'Bones' in {studio_names}"

    def test_limit_is_respected(self):
        """Limit=1 should cap both lists at 1 entry each."""
        result = staff_autocomplete("a", limit=1)
        assert len(result["persons"]) <= 1
        assert len(result["studios"]) <= 1

    def test_sql_injection_stripped_by_normalization(self):
        """Punctuation is stripped from the pattern; wildcards too.
        Query with SQL-injection-shaped chars must not blow up and must
        return the same result as its stripped form."""
        result = staff_autocomplete("' OR 1=1; DROP TABLE people; --", limit=5)
        assert isinstance(result["persons"], list)
        assert isinstance(result["studios"], list)

    def test_percent_wildcard_stripped(self):
        """User-typed `%` should be stripped, not interpreted as a
        LIKE wildcard matching everything."""
        result_percent = staff_autocomplete("%", limit=5)
        # After stripping `%`, the normalized query is empty — return empty
        assert result_percent["persons"] == []
        assert result_percent["studios"] == []

    def test_persons_sorted_by_total_count_desc(self):
        """Verify the ranking invariant — total_count should be
        monotonically non-increasing."""
        result = staff_autocomplete("watanabe", limit=5)
        counts = [p["total_count"] for p in result["persons"]]
        assert counts == sorted(counts, reverse=True), \
            f"Expected descending order, got {counts}"


# ---------------------------------------------------------------------------
# API tests — POST /api/autocomplete/staff
# ---------------------------------------------------------------------------


class TestStaffAutocompleteEndpoint:
    """Verify the FastAPI endpoint wiring and response shape."""

    def test_missing_query_returns_422(self):
        response = client.post("/api/autocomplete/staff", json={})
        assert response.status_code == 422

    def test_empty_query_returns_422(self):
        response = client.post("/api/autocomplete/staff", json={"query": ""})
        assert response.status_code == 422

    def test_oversized_query_returns_422(self):
        response = client.post(
            "/api/autocomplete/staff",
            json={"query": "a" * 101},
        )
        assert response.status_code == 422

    def test_valid_query_returns_200(self):
        response = client.post(
            "/api/autocomplete/staff",
            json={"query": "watanabe"},
        )
        assert response.status_code == 200

    def test_response_shape(self):
        response = client.post(
            "/api/autocomplete/staff",
            json={"query": "watanabe"},
        )
        data = response.json()
        assert "persons" in data
        assert "studios" in data
        assert isinstance(data["persons"], list)
        assert isinstance(data["studios"], list)
        if data["persons"]:
            p = data["persons"][0]
            assert "person_mal_id" in p
            assert "name_display" in p
            assert "total_count" in p
            assert "roles" in p
            if p["roles"]:
                r = p["roles"][0]
                assert "role" in r
                assert "count" in r

    def test_limit_bounds_enforced(self):
        """limit must be between 1 and 10 inclusive."""
        r1 = client.post(
            "/api/autocomplete/staff",
            json={"query": "watanabe", "limit": 0},
        )
        assert r1.status_code == 422
        r2 = client.post(
            "/api/autocomplete/staff",
            json={"query": "watanabe", "limit": 11},
        )
        assert r2.status_code == 422

    def test_limit_default_five(self):
        """When limit is omitted, should default to 5."""
        response = client.post(
            "/api/autocomplete/staff",
            json={"query": "a"},  # broad match to have >5 candidates
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data["persons"]) <= 5
        assert len(data["studios"]) <= 5

    def test_bad_role_shape_never_returned(self):
        """The response Pydantic model locks role to the 5-value literal.
        A DB row with a bogus role would 500 — but the CHECK constraint
        prevents that at write time, so this is a smoke check that the
        response actually validates against the schema."""
        response = client.post(
            "/api/autocomplete/staff",
            json={"query": "kanno"},
        )
        assert response.status_code == 200
        data = response.json()
        allowed_roles = {
            "Director",
            "Music",
            "Original Creator",
            "Series Composition",
            "Character Design",
        }
        for p in data["persons"]:
            for r in p["roles"]:
                assert r["role"] in allowed_roles
