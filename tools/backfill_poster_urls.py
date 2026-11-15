"""Landing Redesign PR #1 — Cover art URL backfill.

Populates `poster_url_large` and `poster_url_medium` on `anime_info` by
fetching each show's `main_picture` field from MAL v2. Uses the project's
app-scoped OAuth via `MALClient.get_with_auto_refresh`, so 401s auto-
refresh once and non-2xx responses bucket cleanly into the manifest.

Rate discipline:
  - 2.5s default sleep between calls (overridable via --sleep). MAL
    doesn't publish an official rate limit; 2.5s is conservative and
    lets a full ~6.4k-row backfill finish within an overnight window.
  - Manifest written at end of run to
    `data/poster_backfill_manifest_{ISO}.json` (gitignored).

Usage:
    python tools/backfill_poster_urls.py                       # Full backfill of missing rows
    python tools/backfill_poster_urls.py --dry-run --limit 3   # Smoke test, no writes
    python tools/backfill_poster_urls.py --only-missing        # Explicit alias for default
    python tools/backfill_poster_urls.py --force               # Re-fetch every show
    python tools/backfill_poster_urls.py --sleep 1.5           # Override sleep interval
    python tools/backfill_poster_urls.py --stats               # Coverage report, no fetching

Failure buckets in the manifest:
  no_mal_entry     — MAL returned 404
  no_poster_url    — MAL returned 200 but the show has no main_picture
  network_or_api   — 5xx or other non-200, timeouts, connection errors
  other            — JSON decode failures, unexpected shapes
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
load_dotenv(str(ROOT / "env_variables.env"))

DEFAULT_DB = ROOT / "data" / "anime_intelligence_v2.db"
MANIFEST_DIR = ROOT / "data"
MANIFEST_PREFIX = "poster_backfill_manifest_"

MAL_BASE_URL = "https://api.myanimelist.net/v2"
DEFAULT_SLEEP_SECONDS = 2.5

FAILURE_BUCKETS: tuple[str, ...] = (
    "no_mal_entry",
    "no_poster_url",
    "network_or_api",
    "other",
)


# ------------------------------------------------------------------------
# Response parsing
# ------------------------------------------------------------------------

@dataclass
class PosterUrls:
    """The two URL fields we care about. Either or both may be None."""
    large: str | None
    medium: str | None

    @property
    def any_present(self) -> bool:
        return bool(self.large or self.medium)


def _extract_poster_urls(payload: object) -> PosterUrls:
    """Pull main_picture.large + main_picture.medium from a MAL v2
    `/anime/{id}?fields=main_picture` response. Returns PosterUrls(None,
    None) when main_picture is absent, empty, or the payload isn't the
    shape we expect — callers use `any_present` to decide the manifest
    bucket."""
    if not isinstance(payload, dict):
        return PosterUrls(None, None)
    main_picture = payload.get("main_picture") or {}
    if not isinstance(main_picture, dict):
        return PosterUrls(None, None)
    return PosterUrls(
        large=main_picture.get("large") or None,
        medium=main_picture.get("medium") or None,
    )


# ------------------------------------------------------------------------
# DB read / write
# ------------------------------------------------------------------------

def enumerate_shows(conn: sqlite3.Connection, *, force: bool) -> list[tuple[int, str]]:
    """Return (mal_id, english_title) for shows to process. Default
    behavior skips rows that already have poster_url_large populated;
    --force re-fetches every row."""
    cur = conn.cursor()
    if force:
        cur.execute("SELECT id, english_title FROM anime_info ORDER BY id")
    else:
        cur.execute(
            "SELECT id, english_title FROM anime_info "
            "WHERE poster_url_large IS NULL "
            "ORDER BY id"
        )
    return [(row[0], row[1] or "") for row in cur.fetchall()]


def write_poster_urls(conn: sqlite3.Connection, anime_id: int,
                      urls: PosterUrls) -> None:
    """Atomic UPDATE of both columns for one show. Per-row commit —
    throughput is dominated by the 2.5s inter-call sleep, so the overhead
    is negligible, and we get free resumability if the process dies mid-run."""
    conn.execute(
        "UPDATE anime_info SET poster_url_large = ?, poster_url_medium = ? "
        "WHERE id = ?",
        (urls.large, urls.medium, anime_id),
    )
    conn.commit()


# ------------------------------------------------------------------------
# Manifest
# ------------------------------------------------------------------------

@dataclass
class Manifest:
    run_id: str  # ISO8601 UTC, filename-safe
    total_probed: int = 0
    success_count: int = 0
    failed_count: int = 0
    failure_details: dict[str, list[int]] = field(
        default_factory=lambda: {b: [] for b in FAILURE_BUCKETS}
    )
    duration_seconds: float = 0.0

    def record_success(self, anime_id: int) -> None:
        self.success_count += 1

    def record_failure(self, anime_id: int, bucket: str) -> None:
        if bucket not in self.failure_details:
            bucket = "other"
        self.failure_details[bucket].append(anime_id)
        self.failed_count += 1

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "total_probed": self.total_probed,
            "success_count": self.success_count,
            "failed_count": self.failed_count,
            "failure_details": {
                b: sorted(self.failure_details.get(b, []))
                for b in FAILURE_BUCKETS
            },
            "duration_seconds": round(self.duration_seconds, 2),
        }


def write_manifest(manifest: Manifest, out_dir: Path) -> Path:
    """Write the manifest JSON to `<out_dir>/poster_backfill_manifest_{run_id}.json`.
    Returns the path written."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{MANIFEST_PREFIX}{manifest.run_id}.json"
    path.write_text(json.dumps(manifest.to_dict(), indent=2), encoding="utf-8")
    return path


def find_latest_manifest(dir_path: Path) -> Path | None:
    """Return the most-recent manifest file in `dir_path`, or None. ISO
    timestamps sort lexicographically so filename sort == chrono sort."""
    if not dir_path.exists():
        return None
    matches = sorted(dir_path.glob(f"{MANIFEST_PREFIX}*.json"))
    return matches[-1] if matches else None


# ------------------------------------------------------------------------
# Fetch (thin wrapper — isolated as a seam for tests)
# ------------------------------------------------------------------------

@dataclass
class FetchOutcome:
    """Result of a single MAL fetch. On success, `urls` is populated and
    `bucket` is None; on failure, vice versa."""
    urls: PosterUrls | None
    bucket: str | None
    detail: str | None


def fetch_poster_urls(mal_client, anime_id: int) -> FetchOutcome:
    """One HTTP round-trip via MALClient.get_with_auto_refresh. Buckets
    the outcome for the manifest without raising."""
    url = f"{MAL_BASE_URL}/anime/{anime_id}"
    try:
        response = mal_client.get_with_auto_refresh(
            url, params={"fields": "main_picture"}, timeout=30
        )
    except (requests.Timeout, requests.ConnectionError) as e:
        return FetchOutcome(None, "network_or_api",
                            f"{type(e).__name__}: {e}")
    except Exception as e:  # noqa: BLE001 — bucket-and-continue on unexpected errors so one bad show doesn't kill the backfill
        return FetchOutcome(None, "other",
                            f"{type(e).__name__}: {e}")

    if response.status_code == 404:
        return FetchOutcome(None, "no_mal_entry", "HTTP 404")
    if response.status_code != 200:
        return FetchOutcome(None, "network_or_api",
                            f"HTTP {response.status_code}")

    try:
        payload = response.json()
    except ValueError as e:
        return FetchOutcome(None, "other", f"JSON decode: {e}")

    urls = _extract_poster_urls(payload)
    if not urls.any_present:
        return FetchOutcome(None, "no_poster_url", "no main_picture in payload")
    return FetchOutcome(urls, None, None)


# ------------------------------------------------------------------------
# Stats
# ------------------------------------------------------------------------

def compute_coverage(conn: sqlite3.Connection) -> dict:
    """Return coverage counts against the current DB state."""
    cur = conn.cursor()
    total = cur.execute("SELECT COUNT(*) FROM anime_info").fetchone()[0]
    with_large = cur.execute(
        "SELECT COUNT(*) FROM anime_info WHERE poster_url_large IS NOT NULL"
    ).fetchone()[0]
    with_medium = cur.execute(
        "SELECT COUNT(*) FROM anime_info WHERE poster_url_medium IS NOT NULL"
    ).fetchone()[0]
    missing = total - with_large
    return {
        "total": total,
        "with_large": with_large,
        "with_medium": with_medium,
        "missing": missing,
        "large_pct": (with_large / total * 100) if total else 0.0,
        "medium_pct": (with_medium / total * 100) if total else 0.0,
    }


def top_missing_by_popularity(conn: sqlite3.Connection,
                              limit: int = 20) -> list[tuple[int, str, int]]:
    """Missing shows ordered by scored_by desc — helps prioritize follow-ups."""
    cur = conn.cursor()
    cur.execute(
        "SELECT id, english_title, COALESCE(scored_by, 0) AS s "
        "FROM anime_info "
        "WHERE poster_url_large IS NULL "
        "ORDER BY s DESC "
        "LIMIT ?",
        (limit,),
    )
    return [(row[0], row[1] or "", row[2]) for row in cur.fetchall()]


def print_stats(db_path: Path) -> int:
    if not db_path.exists():
        print(f"ERROR: DB not found at {db_path}", file=sys.stderr)
        return 2
    conn = sqlite3.connect(str(db_path))
    cov = compute_coverage(conn)
    top_missing = top_missing_by_popularity(conn, limit=20)
    conn.close()

    print("=== Cover Art Coverage ===")
    print(f"Total vault:              {cov['total']:,} shows")
    print(f"With poster_url_large:    {cov['with_large']:,} ({cov['large_pct']:.1f}%)")
    print(f"With poster_url_medium:   {cov['with_medium']:,} ({cov['medium_pct']:.1f}%)")
    print(f"Missing:                  {cov['missing']:,}")
    print()

    if top_missing:
        print(f"=== Top {len(top_missing)} missing by popularity ===")
        for mal_id, title, scored_by in top_missing:
            print(f"MAL {mal_id:>7} | {title[:40]:<40} | scored_by={scored_by:,}")
        print()

    latest = find_latest_manifest(MANIFEST_DIR)
    if latest is not None:
        print(f"=== Missing by reason (from {latest.name}) ===")
        try:
            data = json.loads(latest.read_text(encoding="utf-8"))
            for bucket in FAILURE_BUCKETS:
                count = len(data.get("failure_details", {}).get(bucket, []))
                print(f"  {bucket:<16} {count}")
        except (OSError, ValueError) as e:
            print(f"  (unable to parse manifest: {e})")
    else:
        print("=== Missing by reason ===")
        print("  (no manifest found - run a backfill to generate one)")
    return 0


# ------------------------------------------------------------------------
# Main loop
# ------------------------------------------------------------------------

def _make_run_id() -> str:
    """UTC timestamp with colons stripped for cross-platform filename safety."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


def run_backfill(
    db_path: Path,
    *,
    dry_run: bool,
    limit: int | None,
    force: bool,
    sleep_seconds: float,
    mal_client=None,
    log_every: int = 100,
) -> Manifest:
    """Run the backfill loop. Returns the Manifest. Kept parameterized on
    mal_client so tests can inject a stub without touching the real
    MALClient (which would trigger Supabase / env / disk token reads on
    construction)."""
    if mal_client is None:
        from src.mal_api_client import MALClient
        mal_client = MALClient()

    conn = sqlite3.connect(str(db_path))
    shows = enumerate_shows(conn, force=force)
    if limit is not None:
        shows = shows[:limit]

    manifest = Manifest(run_id=_make_run_id())
    started = time.monotonic()

    dry_tag = " [DRY RUN]" if dry_run else ""
    print(f"[backfill]{dry_tag} Processing {len(shows)} shows against MAL v2")
    print(f"[backfill] Sleep between calls: {sleep_seconds:.2f}s")

    for i, (anime_id, title) in enumerate(shows, 1):
        manifest.total_probed += 1
        outcome = fetch_poster_urls(mal_client, anime_id)

        if outcome.bucket:
            manifest.record_failure(anime_id, outcome.bucket)
            print(f"[backfill] FAIL mal_id={anime_id} title={title[:40]!r} "
                  f"bucket={outcome.bucket} detail={outcome.detail}",
                  file=sys.stderr)
        else:
            manifest.record_success(anime_id)
            if not dry_run:
                write_poster_urls(conn, anime_id, outcome.urls)

        if i % log_every == 0 or i == len(shows):
            print(f"[backfill] {i}/{len(shows)} "
                  f"success={manifest.success_count} failed={manifest.failed_count}")

        if i < len(shows):
            time.sleep(sleep_seconds)

    conn.close()

    manifest.duration_seconds = time.monotonic() - started
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB,
                        help=f"SQLite path. Default: {DEFAULT_DB}")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch and parse but don't write to the DB.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Only process the first N shows. For smoke testing.")
    parser.add_argument("--only-missing", action="store_true",
                        help="(Default behavior) Skip rows with poster_url_large populated.")
    parser.add_argument("--force", action="store_true",
                        help="Re-fetch every show regardless of current state.")
    parser.add_argument("--sleep", type=float, default=DEFAULT_SLEEP_SECONDS,
                        help=f"Seconds to wait between MAL calls. Default: {DEFAULT_SLEEP_SECONDS}")
    parser.add_argument("--stats", action="store_true",
                        help="Print coverage report and exit; no fetching.")
    args = parser.parse_args()

    if args.stats:
        return print_stats(args.db)

    if not args.db.exists():
        print(f"ERROR: DB not found at {args.db}", file=sys.stderr)
        return 2

    if args.force and args.only_missing:
        print("ERROR: --force and --only-missing are mutually exclusive.",
              file=sys.stderr)
        return 2

    manifest = run_backfill(
        args.db,
        dry_run=args.dry_run,
        limit=args.limit,
        force=args.force,
        sleep_seconds=args.sleep,
    )

    manifest_path = write_manifest(manifest, MANIFEST_DIR)

    print("\n" + "=" * 70)
    mode = "dry-run" if args.dry_run else "write"
    print(f"COMPLETE. Processed {manifest.total_probed} shows in {mode} mode.")
    print(f"  Successful:              {manifest.success_count}")
    print(f"  Failed:                  {manifest.failed_count}")
    for bucket in FAILURE_BUCKETS:
        n = len(manifest.failure_details.get(bucket, []))
        if n:
            print(f"    {bucket:<16} {n}")
    print(f"  Duration:                {manifest.duration_seconds:.1f}s")
    print(f"  Manifest:                {manifest_path}")

    return 0 if manifest.failed_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
