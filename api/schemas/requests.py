"""Pydantic request models for API input validation."""
import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# Item 4a — the five roles the staff schema locks via CHECK constraint.
# Kept in sync with tools/setup_staff_schema.py::TARGET_ROLES.
StaffRole = Literal[
    "Director",
    "Music",
    "Original Creator",
    "Series Composition",
    "Character Design",
]


class StaffPersonChip(BaseModel):
    """One person chip in a Staff/Studio search request.

    role=None means "any role for this person" — the chip filters on
    person_mal_id alone. role=<X> scopes the filter to that specific role
    (Director · Watanabe, Music · Kanno). Chips OR each other, so the
    payload can carry multiple people with different role scopes.
    """
    person_mal_id: int = Field(..., gt=0)
    role: StaffRole | None = None


class StaffFilterPayload(BaseModel):
    """Chip payload for Staff/Studio search mode (Item 4a).

    Semantics:
      * person chips OR each other (each carrying its own optional role
        scope);
      * studio chips OR each other;
      * the persons set ANDs with the studios set when both are present.

    At least one chip must be present — validated at the request level.
    """
    persons: list[StaffPersonChip] = Field(default_factory=list, max_length=10)
    studios: list[str] = Field(default_factory=list, max_length=5)

    @field_validator("studios")
    @classmethod
    def sanitize_studios(cls, v):
        cleaned = [s.strip() for s in v if s and s.strip()]
        if any(len(s) > 100 for s in cleaned):
            raise ValueError("Each studio name must be 100 characters or fewer")
        return cleaned


class SearchRequest(BaseModel):
    # Item 4a: query becomes optional. Staff/Studio mode carries intent
    # via staff_filter chips, so the free-text query field is empty on
    # that path. IS and OR still require a non-empty query — enforced
    # by validate_mode_requirements below.
    query: str = Field(default="", max_length=500)
    lens: str = Field(default="Intelligent Search")
    year_min: int | None = Field(default=None, ge=1900, le=2100)
    year_max: int | None = Field(default=None, ge=1900, le=2100)
    era_label: str | None = Field(default=None)
    rating_max: str | None = Field(default=None)
    # PR C — watchlist exclusion. Raw MAL IDs from the user's MAL or
    # AniList list (or both). The engine expands these to full series_id
    # clusters before applying the exclusion (PR #77 fold-in), so a
    # user with "Naruto" listed excludes the whole 19-entry Naruto
    # cluster, not just MAL ID 20. Max 5,000 entries — generous ceiling
    # for the heaviest MAL profiles (top public lists run ~2,000-3,000).
    exclude_mal_ids: list[int] | None = Field(default=None, max_length=5000)
    # Item 4a — chip payload for Staff/Studio mode. None on IS / OR
    # paths. When lens="Staff/Studio", must contain at least one chip.
    staff_filter: StaffFilterPayload | None = Field(default=None)

    @field_validator("lens")
    @classmethod
    def validate_lens(cls, v):
        allowed = {"Intelligent Search", "Objective Rankings", "Staff/Studio"}
        if v not in allowed:
            raise ValueError(f"lens must be one of {allowed}")
        return v

    @field_validator("rating_max")
    @classmethod
    def validate_rating_max(cls, v):
        # Closed set — R+ and Rx are intentionally not exposed as user
        # ceilings (R already excludes them, and R is the strictest cap a
        # user would meaningfully pick).
        if v is None:
            return None
        allowed = {"G", "PG", "PG-13", "R"}
        if v not in allowed:
            raise ValueError(f"rating_max must be one of {sorted(allowed)} or null")
        return v

    @field_validator("query")
    @classmethod
    def sanitize_query(cls, v):
        # Strip control characters, null bytes
        return re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', v.strip())

    @field_validator("exclude_mal_ids")
    @classmethod
    def sanitize_exclude_ids(cls, v):
        if v is None:
            return None
        cleaned = [int(i) for i in v if isinstance(i, int) and i > 0]
        return cleaned or None

    @model_validator(mode="after")
    def validate_mode_requirements(self):
        """Cross-field: query is required for IS/OR, chips required for
        Staff/Studio. Prevents an empty request from silently hitting the
        engine with no intent."""
        if self.lens == "Staff/Studio":
            has_chips = self.staff_filter and (
                self.staff_filter.persons or self.staff_filter.studios
            )
            if not has_chips:
                raise ValueError(
                    "Staff/Studio mode requires at least one person or studio chip"
                )
        else:
            if not self.query:
                raise ValueError(
                    "query is required for Intelligent Search and Objective Rankings modes"
                )
        return self


class SearchFeedbackRequest(BaseModel):
    search_id: str = Field(..., min_length=1, max_length=100)
    rating: str = Field(...)

    @field_validator("search_id")
    @classmethod
    def validate_search_id(cls, v):
        if not re.match(r'^[a-zA-Z0-9\-]+$', v):
            raise ValueError("Invalid search_id format")
        return v

    @field_validator("rating")
    @classmethod
    def validate_rating(cls, v):
        if v not in {"up", "down"}:
            raise ValueError("rating must be 'up' or 'down'")
        return v


class DNARequest(BaseModel):
    titles: list[str] = Field(..., min_length=1, max_length=3)
    negative_anchors: list[str] | None = Field(default=None, max_length=2)
    # PR C — watchlist exclusion (same shape as SearchRequest). See
    # SearchRequest.exclude_mal_ids for the cluster-expansion contract.
    exclude_mal_ids: list[int] | None = Field(default=None, max_length=5000)

    @field_validator("titles")
    @classmethod
    def validate_titles(cls, v):
        cleaned = [t.strip() for t in v if t.strip()]
        if not cleaned:
            raise ValueError("At least one title is required")
        if any(len(t) > 100 for t in cleaned):
            raise ValueError("Each title must be 100 characters or fewer")
        return cleaned

    @field_validator("negative_anchors")
    @classmethod
    def validate_negative_anchors(cls, v):
        if v is None:
            return None
        cleaned = [t.strip() for t in v if t.strip()]
        if any(len(t) > 100 for t in cleaned):
            raise ValueError("Each negative anchor must be 100 characters or fewer")
        # Return None if all entries were empty strings — keeps downstream logic simple
        return cleaned if cleaned else None

    @field_validator("exclude_mal_ids")
    @classmethod
    def sanitize_exclude_ids(cls, v):
        if v is None:
            return None
        cleaned = [int(i) for i in v if isinstance(i, int) and i > 0]
        return cleaned or None
