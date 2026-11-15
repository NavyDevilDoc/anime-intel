"""Seasonal ingest manifest — per-run outcome tracking.

Records exactly which MAL IDs the seasonal ingest touched and what
happened to each: skipped by the TV filter, processed successfully,
quarantined, or (bug) missed. Purpose is silent-miss visibility —
without this record, a show that MAL returned but our pipeline never
touched is invisible until a user complains.

Design refinement from Phase B (2026-09-25): `newly_vaulted` is a
first-class field so operators aren't misled by rescue counts. A show
already in `anime_info` with consensus that gets its consensus
refreshed shows up in `processed_success` but NOT `newly_vaulted`.

Manifest persistence path:
`data/seasonal_ingest_manifests/{run_id}_{Season}_{Year}.json`.
Committed via the seasonal-ingest workflow's `git add` step so
manifest history stays alongside the data it describes.

This module intentionally has no dependency on the ingestor — it
holds pure logic (dataclass + snapshots + outcome computation +
JSON write) so tests can exercise it without spinning up MALClient,
JikanClient, or the distiller.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

MANIFEST_SUBDIR = "seasonal_ingest_manifests"


def _iso_now() -> str:
    """ISO-8601 UTC timestamp safe for filenames (colons → hyphens)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


@dataclass
class SeasonalIngestManifest:
    """Per-invocation snapshot of what happened during ingest_season.

    Populated during the run; serialized to JSON when the run finalizes
    (success or failure). Field order in the dataclass matches the
    logical flow (source → filter → attempt → outcome) so the JSON
    on disk reads top-to-bottom the same way an operator diagnoses.
    """

    run_id: str
    year: int
    season: str              # lower-case, e.g. "fall"
    season_tag: str          # display form, e.g. "Fall_2026"
    target_ids_mode: bool    # True if target_ids filter was applied
    source_ids: list[int] = field(default_factory=list)
    skipped_non_tv: dict[int, str] = field(default_factory=dict)
    attempted_ids: list[int] = field(default_factory=list)
    processed_success: list[int] = field(default_factory=list)
    processed_quarantine: list[int] = field(default_factory=list)
    newly_vaulted: list[int] = field(default_factory=list)
    missed: list[int] = field(default_factory=list)

    @property
    def summary_line(self) -> str:
        """One-line summary printed at end of ingest_season and
        injected into the seasonal-ingest workflow's auto-PR body.

        MISSED gets bolded when non-zero — that's the bug signal
        operators need to notice, not a routine count.
        """
        miss_marker = (
            f"**MISSED={len(self.missed)}**" if self.missed else "MISSED=0"
        )
        return (
            f"MANIFEST {self.season_tag}: "
            f"source={len(self.source_ids)}, "
            f"skipped_non_tv={len(self.skipped_non_tv)}, "
            f"attempted={len(self.attempted_ids)}, "
            f"success={len(self.processed_success)} "
            f"(newly_vaulted={len(self.newly_vaulted)}), "
            f"quarantined={len(self.processed_quarantine)}, "
            f"{miss_marker}"
        )

    def to_dict(self) -> dict:
        """asdict() with sorted ID lists for deterministic diffs.

        Cross-run manifest comparison is a real diagnostic path
        (did this fall's ingest surface more misses than last fall's?)
        — sorted lists mean the diff shows only real differences,
        not ordering noise.
        """
        d = asdict(self)
        for key in (
            "source_ids", "attempted_ids", "processed_success",
            "processed_quarantine", "newly_vaulted", "missed",
        ):
            d[key] = sorted(d[key])
        # JSON keys must be strings; sort so diff-friendliness holds.
        d["skipped_non_tv"] = {
            str(k): v for k, v in sorted(d["skipped_non_tv"].items())
        }
        return d


def snapshot_vault_and_quarantine(db_path: str) -> tuple[set[int], set[int]]:
    """Read current sets of vaulted-with-consensus + quarantined mal_ids.

    Called twice per ingest_season — once at start (for `prior_vaulted`
    to compute `newly_vaulted`) and once at end (for `current_vaulted`
    / `current_quarantined` to compute outcomes). Two indexed queries;
    cheap even at 6000+ vault rows.

    `prior_vaulted` filters on `consensus_json IS NOT NULL` so a row
    that exists but was never distilled (unusual state — would signal
    a partial-write bug) doesn't get double-counted as a vault member.
    """
    with sqlite3.connect(db_path, timeout=20) as conn:
        c = conn.cursor()
        c.execute("SELECT id FROM anime_info WHERE consensus_json IS NOT NULL")
        vaulted = {row[0] for row in c.fetchall()}
        c.execute("SELECT mal_id FROM intelligence_quarantine")
        quarantined = {row[0] for row in c.fetchall()}
    return vaulted, quarantined


def compute_outcomes(
    attempted: list[int],
    current_vaulted: set[int],
    current_quarantined: set[int],
    prior_vaulted: set[int],
) -> tuple[list[int], list[int], list[int], list[int]]:
    """Bucket attempted MAL IDs into success / quarantine / newly_vaulted / missed.

    Pure function — no I/O, no side effects. Extracted so tests can
    exercise the accounting logic without any ingestor scaffolding.

    Returns lists (sorted, for stability) of:
        (processed_success, processed_quarantine, newly_vaulted, missed)

    `missed` is the bug detector — an attempted show that landed in
    NEITHER anime_info NOR intelligence_quarantine. Should always be
    empty in a healthy pipeline; non-zero means an ingest path lost
    track of a show without recording the failure.
    """
    attempted_set = set(attempted)
    success = attempted_set & current_vaulted
    quarantined = attempted_set & current_quarantined
    newly_vaulted = success - prior_vaulted
    missed = attempted_set - success - quarantined
    return (
        sorted(success),
        sorted(quarantined),
        sorted(newly_vaulted),
        sorted(missed),
    )


def write_manifest(manifest: SeasonalIngestManifest, root_dir: str) -> str:
    """Persist manifest JSON. Returns the absolute path written.

    Creates the manifest subdir if it doesn't exist. Filename is
    `{run_id}_{season_tag}.json` — the ISO timestamp gives natural
    ordering, and the season tag makes filtering by season trivial
    with a glob.
    """
    manifest_dir = os.path.join(root_dir, "data", MANIFEST_SUBDIR)
    os.makedirs(manifest_dir, exist_ok=True)
    fname = f"{manifest.run_id}_{manifest.season_tag}.json"
    fpath = os.path.join(manifest_dir, fname)
    with open(fpath, "w", encoding="utf-8") as f:
        json.dump(manifest.to_dict(), f, indent=2, ensure_ascii=False)
    return fpath
