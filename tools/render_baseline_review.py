"""
Render tests/data/regression_baseline.json as a skim-friendly markdown
review document at tests/data/regression_baseline_review.md.

Each entry keeps its 1-indexed position from the harness output so
references like "#23" map cleanly between conversation, the markdown,
and the JSON. Top 3 results per entry — enough to judge whether the
top pick is defensible without burning eyes on the full top-10.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = ROOT / "tests" / "data" / "regression_baseline.json"
OUT_PATH = ROOT / "tests" / "data" / "regression_baseline_review.md"

RESULTS_PER_ENTRY = 3


def fmt_filters(filters):
    if not filters:
        return "none"
    parts = []
    for k, v in filters.items():
        if v in (None, "", [], {}):
            continue
        parts.append(f"`{k}={v}`")
    return ", ".join(parts) if parts else "none"


def render_result(idx: int, r: dict) -> list[str]:
    title = r.get("title") or "(no title)"
    year = r.get("release_year") or "?"
    conf = r.get("match_confidence")
    q_score = r.get("quality_score")
    reasoning = (r.get("ai_reasoning") or "").strip()
    con = (r.get("query_relevant_con") or "").strip()
    spoiler = r.get("contains_spoiler")
    lines = []
    head = f"{idx}. **{title}** ({year}) — match **{conf}%**"
    if q_score is not None:
        head += f" · quality {q_score}"
    if spoiler:
        head += " · ⚠ spoiler"
    lines.append(head)
    if reasoning:
        lines.append(f"   > {reasoning}")
    if con:
        lines.append(f"   *Heads up:* {con}")
    return lines


def render_entry(i: int, e: dict) -> list[str]:
    mode = e.get("mode", "?")
    mode_short = "FaS" if mode == "find_a_show" else "SLT"
    rerank = e.get("expected_rerank_type") or "?"
    intent = e.get("intent")
    src = e.get("source_timestamp") or ""
    src_date = src[:10] if src else ""

    req = e.get("request_body") or {}
    if mode == "find_a_show":
        ident = req.get("query") or "(no query)"
        header_label = f"`{intent}`" if intent else ""
    else:
        ident = " + ".join(req.get("titles") or [])
        negs = req.get("negative_anchors") or []
        if negs:
            ident += f"   *avoiding:* {' + '.join(negs)}"
        header_label = ""

    n = e.get("results_count", 0)
    top = e.get("match_confidence_top")
    banner = "**yes**" if e.get("low_confidence") else "no"
    flag = []
    if n == 0:
        flag.append("**EMPTY**")
    if e.get("low_confidence"):
        flag.append("**LOW CONF**")
    flag_str = f" — {' / '.join(flag)}" if flag else ""

    lines = []
    title_bits = [f"#{i}", mode_short, rerank]
    if header_label:
        title_bits.append(header_label)
    lines.append(f"### {' · '.join(title_bits)}{flag_str}")
    lines.append("")
    lines.append(f"**Input:** {ident}")
    filters_str = fmt_filters(req)
    if mode == "find_a_show":
        # exclude query/lens which we already showed
        non_query = {k: v for k, v in req.items() if k not in ("query", "lens")}
        filters_str = fmt_filters(non_query)
        lines.append(f"**Filters:** {filters_str}  ")
        lens = req.get("lens")
        if lens and lens != "Intelligent Search":
            lines.append(f"**Lens:** {lens}  ")
    src_marker = "augmentation" if src.startswith("2026-05-31") else "historical"
    lines.append(f"**Source:** {src_marker} · {src_date}  ")
    lines.append(f"**Results:** n={n}, top={top}, banner={banner}")

    if mode == "shows_like_this":
        inter = e.get("intersection_summary")
        if inter:
            lines.append("")
            lines.append(f"**Intersection summary:** {inter}")
        notes = e.get("title_resolution_notes")
        if notes:
            lines.append("")
            lines.append(f"**Title-resolution notes:** {notes}")
        negs_used = e.get("negative_anchors_used")
        if negs_used:
            lines.append("")
            lines.append(f"**Negatives used:** {negs_used}")

    results = e.get("results") or []
    if results:
        lines.append("")
        for idx, r in enumerate(results[:RESULTS_PER_ENTRY], 1):
            lines.extend(render_result(idx, r))
    else:
        lines.append("")
        lines.append("*(no results)*")
    lines.append("")
    lines.append("---")
    lines.append("")
    return lines


def main() -> int:
    if not BASELINE_PATH.exists():
        print(f"ERROR: baseline not found at {BASELINE_PATH}")
        return 1
    data = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    entries = data.get("entries", [])

    out = []
    out.append("# Regression Baseline Review")
    out.append("")
    out.append(f"- **Captured:** {data.get('captured_at')}")
    out.append(f"- **Source queries:** {data.get('queries_source')} (seed {data.get('queries_seed')})")
    out.append(f"- **Entries:** {data.get('entry_count')} ({data.get('failure_count')} failures)")
    out.append(f"- **Elapsed:** {data.get('elapsed_seconds')/60:.1f} min")
    out.append(f"- **top_k captured:** {data.get('top_k')} (this report shows top {RESULTS_PER_ENTRY})")
    out.append("")
    out.append("Entry numbers (#1-75) match the harness output and the index "
               "in `regression_baseline.json`. Skim, report verdicts in "
               "conversation, the JSON gets curated based on those.")
    out.append("")
    out.append("## Verdict shorthand")
    out.append("")
    out.append("- **keep** — current top result is defensible; freeze as expected baseline")
    out.append("- **known-bad** — current behavior is wrong; baseline records it, future fix should change it")
    out.append("- **exclude** — entry should not be in the regression suite at all")
    out.append("- **variance-tolerant** — high-variance query; accept either-of multiple outputs")
    out.append("")
    out.append("---")
    out.append("")

    for i, entry in enumerate(entries, 1):
        out.extend(render_entry(i, entry))

    OUT_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"Wrote {len(entries)} entries to {OUT_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
