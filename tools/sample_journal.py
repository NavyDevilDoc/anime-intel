"""
Search Journal Sampler — pull random samples from the search journal for manual review.

Usage:
    python tools/sample_journal.py                  # 10 random entries, summary view
    python tools/sample_journal.py -n 25            # 25 random entries
    python tools/sample_journal.py --full           # Full detail (all result fields)
    python tools/sample_journal.py --mode dna       # Filter by mode (find_a_show, dna)
    python tools/sample_journal.py --lens Objective  # Filter by lens substring
    python tools/sample_journal.py --query mecha    # Filter by query substring
    python tools/sample_journal.py --export sample.json  # Export sample to JSON file
    python tools/sample_journal.py --stats          # Show aggregate statistics only
"""

import argparse
import json
import os
import random
import sys

# Bulletproof path anchoring
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JOURNAL_PATH = os.path.join(ROOT_DIR, "data", "search_journal.jsonl")


def load_journal():
    """Load all journal entries."""
    if not os.path.exists(JOURNAL_PATH):
        print(f"Journal not found at {JOURNAL_PATH}")
        sys.exit(1)
    entries = []
    with open(JOURNAL_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return entries


def filter_entries(entries, mode=None, lens=None, query=None):
    """Apply optional filters."""
    filtered = entries
    if mode:
        filtered = [e for e in filtered if e.get("mode", "").lower() == mode.lower()]
    if lens:
        filtered = [e for e in filtered if lens.lower() in e.get("lens", "").lower()]
    if query:
        filtered = [e for e in filtered if query.lower() in e.get("query", "").lower()]
    return filtered


def print_summary(entry, idx):
    """Print a compact summary of one journal entry."""
    results = entry.get("results", [])
    top_titles = [r["title"] for r in results[:5]]
    top_conf = [r.get("confidence", 0) for r in results[:5]]

    print(f"\n{'='*70}")
    print(f"  [{idx}] \"{entry.get('query', '?')}\"")
    print(f"  Mode: {entry.get('mode', '?')}  |  Lens: {entry.get('lens', '?')}  |  Era: {entry.get('era') or 'All'}")
    print(f"  Intent: {entry.get('intent', '?')}  |  Path: {entry.get('retrieval_path', '?')}")
    if entry.get("concepts_detected"):
        concepts = entry["concepts_detected"]
        # Truncate long concept strings to first word
        short = [c.split()[0] if isinstance(c, str) else str(c) for c in concepts]
        print(f"  Concepts: {', '.join(short)}")
    if entry.get("filters_applied"):
        print(f"  Filters: {entry['filters_applied']}")
    print(f"  Candidates: {entry.get('candidates_retrieved', '?')} retrieved -> {len(results)} after rerank")
    print("  Top 5:")
    for i, title in enumerate(top_titles):
        conf = top_conf[i] if i < len(top_conf) else "?"
        qual = results[i].get("quality_score", "?") if i < len(results) else "?"
        sim = results[i].get("similarity", 0) if i < len(results) else 0
        print(f"    {i+1}. {title}  (conf={conf}%, qual={qual}, sim={sim:.3f})")


def print_full(entry, idx):
    """Print full detail of one journal entry."""
    print(f"\n{'='*70}")
    print(json.dumps(entry, indent=2, ensure_ascii=False))


def print_stats(entries):
    """Print aggregate statistics."""
    print(f"\nJournal Statistics ({len(entries)} entries)")
    print("=" * 50)

    # Mode breakdown
    modes = {}
    for e in entries:
        m = e.get("mode", "unknown")
        modes[m] = modes.get(m, 0) + 1
    print("\nBy mode:")
    for m, c in sorted(modes.items(), key=lambda x: -x[1]):
        print(f"  {m}: {c}")

    # Lens breakdown
    lenses = {}
    for e in entries:
        l = e.get("lens", "unknown")
        lenses[l] = lenses.get(l, 0) + 1
    print("\nBy lens:")
    for l, c in sorted(lenses.items(), key=lambda x: -x[1]):
        print(f"  {l}: {c}")

    # Intent breakdown
    intents = {}
    for e in entries:
        i = e.get("intent", "unknown")
        intents[i] = intents.get(i, 0) + 1
    print("\nBy intent:")
    for i, c in sorted(intents.items(), key=lambda x: -x[1]):
        print(f"  {i}: {c}")

    # Result count stats
    result_counts = [len(e.get("results", [])) for e in entries]
    if result_counts:
        print("\nResults per search:")
        print(f"  Mean: {sum(result_counts)/len(result_counts):.1f}")
        print(f"  Median: {sorted(result_counts)[len(result_counts)//2]}")
        print(f"  Min: {min(result_counts)}  Max: {max(result_counts)}")
        zero = sum(1 for c in result_counts if c == 0)
        print(f"  Zero-result searches: {zero} ({100*zero/len(result_counts):.1f}%)")

    # Retrieval path breakdown
    paths = {}
    for e in entries:
        p = e.get("retrieval_path", "unknown")
        # Simplify to first segment
        simple = p.split(":")[0] if ":" in p else p
        paths[simple] = paths.get(simple, 0) + 1
    print("\nBy retrieval path:")
    for p, c in sorted(paths.items(), key=lambda x: -x[1]):
        print(f"  {p}: {c}")

    # Top queries
    queries = {}
    for e in entries:
        q = e.get("query", "")
        queries[q] = queries.get(q, 0) + 1
    print("\nMost repeated queries:")
    for q, c in sorted(queries.items(), key=lambda x: -x[1])[:10]:
        print(f"  [{c}x] \"{q}\"")


def main():
    parser = argparse.ArgumentParser(description="Sample and explore the search journal.")
    parser.add_argument("-n", type=int, default=10, help="Number of entries to sample (default: 10)")
    parser.add_argument("--full", action="store_true", help="Show full JSON detail")
    parser.add_argument("--mode", type=str, help="Filter by mode (find_a_show, dna)")
    parser.add_argument("--lens", type=str, help="Filter by lens substring (Intelligent, Objective)")
    parser.add_argument("--query", type=str, help="Filter by query substring")
    parser.add_argument("--export", type=str, help="Export sample to JSON file")
    parser.add_argument("--stats", action="store_true", help="Show aggregate statistics only")
    parser.add_argument("--seed", type=int, help="Random seed for reproducibility")
    args = parser.parse_args()

    entries = load_journal()
    print(f"Loaded {len(entries)} journal entries from {JOURNAL_PATH}")

    if args.stats:
        filtered = filter_entries(entries, args.mode, args.lens, args.query)
        if len(filtered) != len(entries):
            print(f"(filtered to {len(filtered)} entries)")
        print_stats(filtered)
        return

    filtered = filter_entries(entries, args.mode, args.lens, args.query)
    if len(filtered) != len(entries):
        print(f"Filtered to {len(filtered)} entries")

    if not filtered:
        print("No entries match the specified filters.")
        return

    if args.seed is not None:
        random.seed(args.seed)

    n = min(args.n, len(filtered))
    sample = random.sample(filtered, n)
    print(f"Sampling {n} entries...")

    if args.export:
        export_path = os.path.join(ROOT_DIR, args.export)
        with open(export_path, "w", encoding="utf-8") as f:
            json.dump(sample, f, indent=2, ensure_ascii=False)
        print(f"Exported {n} entries to {export_path}")
        return

    printer = print_full if args.full else print_summary
    for i, entry in enumerate(sample, 1):
        printer(entry, i)

    print(f"\n{'='*70}")
    print(f"Sampled {n} of {len(filtered)} entries.")


if __name__ == "__main__":
    main()
