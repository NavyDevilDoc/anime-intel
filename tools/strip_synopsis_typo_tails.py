"""One-off cleanup: strip the 5 typo'd MAL attribution tails from
anime_info.mal_synopsis that the Phase 2 regex intentionally does not
catch (the whitelist is strict: Source/Written by/Note, not fuzzy).

The 5 typo cases were surfaced by the pre-Phase-1 probe. Each is a
single-row MAL curator typo of an otherwise-recognizable attribution
pattern. Doing a targeted UPDATE keeps the production strip regex
simple and auditable; the alternative was a fuzzy-match regex, which
widens the false-positive risk against the 15 legitimate parenthetical
endings also observed in the probe.

Idempotent: re-running on an already-cleaned DB is a no-op. Default
is --dry-run; pass --apply to actually mutate.

Usage:
    python tools/strip_synopsis_typo_tails.py              # dry run (default)
    python tools/strip_synopsis_typo_tails.py --apply      # write
"""

import argparse
import logging
import os
import sqlite3
import sys

SCRIPT_DIR = os.path.abspath(os.path.dirname(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

DB_PATH = os.path.join(ROOT_DIR, "data", "anime_intelligence_v2.db")

# (mal_id, exact_trailing_tail_to_strip) — verified at end-of-synopsis
# via the Phase 1 probe. Each tail is the full bracketed/parenthesized
# block including delimiters.
CASES = [
    (9159, "(Soure: ANN)"),
    (10029, "[Writtten by MAL Rewrite]"),
    (18429, "(Souce: Wikipedia)"),
    (37937, "(Sorce: Anilist)"),
    (55636, "(Official Site, translated)"),
]


def clean_tail(synopsis, expected_tail):
    """Strip `expected_tail` from the end of `synopsis` with trailing
    whitespace. Returns (cleaned, changed_bool). If the expected tail
    isn't present at end, returns the original unchanged — safe no-op
    for re-runs.
    """
    if not synopsis:
        return synopsis, False
    rstripped = synopsis.rstrip()
    if not rstripped.endswith(expected_tail):
        return synopsis, False
    cleaned = rstripped[: -len(expected_tail)].rstrip()
    return cleaned, True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Actually write to the DB (default: dry run)")
    args = parser.parse_args()

    mode = "APPLY" if args.apply else "DRY RUN"
    logger.info(f"Mode: {mode}. DB: {DB_PATH}")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    to_write = []
    for mal_id, expected_tail in CASES:
        row = conn.execute(
            "SELECT english_title, mal_synopsis FROM anime_info WHERE id = ?",
            (mal_id,),
        ).fetchone()
        if not row:
            logger.warning(f"  mal_id {mal_id}: NOT IN DB — skipping")
            continue

        title = row["english_title"]
        syn = row["mal_synopsis"]
        cleaned, changed = clean_tail(syn, expected_tail)

        if not changed:
            logger.info(f"  mal_id {mal_id} ({title!r}): tail already absent — no-op")
            continue

        logger.info(f"  mal_id {mal_id} ({title!r}): will strip trailing {expected_tail!r}")
        logger.info(f"    before: ...{syn.rstrip()[-80:]!r}")
        logger.info(f"    after:  ...{cleaned[-80:]!r}")
        to_write.append((mal_id, cleaned))

    if not to_write:
        logger.info("Nothing to do. All targets already clean.")
        conn.close()
        return

    if not args.apply:
        logger.info(f"Dry run — would UPDATE {len(to_write)} row(s). Pass --apply to execute.")
        conn.close()
        return

    # Apply in a single transaction. Verify each UPDATE affected exactly
    # one row — anything else means the DB shape shifted between read
    # and write (concurrent mutation, or someone deleted the row) and
    # we should abort rather than quietly accept the drift.
    try:
        for mal_id, cleaned in to_write:
            cur = conn.execute(
                "UPDATE anime_info SET mal_synopsis = ? WHERE id = ?",
                (cleaned, mal_id),
            )
            if cur.rowcount != 1:
                raise RuntimeError(
                    f"UPDATE for mal_id {mal_id} affected {cur.rowcount} rows (expected 1). Aborting."
                )
        conn.commit()
        logger.info(f"Committed {len(to_write)} UPDATE(s).")
    except Exception:
        conn.rollback()
        logger.error("Rolled back on failure.")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
