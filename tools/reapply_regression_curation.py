"""One-off: re-apply curation snapshot to a freshly-captured regression
baseline by position.

The capture-baseline arm overwrites `tests/data/regression_baseline.json`
without reading the prior curation. This script restores the curation
verdicts/notes using the pre-refresh snapshot written by the operator
before launching capture (positional index — baseline entries align 1:1
with regression_queries.json entries, so position is a stable key).

Usage:
    python tools/reapply_regression_curation.py                  # dry run
    python tools/reapply_regression_curation.py --apply          # write

Safety:
- Dry run by default (shows per-position before/after)
- Verifies entry_count matches between snapshot and new baseline before
  touching anything
- Preserves existing curation keys on entries not in the snapshot
  (defensive — new baseline shouldn't have those, but future-proof)
"""

import argparse
import json
import logging
import os
import sys
from collections import Counter

SCRIPT_DIR = os.path.abspath(os.path.dirname(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

BASELINE_PATH = os.path.join(ROOT_DIR, "tests", "data", "regression_baseline.json")
SNAPSHOT_PATH = os.path.join(ROOT_DIR, "curation_snapshot_pre_refresh.json")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write merged baseline back to disk (default: dry run)")
    args = parser.parse_args()

    if not os.path.exists(SNAPSHOT_PATH):
        logger.error(f"Snapshot not found at {SNAPSHOT_PATH}. Did you snapshot curation before capture?")
        return 1

    snapshot = json.load(open(SNAPSHOT_PATH, encoding="utf-8"))
    baseline = json.load(open(BASELINE_PATH, encoding="utf-8"))

    snap_count = snapshot["entry_count"]
    base_count = len(baseline["entries"])
    if snap_count != base_count:
        logger.error(
            f"Entry count mismatch: snapshot={snap_count}, new baseline={base_count}. "
            "Positional re-apply would scramble verdicts. Aborting."
        )
        return 1

    curation_list = snapshot["curation_by_position"]
    changes = []
    for i, (entry, cur) in enumerate(zip(baseline["entries"], curation_list)):
        existing = entry.get("curation")
        if cur is None:
            continue
        if existing == cur:
            continue
        changes.append((i, existing, cur))

    logger.info(f"Would update curation on {len(changes)} / {base_count} entries.")
    verdict_counts = Counter((c or {}).get("verdict") for c in curation_list if c)
    logger.info(f"Verdicts to apply: {dict(verdict_counts)}")

    if not changes:
        logger.info("No changes needed — curation already matches snapshot. No-op.")
        return 0

    for i, old, new in changes[:5]:
        logger.info(f"  [{i:>2}] old={old} -> new={new}")
    if len(changes) > 5:
        logger.info(f"  ... and {len(changes) - 5} more")

    if not args.apply:
        logger.info("Dry run — pass --apply to write merged baseline.")
        return 0

    for i, cur in enumerate(curation_list):
        if cur is not None:
            baseline["entries"][i]["curation"] = cur

    with open(BASELINE_PATH, "w", encoding="utf-8") as f:
        json.dump(baseline, f, indent=2, ensure_ascii=False)
        f.write("\n")

    logger.info(f"Wrote merged baseline to {BASELINE_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
