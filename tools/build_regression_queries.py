"""
Phase B selector for Initiative 1 (Search Journal Regression Tests).

Reads data/search_journal.jsonl, applies the Phase A stratification plan,
and writes tests/data/regression_queries.json. Deterministic given a fixed
random seed; re-run safely to refresh the selection if the journal grows.

Usage:
    python tools/build_regression_queries.py
    python tools/build_regression_queries.py --seed 123 --no-write

Run from project root.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JOURNAL_PATH = ROOT / "data" / "search_journal.jsonl"
OUTPUT_PATH = ROOT / "tests" / "data" / "regression_queries.json"

DEFAULT_SEED = 42
DEDUP_WINDOW_SECONDS = 300  # 5-minute sliding window per Phase A spec
AUGMENTATION_DATE_PREFIX = "2026-05-31"

# Primary strata targets (Phase A plan)
FAS_TARGETS = {
    "vibe": 12,
    "genre_combo": 12,
    "narrative": 5,
    "hidden_gems": 5,
    "crowd_pleasers": 3,
    "polarizing": 3,
    "objective": 8,
}

SLT_TARGETS = {
    "slt_3ref": 2,
    "slt_with_neg": 5,
    "slt_with_notes": 3,
    "slt_2ref": 5,
    "slt_1ref": 12,
}

# Secondary coverage requirements (satisfied within the primary sample)
SECONDARY_REQUIREMENTS = [
    ("fas_with_era", 10),
    ("fas_with_constraint_filter", 8),
    ("fas_with_mal_rating_max", 3),
    ("fas_short_query", 5),
    ("fas_long_query", 5),
    ("fas_empty_results", 3),
    ("slt_empty_results", 3),
    ("slt_neg_and_notes", 3),
]

# Intent → rerank_type mapping for pre-PR-#62 entries without timings
INTENT_TO_RERANK_TYPE = {
    "vibe": "fusion",
    "genre_combo": "fusion",
    "narrative": "narrative",
    "hidden_gems": "constrained",
    "crowd_pleasers": "constrained",
    "polarizing": "constrained",
    "objective": "objective",
}

PII_PATTERNS = [
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "email"),
    (re.compile(r"\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b"), "phone"),
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "ssn"),
]


def parse_ts(s: str):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def dedup_key(entry: dict):
    mode = entry.get("mode")
    if mode == "find_a_show":
        filters = entry.get("filters_applied") or {}
        rating_max = filters.get("mal_rating_max") if isinstance(filters, dict) else None
        return ("fas", entry.get("query"), entry.get("era"), rating_max)
    if mode == "shows_like_this":
        refs = tuple(entry.get("reference_titles") or [])
        negs = tuple(entry.get("negative_anchors") or [])
        return ("slt", refs, negs)
    return None


def fas_stratum(entry: dict):
    if entry.get("mode") != "find_a_show":
        return None
    return entry.get("intent")


def slt_stratum(entry: dict):
    if entry.get("mode") != "shows_like_this":
        return None
    refs = entry.get("reference_titles") or []
    negs = entry.get("negative_anchors") or []
    notes = entry.get("title_resolution_notes") or []
    if len(refs) == 3:
        return "slt_3ref"
    if negs:
        return "slt_with_neg"
    if notes:
        return "slt_with_notes"
    if len(refs) == 2:
        return "slt_2ref"
    if len(refs) == 1:
        return "slt_1ref"
    return None


def get_stratum(entry: dict):
    return fas_stratum(entry) or slt_stratum(entry)


def get_rerank_type(entry: dict):
    timings = entry.get("timings") or {}
    if timings.get("rerank_type"):
        return timings["rerank_type"]
    if entry.get("mode") == "shows_like_this":
        return "dna"
    return INTENT_TO_RERANK_TYPE.get(entry.get("intent"))


# Secondary requirement predicates
def _filters_dict(entry):
    f = entry.get("filters_applied")
    return f if isinstance(f, dict) else {}


REQ_PREDICATES = {
    "fas_with_era": lambda e: (
        e.get("mode") == "find_a_show"
        and e.get("era")
        and e.get("era") not in ("All Eras", "All")
    ),
    "fas_with_constraint_filter": lambda e: (
        e.get("mode") == "find_a_show"
        and any(
            k in _filters_dict(e)
            for k in ("year_min", "year_max", "genres", "mal_rating_max", "scored_by_max")
        )
    ),
    "fas_with_mal_rating_max": lambda e: (
        e.get("mode") == "find_a_show" and _filters_dict(e).get("mal_rating_max")
    ),
    "fas_short_query": lambda e: (
        e.get("mode") == "find_a_show" and len(e.get("query") or "") < 10
    ),
    "fas_long_query": lambda e: (
        e.get("mode") == "find_a_show" and len(e.get("query") or "") >= 50
    ),
    "fas_empty_results": lambda e: (
        e.get("mode") == "find_a_show" and not (e.get("results") or [])
    ),
    "slt_empty_results": lambda e: (
        e.get("mode") == "shows_like_this" and not (e.get("results") or [])
    ),
    "slt_neg_and_notes": lambda e: (
        e.get("mode") == "shows_like_this"
        and (e.get("negative_anchors") or [])
        and (e.get("title_resolution_notes") or [])
    ),
}


def load_and_dedup(journal_path: Path):
    """Read journal, drop errors, apply 5-min sliding-window dedup per key."""
    last_seen = {}
    usable = []
    total = 0
    errors_dropped = 0
    dedup_dropped = 0
    for line in journal_path.open(encoding="utf-8"):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        total += 1
        if entry.get("error"):
            errors_dropped += 1
            continue
        key = dedup_key(entry)
        if key is None:
            continue
        ts = parse_ts(entry.get("timestamp", ""))
        if ts and key in last_seen:
            if (ts - last_seen[key]).total_seconds() < DEDUP_WINDOW_SECONDS:
                dedup_dropped += 1
                continue
        if ts:
            last_seen[key] = ts
        usable.append(entry)
    return usable, {"total": total, "errors_dropped": errors_dropped, "dedup_dropped": dedup_dropped}


def sample_primary_strata(usable, seed: int):
    """Random-sample target counts from each primary stratum."""
    buckets = defaultdict(list)
    for entry in usable:
        s = get_stratum(entry)
        if s:
            buckets[s].append(entry)

    rng = random.Random(seed)
    selected = []
    availability = {}
    shortages = {}
    targets = {**FAS_TARGETS, **SLT_TARGETS}
    for stratum, target in targets.items():
        available = buckets.get(stratum, [])
        availability[stratum] = len(available)
        n = min(target, len(available))
        if n < target:
            shortages[stratum] = target - n
        if available:
            selected.extend(rng.sample(available, n))
    return selected, buckets, availability, shortages


def satisfy_secondary(selected: list, buckets: dict, seed: int):
    """Swap entries in-stratum to meet secondary requirements where possible."""
    rng = random.Random(seed + 1)
    results = []
    for name, target in SECONDARY_REQUIREMENTS:
        pred = REQ_PREDICATES[name]
        current = sum(1 for e in selected if pred(e))
        if current >= target:
            results.append((name, target, current, current, 0, "met"))
            continue
        need = target - current
        # Find satisfying candidates not in selected
        selected_ids = {id(e) for e in selected}
        all_satisfying = [
            e for s_list in buckets.values() for e in s_list
            if pred(e) and id(e) not in selected_ids
        ]
        swapped = 0
        for cand in all_satisfying:
            if swapped >= need:
                break
            cand_stratum = get_stratum(cand)
            replaceable = [
                e for e in selected
                if get_stratum(e) == cand_stratum and not pred(e)
            ]
            if not replaceable:
                continue
            to_remove = rng.choice(replaceable)
            selected.remove(to_remove)
            selected.append(cand)
            selected_ids.discard(id(to_remove))
            selected_ids.add(id(cand))
            swapped += 1
        new_count = sum(1 for e in selected if pred(e))
        status = "met" if new_count >= target else "short"
        results.append((name, target, current, new_count, swapped, status))
    return results


def build_output_entry(entry: dict):
    out = {
        "search_id": entry.get("search_id"),
        "mode": entry.get("mode"),
        "timestamp": entry.get("timestamp"),
        "rerank_type": get_rerank_type(entry),
    }
    ts = entry.get("timestamp", "")
    out["source"] = (
        "augmentation_2026-05-31"
        if ts.startswith(AUGMENTATION_DATE_PREFIX)
        else "historical"
    )
    if entry.get("mode") == "find_a_show":
        out["query"] = entry.get("query")
        out["lens"] = entry.get("lens")
        out["era"] = entry.get("era")
        out["intent"] = entry.get("intent")
        out["filters_applied"] = entry.get("filters_applied")
    elif entry.get("mode") == "shows_like_this":
        out["reference_titles"] = entry.get("reference_titles")
        out["negative_anchors"] = entry.get("negative_anchors")
        out["title_resolution_notes"] = entry.get("title_resolution_notes")
    return out


def scan_pii(selected: list):
    hits = []
    for entry in selected:
        texts = []
        if entry.get("query"):
            texts.append(entry["query"])
        for ref in entry.get("reference_titles") or []:
            texts.append(str(ref))
        for neg in entry.get("negative_anchors") or []:
            texts.append(str(neg))
        for text in texts:
            for pat, label in PII_PATTERNS:
                if pat.search(text):
                    hits.append((label, text))
    return hits


def validate_output(selected: list):
    """Assert minimum invariants on the selected sample."""
    rerank_types = Counter(get_rerank_type(e) for e in selected)
    required = {"fusion", "narrative", "constrained", "dna", "objective"}
    missing = required - set(rerank_types)
    if missing:
        raise AssertionError(f"Missing rerank_types in sample: {missing}")
    if not (50 <= len(selected) <= 100):
        raise AssertionError(f"Sample size {len(selected)} outside Phase A range [50, 100]")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--no-write", action="store_true", help="Dry run; do not write output file")
    args = parser.parse_args()

    if not JOURNAL_PATH.exists():
        print(f"ERROR: journal not found at {JOURNAL_PATH}", file=sys.stderr)
        return 1

    usable, stats = load_and_dedup(JOURNAL_PATH)
    print(f"Journal: {stats['total']} entries; {stats['errors_dropped']} errors dropped; "
          f"{stats['dedup_dropped']} dedup-dropped; {len(usable)} usable")
    print()

    selected, buckets, availability, shortages = sample_primary_strata(usable, args.seed)
    print("=== PRIMARY STRATA SAMPLING ===")
    targets = {**FAS_TARGETS, **SLT_TARGETS}
    for stratum, target in targets.items():
        have = availability.get(stratum, 0)
        got = min(target, have)
        flag = "OK " if got == target else "LOW"
        print(f"  [{flag}] {stratum:<25} target={target:>3}  available={have:>3}  selected={got:>3}")
    if shortages:
        print(f"  WARN: {len(shortages)} strata below target: {shortages}")
    print()

    secondary_results = satisfy_secondary(selected, buckets, args.seed)
    print("=== SECONDARY REQUIREMENTS ===")
    for name, target, before, after, swapped, status in secondary_results:
        flag = "OK " if status == "met" else "LOW"
        print(f"  [{flag}] {name:<32} target={target}  before={before}  after={after}  (swapped {swapped})")
    print()

    pii_hits = scan_pii(selected)
    if pii_hits:
        print(f"WARN: {len(pii_hits)} potential PII match(es): {pii_hits[:5]}")
    else:
        print("PII scan: clean")
    print()

    output_entries = [build_output_entry(e) for e in selected]
    output_entries.sort(key=lambda x: x.get("timestamp") or "")

    rerank_types = Counter(e["rerank_type"] for e in output_entries if e.get("rerank_type"))
    modes = Counter(e["mode"] for e in output_entries)
    sources = Counter(e["source"] for e in output_entries)
    print("=== FINAL SELECTION ===")
    print(f"  Total entries: {len(output_entries)}")
    print(f"  By mode: {dict(modes)}")
    print(f"  By rerank_type: {dict(rerank_types)}")
    print(f"  By source: {dict(sources)}")
    print()

    validate_output(selected)
    print("Validation: PASS")

    if args.no_write:
        print(f"\n[dry-run] would have written {OUTPUT_PATH.relative_to(ROOT)}")
        return 0

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "journal_source_size": stats["total"],
        "journal_usable_after_dedup": len(usable),
        "random_seed": args.seed,
        "dedup_window_seconds": DEDUP_WINDOW_SECONDS,
        "entry_count": len(output_entries),
        "summary": {
            "by_mode": dict(modes),
            "by_rerank_type": dict(rerank_types),
            "by_source": dict(sources),
            "primary_strata_shortages": shortages,
            "secondary_requirements": [
                {"name": name, "target": target, "achieved": after, "status": status}
                for name, target, _before, after, _swapped, status in secondary_results
            ],
        },
        "entries": output_entries,
    }
    with OUTPUT_PATH.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"\nWrote {len(output_entries)} entries to {OUTPUT_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
