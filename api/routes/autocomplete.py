"""Autocomplete endpoints for chip-based inputs.

Currently backs the Staff/Studio search mode (Item 4a) — 150ms-debounced
type-ahead against `people.name_normalized` + distinct
`anime_info.studio` strings. Falls under the global per-IP rate limit
in `api.middleware.rate_limit`; no per-endpoint quota — keystroke
volume would exhaust the search-level 30/day cap in a single interaction.
"""
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

from analysis.queries import staff_autocomplete

router = APIRouter()


# Kept in sync with tools/setup_staff_schema.py::TARGET_ROLES and
# api/schemas/requests.py::StaffRole. Duplicated inline here so the
# response schema doesn't import from a request-model module.
StaffRole = Literal[
    "Director",
    "Music",
    "Original Creator",
    "Series Composition",
    "Character Design",
]


class StaffAutocompleteRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=100)
    # Bounded by the type-ahead spec (10 suggestions per keystroke, 5 per
    # bucket in the current split). Cap at 10 to prevent pathological
    # queries from returning huge payloads.
    limit: int = Field(default=5, ge=1, le=10)


class RoleBreakdown(BaseModel):
    role: StaffRole
    count: int


class PersonSuggestion(BaseModel):
    person_mal_id: int
    name_display: str
    total_count: int
    roles: list[RoleBreakdown]


class StudioSuggestion(BaseModel):
    name: str
    show_count: int


class StaffAutocompleteResponse(BaseModel):
    persons: list[PersonSuggestion]
    studios: list[StudioSuggestion]


@router.post(
    "/api/autocomplete/staff",
    response_model=StaffAutocompleteResponse,
)
def staff_autocomplete_endpoint(req: StaffAutocompleteRequest):
    """Chip type-ahead for Staff/Studio search mode.

    Returns per-person role breakdowns so the frontend renders the
    pinned "Any role" option + per-role scoped chips without a second
    round-trip. Studios returned verbatim (raw column strings) — the
    Staff/Studio search filter uses LIKE against them, so a "Bones"
    pick catches co-production strings like "Bones, Studio Rikka".
    """
    result = staff_autocomplete(req.query, limit=req.limit)
    return StaffAutocompleteResponse(**result)
