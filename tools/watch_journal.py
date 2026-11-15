"""
tools/watch_journal.py

Live pretty-printer for data/search_journal.jsonl. Tails the journal and
formats each new entry for Phase 2 testing visibility — query, negatives,
intersection summary, top picks with confidence/similarity/quality
side-by-side, pipeline counts (excluded, prefiltered, after rerank), and
per-stage timings.

Usage:
    python tools/watch_journal.py

Run in a third terminal alongside uvicorn + the frontend dev server.
Each search through the browser produces a formatted block here within
~100ms of the response landing.

Env vars:
    WATCH_JOURNAL_NO_COLOR=1  Disable ANSI colors (useful if your terminal
                              doesn't render them — output will be plain text).
"""
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# Force UTF-8 stdout — anime titles include non-ASCII characters (CJK,
# accented Latin) and cp1252 (the default Windows console encoding for
# Python ≤ 3.14 without UTF-8 mode) will crash on them.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT_DIR = Path(__file__).resolve().parent.parent
JOURNAL_PATH = ROOT_DIR / "data" / "search_journal.jsonl"

# ANSI color codes degrade to plain text on terminals that don't support
# them, or when WATCH_JOURNAL_NO_COLOR=1.
_USE_COLOR = sys.stdout.isatty() and not os.environ.get("WATCH_JOURNAL_NO_COLOR")


def _c(text, code):
    if _USE_COLOR:
        return f"\033[{code}m{text}\033[0m"
    return text


def BOLD(s): return _c(s, "1")
def DIM(s): return _c(s, "2")
def RED(s): return _c(s, "31")
def GREEN(s): return _c(s, "32")
def YELLOW(s): return _c(s, "33")
def CYAN(s): return _c(s, "36")
def MAGENTA(s): return _c(s, "35")


def _ts():
    return datetime.now().strftime("%H:%M:%S")


def _confidence_color(conf):
    if conf >= 85:
        return GREEN(f"{conf:3d}")
    if conf >= 65:
        return YELLOW(f"{conf:3d}")
    return DIM(f"{conf:3d}")


def _format_timings(timings):
    """Compact one-liner: 'retrieval 234ms, rerank 1840ms, total 3103ms'."""
    parts = []
    for key, val in timings.items():
        if not isinstance(val, (int, float)):
            continue
        label = key.replace("_ms", "")
        parts.append(f"{label} {val:.0f}ms")
    return ", ".join(parts)


def _format_results_table(results):
    """Render the top-N results as confidence | similarity | quality columns."""
    lines = []
    lines.append(f"  {BOLD('Top picks:')} {DIM('(conf | sim   | qual)')}")
    for i, r in enumerate(results[:10], 1):
        title = r.get("title", "?")
        conf = int(r.get("confidence", 0))
        sim = float(r.get("similarity", 0.0))
        qual = float(r.get("quality_score", 0.0))
        lines.append(
            f"    {i:2d}. {title[:48]:48s}  {_confidence_color(conf)} | {sim:5.2f} | {qual:4.1f}"
        )
    return lines


def _format_dna_entry(entry):
    """Format a Shows Like This entry — the Phase 2 focus."""
    lines = [BOLD(CYAN(f"=== {_ts()} — Shows Like This ==="))]

    references = entry.get("reference_titles") or []
    negatives = entry.get("negative_anchors") or []

    lines.append(f"  {BOLD('Positive')}: {', '.join(references) if references else '(none)'}")
    if negatives:
        lines.append(f"  {BOLD(RED('Negative'))}: {', '.join(negatives)}")

    if not entry.get("success"):
        lines.append(f"  {BOLD(RED('FAILED'))}: {entry.get('error', 'unknown error')}")
        timings = entry.get("timings", {})
        if timings:
            lines.append(f"  {DIM('Timings:')} {_format_timings(timings)}")
        return "\n".join(lines)

    summary = entry.get("intersection_summary")
    if summary:
        lines.append(f"  {DIM('DNA:')} {summary}")

    retrieved = entry.get("candidates_retrieved", 0)
    excluded = entry.get("excluded_count", 0)
    prefiltered = entry.get("prefiltered_count", 0)
    after_rerank = entry.get("candidates_after_rerank", 0)
    neg_count = entry.get("negative_anchor_count", 0)
    pipeline_bits = [
        f"retrieved {retrieved}",
        f"excluded {excluded}",
        f"prefiltered {prefiltered}" if neg_count > 0 else None,
        f"after rerank {after_rerank}",
    ]
    pipeline_str = ", ".join(b for b in pipeline_bits if b is not None)
    lines.append(f"  {DIM('Pipeline:')} {pipeline_str}")

    results = entry.get("results") or []
    if results:
        lines.extend(_format_results_table(results))

    timings = entry.get("timings", {})
    if timings:
        lines.append(f"  {DIM('Timings:')} {_format_timings(timings)}")

    return "\n".join(lines)


def _format_find_a_show_entry(entry):
    """Format a Find a Show entry — secondary mode, lighter touch."""
    lines = [BOLD(MAGENTA(f"=== {_ts()} — Find a Show ==="))]

    query = entry.get("query") or "(no query)"
    lens = entry.get("lens") or ""
    era = entry.get("era") or None

    lines.append(f"  {BOLD('Query')}: \"{query}\"")
    meta_bits = []
    if lens:
        meta_bits.append(lens)
    if era:
        meta_bits.append(era)
    if meta_bits:
        lines.append(f"  {DIM('Lens:')} {' · '.join(meta_bits)}")

    if not entry.get("success", True):
        lines.append(f"  {BOLD(RED('FAILED'))}: {entry.get('error', 'unknown error')}")
        timings = entry.get("timings", {})
        if timings:
            lines.append(f"  {DIM('Timings:')} {_format_timings(timings)}")
        return "\n".join(lines)

    results = entry.get("results") or []
    if results:
        lines.extend(_format_results_table(results))

    timings = entry.get("timings", {})
    if timings:
        lines.append(f"  {DIM('Timings:')} {_format_timings(timings)}")

    return "\n".join(lines)


def _format_entry(line):
    """Parse one JSONL line and route to the appropriate formatter."""
    line = line.strip()
    if not line:
        return None
    try:
        entry = json.loads(line)
    except json.JSONDecodeError as e:
        return f"  {RED('!!!')} Malformed JSON in journal: {e}\n  {DIM(line[:120])}..."

    mode = entry.get("mode", "")
    if mode == "shows_like_this":
        return _format_dna_entry(entry)
    if mode == "find_a_show":
        return _format_find_a_show_entry(entry)
    return f"  {YELLOW('?')} Unknown journal mode '{mode}' — {DIM(json.dumps(entry)[:200])}"


def _print_startup_banner():
    print(BOLD(GREEN(f"Watching {JOURNAL_PATH}")))
    alpha = os.environ.get("DNA_NEGATIVE_ALPHA")
    if alpha:
        print(f"  {DIM('DNA_NEGATIVE_ALPHA:')} {alpha}")
    else:
        print(f"  {DIM('DNA_NEGATIVE_ALPHA:')} 0.5 (default)")
    print(DIM("(Run a search in the browser to see entries appear here.)"))
    print()


def watch(journal_path):
    """Tail the journal file, printing each new entry as it lands.

    If the journal doesn't exist yet (no searches run since last vault rebuild),
    block until it shows up rather than crashing.
    """
    if not journal_path.exists():
        print(DIM(f"Journal not found yet at {journal_path}"))
        print(DIM("Waiting for the first search to create it..."))
        while not journal_path.exists():
            time.sleep(0.5)

    _print_startup_banner()

    with open(journal_path, encoding="utf-8") as f:
        # Seek to end — only show NEW entries from this point on, not historical ones.
        f.seek(0, 2)
        while True:
            line = f.readline()
            if not line:
                time.sleep(0.1)
                continue
            formatted = _format_entry(line)
            if formatted is not None:
                print(formatted)
                print()


if __name__ == "__main__":
    try:
        watch(JOURNAL_PATH)
    except KeyboardInterrupt:
        print(BOLD("\nStopped."))
        sys.exit(0)
