"""
tools/quadrant_analysis.py

Offline analysis: plots Bayesian-adjusted MAL quality vs. log popularity
for every rated anime in the vault, classifies each into one of four
quadrants (Crowd Pleasers, Hidden Gems, Overhyped, Deep Cuts), and
labels the most distinctive title in each quadrant.

Inputs:
    data/anime_intelligence_v2.db (read-only)

Outputs:
    data/quadrant_analysis.png  — labeled scatter plot
    data/quadrant_summary.txt   — per-quadrant top-10 lists + medians

Quadrant axes:
    X = log10(scored_by)                       — popularity (vote count)
    Y = Bayesian-adjusted score                — quality
        formula matches analysis/queries.py    — C=7.05, m=5000

Boundaries are the medians of each axis (data-driven, not arbitrary).

Usage:
    python tools/quadrant_analysis.py
"""
import math
import sqlite3
import sys
from pathlib import Path

import matplotlib.pyplot as plt

ROOT_DIR = Path(__file__).resolve().parent.parent
DB_PATH = ROOT_DIR / "data" / "anime_intelligence_v2.db"
PNG_PATH = ROOT_DIR / "data" / "quadrant_analysis.png"
TXT_PATH = ROOT_DIR / "data" / "quadrant_summary.txt"
# Optimized web-serving copy. Downsized + recompressed from PNG_PATH for the
# `/how-it-works` "About the Data" section. The high-res source stays in data/
# (gitignored) for archival; this copy is committed and served as
# `/quadrant_analysis.png` by Vercel.
PUBLIC_PNG_PATH = ROOT_DIR / "frontend" / "public" / "quadrant_analysis.png"
PUBLIC_PNG_TARGET_WIDTH = 1200  # plenty for max-w-3xl (768px) + 2x retina

# --- Bayesian smoothing (must match analysis/queries.py) ---
BAYESIAN_GLOBAL_MEAN = 7.05
BAYESIAN_MIN_VOTES = 5000

# Minimum vote count for a show to qualify as a labeled outlier or appear in
# the per-quadrant top-10 summary. Shows with fewer votes have Bayesian
# scores compressed toward the prior (≈7.05), so their quality signal is
# noise — they'd otherwise crowd out genuine outliers in the labels.
# Set at the Bayesian m parameter, beyond which the smoothing weight on the
# raw score is ≥ 50% and quality differentiation becomes meaningful.
# All shows still appear in the scatter plot itself.
MIN_LABEL_VOTES = 5000

# Per-quadrant ranking for "most interesting outlier." Each quadrant has a
# different signature — Crowd Pleasers reward both axes, Hidden Gems reward
# raw quality (since they're already by definition niche), Overhyped reward
# the gap between popularity and quality, Deep Cuts reward extremity in both
# directions of being low.
SORT_KEYS = {
    "crowd_pleasers": lambda r: r["quality"] + r["popularity"],
    "hidden_gems":    lambda r: r["quality"],
    "overhyped":      lambda r: r["popularity"] - r["quality"],
    "deep_cuts":      lambda r: -(r["quality"] + r["popularity"]),
}

# --- Visual palette (matches Anime Intel dark theme) ---
COLORS = {
    "crowd_pleasers": "#22c55e",  # green   — top-right (high quality, high popularity)
    "hidden_gems":    "#a78bfa",  # purple  — top-left  (high quality, low popularity)
    "overhyped":      "#ef4444",  # red     — bottom-right (low quality, high popularity)
    "deep_cuts":      "#94a3b8",  # gray    — bottom-left  (low quality, low popularity)
}

LEGEND_LABELS = {
    "crowd_pleasers": "Crowd Pleasers — high quality, mainstream",
    "hidden_gems":    "Hidden Gems — high quality, niche audience",
    "overhyped":      "Overhyped — lower quality, mainstream",
    "deep_cuts":      "Deep Cuts — lower quality, niche",
}

# Per-quadrant label offsets — three distinct directions per quadrant so multiple
# picks fan out instead of stacking. Arrows from annotations connect each label
# back to its data point, so generous spreading is fine. All directions point
# away from the chart center toward the quadrant's corner of the screen.
LABEL_OFFSETS = {
    "crowd_pleasers": [(18, 14), (-25, 28), (28, -10)],
    "hidden_gems":    [(-18, 14), (22, 28), (-28, -10)],
    "overhyped":      [(18, -14), (-25, -28), (28, 12)],
    "deep_cuts":      [(-18, -14), (22, -28), (-28, 12)],
}


def bayesian_quality(raw_score: float, vote_count: int) -> float:
    """Same formula as analysis/queries.py — shrinks low-vote shows toward the global mean."""
    v = vote_count
    m = BAYESIAN_MIN_VOTES
    C = BAYESIAN_GLOBAL_MEAN
    return (v / (v + m)) * raw_score + (m / (v + m)) * C


def load_anime() -> tuple[list[dict], int]:
    """
    Load all rated anime from SQLite (read-only). Returns (rows, total_in_vault).

    The vault's true row count and the plotted count differ when MAL hasn't
    published an aggregate score for a title yet (typically very-niche
    kids/experimental shows below MAL's display threshold). Surfacing both
    counts lets the chart explain the gap to readers comparing it against
    the live site count.
    """
    if not DB_PATH.exists():
        print(f"ERROR: database not found at {DB_PATH}")
        sys.exit(1)

    uri = f"file:{DB_PATH}?mode=ro"
    rows = []
    with sqlite3.connect(uri, uri=True) as conn:
        total_in_vault = conn.execute("SELECT COUNT(*) FROM anime_info").fetchone()[0]
        cursor = conn.execute("""
            SELECT id, english_title, romaji_title, mal_score, scored_by, studio, release_year
            FROM anime_info
            WHERE mal_score IS NOT NULL
              AND scored_by IS NOT NULL
              AND scored_by > 0
        """)
        for anime_id, eng_title, romaji_title, raw_score, scored_by, studio, year in cursor.fetchall():
            display_title = eng_title or romaji_title or f"#{anime_id}"
            rows.append({
                "id": anime_id,
                "title": display_title,
                "raw_score": raw_score,
                "scored_by": scored_by,
                "quality": bayesian_quality(raw_score, scored_by),
                "popularity": math.log10(scored_by),
                "studio": studio,
                "year": year,
            })
    return rows, total_in_vault


def median(values: list[float]) -> float:
    s = sorted(values)
    n = len(s)
    if n % 2:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) / 2


def stdev(values: list[float], mean: float) -> float:
    if len(values) < 2:
        return 1.0
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (len(values) - 1))


def assign_quadrant(quality: float, popularity: float, q_med: float, p_med: float) -> str:
    high_q = quality >= q_med
    high_p = popularity >= p_med
    if high_q and high_p:
        return "crowd_pleasers"
    if high_q and not high_p:
        return "hidden_gems"
    if not high_q and high_p:
        return "overhyped"
    return "deep_cuts"


def distinctiveness(row: dict, q_med: float, p_med: float, q_std: float, p_std: float) -> float:
    """
    Z-score distance from the median crosshair. Larger = further into the
    quadrant's signature corner = more exemplary of that quadrant's character.
    """
    nq = (row["quality"] - q_med) / q_std
    np_ = (row["popularity"] - p_med) / p_std
    return math.sqrt(nq * nq + np_ * np_)


def truncate(text: str, n: int = 28) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def diverse_top_n(candidates: list[dict], n: int, sort_key, min_distance: float = 0.4) -> list[dict]:
    """
    Pick n diverse outliers: highest-scoring first, then iteratively pick the
    next highest that's at least `min_distance` (in chart coordinates) from
    all previously-picked. Prevents the "all top picks cluster in one corner
    and their labels overlap" problem.

    Falls back to top remaining if we can't find enough diverse picks.
    """
    if not candidates:
        return []

    sorted_cands = sorted(candidates, key=sort_key, reverse=True)
    picks = [sorted_cands[0]]

    for c in sorted_cands[1:]:
        if len(picks) >= n:
            break
        nearest = min(
            math.hypot(c["popularity"] - p["popularity"], c["quality"] - p["quality"])
            for p in picks
        )
        if nearest >= min_distance:
            picks.append(c)

    # Fallback: if we couldn't find n diverse picks, fill with top remaining
    if len(picks) < n:
        existing_ids = {p["id"] for p in picks}
        for c in sorted_cands:
            if c["id"] not in existing_ids:
                picks.append(c)
                if len(picks) >= n:
                    break

    return picks


def build_plot(rows: list[dict], q_med: float, p_med: float, q_std: float, p_std: float, total_in_vault: int):
    by_quadrant: dict[str, list[dict]] = {k: [] for k in COLORS}
    for r in rows:
        q = assign_quadrant(r["quality"], r["popularity"], q_med, p_med)
        r["quadrant"] = q
        r["distinctiveness"] = distinctiveness(r, q_med, p_med, q_std, p_std)
        by_quadrant[q].append(r)

    fig, ax = plt.subplots(figsize=(14, 9), dpi=120)
    fig.patch.set_facecolor("#0f172a")
    ax.set_facecolor("#0f172a")

    # --- Scatter, one series per quadrant for the legend ---
    for q, items in by_quadrant.items():
        if not items:
            continue
        ax.scatter(
            [r["popularity"] for r in items],
            [r["quality"] for r in items],
            c=COLORS[q],
            alpha=0.45,
            s=14,
            edgecolors="none",
            label=f"{LEGEND_LABELS[q]}  ({len(items):,})",
        )

    # --- Median dividers ---
    ax.axhline(q_med, color="#fbbf24", linewidth=1.0, alpha=0.6, linestyle="--")
    ax.axvline(p_med, color="#fbbf24", linewidth=1.0, alpha=0.6, linestyle="--")

    # --- Outlier annotations: top 3 per quadrant by quadrant-specific sort ---
    # Filter to shows with enough votes that their quality signal is real;
    # otherwise undersampled shows clustering near the Bayesian prior
    # crowd out genuinely interesting outliers.
    # diverse_top_n enforces spatial separation; cycling through LABEL_OFFSETS
    # gives each pick a different direction so labels fan out even when data
    # points cluster tightly.
    for q, items in by_quadrant.items():
        candidates = [r for r in items if r["scored_by"] >= MIN_LABEL_VOTES]
        top = diverse_top_n(candidates, n=3, sort_key=SORT_KEYS[q], min_distance=0.4)
        offsets = LABEL_OFFSETS[q]
        for i, r in enumerate(top):
            dx, dy = offsets[i % len(offsets)]
            ax.annotate(
                truncate(r["title"]),
                xy=(r["popularity"], r["quality"]),
                xytext=(dx, dy),
                textcoords="offset points",
                fontsize=8,
                color="#f1f5f9",
                bbox=dict(
                    facecolor="#1e293b",
                    edgecolor=COLORS[q],
                    alpha=0.92,
                    boxstyle="round,pad=0.3",
                    linewidth=0.8,
                ),
                arrowprops=dict(arrowstyle="-", color=COLORS[q], alpha=0.5, linewidth=0.6),
            )

    # --- Styling ---
    p_med_scored = 10 ** p_med
    ax.set_xlabel(
        "Popularity  (log₁₀ of MAL ratings count)",
        color="#cbd5e1",
        fontsize=11,
    )
    ax.set_ylabel(
        "Quality  (Bayesian-adjusted MAL score)",
        color="#cbd5e1",
        fontsize=11,
    )
    ax.set_title(
        f"Quality vs. Popularity Quadrant — {len(rows):,} rated anime in the Anime Intel vault",
        color="#f1f5f9",
        fontsize=14,
        pad=18,
    )
    ax.tick_params(colors="#94a3b8")
    for spine in ax.spines.values():
        spine.set_color("#334155")
    ax.grid(True, alpha=0.10, color="#475569", linestyle=":")

    legend = ax.legend(
        loc="lower left",
        frameon=True,
        fontsize=9,
        facecolor="#1e293b",
        edgecolor="#334155",
        labelcolor="#e2e8f0",
        title="Quadrant",
        title_fontsize=10,
    )
    legend.get_title().set_color("#cbd5e1")

    excluded = total_in_vault - len(rows)
    footnote = (
        f"Quadrant boundaries (medians):  quality = {q_med:.2f}   |   "
        f"popularity = {p_med_scored:,.0f} ratings"
    )
    if excluded > 0:
        footnote += (
            f"    ·   {excluded} of {total_in_vault:,} vault titles excluded "
            f"(no MAL aggregate score yet)"
        )
    footnote += "    ·   Source: animeintel.app"
    ax.text(
        0.005,
        -0.085,
        footnote,
        transform=ax.transAxes,
        color="#94a3b8",
        fontsize=8,
    )

    plt.tight_layout(rect=(0, 0.02, 1, 1))
    plt.savefig(PNG_PATH, dpi=120, facecolor=fig.get_facecolor(), bbox_inches="tight")
    plt.close(fig)

    # Web-optimized copy for the About the Data section. Source PNG is
    # ~440KB at 1680x1080 with thousands of alpha-blended scatter points.
    # Two-step optimization: (1) downsize to 1200px wide; (2) quantize to
    # an adaptive 256-color palette. The chart has limited distinct colors
    # (4 quadrant tints + axis lines + text), so 256 colors preserves
    # visual quality while cutting the file ~4x. Target ≤ 200KB.
    from PIL import Image
    src = Image.open(PNG_PATH).convert("RGB")
    if src.width > PUBLIC_PNG_TARGET_WIDTH:
        ratio = PUBLIC_PNG_TARGET_WIDTH / src.width
        new_size = (PUBLIC_PNG_TARGET_WIDTH, int(src.height * ratio))
        src = src.resize(new_size, Image.LANCZOS)
    quantized = src.quantize(colors=256, method=Image.Quantize.MEDIANCUT)
    PUBLIC_PNG_PATH.parent.mkdir(parents=True, exist_ok=True)
    quantized.save(PUBLIC_PNG_PATH, "PNG", optimize=True)

    return by_quadrant


def write_summary(rows, by_quadrant, q_med, p_med):
    p_med_scored = 10 ** p_med
    lines = []
    lines.append("Quality vs. Popularity Quadrant Analysis")
    lines.append("=" * 60)
    lines.append("")
    lines.append(f"Total rated anime: {len(rows):,}")
    lines.append(f"Quality median (Bayesian-adjusted MAL score): {q_med:.3f}")
    lines.append(f"Popularity median: {p_med_scored:,.0f} ratings  (log10 = {p_med:.3f})")
    lines.append("")
    lines.append("Quadrant counts:")
    for q in ("crowd_pleasers", "hidden_gems", "overhyped", "deep_cuts"):
        lines.append(f"  {LEGEND_LABELS[q]:<55s}  {len(by_quadrant[q]):>5,}")
    lines.append("")

    quadrant_order = ["crowd_pleasers", "hidden_gems", "overhyped", "deep_cuts"]

    for q in quadrant_order:
        items = by_quadrant[q]
        candidates = [r for r in items if r["scored_by"] >= MIN_LABEL_VOTES]
        lines.append(f"--- Top 10: {LEGEND_LABELS[q]} ---")
        lines.append(f"    (filtered to shows with ≥ {MIN_LABEL_VOTES:,} ratings; "
                     f"{len(candidates):,} of {len(items):,} qualify)")
        top = sorted(candidates, key=SORT_KEYS[q], reverse=True)[:10]
        for r in top:
            year = r["year"] if r["year"] else "?"
            studio = r["studio"] if r["studio"] else "unknown"
            lines.append(
                f"  {r['title']:<40s}  Q={r['quality']:.2f}  "
                f"votes={r['scored_by']:>9,}  ({year}, {studio})"
            )
        lines.append("")

    with open(TXT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def main():
    print(f"Loading from {DB_PATH}...")
    rows, total_in_vault = load_anime()
    excluded = total_in_vault - len(rows)
    print(f"Loaded {len(rows):,} rated anime  ({excluded} of {total_in_vault:,} excluded — no MAL score).")

    qualities = [r["quality"] for r in rows]
    popularities = [r["popularity"] for r in rows]

    q_med = median(qualities)
    p_med = median(popularities)
    q_std = stdev(qualities, sum(qualities) / len(qualities))
    p_std = stdev(popularities, sum(popularities) / len(popularities))

    print("\nQuadrant boundaries (medians):")
    print(f"  Quality:    {q_med:.3f}  (std {q_std:.3f})")
    print(f"  Popularity: {p_med:.3f} log10  =  {10 ** p_med:,.0f} ratings  (std {p_std:.3f})")

    by_quadrant = build_plot(rows, q_med, p_med, q_std, p_std, total_in_vault)
    print(f"\nSaved scatter plot:  {PNG_PATH}")
    print(f"Saved web copy:      {PUBLIC_PNG_PATH}")

    write_summary(rows, by_quadrant, q_med, p_med)
    print(f"Saved text summary:  {TXT_PATH}")

    print("\nQuadrant counts:")
    for q in ("crowd_pleasers", "hidden_gems", "overhyped", "deep_cuts"):
        print(f"  {q:<18s}  {len(by_quadrant[q]):>5,}")


if __name__ == "__main__":
    main()
