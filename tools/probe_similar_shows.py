"""Item 3 Phase B — Similar-shows generator probe.

Runs a small canonical-show slice through the proposed ranking formula
so we can judge the results qualitatively before committing to full-vault
generation.

Ranking formula (Phase A agreed):
    composite = 0.55 * similarity_normalized
              + 0.15 * bayesian_quality_normalized
              + personnel_bonus

Where personnel_bonus sums:
    +0.15 if candidate shares Director with reference
    +0.10 if candidate shares Music composer
    +0.05 if candidate shares Original Creator (excluding pseudonyms)
    +0.03 if candidate shares Series Composition
    +0.03 if candidate shares Character Design

Filters:
    - Drop candidates with mal_score < 6.0 (quality floor)
    - Drop candidates in the same series_id (franchise dedup)
    - Drop the reference show itself

Provenance-tier priority (highest wins):
    1. Director
    2. Music composer
    3. Original Creator (real named, not pseudonym)
    4. Thematic fallback (from consensus_json.thematic_vibe)

Cost: $0 (pure FAISS + SQL, no Gemini). Wallclock: ~2-5 min for 20 shows.
"""
from __future__ import annotations

import io
import json
import sqlite3
import sys
from pathlib import Path

# Windows cp1252 default console can't print titles like "∀ Gundam" or
# other non-ASCII romanization; force UTF-8 on stdout so the probe output
# stays intact.
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from analysis import vector_store

DB_PATH = ROOT / "data" / "anime_intelligence_v2.db"

# ------------------------------------------------------------------------
# Phase A design constants
# ------------------------------------------------------------------------

# Per-role allowlists of "signal-degenerate" personnel to skip in
# personnel scoring. Splitting by role because the pollution patterns
# differ — Original Creator has studio/IP-holder pseudonyms; Music has
# classical composers whose public-domain works get licensed into
# unrelated shows.
_SKIP_BY_ROLE: dict[str, frozenset[int]] = {
    # Sunrise/Toei/Bushiroad/Capcom/Nitroplus corporate & pseudonym
    # credits. Discovered during Item 3 Phase A via top-15 Original
    # Creator ranking analysis + hypothesis-testing (Yatate sole vs
    # shared credit — confirms "sole = brand-IP, shared = real
    # co-creator carries the signal").
    "Original Creator": frozenset({
        2197,   # Hajime Yatate (Sunrise)
        3940,   # Izumi Toudou (Toei Animation)
        55301,  # Takaaki Kidani (Bushiroad business president)
        14955,  # Capcom (video game company IP)
        4960,   # Nitroplus (visual novel company)
    }),
    # Classical composers whose public-domain works get credited as
    # "Music" for anime that license them for scenes but don't share
    # any real compositional fingerprint. Discovered during Item 3
    # Phase B round 1 — Cowboy Bebop credited Tchaikovsky, NGE
    # credited Bach + Beethoven + Handel + Rachmaninoff, Madoka
    # credited Bach + Gounod + Rachmaninoff. "Music by Johann
    # Sebastian Bach" is not a meaningful similar-shows signal.
    "Music": frozenset({
        9160,   # Ludwig van Beethoven
        9159,   # Johann Sebastian Bach
        9178,   # Pyotr Ilyich Tchaikovsky
        11768,  # Wolfgang Amadeus Mozart
        9162,   # Sergei Rachmaninoff
        11779,  # Georg Friedrich Handel
        24725,  # Charles Gounod
    }),
}

_ROLE_BONUS: dict[str, float] = {
    "Director": 0.15,
    "Music": 0.10,
    "Original Creator": 0.05,
    "Series Composition": 0.03,
    "Character Design": 0.03,
}

# Provenance-tier priority — lower index = higher priority
_PROVENANCE_TIER_ORDER = ("Director", "Music", "Original Creator")

_W_SIMILARITY = 0.55
_W_QUALITY = 0.15
_QUALITY_FLOOR = 6.0

# From analysis/vector_store.py — mirror for consistency
_MAL_SCORE_MIN = 2.87
_MAL_SCORE_MAX = 9.28
_BAYESIAN_GLOBAL_MEAN = 7.05
_BAYESIAN_MIN_VOTES = 5000


# ------------------------------------------------------------------------
# Data loading
# ------------------------------------------------------------------------

def load_show_metadata(conn: sqlite3.Connection) -> dict[str, dict]:
    """Build title -> {id, mal_score, scored_by, series_id, consensus_json}
    lookup. Keyed on english_title (canonical form used by FAISS metadata)."""
    cur = conn.cursor()
    lookup: dict[str, dict] = {}
    for row in cur.execute("""
        SELECT id, english_title, mal_score, scored_by, series_id,
               consensus_json, release_year
        FROM anime_info
        WHERE english_title IS NOT NULL
    """):
        aid, title, score, votes, sid, cjson, year = row
        lookup[title] = {
            "anime_id": aid,
            "mal_score": score or 0.0,
            "scored_by": votes or 0,
            "series_id": sid,
            "consensus_json": cjson,
            "release_year": year,
        }
    return lookup


def load_staff_by_anime(conn: sqlite3.Connection) -> dict[int, dict[str, set[int]]]:
    """For every anime_id, return {role: {person_mal_id, ...}}. Applies
    the per-role skip lists for signal-degenerate entities (studio
    pseudonyms in Original Creator, classical composers in Music)."""
    cur = conn.cursor()
    out: dict[int, dict[str, set[int]]] = {}
    for anime_id, person_id, role in cur.execute(
        "SELECT anime_id, person_mal_id, role FROM anime_staff"
    ):
        skip_set = _SKIP_BY_ROLE.get(role)
        if skip_set and person_id in skip_set:
            continue
        out.setdefault(anime_id, {}).setdefault(role, set()).add(person_id)
    return out


def share_franchise_signal(ref_title: str, cand_title: str,
                           min_shared: int = 10) -> bool:
    """Fuzzy franchise-sibling detector for cases series_id missed.

    PR #77 established series_id clustering; PR #79 added 42 manual
    overrides. Some franchise spinoffs still slip through — e.g.,
    "Puella Magi Madoka Magica" (series_id=9756) and "Magia Record:
    Puella Magi Madoka Magica Side Story" have different series_ids
    but are clearly the same franchise from a user perspective.

    Heuristic: (a) shorter title fully contained in longer AND shorter
    is >= 5 chars, OR (b) both titles share a substring of length
    >= min_shared. The 10-char default catches "Code Geass", "Cowboy
    Bebop", "Ghost in the Shell", "Puella Magi Madoka" while avoiding
    "K-On!" / "FLCL" false positives.
    """
    ref = ref_title.lower()
    cand = cand_title.lower()
    shorter, longer = (ref, cand) if len(ref) <= len(cand) else (cand, ref)

    # Case (a): full containment with minimum length guard
    if len(shorter) >= 5 and shorter in longer:
        return True

    # Case (b): shared substring of length >= min_shared
    max_check = min(len(shorter), 30)
    for length in range(max_check, min_shared - 1, -1):
        for i in range(len(shorter) - length + 1):
            if shorter[i:i + length] in longer:
                return True
    return False


def load_people_names(conn: sqlite3.Connection) -> dict[int, str]:
    cur = conn.cursor()
    return {
        row[0]: row[1] for row in cur.execute(
            "SELECT person_mal_id, name_display FROM people"
        )
    }


# ------------------------------------------------------------------------
# Composite scoring
# ------------------------------------------------------------------------

def bayesian_norm_quality(mal_score: float, scored_by: int) -> float:
    v, m, C = scored_by, _BAYESIAN_MIN_VOTES, _BAYESIAN_GLOBAL_MEAN
    if v + m == 0:
        return 0.0
    bayesian = (v / (v + m)) * mal_score + (m / (v + m)) * C
    n = (bayesian - _MAL_SCORE_MIN) / (_MAL_SCORE_MAX - _MAL_SCORE_MIN)
    return max(0.0, min(1.0, n))


def compute_personnel_bonus(
    ref_staff: dict[str, set[int]],
    cand_staff: dict[str, set[int]],
) -> tuple[float, dict[str, set[int]]]:
    """Return (total_bonus, {role: {shared_person_ids}}).

    The shared-person dict is used later for provenance-string generation
    without re-computing overlaps."""
    total = 0.0
    shared: dict[str, set[int]] = {}
    for role, weight in _ROLE_BONUS.items():
        overlap = ref_staff.get(role, set()) & cand_staff.get(role, set())
        if overlap:
            total += weight
            shared[role] = overlap
    return total, shared


def build_provenance(
    shared: dict[str, set[int]],
    people_names: dict[int, str],
    cand_meta: dict,
) -> str:
    """Priority: Director -> Music -> Original Creator -> thematic fallback.
    Falls through to consensus_json.thematic_vibe when no shared personnel."""
    for role in _PROVENANCE_TIER_ORDER:
        if role in shared:
            # Pick one representative person from the shared set — the one
            # with the lowest mal_id (stable arbitrary choice)
            pid = min(shared[role])
            name = people_names.get(pid, f"person #{pid}")
            if role == "Director":
                return f"Also directed by {name}"
            if role == "Music":
                return f"Music by {name}"
            if role == "Original Creator":
                return f"Adapted from a work by {name}"

    # Thematic fallback — extract from consensus_json.thematic_vibe
    if cand_meta.get("consensus_json"):
        try:
            cj = json.loads(cand_meta["consensus_json"])
            vibe = cj.get("thematic_vibe")
            if vibe:
                # Trim to a short phrase for card copy
                vibe = vibe.split(".")[0].strip()
                return f"Similar theme: {vibe[:80]}"
        except (json.JSONDecodeError, TypeError):
            pass

    return "Similar thematic match"


# ------------------------------------------------------------------------
# Main pipeline
# ------------------------------------------------------------------------

def rank_similar_shows(
    reference_title: str,
    title_lookup: dict[str, dict],
    staff_by_anime: dict[int, dict[str, set[int]]],
    people_names: dict[int, str],
    top_k: int = 6,
    faiss_fetch_k: int = 100,
) -> list[dict]:
    """Return the top-K similar shows for `reference_title` with scoring
    and provenance metadata."""
    ref_meta = title_lookup.get(reference_title)
    if not ref_meta:
        return []

    ref_aid = ref_meta["anime_id"]
    ref_series = ref_meta["series_id"]
    ref_staff = staff_by_anime.get(ref_aid, {})

    # Get reference embedding by looking up its position in FAISS
    index, metadata = vector_store._load_index()
    title_to_pos = {m["english_title"]: i for i, m in enumerate(metadata)}
    ref_pos = title_to_pos.get(reference_title)
    if ref_pos is None:
        return []

    ref_embedding = index.reconstruct(ref_pos).reshape(1, -1)
    raw_results = vector_store._search_faiss_raw(ref_embedding, faiss_fetch_k)

    scored: list[dict] = []
    for r in raw_results:
        cand_title = r["title"]
        if cand_title == reference_title:
            continue

        cand_meta = title_lookup.get(cand_title)
        if not cand_meta:
            continue

        # Quality floor
        if cand_meta["mal_score"] < _QUALITY_FLOOR:
            continue

        # Same-franchise exclusion — series_id first (structural), then
        # fuzzy title check for cases series_id missed
        if ref_series and cand_meta["series_id"] == ref_series:
            continue
        if share_franchise_signal(reference_title, cand_title):
            continue

        cand_aid = cand_meta["anime_id"]
        cand_staff = staff_by_anime.get(cand_aid, {})

        similarity = r["similarity"]
        norm_quality = bayesian_norm_quality(
            cand_meta["mal_score"], cand_meta["scored_by"]
        )
        personnel_bonus, shared = compute_personnel_bonus(ref_staff, cand_staff)

        composite = (
            _W_SIMILARITY * similarity
            + _W_QUALITY * norm_quality
            + personnel_bonus
        )
        provenance = build_provenance(shared, people_names, cand_meta)

        scored.append({
            "title": cand_title,
            "anime_id": cand_aid,
            "series_id": cand_meta["series_id"],
            "release_year": cand_meta["release_year"],
            "mal_score": cand_meta["mal_score"],
            "scored_by": cand_meta["scored_by"],
            "similarity": similarity,
            "norm_quality": norm_quality,
            "personnel_bonus": personnel_bonus,
            "composite": composite,
            "shared_roles": {r: len(pids) for r, pids in shared.items()},
            "provenance": provenance,
        })

    # Candidate franchise dedup — for each unique series_id among the
    # scored candidates, keep only the top-scoring entry. Prevents Gintama
    # taking 4 of 6 slots on Champloo, K-ON seasons crowding Bocchi, etc.
    # Same-series entries with a NULL series_id (rare — pre-PR-#77 gaps)
    # are treated as distinct, which is the safe default.
    scored.sort(key=lambda x: x["composite"], reverse=True)
    seen_series: set[int] = set()
    deduped: list[dict] = []
    for cand in scored:
        sid = cand["series_id"]
        if sid is not None and sid in seen_series:
            continue
        if sid is not None:
            seen_series.add(sid)
        deduped.append(cand)
        if len(deduped) >= top_k:
            break
    return deduped


def main() -> int:
    conn = sqlite3.connect(str(DB_PATH))
    print("[probe] Loading show metadata + staff...")
    title_lookup = load_show_metadata(conn)
    staff_by_anime = load_staff_by_anime(conn)
    people_names = load_people_names(conn)
    print(f"[probe] {len(title_lookup):,} shows in title lookup")
    print(f"[probe] {sum(len(v) for v in staff_by_anime.values()):,} staff attributions")
    print()

    # Canonical probe slice — chosen to test multiple axes:
    #   - Classic vs modern, popular vs niche, distinctive style
    #   - Multi-franchise anchors that will stress the Sunrise-Yatate filter
    #   - Composer-signal test (Music-heavy shows)
    canonical = [
        "Cowboy Bebop",              # Watanabe + Kanno + Yatate-only. Champloo?
        "Samurai Champloo",          # Reverse of above — should surface Bebop
        "Neon Genesis Evangelion",   # Anno signature + Sagisu
        "Attack on Titan",           # Sawano composer, popular anchor
        "Steins;Gate",               # Sci-fi thriller
        "Mushi-Shi",                 # Meditative/atmospheric
        "Serial Experiments Lain",   # Konaka series comp, distinctive
        "Kino's Journey",            # Older philosophical
        "Berserk",                   # 1997 Berserk
        "Made in Abyss",             # Kevin Penkin signature
        "Frieren: Beyond Journey's End",  # Recent quality anchor
        "Code Geass: Lelouch of the Rebellion",  # Sunrise mecha — Yatate stress
        "Mobile Suit Gundam: Iron-Blooded Orphans",  # Sunrise + Yatate + Tomino stress
        "FLCL",                      # Distinctive style, the pillows composer
        "Puella Magi Madoka Magica", # Shinbou director, Kajiura composer
        "Ghost in the Shell: Stand Alone Complex",  # Kawai composer test
        "Monster",                   # Long-form drama
        "Legend of the Galactic Heroes",  # Space opera classic
        "Bocchi the Rock!",          # Music genre — composer signal
        "K-ON!",                     # Music genre — composer test
    ]

    print("=" * 100)
    for title in canonical:
        print(f"\n{'=' * 100}")
        print(f"REFERENCE: {title}")
        ref_meta = title_lookup.get(title)
        if ref_meta:
            ref_staff = staff_by_anime.get(ref_meta["anime_id"], {})
            role_summary = ", ".join(
                f"{r}={','.join(people_names.get(p, str(p)) for p in sorted(pids))}"
                for r, pids in sorted(ref_staff.items())
            )
            print(f"  Meta: year={ref_meta['release_year']}  mal={ref_meta['mal_score']}  votes={ref_meta['scored_by']:,}  series_id={ref_meta['series_id']}")
            if role_summary:
                # Truncate for readability
                print(f"  Ref staff: {role_summary[:220]}{'...' if len(role_summary) > 220 else ''}")
        print("-" * 100)

        recs = rank_similar_shows(title, title_lookup, staff_by_anime, people_names)
        if not recs:
            print("  NO RECOMMENDATIONS (reference title not in FAISS or lookup)")
            continue

        for i, r in enumerate(recs, 1):
            print(f"  #{i}  composite={r['composite']:.3f}  "
                  f"sim={r['similarity']:.3f}  q={r['norm_quality']:.3f}  "
                  f"personnel={r['personnel_bonus']:.2f}  "
                  f"mal={r['mal_score']}")
            print(f"       {r['title']}  ({r['release_year']})")
            print(f"       Provenance: {r['provenance']}")
            if r['shared_roles']:
                print(f"       Shared roles: {r['shared_roles']}")

    conn.close()
    print()
    print("=" * 100)
    print("[probe] Complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
