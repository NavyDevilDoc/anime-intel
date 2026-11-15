"""SQLite read-only access layer for the recommendation engine.

Everything that reads from `data/anime_intelligence_v2.db` funnels
through here (except vector_store.py, which owns its own read-only
connection helper for the FAISS-adjacent paths). Owns:

- `resolve_show_title`: three-tier fuzzy title matcher used by both
  DNA triangulation and Encyclopedia lookup.
- `find_franchise_titles`: prefix-based franchise expansion.
- `fetch_fusion_profiles`: hydrated multi-column packet builder for
  the Phase 2 reranker.
- `fetch_series_siblings` / `expand_to_cluster_ids`: series-cluster
  helpers (PR #77 + PR C watchlist exclusion).
- `staff_autocomplete`: chip type-ahead for Staff/Studio search
  (Item 4a).

The retired `execute_lens_query` SQL-generation path lives in
`Archive/queries_deprecated.py` — kept as historical reference for
the pre-RAG SQL architecture, not imported.
"""
import difflib
import json
import logging
import os
import re
import sqlite3

logger = logging.getLogger(__name__)

# --- BULLETPROOF PATH ANCHORING ---
SCRIPT_DIR = os.path.abspath(os.path.dirname(__file__))
if os.path.basename(SCRIPT_DIR) in ['tools', 'analysis', 'src']:
    ROOT_DIR = os.path.dirname(SCRIPT_DIR)
else:
    ROOT_DIR = SCRIPT_DIR

DB_PATH = os.path.join(ROOT_DIR, "data", "anime_intelligence_v2.db")

def _get_readonly_connection():
    """Forces SQLite into Read-Only mode."""
    if not os.path.exists(DB_PATH):
        raise FileNotFoundError(f"Vault missing at {DB_PATH}")
    uri_path = f"file:{DB_PATH}?mode=ro"
    return sqlite3.connect(uri_path, uri=True)

# --- HUMAN FAULT TOLERANCE ---

def resolve_show_title(user_input):
    """
    Three-Tier Fault Tolerance Engine.
    Returns a LIST of potential official 'english_title' matches.
    """
    if not user_input or len(user_input.strip()) < 2:
        return []

    user_input = user_input.strip()

    with _get_readonly_connection() as conn:
        cursor = conn.cursor()

        # TIER 1: The Exact Match (Fastest) - Case Insensitive
        cursor.execute("SELECT english_title FROM anime_info WHERE english_title COLLATE NOCASE = ? OR romaji_title COLLATE NOCASE = ?", (user_input, user_input))
        rows = cursor.fetchall()
        if rows:
            return list(dict.fromkeys([r[0] for r in rows]))

        # TIER 2: The Punctuation Agnostic / Substring Match (Franchise Collision catcher)
        clean_input = re.sub(r'[^\w\s]', '', user_input)
        wildcard_query = "%" + "%".join(clean_input.split()) + "%"

        cursor.execute("SELECT english_title FROM anime_info WHERE english_title LIKE ? OR romaji_title LIKE ? LIMIT 10", (wildcard_query, wildcard_query))
        rows = cursor.fetchall()
        if rows:
            return list(dict.fromkeys([r[0] for r in rows]))

        # TIER 3: The Typo-Resistant Fuzzy Match (difflib)
        cursor.execute("SELECT english_title, romaji_title FROM anime_info")
        all_titles = cursor.fetchall()

        flat_titles = [t for pair in all_titles for t in pair if t]

        matches = difflib.get_close_matches(user_input, flat_titles, n=5, cutoff=0.5)

        if matches:
            resolved_titles = []
            for match in matches:
                cursor.execute("SELECT english_title FROM anime_info WHERE english_title = ? OR romaji_title = ?", (match, match))
                result = cursor.fetchone()
                if result and result[0] not in resolved_titles:
                    resolved_titles.append(result[0])
            return resolved_titles

    return []


def find_franchise_titles(reference_titles):
    """
    Given a list of canonical english_titles, find all franchise variants
    in the DB. Uses prefix matching (title must START with the reference
    title) to catch sequels/seasons without false positives from substring
    containment (e.g., "Demon Slayer" won't exclude "Blue Demon").

    Returns a set of english_titles that are variants of the reference
    shows. Does NOT include the original reference titles themselves.
    """
    if not reference_titles:
        return set()

    variants = set()
    with _get_readonly_connection() as conn:
        cursor = conn.cursor()
        for title in reference_titles:
            if len(title) < 4:
                continue
            # Prefix match: catches "Attack on Titan Season 2",
            # "Attack on Titan: The Final Season", etc.
            cursor.execute(
                "SELECT english_title FROM anime_info WHERE english_title LIKE ? AND english_title != ?",
                (f"{title}%", title),
            )
            variants.update(row[0] for row in cursor.fetchall())
            # Also check romaji prefix for spelling variants
            # (e.g., "Haikyu!!" vs "Haikyuu!!")
            cursor.execute(
                "SELECT romaji_title FROM anime_info WHERE english_title = ?",
                (title,),
            )
            romaji_row = cursor.fetchone()
            if romaji_row and romaji_row[0] and len(romaji_row[0]) >= 4 and romaji_row[0] != title:
                cursor.execute(
                    "SELECT english_title FROM anime_info WHERE english_title LIKE ? AND english_title != ?",
                    (f"{romaji_row[0]}%", title),
                )
                variants.update(row[0] for row in cursor.fetchall())
    return variants


# --- BAYESIAN SCORE CONSTANTS ---
# C = global mean MAL score across all 5,952 rated shows
# m = prior weight (minimum votes before raw score is trusted)
#
# Also duplicated in vector_store._composite_rerank — both files
# operate on Bayesian-adjusted scores and must agree on the formula.
# Any tuning has to update both call sites together; there's no
# runtime coupling to enforce it.
_BAYESIAN_GLOBAL_MEAN = 7.05
_BAYESIAN_MIN_VOTES = 5000


def _bayesian_score(raw_score, vote_count):
    """
    Bayesian-adjusted MAL score that shrinks low-vote shows toward the
    global mean. Formula: (v/(v+m))*R + (m/(v+m))*C
    """
    if raw_score is None or vote_count is None:
        return 0.0
    v = vote_count
    m = _BAYESIAN_MIN_VOTES
    C = _BAYESIAN_GLOBAL_MEAN
    return (v / (v + m)) * raw_score + (m / (v + m)) * C


# --- SEMANTIC FUSION PACKAGING ---

def fetch_fusion_profiles(candidate_titles):
    """
    Takes candidate titles and builds the rich, multi-variable intelligence
    packets needed for the AI Reranker and Streamlit UI.
    """
    if not candidate_titles:
        return []

    fusion_profiles = []

    with _get_readonly_connection() as conn:
        cursor = conn.cursor()

        placeholders = ','.join(['?'] * len(candidate_titles))

        query = f"""
                SELECT id, english_title, studio, mal_score, scored_by,
                avg_sentiment, consensus_json, release_year,
                mal_synopsis, narrative_json, mal_rating, series_id
                FROM anime_info
                WHERE english_title IN ({placeholders})
        """

        cursor.execute(query, candidate_titles)
        rows = cursor.fetchall()

        for mal_id, title, studio, score, voted, sentiment, raw_json, release_year, synopsis, narrative_raw, mal_rating, series_id in rows:
            try:
                consensus_data = json.loads(raw_json) if raw_json else {}

                try:
                    narrative_data = json.loads(narrative_raw) if narrative_raw else {}
                except (json.JSONDecodeError, TypeError):
                    narrative_data = {}

                profile = {
                    "id": mal_id,
                    "title": title,
                    "studio": studio,
                    "quality_score": round(_bayesian_score(score, voted), 1),
                    "raw_mal_score": score,
                    "scored_by": voted,
                    "audience_sentiment": sentiment,
                    "release_year": release_year,
                    "synopsis": synopsis,
                    "audience_consensus": consensus_data.get("consensus_summary", ""),
                    "pros": consensus_data.get("pros", []),
                    "cons": consensus_data.get("cons", []),
                    "controversy_score": consensus_data.get("controversy_score", 0),
                    "thematic_vibe": consensus_data.get("thematic_vibe", ""),
                    "narrative_data": narrative_data,
                    "mal_rating": mal_rating,
                    "series_id": series_id,
                }
                fusion_profiles.append(profile)
            except json.JSONDecodeError:
                continue

    return fusion_profiles


def fetch_series_siblings(series_ids):
    """Fetch sibling rows for a batch of series_ids.

    Returns {series_id: [sibling_dict, ...]}. Each sibling carries the
    fields needed for SeriesContext computation: id, english_title,
    release_year, mal_score, avg_sentiment, subtype. Siblings are
    ordered by (release_year ASC, id ASC) — the same ordering used by
    compute_series_context for `position` assignment.

    `subtype` carries the MAL anime-type string ("TV" / "TV Special" /
    "Movie" / "OVA" / "Special" / "ONA" / "Music" / "PV" / "CM") or
    None / "" when not yet backfilled. compute_series_context uses it
    to filter the displayed score range to TV+TV Special+Movie entries
    and to compute tv_position / tv_count for the "Season X of Y" label.

    Standalones (series_id == str(self.id)) come back as a single-entry
    list. Empty input returns an empty dict.
    """
    if not series_ids:
        return {}

    unique_ids = list({str(sid) for sid in series_ids if sid is not None})
    if not unique_ids:
        return {}

    out = {sid: [] for sid in unique_ids}
    with _get_readonly_connection() as conn:
        cursor = conn.cursor()
        placeholders = ','.join(['?'] * len(unique_ids))
        cursor.execute(
            f"""
            SELECT id, english_title, release_year, mal_score, avg_sentiment, subtype, series_id
            FROM anime_info
            WHERE series_id IN ({placeholders})
            ORDER BY release_year ASC, id ASC
            """,
            unique_ids,
        )
        for mal_id, title, year, score, sentiment, subtype, sid in cursor.fetchall():
            out[sid].append({
                "id": mal_id,
                "title": title,
                "release_year": year,
                "mal_score": score,
                "avg_sentiment": sentiment,
                "subtype": subtype,
            })
    return out


def expand_to_cluster_ids(mal_ids):
    """Expand a list of MAL IDs to include every cluster sibling.

    PR C (watchlist exclusion) cluster-aware enhancement built on PR #77's
    series_id column. When a user lists "Naruto" (id=20) as watched, the
    watchlist exclusion needs to filter out the entire 19-entry Naruto
    cluster (Shippuden, all movies, Road of Naruto, etc.) — not just the
    original TV. Without this expansion, the user would still see Naruto
    Shippuden recommendations after marking Naruto as completed.

    Behavior
    --------
    For each input MAL ID, looks up its series_id and unions in every
    sibling MAL ID under that series_id. Returns the deduplicated set —
    the original IDs are always included, even when their series_id is
    NULL (standalones or unenriched rows). Unknown MAL IDs (not in
    anime_info) pass through as themselves.

    Empty input returns an empty set.

    SQLite parameter limit (999) handling: when len(mal_ids) > 900, the
    lookup is chunked transparently. Caller-supplied lists from
    watchlists typically run 0-2,000 entries; this keeps the query
    correct across the full range without forcing the caller to chunk.
    """
    if not mal_ids:
        return set()
    input_set = {int(i) for i in mal_ids if i is not None}
    if not input_set:
        return set()

    out = set(input_set)
    series_ids = set()
    input_list = sorted(input_set)
    CHUNK = 900  # below SQLite's 999-parameter limit with headroom

    with _get_readonly_connection() as conn:
        cursor = conn.cursor()
        # Phase 1: collect series_id for each input MAL ID
        for offset in range(0, len(input_list), CHUNK):
            chunk = input_list[offset:offset + CHUNK]
            placeholders = ",".join(["?"] * len(chunk))
            cursor.execute(
                f"SELECT series_id FROM anime_info "
                f"WHERE id IN ({placeholders}) AND series_id IS NOT NULL",
                chunk,
            )
            for (sid,) in cursor.fetchall():
                series_ids.add(sid)

        # Phase 2: collect every sibling MAL ID under those clusters
        if series_ids:
            sid_list = sorted(series_ids)
            for offset in range(0, len(sid_list), CHUNK):
                chunk = sid_list[offset:offset + CHUNK]
                placeholders = ",".join(["?"] * len(chunk))
                cursor.execute(
                    f"SELECT id FROM anime_info "
                    f"WHERE series_id IN ({placeholders})",
                    chunk,
                )
                for (mid,) in cursor.fetchall():
                    out.add(mid)
    return out


# --- STAFF / STUDIO AUTOCOMPLETE (Item 4a, Phase 2) ---


def staff_autocomplete(user_query, limit=5):
    """Return top-N people and studios matching the user's typed query
    for Staff/Studio mode's chip type-ahead (Item 4a).

    Query normalization matches ``tools/backfill_staff.normalize_name`` —
    lowercase with punctuation stripped — so the LIKE match against
    ``people.name_normalized`` is punctuation-tolerant ("O'Brien" and
    "obrien" both resolve). Studios are matched case-insensitively
    against ``anime_info.studio`` since that column isn't normalized.

    Each person suggestion carries a per-role breakdown so the frontend
    can offer role-scoped chips alongside a pinned "Any role" option
    without a second round-trip:

        Any role · Watanabe (12 shows)
        Director · Watanabe (10 shows)
        Series Composition · Watanabe (2 shows)

    Returns:
        {
            "persons": [
                {"person_mal_id": int, "name_display": str,
                 "total_count": int,
                 "roles": [{"role": str, "count": int}, ...]},
                ...
            ],
            "studios": [
                {"name": str, "show_count": int},
                ...
            ],
        }
        Both lists capped at ``limit`` entries. Empty input yields
        empty lists; caller can treat that as the "start typing" state.
    """
    if not user_query or not user_query.strip():
        return {"persons": [], "studios": []}

    # Normalize the user query to match the shape stored in name_normalized.
    normalized = re.sub(r"[^\w\s]", "", user_query.lower())
    normalized = re.sub(r"\s+", " ", normalized).strip()
    # Strip SQL LIKE wildcards from the user side — user text as literal
    # match target, not a pattern language. Prevents "%" from matching
    # everything and "_" from matching arbitrary single chars.
    normalized = normalized.replace("%", "").replace("_", "")
    if not normalized:
        return {"persons": [], "studios": []}

    person_pattern = f"%{normalized}%"
    # Studio column isn't normalized, so keep original casing/punctuation
    # for the pattern but strip wildcards for the same reason.
    raw_query = user_query.strip().replace("%", "").replace("_", "")
    studio_pattern = f"%{raw_query}%"

    with _get_readonly_connection() as conn:
        cursor = conn.cursor()

        # Query 1: top-N people by unique-show count. DISTINCT anime_id
        # collapses multi-role rows for the same show (Watanabe as
        # Director + Series Composition on Bebop = 1 show, not 2) so
        # total_count matches what the frontend renders on the chip
        # ("Any role · <person> · N shows"). Ranking by unique-show
        # count is a v1 signal — prolific character designers can
        # outrank iconic directors on common surnames; users refine
        # by typing more characters. v2 could weight by scored_by
        # aggregation to prefer creators of popular shows.
        cursor.execute(
            """
            SELECT p.person_mal_id, p.name_display,
                   COUNT(DISTINCT s.anime_id) AS total_count
            FROM people p
            JOIN anime_staff s ON s.person_mal_id = p.person_mal_id
            WHERE p.name_normalized LIKE ?
            GROUP BY p.person_mal_id
            ORDER BY total_count DESC, p.name_display ASC
            LIMIT ?
            """,
            (person_pattern, limit),
        )
        top_people = cursor.fetchall()

        persons = []
        if top_people:
            person_ids = [row[0] for row in top_people]
            placeholders = ",".join(["?"] * len(person_ids))

            # Query 2: per-role counts for those people. DISTINCT anime_id
            # collapses "Watanabe: Director + Series Composition on Bebop"
            # into one show per role (not two).
            cursor.execute(
                f"""
                SELECT person_mal_id, role, COUNT(DISTINCT anime_id) AS role_count
                FROM anime_staff
                WHERE person_mal_id IN ({placeholders})
                GROUP BY person_mal_id, role
                ORDER BY role_count DESC
                """,
                person_ids,
            )
            role_map = {}
            for pid, role, count in cursor.fetchall():
                role_map.setdefault(pid, []).append(
                    {"role": role, "count": count}
                )

            for pid, name, total in top_people:
                persons.append({
                    "person_mal_id": pid,
                    "name_display": name,
                    "total_count": total,
                    "roles": role_map.get(pid, []),
                })

        # Query 3: top-N distinct studio strings by show count.
        # anime_info.studio holds free-form co-production strings like
        # "Bones, Studio Rikka" — returned verbatim so the user picks
        # which combination they want. The Staff/Studio filter uses LIKE
        # against this string, so picking "Bones" catches both the
        # standalone and co-production variants.
        cursor.execute(
            """
            SELECT studio, COUNT(*) AS show_count
            FROM anime_info
            WHERE studio LIKE ? COLLATE NOCASE
              AND studio IS NOT NULL AND studio != ''
            GROUP BY studio
            ORDER BY show_count DESC, studio ASC
            LIMIT ?
            """,
            (studio_pattern, limit),
        )
        studios = [
            {"name": row[0], "show_count": row[1]}
            for row in cursor.fetchall()
        ]

    return {"persons": persons, "studios": studios}
