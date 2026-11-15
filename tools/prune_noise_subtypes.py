"""One-off cleanup: delete anime_info rows with subtype IN (Music, CM, PV).

These subtypes carry music videos (Music), commercials (CM), and
promotional trailers (PV) that landed in the vault via franchise-sibling
expansion and popularity backfills over time. Zero recommendation value
by design — users don't come to an anime discovery engine looking for
30-second commercials — and they dilute FAISS retrieval breadth +
search clarity.

Verified exceptions (preserved despite subtype match):
  mal_id 34240 — Shelter (A-1 Pictures × Porter Robinson, 2016,
                 220,028 ratings, 8.31). Technically categorized as
                 Music because it's built around a song, but it's a
                 6-minute short film with real narrative content and
                 wider engagement than most TV series in the vault.

Scope of mutation (writes gated behind --apply):
  - anime_info      : delete rows matching the predicate
  - anime_staff     : delete rows referencing the deleted anime_ids
  - people          : delete rows whose person_mal_id has zero
                      remaining anime_staff references after the above
  - FAISS index     : NOT touched here — a full rebuild via
                      `python vault_enrichment.py` or the FAISS build
                      path is the correct downstream step
  - frontend data   : NOT touched here — regenerate separately via
                      tools/export_seo_data.py and
                      tools/build_similar_shows.py

All mutations run inside a single transaction. Any error during the
delete block rolls everything back; partial-state DBs are not possible.

Usage:
    python tools/prune_noise_subtypes.py              # dry run (default)
    python tools/prune_noise_subtypes.py --apply      # write
"""

import argparse
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone

SCRIPT_DIR = os.path.abspath(os.path.dirname(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

DB_PATH = os.path.join(ROOT_DIR, "data", "anime_intelligence_v2.db")
MANIFEST_DIR = os.path.join(ROOT_DIR, "data")

NOISE_SUBTYPES = ("Music", "CM", "PV")

# Hand-curated exceptions. See module docstring for the reasoning behind
# each entry — never add an id here without a documented reason.
KEEP_IDS = {
    34240,  # Shelter (A-1 Pictures × Porter Robinson)
}


def _placeholders(items):
    return ",".join("?" * len(items))


def audit(conn):
    """Return a dict of counts for the dry-run report."""
    cur = conn.cursor()
    sub_ph = _placeholders(NOISE_SUBTYPES)
    keep_ph = _placeholders(KEEP_IDS) if KEEP_IDS else "NULL"
    keep_clause = f"AND id NOT IN ({keep_ph})" if KEEP_IDS else ""
    keep_params = list(KEEP_IDS) if KEEP_IDS else []

    anime_rows = cur.execute(
        f"SELECT COUNT(*) FROM anime_info WHERE subtype IN ({sub_ph}) {keep_clause}",
        list(NOISE_SUBTYPES) + keep_params,
    ).fetchone()[0]

    anime_rows_by_subtype = {}
    for st in NOISE_SUBTYPES:
        n = cur.execute(
            f"SELECT COUNT(*) FROM anime_info WHERE subtype = ? {keep_clause}",
            [st] + keep_params,
        ).fetchone()[0]
        anime_rows_by_subtype[st] = n

    staff_rows = cur.execute(
        f"""SELECT COUNT(*) FROM anime_staff WHERE anime_id IN (
                SELECT id FROM anime_info WHERE subtype IN ({sub_ph}) {keep_clause}
            )""",
        list(NOISE_SUBTYPES) + keep_params,
    ).fetchone()[0]

    # Orphaned people: those whose only anime_staff rows reference
    # about-to-be-deleted shows.
    orphan_people = cur.execute(
        f"""SELECT COUNT(*) FROM people p
            WHERE p.person_mal_id IN (
                SELECT DISTINCT person_mal_id FROM anime_staff
                WHERE anime_id IN (
                    SELECT id FROM anime_info WHERE subtype IN ({sub_ph}) {keep_clause}
                )
            )
            AND NOT EXISTS (
                SELECT 1 FROM anime_staff s
                WHERE s.person_mal_id = p.person_mal_id
                  AND s.anime_id NOT IN (
                      SELECT id FROM anime_info WHERE subtype IN ({sub_ph}) {keep_clause}
                  )
            )""",
        list(NOISE_SUBTYPES) + keep_params + list(NOISE_SUBTYPES) + keep_params,
    ).fetchone()[0]

    return {
        "anime_info_total": anime_rows,
        "anime_info_by_subtype": anime_rows_by_subtype,
        "anime_staff_cascade": staff_rows,
        "orphaned_people": orphan_people,
        "preserved_exception_ids": sorted(KEEP_IDS),
    }


def apply_prune(conn):
    """Transactional delete. Returns actual-affected counts."""
    cur = conn.cursor()
    sub_ph = _placeholders(NOISE_SUBTYPES)
    keep_ph = _placeholders(KEEP_IDS) if KEEP_IDS else "NULL"
    keep_clause = f"AND id NOT IN ({keep_ph})" if KEEP_IDS else ""
    keep_params = list(KEEP_IDS) if KEEP_IDS else []

    try:
        cur.execute("BEGIN")

        # 1. Grab the set of anime_ids being deleted so later cascades
        #    reference a stable set (don't rely on SELECT after DELETE).
        cur.execute(
            f"SELECT id FROM anime_info WHERE subtype IN ({sub_ph}) {keep_clause}",
            list(NOISE_SUBTYPES) + keep_params,
        )
        doomed_ids = [row[0] for row in cur.fetchall()]
        if not doomed_ids:
            logger.info("Nothing matches the prune predicate. No-op.")
            conn.rollback()
            return {"deleted_anime_info": 0, "deleted_anime_staff": 0, "deleted_people": 0}

        doomed_ph = _placeholders(doomed_ids)

        # 2. Identify people who will be orphaned after the cascade.
        #    Compute BEFORE deleting anime_staff so the SELECT sees the
        #    current state.
        cur.execute(
            f"""SELECT p.person_mal_id FROM people p
                WHERE p.person_mal_id IN (
                    SELECT DISTINCT person_mal_id FROM anime_staff
                    WHERE anime_id IN ({doomed_ph})
                )
                AND NOT EXISTS (
                    SELECT 1 FROM anime_staff s
                    WHERE s.person_mal_id = p.person_mal_id
                      AND s.anime_id NOT IN ({doomed_ph})
                )""",
            doomed_ids + doomed_ids,
        )
        orphan_ids = [row[0] for row in cur.fetchall()]

        # 3. Delete anime_staff rows first (FK-ish ordering, even though
        #    SQLite doesn't enforce FKs by default here).
        cur.execute(
            f"DELETE FROM anime_staff WHERE anime_id IN ({doomed_ph})",
            doomed_ids,
        )
        staff_deleted = cur.rowcount

        # 4. Delete orphaned people rows.
        people_deleted = 0
        if orphan_ids:
            orphan_ph = _placeholders(orphan_ids)
            cur.execute(
                f"DELETE FROM people WHERE person_mal_id IN ({orphan_ph})",
                orphan_ids,
            )
            people_deleted = cur.rowcount

        # 5. Delete anime_info rows.
        cur.execute(
            f"DELETE FROM anime_info WHERE id IN ({doomed_ph})",
            doomed_ids,
        )
        anime_deleted = cur.rowcount

        conn.commit()
        return {
            "deleted_anime_info": anime_deleted,
            "deleted_anime_staff": staff_deleted,
            "deleted_people": people_deleted,
            "deleted_anime_ids": doomed_ids,
            "deleted_person_ids": orphan_ids,
        }
    except Exception:
        conn.rollback()
        raise


def write_manifest(data):
    os.makedirs(MANIFEST_DIR, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    path = os.path.join(MANIFEST_DIR, f"noise_prune_manifest_{ts}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Actually mutate the DB (default: dry run)")
    args = parser.parse_args()

    mode = "APPLY" if args.apply else "DRY RUN"
    logger.info(f"Mode: {mode}. DB: {DB_PATH}")
    logger.info(f"Noise subtypes to prune: {list(NOISE_SUBTYPES)}")
    logger.info(f"Preserved exceptions: {sorted(KEEP_IDS)}")

    conn = sqlite3.connect(DB_PATH)
    audit_data = audit(conn)

    logger.info("")
    logger.info("=== Audit (dry-run shape) ===")
    for st, n in audit_data["anime_info_by_subtype"].items():
        logger.info(f"  {st:>6} to delete:   {n}")
    logger.info(f"  TOTAL anime_info delete:    {audit_data['anime_info_total']}")
    logger.info(f"  anime_staff cascade:        {audit_data['anime_staff_cascade']}")
    logger.info(f"  orphaned people cleanup:    {audit_data['orphaned_people']}")

    if not args.apply:
        logger.info("")
        logger.info("Dry run complete. Pass --apply to execute.")
        conn.close()
        return 0

    logger.info("")
    logger.info("Executing delete transaction...")
    result = apply_prune(conn)
    conn.close()

    manifest = {
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "audit": audit_data,
        "result": {
            k: v for k, v in result.items()
            if k in ("deleted_anime_info", "deleted_anime_staff", "deleted_people")
        },
        "deleted_anime_ids_count": len(result.get("deleted_anime_ids", [])),
        "deleted_person_ids_count": len(result.get("deleted_person_ids", [])),
    }
    path = write_manifest(manifest)
    logger.info("")
    logger.info(f"Deleted {result['deleted_anime_info']} anime_info rows, "
                f"{result['deleted_anime_staff']} anime_staff rows, "
                f"{result['deleted_people']} orphaned people rows.")
    logger.info(f"Manifest: {path}")
    logger.info("")
    logger.info("REMEMBER THE DOWNSTREAM SYNC:")
    logger.info("  1. Rebuild FAISS: python vault_enrichment.py  (or targeted FAISS rebuild)")
    logger.info("  2. Regenerate SEO JSON: python tools/export_seo_data.py")
    logger.info("  3. Regenerate similar-shows: python tools/build_similar_shows.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
