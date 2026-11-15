"""
Render tests/data/regression_check_report.json as a compact markdown
summary suitable for GITHUB_STEP_SUMMARY. Emits to stdout; the CI
workflow redirects into $GITHUB_STEP_SUMMARY.

Failure highlights and behavior_changed entries are shown inline so a
reviewer can triage from the run summary alone, without downloading the
full JSON artifact.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT_PATH = ROOT / "tests" / "data" / "regression_check_report.json"


def main() -> int:
    if not REPORT_PATH.exists():
        print("**Engine Regression — no report generated** "
              "(check step likely failed before writing).")
        return 0
    d = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    s = d.get("summary", {})

    print("## Engine Regression Check")
    print()
    print(f"- **Entries checked:** {d['entry_count']}")
    print(f"- **Elapsed:** {d['elapsed_seconds']}s")
    by_status = s.get("by_status", {})
    print("- **Status counts:** "
          + ", ".join(f"{k}={v}" for k, v in by_status.items()))
    if s.get("by_verdict_failed"):
        print(f"- **Failed by verdict:** {s['by_verdict_failed']}")
    if s.get("by_rerank_type_failed"):
        print(f"- **Failed by rerank_type:** {s['by_rerank_type_failed']}")
    print(f"- **Soft warnings (Kendall etc.):** {s.get('warning_count', 0)}")
    print(f"- **Baseline:** `{d.get('baseline_path')}` "
          f"captured {d.get('baseline_captured_at')}")
    print()

    failures = [e for e in d["entries"] if e.get("status") == "fail"]
    changes = [e for e in d["entries"] if e.get("status") == "behavior_changed"]

    if failures:
        print(f"### Failures ({len(failures)})")
        print()
        print("| # | mode | verdict | jaccard | baseline top | new top |")
        print("|---|---|---|---|---|---|")
        for e in failures:
            j = e.get("jaccard")
            b_top = (e.get("baseline_top") or "—")[:35]
            n_top = (e.get("new_top") or "—")[:35]
            print(f"| #{e['entry_index']} | {e['mode']} | "
                  f"{e.get('verdict')} | {j} | {b_top} | {n_top} |")
        print()

    if changes:
        print(f"### Known-bad changes ({len(changes)})")
        print()
        print("These entries were marked `known-bad` in the curated baseline; "
              "a Jaccard drift here means the engine's behavior on the "
              "documented failure mode has changed. Confirm whether this is "
              "the expected fix and refresh the baseline if so.")
        print()
        print("| # | mode | jaccard | baseline top | new top |")
        print("|---|---|---|---|---|")
        for e in changes:
            j = e.get("jaccard")
            b_top = (e.get("baseline_top") or "—")[:35]
            n_top = (e.get("new_top") or "—")[:35]
            print(f"| #{e['entry_index']} | {e['mode']} | {j} | "
                  f"{b_top} | {n_top} |")
        print()

    if not failures and not changes:
        print("### No actionable findings")
        print()
        print("All checked entries passed. Soft warnings (Kendall tau drops) "
              "may be present in the report; those reflect Gemini's ranking "
              "noise within stable sets and aren't actionable.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
