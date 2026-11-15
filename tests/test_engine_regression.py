"""
Engine regression test harness for Initiative 1.

Two modes:

1. `--capture-baseline` (Phase C): replays each entry from
   tests/data/regression_queries.json through the live engine and writes
   tests/data/regression_baseline.json. One-shot operation. Re-runs will
   overwrite the curated baseline file; use deliberately during a baseline
   refresh PR.

2. `--check-regression` (Phase E): replays each non-excluded baseline
   entry through the engine, computes Jaccard top-K + Kendall tau against
   the curated baseline, and writes tests/data/regression_check_report.json
   with per-entry metrics. Returns exit code 1 if any `keep` or
   `variance-tolerant` entry drifts below its verdict's Jaccard threshold
   (keep=0.6, variance-tolerant=0.4 — see JACCARD_THRESHOLD_BY_VERDICT).
   `known-bad` entries record `behavior_changed` status (not a failure)
   when the recorded wrong behavior shifts — surfaces fixes for human
   review but doesn't block.

Also exposes pytest-discoverable `test_engine_regression_check_passes`
under the `regression` marker. Slow (~30-45 min) so skipped in default
runs; opt-in via `pytest -m regression` or run the CLI form directly.

Usage:
    python tests/test_engine_regression.py --capture-baseline           # full
    python tests/test_engine_regression.py --capture-baseline --limit 3 # smoke
    python tests/test_engine_regression.py --check-regression
    python tests/test_engine_regression.py --check-regression --limit 5
    pytest tests/test_engine_regression.py -m regression -v
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / "env_variables.env")

# Must be set BEFORE importing the app.
os.environ["TESTING"] = "1"
os.environ.setdefault("ADMIN_USERNAME", "test-admin")
os.environ.setdefault("ADMIN_PASSWORD", "test-password")

# Regression runs make REAL Gemini calls (no mocks). Override conftest's
# default 'test' origin so the dashboard's attribution panel separates
# regression spend from the rest of the test suite's incidental traffic.
os.environ["ANIME_INTEL_CALL_ORIGIN"] = "regression"

from fastapi.testclient import TestClient  # noqa: E402

from api.main import app  # noqa: E402
from api.middleware.anon_quota import AnonRemaining  # noqa: E402

# Bypass per-IP daily caps for the regression run. Matches tests/test_api.py.
_BYPASS_REMAINING = AnonRemaining(30, 10)
for _mod in ("api.routes.search", "api.routes.dna"):
    patch(f"{_mod}.check_anon_quota", return_value=_BYPASS_REMAINING).start()

client = TestClient(app)

QUERIES_PATH = ROOT / "tests" / "data" / "regression_queries.json"
BASELINE_PATH = ROOT / "tests" / "data" / "regression_baseline.json"
REPORT_PATH = ROOT / "tests" / "data" / "regression_check_report.json"
TOP_K = 10

# Drift thresholds (Phase A spec). Per-query flag when either fails.
#
# Per-verdict Jaccard thresholds. `variance-tolerant` uses a looser
# threshold because the entries curated into that verdict are small-pool
# queries (SLT with 10-15 candidates, FaS with "underrated X" small-
# domain prompts) that hit the Gemini score-tie-resolution noise ceiling
# even against freshly-captured baselines — the 2026-10-14 baseline
# refresh demonstrated 4 entries flapping jaccard 0.46-0.57 against a
# baseline captured ~10 min prior, 3 of 4 with identical top-1. The 0.4
# floor reliably distinguishes genuine drift from Gemini sampling noise
# on these entries without masking real regressions.
JACCARD_THRESHOLD_BY_VERDICT = {
    "keep": 0.6,
    "variance-tolerant": 0.4,
}
KENDALL_THRESHOLD = 0.5

regression = pytest.mark.regression


def build_search_body(entry: dict) -> dict:
    """Translate a regression query entry to a SearchRequest body."""
    body = {
        "query": entry.get("query") or "",
        "lens": entry.get("lens") or "Intelligent Search",
    }
    filters = entry.get("filters_applied")
    if isinstance(filters, dict):
        if filters.get("year_min"):
            body["year_min"] = filters["year_min"]
        if filters.get("year_max"):
            body["year_max"] = filters["year_max"]
        if filters.get("mal_rating_max"):
            body["rating_max"] = filters["mal_rating_max"]
    return body


def build_dna_body(entry: dict) -> dict:
    body = {"titles": entry.get("reference_titles") or []}
    negs = entry.get("negative_anchors")
    if negs:
        body["negative_anchors"] = negs
    return body


def truncate(text, n=120):
    if not text:
        return text
    return text if len(text) <= n else text[: n - 1] + "..."


def replay_entry(entry: dict) -> dict:
    """Submit one entry, return captured top-K results."""
    captured = {
        "search_id": entry.get("search_id"),
        "mode": entry["mode"],
        "source_timestamp": entry.get("timestamp"),
        "expected_rerank_type": entry.get("rerank_type"),
    }
    if entry["mode"] == "find_a_show":
        body = build_search_body(entry)
        captured["request_body"] = body
        captured["intent"] = entry.get("intent")
        resp = client.post("/api/search", json=body)
    elif entry["mode"] == "shows_like_this":
        body = build_dna_body(entry)
        captured["request_body"] = body
        resp = client.post("/api/dna", json=body)
    else:
        captured["error"] = f"unknown mode: {entry['mode']}"
        captured["results"] = []
        return captured

    captured["http_status"] = resp.status_code
    if resp.status_code != 200:
        captured["error"] = resp.text[:500]
        captured["results"] = []
        return captured

    body_json = resp.json()
    # SearchResponse exposes results under `pool`; DNAResponse under `data`.
    if entry["mode"] == "find_a_show":
        scored = body_json.get("pool") or []
        captured["search_id"] = body_json.get("search_id") or captured.get("search_id")
        captured["low_confidence"] = body_json.get("low_confidence")
    else:
        scored = body_json.get("data") or []
        captured["low_confidence"] = body_json.get("low_confidence")
        captured["intersection_summary"] = body_json.get("intersection_summary")
        captured["title_resolution_notes"] = body_json.get("title_resolution_notes")
        captured["negative_anchors_used"] = body_json.get("negative_anchors_used")

    captured["results_count"] = len(scored)
    captured["match_confidence_top"] = (
        scored[0].get("match_confidence") if scored else None
    )
    captured["results"] = [
        {
            "title": (r.get("profile") or {}).get("title"),
            "release_year": (r.get("profile") or {}).get("release_year"),
            "quality_score": (r.get("profile") or {}).get("quality_score"),
            "semantic_similarity": (r.get("profile") or {}).get("semantic_similarity"),
            "match_confidence": r.get("match_confidence"),
            "ai_reasoning": r.get("ai_reasoning"),
            "query_relevant_con": r.get("query_relevant_con"),
            "contains_spoiler": r.get("contains_spoiler"),
        }
        for r in scored[:TOP_K]
    ]
    return captured


def capture_baseline(limit: int | None = None) -> int:
    if not QUERIES_PATH.exists():
        print(f"ERROR: queries not found at {QUERIES_PATH}", file=sys.stderr)
        return 1
    queries = json.loads(QUERIES_PATH.read_text(encoding="utf-8"))
    entries = queries["entries"]
    if limit:
        entries = entries[:limit]
    print(f"Replaying {len(entries)} entries against the engine via TestClient...")
    print()

    captured = []
    failures = []
    start = datetime.now(timezone.utc)
    for i, entry in enumerate(entries, 1):
        ident = entry.get("query") or " + ".join(entry.get("reference_titles") or [])
        prefix = f"  [{i:>3}/{len(entries)}] ({entry['mode'][:3]}) {truncate(ident, 55):<55}"
        try:
            result = replay_entry(entry)
            captured.append(result)
            if result.get("http_status", 200) != 200:
                print(f"{prefix} -> HTTP {result['http_status']}")
                failures.append((ident, f"HTTP {result['http_status']}"))
            else:
                top = result.get("match_confidence_top")
                n = result.get("results_count", 0)
                top_title = (result["results"][0]["title"]) if result.get("results") else "—"
                print(f"{prefix} -> n={n:<2} top={top} {truncate(top_title, 35)}")
        except Exception as e:
            print(f"{prefix} -> EXC {type(e).__name__}: {e}")
            failures.append((ident, f"EXC {type(e).__name__}: {e}"))
            captured.append({
                "search_id": entry.get("search_id"),
                "mode": entry["mode"],
                "source_timestamp": entry.get("timestamp"),
                "error": f"{type(e).__name__}: {e}",
                "results": [],
            })
    elapsed = (datetime.now(timezone.utc) - start).total_seconds()

    payload = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds": round(elapsed, 1),
        "queries_source": str(QUERIES_PATH.relative_to(ROOT)).replace("\\", "/"),
        "queries_generated_at": queries.get("generated_at"),
        "queries_seed": queries.get("random_seed"),
        "entry_count": len(captured),
        "failure_count": len(failures),
        "top_k": TOP_K,
        "entries": captured,
    }
    BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    BASELINE_PATH.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print()
    print(f"Elapsed: {elapsed:.1f}s")
    print(f"Wrote {len(captured)} entries ({len(failures)} failed) to "
          f"{BASELINE_PATH.relative_to(ROOT)}")
    if failures:
        print()
        print("Failures:")
        for ident, reason in failures[:10]:
            print(f"  - {truncate(ident, 60)}: {reason}")
    return 0 if not failures else 1


# -----------------------------------------------------------------------
# Phase E — drift detection
# -----------------------------------------------------------------------


def jaccard_top_k(a_titles: list, b_titles: list, k: int = TOP_K) -> float:
    """Jaccard similarity on the top-K title sets. 1.0 when both empty."""
    a_set = {t for t in (a_titles or [])[:k] if t}
    b_set = {t for t in (b_titles or [])[:k] if t}
    union = a_set | b_set
    if not union:
        return 1.0
    return len(a_set & b_set) / len(union)


def kendall_tau_top_k(a_titles: list, b_titles: list, k: int = TOP_K):
    """
    Kendall tau over the common subset of top-K. Returns None when the
    overlap is too small to compute (< 2 shared titles). 1.0 for an empty-
    overlap edge case where all shared pairs are ties (impossible here but
    documented for safety).
    """
    a_list = [t for t in (a_titles or [])[:k] if t]
    b_list = [t for t in (b_titles or [])[:k] if t]
    common = [t for t in a_list if t in b_list]
    if len(common) < 2:
        return None
    a_rank = {t: i for i, t in enumerate(a_list)}
    b_rank = {t: i for i, t in enumerate(b_list)}
    concordant = discordant = 0
    n = len(common)
    for i in range(n):
        for j in range(i + 1, n):
            ti, tj = common[i], common[j]
            sign = (a_rank[ti] - a_rank[tj]) * (b_rank[ti] - b_rank[tj])
            if sign > 0:
                concordant += 1
            elif sign < 0:
                discordant += 1
    total = concordant + discordant
    if total == 0:
        return 1.0
    return (concordant - discordant) / total


def stratified_subset(entries: list, target_size: int = 13, seed: int = 42) -> list:
    """
    Pick a stratified slice of non-excluded entries that covers all
    rerank_types proportionally. Used by the CI fast tier so PR checks
    don't burn 30-45 minutes on every push.

    Slots are allocated proportionally to each rerank_type's share of
    the non-excluded pool, with a floor of 1 per type so no rerank
    surface is skipped. Deterministic given a fixed seed.
    """
    rng = random.Random(seed)
    by_rt = defaultdict(list)
    for e in entries:
        if (e.get("curation") or {}).get("verdict") == "exclude":
            continue
        rt = e.get("expected_rerank_type") or "?"
        by_rt[rt].append(e)
    if not by_rt:
        return []

    rts = sorted(by_rt.keys())
    total = sum(len(by_rt[rt]) for rt in rts)
    slots = {rt: max(1, round(len(by_rt[rt]) / total * target_size)) for rt in rts}
    # Trim or pad to hit target_size exactly
    while sum(slots.values()) > target_size:
        largest = max(slots, key=lambda r: slots[r])
        if slots[largest] <= 1:
            break
        slots[largest] -= 1
    while sum(slots.values()) < target_size:
        smallest = min(slots, key=lambda r: slots[r])
        smallest_capped = len(by_rt[smallest])
        if slots[smallest] >= smallest_capped:
            # Pool exhausted; pick from any non-exhausted bucket
            candidates = [rt for rt in rts if slots[rt] < len(by_rt[rt])]
            if not candidates:
                break
            slots[candidates[0]] += 1
        else:
            slots[smallest] += 1

    selected = []
    for rt in rts:
        sample = rng.sample(by_rt[rt], min(slots[rt], len(by_rt[rt])))
        selected.extend(sample)
    return selected


def baseline_to_queries_format(baseline_entry: dict) -> dict:
    """
    Adapter — baseline entries store request fields under `request_body`
    (it's a captured artifact); `replay_entry` was built for the queries
    file where request fields live at the top level. This bridges the two
    without forking the harness.
    """
    req = baseline_entry.get("request_body") or {}
    mode = baseline_entry.get("mode")
    out = {
        "mode": mode,
        "search_id": baseline_entry.get("search_id"),
        "timestamp": baseline_entry.get("source_timestamp"),
    }
    if mode == "find_a_show":
        out["query"] = req.get("query")
        out["lens"] = req.get("lens")
        out["intent"] = baseline_entry.get("intent")
        filters = {}
        for k in ("year_min", "year_max"):
            if req.get(k):
                filters[k] = req[k]
        if req.get("rating_max"):
            filters["mal_rating_max"] = req["rating_max"]
        out["filters_applied"] = filters or None
    elif mode == "shows_like_this":
        out["reference_titles"] = req.get("titles") or []
        out["negative_anchors"] = req.get("negative_anchors")
    return out


def compare_entry(baseline_entry: dict, new_capture: dict) -> dict:
    """
    Compute metrics and flag reasons for one entry. The verdict on the
    baseline entry determines which thresholds apply.
    """
    curation = baseline_entry.get("curation") or {}
    verdict = curation.get("verdict", "keep")

    baseline_titles = [r.get("title") for r in (baseline_entry.get("results") or [])]
    new_titles = [r.get("title") for r in (new_capture.get("results") or [])]

    jaccard = jaccard_top_k(baseline_titles, new_titles)
    kendall = kendall_tau_top_k(baseline_titles, new_titles)

    result = {
        "verdict": verdict,
        "jaccard": round(jaccard, 3),
        "kendall_tau": round(kendall, 3) if kendall is not None else None,
        "baseline_count": len(baseline_titles),
        "new_count": len(new_titles),
        "baseline_top": baseline_titles[0] if baseline_titles else None,
        "new_top": new_titles[0] if new_titles else None,
    }

    # Jaccard drives pass/fail. It measures whether the engine surfaces
    # meaningfully different shows for the query — the actual drift signal.
    #
    # Kendall tau measures rank consistency, but Gemini's score variance
    # produces frequent ranking shuffles within stable result sets even
    # on identical inputs. Phase E smoke runs showed ~60% of `keep` entries
    # tripping kendall < 0.5 on the first probe with no engine change.
    # The original Phase A plan included Kendall in the fail condition;
    # the empirical finding (2026-06-02) is that Jaccard alone is the
    # meaningful drift signal. Kendall is recorded as a `warnings` entry
    # for human-review visibility but does not block.
    flag_reasons = []
    warnings = []
    status = "pass"

    if verdict in ("keep", "variance-tolerant"):
        threshold = JACCARD_THRESHOLD_BY_VERDICT[verdict]
        if jaccard < threshold:
            flag_reasons.append("jaccard_below_threshold")
            status = "fail"
        if verdict == "keep" and kendall is not None and kendall < KENDALL_THRESHOLD:
            warnings.append("kendall_below_threshold")
    elif verdict == "known-bad":
        # Detect when a future engine fix changes recorded known-bad
        # behavior. Not a failure — surfaced so the human reviewer can
        # confirm the fix and refresh the baseline. Uses the tightest
        # (keep) threshold so a behavior change is detected even on
        # small-pool known-bad entries.
        if jaccard < JACCARD_THRESHOLD_BY_VERDICT["keep"]:
            flag_reasons.append("known_bad_behavior_changed")
            status = "behavior_changed"

    result["flag_reasons"] = flag_reasons
    result["warnings"] = warnings
    result["status"] = status
    return result


def run_regression_check(limit: int | None = None, subset: int | None = None) -> int:
    """
    Replay each non-excluded baseline entry through the engine, compare
    against the recorded baseline, write a per-entry report. Returns 0
    when no `keep`/`variance-tolerant` entries flagged; 1 otherwise.

    `subset` (int): pick a stratified slice of that size via
    `stratified_subset()`. Used by the CI fast tier.
    `limit` (int): truncate to the first N entries after subset
    selection. Mostly for local smoke testing.
    """
    if not BASELINE_PATH.exists():
        print(f"ERROR: baseline not found at {BASELINE_PATH}", file=sys.stderr)
        return 1
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    all_entries = baseline["entries"]
    # Annotate each entry with its original baseline position so the
    # report's entry_index always references the source file regardless
    # of subset/limit selection.
    #
    # IMPORTANT: _source_idx is 1-BASED (enumerate(..., 1)) to match the
    # human-readable `[1/N]`, `[2/N]`, ... progress log printed below. The
    # value propagates verbatim to report entries' `entry_index` field, so
    # any downstream code joining the report back to `baseline["entries"]`
    # must subtract 1 before indexing. See the `all_entries[... - 1]` at
    # the end of this function for the receiver pattern.
    indexed_entries = []
    for i, e in enumerate(all_entries, 1):
        e_with_idx = dict(e)
        e_with_idx["_source_idx"] = i
        indexed_entries.append(e_with_idx)
    if subset:
        entries = stratified_subset(indexed_entries, target_size=subset)
        print(f"Subset selected: {len(entries)} entries "
              f"(stratified by rerank_type, seed=42)")
    else:
        entries = indexed_entries
    if limit:
        entries = entries[:limit]
    print(f"Checking {len(entries)} baseline entries against the live engine...")
    print()

    report_entries = []
    start = datetime.now(timezone.utc)
    for i, entry in enumerate(entries, 1):
        source_idx = entry.get("_source_idx", i)
        curation = entry.get("curation") or {}
        verdict = curation.get("verdict", "keep")
        ident = entry.get("request_body", {}).get("query") or " + ".join(
            entry.get("request_body", {}).get("titles") or []
        )
        prefix = (
            f"  [{i:>3}/{len(entries)}] (#{source_idx:>2} {verdict:<18}) "
            f"{truncate(ident, 40):<40}"
        )

        if verdict == "exclude":
            print(f"{prefix} -> skipped")
            report_entries.append({
                # entry_index is 1-based (see enumerate at line ~472) —
                # downstream joiners must use baseline["entries"][entry_index - 1].
                "entry_index": source_idx,
                "mode": entry["mode"],
                "verdict": verdict,
                "status": "skipped",
            })
            continue

        try:
            new_capture = replay_entry(baseline_to_queries_format(entry))
        except Exception as e:
            print(f"{prefix} -> EXC {type(e).__name__}: {e}")
            report_entries.append({
                "entry_index": source_idx,
                "mode": entry["mode"],
                "verdict": verdict,
                "status": "error",
                "error": f"{type(e).__name__}: {e}",
            })
            continue

        if new_capture.get("http_status", 200) != 200:
            print(f"{prefix} -> HTTP {new_capture['http_status']}")
            report_entries.append({
                "entry_index": source_idx,
                "mode": entry["mode"],
                "verdict": verdict,
                "status": "error",
                "error": f"HTTP {new_capture['http_status']}",
            })
            continue

        cmp_result = compare_entry(entry, new_capture)
        cmp_result["entry_index"] = source_idx
        cmp_result["mode"] = entry["mode"]
        report_entries.append(cmp_result)

        status = cmp_result["status"]
        suffix_parts = []
        if status == "fail":
            suffix_parts.append(f"FAIL[{','.join(cmp_result['flag_reasons'])}]")
        elif status == "behavior_changed":
            suffix_parts.append(f"CHANGED[{','.join(cmp_result['flag_reasons'])}]")
        if cmp_result.get("warnings"):
            suffix_parts.append(f"warn[{','.join(cmp_result['warnings'])}]")
        suffix = (" " + " ".join(suffix_parts)) if suffix_parts else ""
        k_str = cmp_result.get("kendall_tau")
        k_str = f"{k_str}" if k_str is not None else "—"
        print(f"{prefix} -> j={cmp_result['jaccard']} k={k_str}{suffix}")
    elapsed = (datetime.now(timezone.utc) - start).total_seconds()

    by_status = Counter(r["status"] for r in report_entries)
    by_verdict_failed = Counter(
        r["verdict"] for r in report_entries if r["status"] == "fail"
    )
    by_rerank_type_failed = Counter()
    for r in report_entries:
        if r["status"] != "fail":
            continue
        # entry_index is 1-based (set via enumerate(..., 1) at line ~472);
        # subtract 1 to index into the 0-based baseline["entries"] list.
        be = all_entries[r["entry_index"] - 1]
        rt = be.get("expected_rerank_type") or "?"
        by_rerank_type_failed[rt] += 1
    total_warnings = sum(1 for r in report_entries if r.get("warnings"))

    payload = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds": round(elapsed, 1),
        "baseline_path": str(BASELINE_PATH.relative_to(ROOT)).replace("\\", "/"),
        "baseline_captured_at": baseline.get("captured_at"),
        "entry_count": len(entries),
        "thresholds": {
            "jaccard_by_verdict": JACCARD_THRESHOLD_BY_VERDICT,
            "kendall_tau": KENDALL_THRESHOLD,
        },
        "summary": {
            "by_status": dict(by_status),
            "by_verdict_failed": dict(by_verdict_failed),
            "by_rerank_type_failed": dict(by_rerank_type_failed),
            "warning_count": total_warnings,
        },
        "entries": report_entries,
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    failures = [r for r in report_entries if r["status"] == "fail"]
    changes = [r for r in report_entries if r["status"] == "behavior_changed"]

    print()
    print(f"Elapsed: {elapsed:.1f}s")
    print(f"Status counts: {dict(by_status)}")
    if by_verdict_failed:
        print(f"Failed by verdict: {dict(by_verdict_failed)}")
    if by_rerank_type_failed:
        print(f"Failed by rerank_type: {dict(by_rerank_type_failed)}")
    print(f"Failures (Jaccard drift on keep/variance-tolerant): {len(failures)}")
    print(f"Known-bad behavior changes: {len(changes)}")
    print(f"Soft warnings (kendall_below_threshold etc.): {total_warnings}")
    print(f"Report: {REPORT_PATH.relative_to(ROOT)}")
    return 0 if not failures else 1


# -----------------------------------------------------------------------
# pytest entry point
# -----------------------------------------------------------------------


@regression
def test_engine_regression_check_passes():
    """
    Run the full regression check against the curated baseline; fail when
    the engine has drifted on `keep` or `variance-tolerant` entries
    (Jaccard < 0.6 on the top-K set). Slow (~30-45 min); skipped in
    default runs. Opt-in via `pytest -m regression`.

    Per the 2026-06-02 Phase E refinement, Kendall tau is recorded for
    visibility but does not drive failures — Gemini's score variance
    produces frequent rank shuffles within stable sets that aren't real
    drift. `known-bad` behavior changes are surfaced as informational
    (status=behavior_changed) so the human can confirm a fix and
    refresh the baseline.

    The baseline file is a private artifact (gitignored). Run
    `--capture-baseline` to generate it locally before running this test.
    """
    import pytest
    if not BASELINE_PATH.exists():
        pytest.skip(
            f"Regression baseline not found at {BASELINE_PATH.relative_to(ROOT)} "
            f"— private artifact. Run: python tests/test_engine_regression.py "
            f"--capture-baseline"
        )
    exit_code = run_regression_check()
    assert exit_code == 0, (
        "Regression check found Jaccard drift on one or more keep/variance-"
        "tolerant entries. See "
        f"{REPORT_PATH.relative_to(ROOT)} for per-entry diff."
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture-baseline", action="store_true",
                        help="Replay queries and overwrite "
                             "tests/data/regression_baseline.json")
    parser.add_argument("--check-regression", action="store_true",
                        help="Compare engine output against curated baseline; "
                             "write tests/data/regression_check_report.json")
    parser.add_argument("--subset", type=int, default=None,
                        help="Run a stratified subset of N entries (fast tier; "
                             "covers all rerank_types proportionally)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Run only the first N entries (smoke test)")
    args = parser.parse_args()

    if args.capture_baseline:
        sys.exit(capture_baseline(limit=args.limit))
    if args.check_regression:
        sys.exit(run_regression_check(limit=args.limit, subset=args.subset))

    print("Specify --capture-baseline or --check-regression.")
    sys.exit(2)
