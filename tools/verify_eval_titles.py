"""
tools/verify_eval_titles.py

Cross-references every title in tests/data/engine_eval_queries.json
against the vault (SQLite english_title OR romaji_title). Flags titles
that don't exist — those would cause false-negative scoring in the
eval harness (engine can't return what isn't there).

Checks four fields per entry:
  - ideal_top_3
  - acceptable_top_10
  - must_not_appear
  - reference_titles (SLT mode only)

Usage (from repo root):
    python tools/verify_eval_titles.py

Re-run whenever the eval set is edited. Also worth running before any
scoring baseline capture — no point measuring the engine against
titles that structurally can't appear.

Exit code 0 if all titles resolve; 1 if any are missing (so this can
be wired into CI later if we want).
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

# --- BULLETPROOF PATH ANCHORING ---
SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent if SCRIPT_DIR.name == "tools" else SCRIPT_DIR
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# Force UTF-8 stdout for CJK titles.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

# Late import — needs sys.path to include ROOT_DIR.
from analysis.queries import resolve_show_title  # noqa: E402

DB_PATH = ROOT_DIR / "data" / "anime_intelligence_v2.db"
EVAL_PATH = ROOT_DIR / "tests" / "data" / "engine_eval_queries.json"

FIELDS_TO_CHECK = ["ideal_top_3", "acceptable_top_10", "must_not_appear", "reference_titles"]


def load_vault_titles() -> set[str]:
    """Return the set of every english_title and romaji_title in the vault.
    Titles are case-preserved for reporting; matching is case-sensitive
    (users are responsible for exact spelling in the eval set)."""
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.cursor()
    cur.execute("SELECT english_title, romaji_title FROM anime_info")
    titles: set[str] = set()
    for en, ro in cur.fetchall():
        if en:
            titles.add(en)
        if ro:
            titles.add(ro)
    conn.close()
    return titles


def collect_eval_titles(eval_data: dict) -> dict[str, list[tuple[str, str]]]:
    """Return {title: [(entry_id, field_name), ...]} for every title
    referenced across the eval set. Same title appearing in multiple
    entries gets multiple provenance tuples."""
    provenance: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for entry in eval_data.get("queries", []):
        entry_id = entry.get("id", "?")
        for field in FIELDS_TO_CHECK:
            for title in entry.get(field) or []:
                provenance[title].append((entry_id, field))
    return provenance


def main() -> int:
    print(f"Loading vault titles from {DB_PATH}...", file=sys.stderr)
    vault_titles = load_vault_titles()
    print(f"  {len(vault_titles):,} distinct titles in vault (english + romaji).", file=sys.stderr)

    print(f"Loading eval set from {EVAL_PATH}...", file=sys.stderr)
    with EVAL_PATH.open(encoding="utf-8") as f:
        eval_data = json.load(f)

    provenance = collect_eval_titles(eval_data)
    print(f"  {len(provenance):,} distinct titles referenced across the eval set.\n", file=sys.stderr)

    missing = {t: p for t, p in provenance.items() if t not in vault_titles}
    found = len(provenance) - len(missing)

    print("=" * 72)
    print("VAULT-TITLE EXISTENCE CHECK")
    print("=" * 72)
    print(f"Titles found in vault:      {found:>4d}")
    print(f"Titles MISSING from vault:  {len(missing):>4d}")
    print()

    if not missing:
        print("All eval-set titles resolve via strict match. Ready to score.")
        return 0

    # Try the backend's fuzzy resolver for each missing title. If it
    # returns a single canonical form, the title WILL work in the engine
    # (scoring tool uses the same resolver), but we still recommend
    # updating the JSON to the canonical form for readability.
    print(f"Strict-match misses ({len(missing)}). Running fuzzy resolver...\n")
    fuzzy_resolved: dict[str, list[str]] = {}
    truly_missing: dict[str, list[tuple[str, str]]] = {}
    for title in missing:
        matches = resolve_show_title(title)
        if matches:
            fuzzy_resolved[title] = matches
        else:
            truly_missing[title] = missing[title]

    if fuzzy_resolved:
        print(f"RESOLVES VIA FUZZY MATCH ({len(fuzzy_resolved)}):")
        print("These will score correctly in the engine (which uses the same")
        print("resolver), but update the JSON to the canonical vault form for")
        print("readability.")
        print("-" * 72)
        for title in sorted(fuzzy_resolved.keys()):
            matches = fuzzy_resolved[title]
            locations = missing[title]
            loc_str = ", ".join(f"{eid} [{field}]" for eid, field in locations)
            print(f"  {title!r}")
            print(f"    resolves to → {matches[0]!r}" +
                  (f"  (+{len(matches)-1} other matches)" if len(matches) > 1 else ""))
            print(f"    used in → {loc_str}")
        print()

    if truly_missing:
        print(f"TRULY MISSING — no fuzzy match ({len(truly_missing)}):")
        print("These need substitution or removal.")
        print("-" * 72)
        for title in sorted(truly_missing.keys()):
            locations = truly_missing[title]
            loc_str = ", ".join(f"{eid} [{field}]" for eid, field in locations)
            print(f"  {title!r}")
            print(f"    → {loc_str}")
        print()
    else:
        print("No truly-missing titles — every reference resolves via strict or fuzzy match.")
        print()

    print("Fix by: (a) updating strict-match misses to their canonical vault form,")
    print("        (b) swapping truly-missing titles for something present, or")
    print("        (c) removing the reference entirely.")
    print("Re-run this tool after editing to confirm the fix.")
    # Only exit-code-fail on truly missing — fuzzy-resolvable is a soft warning
    return 1 if truly_missing else 0


if __name__ == "__main__":
    sys.exit(main())
