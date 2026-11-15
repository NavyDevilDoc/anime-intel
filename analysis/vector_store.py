"""FAISS-based semantic retrieval layer for the recommendation engine.

Owns query embedding via Google gemini-embedding-001, FAISS index
build/update, hybrid SQL+FAISS filtered retrieval, intent classification,
composite reranking (Bayesian quality / popularity / recency / similarity),
and DNA centroid search for Shows Like This. Phase 2 (Gemini reranking) is
downstream of this module and lives in recommendation_engine.py.
"""

import json
import logging
import os
import re
import sqlite3
import sys
import time

import faiss
import numpy as np
from dotenv import load_dotenv

from analysis import gemini_client

# --- BULLETPROOF PATH ANCHORING ---
SCRIPT_DIR = os.path.abspath(os.path.dirname(__file__))
if os.path.basename(SCRIPT_DIR) in ['tools', 'analysis', 'src']:
    ROOT_DIR = os.path.dirname(SCRIPT_DIR)
else:
    ROOT_DIR = SCRIPT_DIR

if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(ROOT_DIR, "data", "anime_intelligence_v2.db")
INDEX_PATH = os.path.join(ROOT_DIR, "data", "anime_vector_index.faiss")
METADATA_PATH = os.path.join(ROOT_DIR, "data", "anime_vector_metadata.json")
EMBEDDING_MODEL = "gemini-embedding-001"

# Module-level singletons (populated on first call)
_client = None
_index = None
_metadata = None


class EmbeddingRateLimited(RuntimeError):
    """Raised by `_embed` when Gemini embedding API rate-limit retries are
    exhausted. The engine layer catches this and converts it to a
    graceful `success: False` response instead of letting a raw 429 bubble
    up through FastAPI's unhandled-exception middleware as a 500 (which
    the frontend renders as a scary "Internal Server Error" block).

    Only fires AFTER the existing retry-with-backoff logic in `_embed`
    has used all 5 attempts (~30 seconds of sustained limiting). Transient
    rate-limit blips still succeed silently via the retry loop."""


# --- CONCEPT VOCABULARY (shared across intent classification, filter
# extraction, and studio detection) ---
#
# Each key is a canonical concept; values are synonyms/variants that
# signal it. Used by:
#   - _expand_vibe_tags: expand AI-distilled vibes with synonyms so
#     embedding-search finds "romance" from "Romantic"
#   - _detect_concepts / _decompose_query: multi-concept query splits
#   - _detect_studio (via _NON_STUDIO_TERMS): reject genre words that
#     regex captures might mistake for studio names
#   - _CONCEPT_TO_MAL_GENRES below: map canonical concepts to
#     authoritative MAL genre tags for SQL LIKE filtering

_CONCEPT_VOCABULARY = {
    "mecha": ["mecha", "mech", "robot", "giant robot", "gundam", "piloting"],
    "romance": ["romance", "romantic", "love story", "love interest", "relationship"],
    "action": ["action", "battle", "fighting", "combat", "martial arts"],
    "comedy": ["comedy", "comedic", "funny", "humor", "gag", "slapstick"],
    "horror": ["horror", "scary", "creepy", "terrifying", "gore"],
    "psychological": ["psychological", "mind game", "mental", "psyche"],
    "thriller": ["thriller", "suspense", "suspenseful", "tense"],
    "sports": ["sports", "sport", "athletic", "tournament", "competition"],
    "fantasy": ["fantasy", "magic", "magical", "isekai", "sorcery"],
    "sci-fi": ["sci-fi", "science fiction", "futuristic", "space", "cyberpunk"],
    "drama": ["drama", "dramatic", "emotional", "tragedy", "tragic"],
    "slice of life": ["slice of life", "everyday life", "daily life", "mundane"],
    "mystery": ["mystery", "detective", "whodunit", "investigation"],
    "supernatural": ["supernatural", "ghost", "spirit", "paranormal", "demon"],
    "military": ["military", "war", "army", "soldier", "warfare"],
    "music": ["music", "musical", "band", "idol", "singing"],
    "school": ["school", "high school", "academy", "student council"],
    "dark": ["dark", "gritty", "bleak", "nihilistic", "dystopian"],
    "wholesome": ["wholesome", "heartwarming", "feel-good", "healing", "iyashikei"],
    "adventure": ["adventure", "journey", "quest", "exploration"],
    "harem": ["harem", "reverse harem", "love triangle"],
    "ecchi": ["ecchi", "fanservice", "fan service"],
    "shounen": ["shounen", "shonen", "boys", "battle anime", "fighting spirit", "tournament arc"],
    "seinen": ["seinen", "mature", "adult themes", "cerebral"],
    "josei": ["josei", "adult romance", "working woman", "mature romance"],
    "kodomomuke": ["kodomomuke", "kodomo", "kids", "children", "family friendly"],
}

# Demographic concepts are MAL publishing categories, not thematic
# descriptors. They embed poorly (uniformly weak semantic signal across
# anime) so `_extract_filters` enforces them as hard SQL filters on the
# genres column and `_strip_filter_terms` removes them from the
# embedding query text.
_DEMOGRAPHIC_CONCEPTS = {"shounen", "seinen", "josei", "kodomomuke"}

# Concept → MAL-genre mapping used by `objective_rankings_search`. Values
# are lists so a single detected concept can OR across multiple MAL
# genre tags (e.g., "sports" matches Sports/Team Sports/Combat Sports/Racing).
# `_extract_filters` uses the singular map below instead — it builds AND
# clauses across concepts, so it deliberately picks one representative
# tag per concept to avoid over-constraining.
_CONCEPT_TO_MAL_GENRES = {
    "mecha":         ["Mecha"],
    "romance":       ["Romance", "Love Polygon"],
    "action":        ["Action", "Martial Arts", "Combat Sports"],
    "comedy":        ["Comedy", "Gag Humor", "Parody"],
    "horror":        ["Horror"],
    "psychological": ["Psychological"],
    "thriller":      ["Suspense", "High Stakes Game"],
    "sports":        ["Sports", "Team Sports", "Combat Sports", "Racing"],
    "fantasy":       ["Fantasy", "Urban Fantasy", "Mythology", "Isekai"],
    "sci-fi":        ["Sci-Fi", "Space", "Time Travel"],
    "drama":         ["Drama"],
    "slice of life": ["Slice of Life", "Iyashikei", "CGDCT"],
    "mystery":       ["Mystery", "Detective"],
    "supernatural":  ["Supernatural", "Vampire"],
    "military":      ["Military", "Strategy Game"],
    "music":         ["Music", "Performing Arts", "Idols (Female)", "Idols (Male)"],
    "school":        ["School"],
    "dark":          ["Psychological", "Suspense"],
    "wholesome":     ["Iyashikei", "CGDCT", "Childcare"],
    "adventure":     ["Adventure"],
    "harem":         ["Harem", "Reverse Harem"],
    "ecchi":         ["Ecchi"],
    "shounen":       ["Shounen"],
    "seinen":        ["Seinen"],
    "josei":         ["Josei"],
    "kodomomuke":    ["Kids"],
}

# Singular concept → MAL-genre mapping for `_extract_filters`, which builds
# `genres LIKE ? AND genres LIKE ? ...` across detected concepts. Using the
# plural list here would produce over-constrained (per-concept AND-of-tags)
# SQL. Deliberately narrower coverage than `_CONCEPT_TO_MAL_GENRES` — "dark"
# and "wholesome" are excluded because they're vibes better served by FAISS.
_CONCEPT_TO_MAL_GENRE_HARD_FILTER = {
    "mecha": "Mecha", "romance": "Romance", "action": "Action",
    "comedy": "Comedy", "horror": "Horror", "psychological": "Psychological",
    "thriller": "Suspense", "sports": "Sports", "fantasy": "Fantasy",
    "sci-fi": "Sci-Fi", "drama": "Drama", "slice of life": "Slice of Life",
    "mystery": "Mystery", "supernatural": "Supernatural", "military": "Military",
    "music": "Music", "school": "School", "adventure": "Adventure",
    "harem": "Harem", "ecchi": "Ecchi",
    "shounen": "Shounen", "seinen": "Seinen", "josei": "Josei",
    "kodomomuke": "Kids",
}


def _get_client():
    """Lazy-load the Google GenAI client (singleton)."""
    global _client
    if _client is None:
        load_dotenv(os.path.join(ROOT_DIR, "env_variables.env"))
        api_key = os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise ValueError("GOOGLE_API_KEY not found in environment.")
        _client = gemini_client.make_client(api_key)
        logger.info("Google GenAI client initialized for embeddings.")
    return _client


def _embed(texts, task_type="RETRIEVAL_QUERY", batch_size=20):
    """
    Encode a list of texts into dense embeddings using Google's gemini-embedding-001.
    Returns an (N, dim) numpy float32 array, L2-normalized.

    task_type: "RETRIEVAL_DOCUMENT" for index building (documents to be searched),
               "RETRIEVAL_QUERY" for search-time queries (default).
    """
    client = _get_client()
    all_embeddings = []

    for i in range(0, len(texts), batch_size):
        batch = texts[i : i + batch_size]

        # Retry with exponential backoff on rate limits
        for attempt in range(5):
            try:
                result = gemini_client.embed_content(
                    client,
                    model=EMBEDDING_MODEL,
                    contents=batch,
                    config={"task_type": task_type},
                )
                for emb in result.embeddings:
                    all_embeddings.append(emb.values)
                break
            except Exception as e:
                is_rate_limit = "429" in str(e)
                if is_rate_limit and attempt < 4:
                    wait = 2 ** (attempt + 1)
                    logger.warning(f"Rate limited on batch {i//batch_size + 1}, waiting {wait}s...")
                    time.sleep(wait)
                elif is_rate_limit:
                    # All 5 attempts exhausted on sustained 429. Surface as a
                    # typed exception so the engine layer can convert to a
                    # user-visible error rather than letting the generic
                    # exception bubble to FastAPI's 500 handler.
                    raise EmbeddingRateLimited(
                        f"Gemini embedding API rate-limited after {attempt + 1} attempts"
                    ) from e
                else:
                    raise

        # Rate-limit courtesy: delay between batches during bulk operations
        if len(texts) > batch_size and i + batch_size < len(texts):
            time.sleep(1.0)

    result = np.array(all_embeddings, dtype=np.float32)
    faiss.normalize_L2(result)
    return result


def _get_readonly_connection():
    """Opens SQLite in read-only mode."""
    if not os.path.exists(DB_PATH):
        raise FileNotFoundError(f"Vault missing at {DB_PATH}")
    uri_path = f"file:{DB_PATH}?mode=ro"
    return sqlite3.connect(uri_path, uri=True)


def _expand_vibe_tags(vibe_str):
    """
    Cross-references AI-distilled thematic_vibe tags against the concept
    vocabulary and expands them with standardized synonyms.  This fixes
    inconsistencies like "Romantic" (no "Romance") or missing "Mecha" on
    mecha shows whose vibe was distilled as "Coming-of-age, Mecha, Dystopian".

    Example: "Nostalgic, Musical, Romantic"
         ->  "Nostalgic, Musical, Romantic, romance, romantic, love story, love interest"
    """
    if not vibe_str:
        return vibe_str

    vibe_lower = vibe_str.lower()
    expansions = []

    for concept, synonyms in _CONCEPT_VOCABULARY.items():
        # Check if any synonym of this concept appears in the vibe string
        matched = any(syn in vibe_lower for syn in synonyms)
        # Also check the concept name itself (e.g., "mecha" in "Coming-of-age, Mecha")
        if not matched and concept in vibe_lower:
            matched = True
        if matched:
            # Add all synonyms that aren't already present
            for syn in synonyms:
                if syn not in vibe_lower:
                    expansions.append(syn)

    if expansions:
        return vibe_str + ", " + ", ".join(expansions)
    return vibe_str


def _build_embedding_document(title, synopsis, consensus_json, genres=None, narrative_json=None):
    """
    Build a structured natural-language embedding document from raw DB fields.
    Front-loads genre keywords so they fall within the model's attention
    window, and strips JSON structural noise from consensus_json.

    Uses authoritative MAL genre tags (from the `genres` column) as the
    primary genre signal, with AI-distilled thematic_vibe tags as a
    secondary vibe/mood layer. Both are expanded via _expand_vibe_tags().

    When narrative_json is present (Level 3 data), a Narrative section is
    inserted between Vibe and Synopsis to enable plot-pattern and character-
    archetype search without disrupting existing vibe/thematic queries.
    """
    parts = []

    # Parse consensus_json for structured fields
    consensus = {}
    if consensus_json:
        try:
            consensus = json.loads(consensus_json)
        except json.JSONDecodeError:
            pass

    # Parse narrative_json for plot/character fields
    narrative = {}
    if narrative_json:
        try:
            narrative = json.loads(narrative_json)
        except json.JSONDecodeError:
            pass

    # 1. Title (helps with franchise/sequel matching)
    parts.append(f"Title: {title}")

    # 2. MAL Genre tags — authoritative editorial taxonomy, placed first
    if genres:
        expanded_genres = _expand_vibe_tags(genres)
        parts.append(f"Genres: {expanded_genres}")

    # 3. AI-distilled vibe tags — secondary mood/theme signal
    vibe = consensus.get("thematic_vibe", "")
    if vibe:
        expanded_vibe = _expand_vibe_tags(vibe)
        parts.append(f"Vibe: {expanded_vibe}")

    # 4. Narrative data (Level 3) — plot arcs, character dynamics, archetypes
    if narrative:
        narrative_parts = []
        arcs = narrative.get("narrative_arcs", [])
        if arcs:
            narrative_parts.append(". ".join(arcs))
        dynamics = narrative.get("character_dynamics", [])
        if dynamics:
            narrative_parts.append(f"Character dynamics: {'. '.join(dynamics)}")
        archetypes = narrative.get("character_archetypes", [])
        if archetypes:
            narrative_parts.append(f"Characters: {'. '.join(archetypes)}")
        trajectory = narrative.get("emotional_trajectory", "")
        if trajectory:
            narrative_parts.append(f"Emotional arc: {trajectory}")
        scenarios = narrative.get("key_scenarios", [])
        if scenarios:
            narrative_parts.append(f"Key scenarios: {'. '.join(scenarios)}")
        tone = narrative.get("tone_shifts", "")
        if tone and tone.lower() != "none":
            narrative_parts.append(f"Tone shifts: {tone}")
        if narrative_parts:
            parts.append(f"Narrative: {' '.join(narrative_parts)}")

    # 5. Synopsis
    if synopsis:
        parts.append(f"Synopsis: {synopsis}")

    # 6. Audience consensus summary (natural language, not raw JSON)
    summary = consensus.get("consensus_summary", "")
    if summary:
        parts.append(f"Audience Consensus: {summary}")

    # 7. Pros/cons as thematic keywords
    pros = consensus.get("pros", [])
    cons = consensus.get("cons", [])
    if pros:
        parts.append(f"Strengths: {', '.join(pros)}")
    if cons:
        parts.append(f"Weaknesses: {', '.join(cons)}")

    return "\n".join(parts)


def build_index():
    """
    Reads all rows from SQLite, builds structured embedding documents,
    and writes a FAISS index + metadata mapping to disk.
    """
    with _get_readonly_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, english_title, mal_synopsis, consensus_json, genres, narrative_json FROM anime_info"
        )
        rows = cursor.fetchall()

    documents = []
    metadata = []

    for mal_id, title, synopsis, consensus_json, genres, narrative_json in rows:
        if not synopsis and not consensus_json:
            continue

        doc_text = _build_embedding_document(title, synopsis, consensus_json, genres, narrative_json)
        documents.append(doc_text)
        metadata.append({"id": mal_id, "english_title": title})

    if not documents:
        raise ValueError("No embeddable documents found in the database.")

    logger.info(f"Embedding {len(documents)} anime documents via Google API...")
    embeddings = _embed(documents, task_type="RETRIEVAL_DOCUMENT")

    dim = embeddings.shape[1]
    index = faiss.IndexFlatIP(dim)
    index.add(embeddings)

    faiss.write_index(index, INDEX_PATH)
    with open(METADATA_PATH, "w", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=False)

    logger.info(
        f"Index built: {index.ntotal} vectors, dim={dim}. "
        f"Saved to {INDEX_PATH} and {METADATA_PATH}"
    )
    return index.ntotal


def update_index(mal_ids):
    """
    Incrementally update the FAISS index for a list of MAL IDs.
    - Existing IDs: removes old vectors, re-embeds, and appends updated vectors.
    - New IDs: embeds and appends.
    Saves the updated index + metadata to disk and clears singletons.
    """
    global _index, _metadata

    if not mal_ids:
        return 0

    # Load current index + metadata
    index, metadata = _load_index()

    # Build a lookup: MAL ID -> positional index in FAISS
    id_to_pos = {entry["id"]: i for i, entry in enumerate(metadata)}

    # Separate into existing (update) vs new (append)
    update_ids = [mid for mid in mal_ids if mid in id_to_pos]
    new_ids = [mid for mid in mal_ids if mid not in id_to_pos]

    # Remove old vectors for shows being updated
    if update_ids:
        positions_to_remove = np.array(
            [id_to_pos[mid] for mid in update_ids], dtype=np.int64
        )
        id_selector = faiss.IDSelectorArray(positions_to_remove)
        index.remove_ids(id_selector)

        # Remove corresponding metadata entries (iterate in reverse to preserve indices)
        for pos in sorted(positions_to_remove, reverse=True):
            metadata.pop(pos)

    # Fetch fresh data from SQLite for all target IDs
    all_target_ids = update_ids + new_ids
    placeholders = ",".join(["?"] * len(all_target_ids))
    with _get_readonly_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT id, english_title, mal_synopsis, consensus_json, genres, narrative_json "
            f"FROM anime_info WHERE id IN ({placeholders})",
            all_target_ids,
        )
        rows = cursor.fetchall()

    documents = []
    new_metadata = []
    for mal_id, title, synopsis, consensus_json, genres, narrative_json in rows:
        if not synopsis and not consensus_json:
            continue
        doc_text = _build_embedding_document(title, synopsis, consensus_json, genres, narrative_json)
        documents.append(doc_text)
        new_metadata.append({"id": mal_id, "english_title": title})

    if not documents:
        logger.warning("No embeddable documents found for the given MAL IDs.")
        return 0

    # Embed and append
    embeddings = _embed(documents, task_type="RETRIEVAL_DOCUMENT")
    index.add(embeddings)
    metadata.extend(new_metadata)

    # Save to disk
    faiss.write_index(index, INDEX_PATH)
    with open(METADATA_PATH, "w", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=False)

    # Clear singletons so next search picks up fresh data
    _index = None
    _metadata = None

    logger.info(
        f"Index updated: {len(update_ids)} replaced, {len(new_ids)} added. "
        f"Total vectors: {index.ntotal}"
    )
    return len(documents)


def _load_index():
    """Lazy-load the FAISS index and metadata mapping (singletons)."""
    global _index, _metadata
    if _index is None:
        if not os.path.exists(INDEX_PATH) or not os.path.exists(METADATA_PATH):
            raise FileNotFoundError(
                "FAISS index not found. Run build_index() first."
            )
        _index = faiss.read_index(INDEX_PATH)
        with open(METADATA_PATH, encoding="utf-8") as f:
            _metadata = json.load(f)
        logger.info(f"FAISS index loaded: {_index.ntotal} vectors.")
    return _index, _metadata


def _parse_objective_query(query):
    """
    Parse an Objective Rankings query into structured filters.
    Extracts season, year/year-range, and genre keywords.

    Returns a dict with:
        season: str or None (e.g., "Winter")
        year: int or None (exact year)
        year_min: int or None (for "after YYYY" / "since YYYY")
        year_max: int or None (for "before YYYY")
        genres: list[str] (detected genre keywords from _CONCEPT_VOCABULARY)

    Examples:
        "top 10 sports anime from fall 2019" -> {top_n: 10, season: "Fall", year: 2019, genres: ["sports"]}
        "best romance anime after 2020"      -> {top_n: None, year_min: 2021, genres: ["romance"]}
        "top 5 anime of 2023"               -> {top_n: 5, year: 2023, genres: []}
    """
    query_lower = query.lower()

    # Top-N extraction: "top 5", "top 10", "top 20"
    top_n = None
    top_n_match = re.search(r'top\s+(\d{1,3})\b', query_lower)
    if top_n_match:
        top_n = int(top_n_match.group(1))

    # Season detection
    season = None
    for s in ["winter", "spring", "summer", "fall"]:
        if s in query_lower:
            season = s.capitalize()
            break

    # Temporal range detection ("after/since YYYY", "before YYYY")
    year = None
    year_min = None
    year_max = None

    # Year range: "2018 to 2022", "2018-2022", "between 2018 and 2022"
    range_match = re.search(r'between\s+((?:19|20)\d{2})\s+and\s+((?:19|20)\d{2})', query_lower)
    if not range_match:
        range_match = re.search(r'((?:19|20)\d{2})\s*(?:to|-)\s*((?:19|20)\d{2})', query_lower)
    if range_match:
        year_min = int(range_match.group(1))
        year_max = int(range_match.group(2))
    else:
        after_match = re.search(r'after\s+((?:19|20)\d{2})', query_lower)
        since_match = re.search(r'since\s+((?:19|20)\d{2})', query_lower)
        before_match = re.search(r'before\s+((?:19|20)\d{2})', query_lower)

        if after_match:
            year_min = int(after_match.group(1)) + 1  # "after 2020" means >= 2021
        elif since_match:
            year_min = int(since_match.group(1))      # "since 2020" means >= 2020
        if before_match:
            year_max = int(before_match.group(1)) - 1  # "before 2020" means <= 2019

    # Exact year (only if no range was detected)
    if not year_min and not year_max:
        year_match = re.search(r'\b(19|20)\d{2}\b', query)
        if year_match:
            year = int(year_match.group())

    # Genre detection via concept vocabulary (word-boundary safe)
    genres = _detect_concepts(query_lower)

    # Studio detection: "by MAPPA", "studio Bones", "Sunrise anime"
    # Reuses the same exclusion list as _extract_filters()
    studio = _detect_studio(query)


    return {
        "top_n": top_n,
        "season": season,
        "year": year,
        "year_min": year_min,
        "year_max": year_max,
        "genres": genres,
        "studio": studio,
    }


def objective_rankings_search(query, top_k=50):
    """
    Structured database query for Objective Rankings mode.
    Handles compound queries like "top 10 sports anime from fall 2019"
    by combining temporal filters with precise genre matching against
    the authoritative MAL genres column.

    Genre filtering relies on MAL's authoritative tags only — no vibe gate.
    MAL genre tags are the ground truth for categorical queries; the AI-distilled
    thematic_vibe often omits categorical labels (e.g., a sports anime vibed as
    "Coming-of-Age, Friendship") which causes false negatives in narrow pools.
    """
    parsed = _parse_objective_query(query)
    season = parsed["season"]
    year = parsed["year"]
    year_min = parsed["year_min"]
    year_max = parsed["year_max"]
    genres = parsed["genres"]
    studio = parsed.get("studio")

    # Honor "top N" if specified in the query
    if parsed.get("top_n"):
        top_k = min(parsed["top_n"], top_k)

    # Map detected concept names to authoritative MAL genre tags
    mal_genre_tags = set()
    for g in genres:
        if g in _CONCEPT_TO_MAL_GENRES:
            mal_genre_tags.update(_CONCEPT_TO_MAL_GENRES[g])

    with _get_readonly_connection() as conn:
        cursor = conn.cursor()
        where_clauses = ["consensus_json IS NOT NULL"]
        params = []

        # Temporal filters
        if season and year:
            where_clauses.append("season = ?")
            params.append(f"{season}_{year}")
        elif year:
            where_clauses.append("release_year = ?")
            params.append(year)
        if year_min:
            where_clauses.append("release_year >= ?")
            params.append(year_min)
        if year_max:
            where_clauses.append("release_year <= ?")
            params.append(year_max)

        # Genre filtering: SQL LIKE on the authoritative genres column.
        if mal_genre_tags:
            genre_conditions = ["genres LIKE ?"] * len(mal_genre_tags)
            where_clauses.append(f"({' OR '.join(genre_conditions)})")
            for tag in mal_genre_tags:
                params.append(f"%{tag}%")

        # Studio filtering
        if studio:
            where_clauses.append("studio LIKE ?")
            params.append(f"%{studio}%")

        cursor.execute(
            f"SELECT english_title FROM anime_info "
            f"WHERE {' AND '.join(where_clauses)} "
            f"ORDER BY mal_score DESC LIMIT ?",
            params + [top_k],
        )
        rows = cursor.fetchall()

    return [{"title": row[0], "similarity": 1.0} for row in rows]


def staff_studio_search(staff_filter, top_k=15):
    """Item 4a — deterministic SQL retrieval for Staff/Studio search mode.

    Third mode alongside Intelligent Search and Objective Rankings. Person
    chips OR each other (each with optional role scope: Director / Music /
    Original Creator / Series Composition / Character Design, or None for
    "any role"); studio chips OR each other; the persons set ANDs with the
    studios set when both are present.

    Bypasses FAISS retrieval + Gemini reranker entirely — same architectural
    slot as Objective Rankings. Ordering follows the OR precedent
    (raw mal_score DESC). Bayesian shrinkage is a parked signal-level
    question (see IMPLEMENTATION_PLAN.md strategic-decisions); reapplying
    Bayesian here without doing the same in OR would be inconsistent.

    Args:
        staff_filter: A StaffFilterPayload (or dict-shaped equivalent) with
            `persons: list[StaffPersonChip]` and `studios: list[str]`. Empty
            payloads return `[]` — the request-level validator rejects
            them before reaching this function on the API path.
        top_k: cap on returned rows (v1 default 15, matches the pool cap
            we render on the result surface).

    Returns:
        List of `{"title": str, "similarity": 1.0}` dicts. Same shape as
        `objective_rankings_search` so `fetch_fusion_profiles` and the
        downstream hydration path work unchanged.
    """
    if not staff_filter:
        return []
    persons = list(getattr(staff_filter, "persons", None) or [])
    studios = list(getattr(staff_filter, "studios", None) or [])
    if not persons and not studios:
        return []

    where_clauses = ["consensus_json IS NOT NULL"]
    params: list = []
    joins: list[str] = []
    # anime_staff JOIN with "any role" chips can produce duplicate rows
    # for shows where the person holds multiple roles (Watanabe =
    # Director + Series Composition on Bebop). GROUP BY a.id collapses.
    needs_group_by = False

    if persons:
        joins.append("JOIN anime_staff s ON s.anime_id = a.id")
        needs_group_by = True
        person_clauses = []
        for chip in persons:
            chip_role = getattr(chip, "role", None)
            chip_id = getattr(chip, "person_mal_id", None)
            if chip_id is None:
                continue
            if chip_role is not None:
                person_clauses.append("(s.person_mal_id = ? AND s.role = ?)")
                params.extend([chip_id, chip_role])
            else:
                person_clauses.append("s.person_mal_id = ?")
                params.append(chip_id)
        if not person_clauses:
            # All person chips were malformed; fall back to empty result
            # rather than run a studio-only query the caller didn't intend.
            return []
        where_clauses.append("(" + " OR ".join(person_clauses) + ")")

    if studios:
        studio_clauses = []
        for studio in studios:
            studio_clauses.append("a.studio LIKE ?")
            params.append(f"%{studio}%")
        where_clauses.append("(" + " OR ".join(studio_clauses) + ")")

    join_sql = " ".join(joins)
    where_sql = " AND ".join(where_clauses)
    group_by_sql = "GROUP BY a.id" if needs_group_by else ""

    sql = (
        f"SELECT a.english_title FROM anime_info a {join_sql} "
        f"WHERE {where_sql} "
        f"{group_by_sql} "
        f"ORDER BY a.mal_score DESC LIMIT ?"
    )
    params.append(top_k)

    with _get_readonly_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(sql, params)
        rows = cursor.fetchall()

    return [{"title": row[0], "similarity": 1.0} for row in rows if row[0]]


# --- FILTERED RETRIEVAL (Hybrid SQL + FAISS) ---

# Regex patterns for temporal terms to strip from queries before embedding
_TEMPORAL_PATTERNS = [
    re.compile(r'\b(?:released?|airing|aired|came out|from|of|in)\s+(?:after|since|before|during)?\s*(?:(?:19|20)\d{2})\b', re.IGNORECASE),
    re.compile(r'\b(?:after|since|before)\s+(?:19|20)\d{2}\b', re.IGNORECASE),
    re.compile(r'\b(?:19|20)\d{2}\s*(?:to|-)\s*(?:19|20)\d{2}\b', re.IGNORECASE),
    re.compile(r'\b(?:winter|spring|summer|fall)\s+(?:19|20)\d{2}\b', re.IGNORECASE),
    re.compile(r'\b(?:19|20)\d{2}\b'),
]


# Lazy-loaded cache for the bare-studio-name fallback in _detect_studio.
# Populated on first call via _load_known_studios() so test environments
# without a live DB don't pay the cost (and don't crash at import time).
_KNOWN_STUDIO_REGEX = None
_KNOWN_STUDIO_CANONICAL_MAP = None


def _load_known_studios():
    """
    Build (and memoize) a regex of all distinct studio names in the DB plus a
    canonical-casing lookup map. Used as a fallback in _detect_studio() to
    catch bare studio names like "top 10 sunrise" — queries the prefix/suffix
    regex patterns can't see.

    Filters out studios shorter than 4 characters to avoid false positives on
    common English words (e.g., a 3-char studio "P.A." would shadow the
    substring "pa" anywhere it appeared).

    Returns (regex, canonical_map) or (None, None) if the DB is unreachable.
    """
    global _KNOWN_STUDIO_REGEX, _KNOWN_STUDIO_CANONICAL_MAP
    if _KNOWN_STUDIO_REGEX is not None:
        return _KNOWN_STUDIO_REGEX, _KNOWN_STUDIO_CANONICAL_MAP

    try:
        conn = _get_readonly_connection()
        try:
            rows = conn.execute(
                "SELECT DISTINCT studio FROM anime_info WHERE studio IS NOT NULL"
            ).fetchall()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — any DB failure (missing file, locked, corrupt) must degrade gracefully to Pattern 1/2 only; module import at cold start / test env
        # DB unavailable (e.g., import-time test environment) — skip the
        # fallback rather than crashing. The two regex patterns above still
        # work without it.
        return None, None

    studios = set()
    for (raw,) in rows:
        for part in raw.split(","):
            part = part.strip()
            if part and len(part) >= 4:
                studios.add(part)

    if not studios:
        return None, None

    canonical_map = {s.lower(): s for s in studios}
    # Sort longest-first so multi-word names like "A-1 Pictures" beat shorter
    # substrings.
    sorted_studios = sorted(studios, key=len, reverse=True)
    pattern = r"\b(" + "|".join(re.escape(s) for s in sorted_studios) + r")\b"

    _KNOWN_STUDIO_REGEX = re.compile(pattern, re.IGNORECASE)
    _KNOWN_STUDIO_CANONICAL_MAP = canonical_map
    return _KNOWN_STUDIO_REGEX, _KNOWN_STUDIO_CANONICAL_MAP


def _detect_studio(query):
    """
    Detect a studio name from a natural language query.
    Three patterns, tried in order:
      1. Prefix-based: "by MAPPA", "from Sunrise", "studio Bones"
      2. Suffix-based: "MAPPA anime", "Sunrise shows", "Madhouse series"
      3. Bare-name fallback: any DB-known studio mentioned without prefix or
         "anime/shows" trailer (e.g., "top 10 sunrise", "underrated MAPPA").

    Patterns 1 and 2 use lax regex captures — historically this class of
    detector has leaked bogus studio names into the SQL filter (e.g.,
    `studio LIKE '%TV%'` from "TV series", `studio LIKE '%studio bones%'`
    from "by studio bones") that then silently zero-fill the filtered
    pool. Since PR #113 shipped the specific "studio" stop-word case, we
    now validate every Pattern 1/2 capture against `_load_known_studios`
    (see `_canonicalize_capture`) — if the capture doesn't contain a real
    studio name from the DB, we reject it and fall through to Pattern 3.
    Preserves current lax behavior in test environments where the DB
    isn't reachable (`_load_known_studios` returns None).

    Returns the canonical studio name (str) or None.
    """
    _NON_STUDIO_TERMS = set(_CONCEPT_VOCABULARY.keys()) | {
        "top", "best", "worst", "new", "old", "classic", "modern", "recent",
        "popular", "underrated", "obscure", "famous", "mainstream",
        "good", "great", "amazing", "bad", "terrible",
        "winter", "spring", "summer", "fall",
        # Anime medium/format words — never a studio. Without these, Pattern 2
        # captures "TV" from "TV series", "OVA" from "OVA anime", etc., and
        # injects a bogus `studio LIKE '%TV%'` SQL clause that zeros the
        # filtered pool and silently drops UI era bounds to the unfiltered
        # fallback (surfaced 2026-09-06 via `hidden-90s-mature-tv` eval entry).
        "tv", "ova", "ona", "movie", "movies", "film", "films",
        "special", "specials",
        # Item 4a Phase 6 fold-in — English determiners / stop-words that
        # Pattern 1 was capturing via `re.IGNORECASE`. Queries like
        # "anime from the 80s" fed "the 80s" through as a studio candidate.
        # Adding these here catches the first-word-check in `_is_non_studio`
        # (surfaced 2026-09-06 during the hidden-90s eval verification; also
        # noted in the Engine Deep-Dive follow-ups as parked for the next
        # studio-detector touch).
        "the", "a", "an", "some", "any", "many", "much", "few",
        # Pattern 1's regex alternation `(?:by|from|studio)` matches "studio"
        # as a prefix keyword, but when the query is "by studio X" the "by"
        # prefix fires FIRST (left-to-right scan) and the lazy capture greedily
        # extends through end-of-string, yielding "studio X" as the studio
        # name. The SQL then runs `studio LIKE '%studio X%'`, matches zero
        # rows, and the filtered path silently falls back to unfiltered —
        # non-X shows leak into the result set (surfaced 2026-09-17 during
        # Item 4 live QA on "top rated shows by studio bones"). Adding
        # "studio" here trips `_is_non_studio` on the first word of the
        # capture, rejects Pattern 1, and lets Pattern 3's bare-name lookup
        # against the DB-known-studios set correctly resolve the actual
        # studio ("Bones" for the QA case). Well-known "Studio X" names
        # (Studio Ghibli, Studio Pierrot, Studio Deen, etc.) still resolve
        # correctly via Pattern 3 because it's a whole-vault regex sorted
        # longest-first.
        "studio",
    }

    def _is_non_studio(candidate):
        """Check if the first word of the candidate is a known non-studio term."""
        first_word = candidate.lower().split()[0] if candidate.strip() else ""
        return first_word in _NON_STUDIO_TERMS

    known_regex, canonical_map = _load_known_studios()

    def _canonicalize_capture(candidate):
        """Broader Pattern 1/2 fix: reject a regex capture unless it
        contains a real studio name from the DB. Returns the canonical
        casing when validated, None when the capture doesn't map to a
        known studio, and the raw candidate when the DB is unreachable
        (test env) — preserving pre-fix lax behavior in that case.
        """
        if known_regex is None:
            return candidate  # DB unreachable, fall back to lax behavior
        m = known_regex.search(candidate)
        if m is None:
            return None
        return canonical_map.get(m.group(1).lower(), m.group(1))

    # Pattern 1: "by MAPPA", "from Sunrise", "studio Bones"
    studio_match = re.search(
        r'(?:by|from|studio)\s+([A-Z][A-Za-z0-9][\w\s.&-]{0,28}?)(?:\s+(?:anime|shows?|series)|,|\s*$)',
        query, re.IGNORECASE
    )
    if studio_match:
        candidate = studio_match.group(1).strip()
        if not _is_non_studio(candidate):
            canonical = _canonicalize_capture(candidate)
            if canonical is not None:
                return canonical

    # Pattern 2: "<Studio> anime/shows/series" (no prefix keyword, case-sensitive start)
    studio_match = re.search(
        r'\b([A-Z][A-Za-z0-9][\w\s.&-]{0,28}?)\s+(?:anime|shows?|series)\b',
        query
    )
    if studio_match:
        candidate = studio_match.group(1).strip()
        if not _is_non_studio(candidate):
            canonical = _canonicalize_capture(candidate)
            if canonical is not None:
                return canonical

    # Pattern 3: bare studio name against the DB-known-studios set. Catches
    # queries like "top 10 sunrise" or "underrated MAPPA" where there's no
    # prefix keyword and no trailing "anime/shows".
    if known_regex is not None:
        m = known_regex.search(query)
        if m:
            matched_lower = m.group(1).lower()
            if matched_lower not in _NON_STUDIO_TERMS:
                return canonical_map.get(matched_lower, m.group(1))

    return None


def _extract_filters(query):
    """
    Extract structured filters (temporal, genre) from a natural language query.
    Reuses _parse_objective_query() for parsing, then determines if any hard
    filters should be applied.

    Returns a dict with:
        has_filters: bool — whether any hard filters were detected
        year: int or None
        year_min: int or None
        year_max: int or None
        season: str or None
        genres: list[str] — canonical genre names from _CONCEPT_VOCABULARY
        genre_db_terms: list[str] — SQL LIKE terms for the genres column
    """
    parsed = _parse_objective_query(query)

    has_temporal = any([parsed["year"], parsed["year_min"], parsed["year_max"], parsed["season"]])

    # Only trigger filtered path when temporal constraints are present.
    # Genre-only queries are well-served by the standard FAISS semantic path
    # since genre concepts are already embedded. Temporal constraints are what
    # FAISS fundamentally cannot handle.

    genre_db_terms = []
    has_demographic = False
    for g in parsed["genres"]:
        mal_genre = _CONCEPT_TO_MAL_GENRE_HARD_FILTER.get(g)
        if mal_genre:
            genre_db_terms.append(mal_genre)
        if g in _DEMOGRAPHIC_CONCEPTS:
            has_demographic = True

    # --- Numeric constraint parsing ---
    # Quality score ranges: "between 6.5 and 7.5", "under 7", "above 8", "over 8.5"
    quality_min = None
    quality_max = None
    query_lower = query.lower()

    q_between = re.search(r'(?:quality|score|rating)\s*(?:between|from)\s+(\d+\.?\d*)\s*(?:and|to|-)\s*(\d+\.?\d*)', query_lower)
    if q_between:
        quality_min = float(q_between.group(1))
        quality_max = float(q_between.group(2))
    else:
        # "between X and Y" without explicit "quality" prefix — check for standalone pattern
        q_between_bare = re.search(r'between\s+(\d+\.?\d*)\s+and\s+(\d+\.?\d*)', query_lower)
        if q_between_bare:
            v1, v2 = float(q_between_bare.group(1)), float(q_between_bare.group(2))
            # Only treat as quality range if values are plausible MAL scores (1-10)
            if 1.0 <= v1 <= 10.0 and 1.0 <= v2 <= 10.0:
                quality_min, quality_max = v1, v2

    if quality_min is None:
        q_above = re.search(r'(?:quality|score|rating)\s*(?:above|over|greater than|higher than|>=?)\s*(\d+\.?\d*)', query_lower)
        if q_above:
            quality_min = float(q_above.group(1))

    if quality_max is None:
        q_below = re.search(r'(?:quality|score|rating)\s*(?:below|under|less than|lower than|<=?)\s*(\d+\.?\d*)', query_lower)
        if q_below:
            quality_max = float(q_below.group(1))

    # Sentiment/mood: "high mood", "positive sentiment", "negative mood"
    sentiment_min = None
    sentiment_max = None

    if re.search(r'\b(high|positive|good)\s+(mood|sentiment|audience mood)\b', query_lower):
        sentiment_min = 0.3
    elif re.search(r'\b(low|negative|bad)\s+(mood|sentiment|audience mood)\b', query_lower):
        sentiment_max = -0.1
    elif re.search(r'\b(mixed|neutral)\s+(mood|sentiment|audience mood)\b', query_lower):
        sentiment_min = -0.1
        sentiment_max = 0.3

    # --- Popularity (scored_by) constraints ---
    scored_by_min = None
    scored_by_max = None

    # Explicit numeric: "fewer than 50k ratings", "more than 100k votes"
    pop_lt = re.search(r'(?:fewer|less|under)\s+than\s+(\d+)\s*k\s*(?:ratings?|votes?|members?)', query_lower)
    pop_gt = re.search(r'(?:more|over|greater)\s+than\s+(\d+)\s*k\s*(?:ratings?|votes?|members?)', query_lower)
    if pop_lt:
        scored_by_max = int(pop_lt.group(1)) * 1000
    if pop_gt:
        scored_by_min = int(pop_gt.group(1)) * 1000

    # Keyword-based popularity thresholds
    if scored_by_max is None and re.search(
        r'\b(underrated|obscure|hidden\s*gems?|niche|overlooked|under.?the.?radar)\b', query_lower
    ):
        scored_by_max = 50_000
    if scored_by_min is None and re.search(
        r'\b(popular|mainstream|well.?known|widely.?watched|famous)\b', query_lower
    ):
        scored_by_min = 100_000

    # --- Studio constraint ---
    studio = _detect_studio(query)

    has_numeric = any([quality_min, quality_max, sentiment_min is not None,
                       sentiment_max is not None, scored_by_min, scored_by_max])
    has_studio = studio is not None

    return {
        "has_filters": has_temporal or has_demographic or has_numeric or has_studio,
        "has_numeric_constraints": has_numeric,
        "year": parsed["year"],
        "year_min": parsed["year_min"],
        "year_max": parsed["year_max"],
        "season": parsed["season"],
        "genres": parsed["genres"],
        "genre_db_terms": genre_db_terms,
        "quality_min": quality_min,
        "quality_max": quality_max,
        "sentiment_min": sentiment_min,
        "sentiment_max": sentiment_max,
        "scored_by_min": scored_by_min,
        "scored_by_max": scored_by_max,
        "studio": studio,
        "mal_rating_max": None,
    }


# MAL rating bucket hierarchy (least to most restricted). The Jikan
# `rating` field always begins with one of these prefixes followed by
# the literal " - " delimiter, so `LIKE '<bucket> - %'` is a closed-set
# prefix match. Strict-NULL behavior is implicit: NULL never matches
# LIKE, and the empty-string ("Jikan-confirmed-no-rating") case also
# never matches a `<bucket> - %` pattern.
_RATING_BUCKET_HIERARCHY = ["G", "PG", "PG-13", "R", "R+", "Rx"]


def _allowed_rating_buckets(rating_max):
    """Return the bucket prefixes allowed under a given ceiling.
    'G' -> ['G']; 'PG' -> ['G', 'PG']; 'PG-13' -> ['G', 'PG', 'PG-13'];
    'R' -> ['G', 'PG', 'PG-13', 'R']. R+ and Rx are intentionally not
    exposed as user-selectable ceilings — 'R' already excludes them and
    that's the strictest adult cap a user would meaningfully pick."""
    if rating_max is None or rating_max not in _RATING_BUCKET_HIERARCHY:
        return None
    idx = _RATING_BUCKET_HIERARCHY.index(rating_max)
    return _RATING_BUCKET_HIERARCHY[: idx + 1]


def _strip_filter_terms(query, filters):
    """
    Remove temporal phrases and demographic terms from the query text so the
    embedding focuses on semantic content rather than structured constraints
    that FAISS can't handle.

    Thematic genre keywords are KEPT because they carry valuable semantic signal
    for the embedding model. Demographic terms (shounen, seinen, josei, kodomomuke)
    are STRIPPED because they are publishing categories with uniformly weak
    embedding discrimination — the SQL pre-filter enforces them as hard constraints.

    "harem anime released after 2020" -> "harem anime"
    "gritty military show from spring 2024" -> "gritty military show"
    "shounen sports anime" -> "sports anime"
    """
    stripped = query

    # Strip temporal phrases (year ranges, after/before/since, season+year, bare years)
    for pattern in _TEMPORAL_PATTERNS:
        stripped = pattern.sub("", stripped)

    # Clean up residual temporal connectors left behind
    stripped = re.sub(r'\b(released|airing|aired|came out|from|after|since|before)\b',
                      '', stripped, flags=re.IGNORECASE)

    # Strip demographic terms — they are enforced via SQL, not embedding
    _DEMOGRAPHIC_TERMS = re.compile(
        r'\b(shounen|shonen|seinen|josei|kodomomuke|kodomo)\b', re.IGNORECASE
    )
    stripped = _DEMOGRAPHIC_TERMS.sub("", stripped)

    # Strip numeric constraint phrases — these are enforced via SQL/post-filter
    _NUMERIC_PATTERNS = [
        re.compile(r'(?:quality|score|rating)\s*(?:between|from)\s+\d+\.?\d*\s*(?:and|to|-)\s*\d+\.?\d*', re.IGNORECASE),
        re.compile(r'between\s+\d+\.?\d*\s+and\s+\d+\.?\d*', re.IGNORECASE),
        re.compile(r'(?:quality|score|rating)\s*(?:above|over|greater than|higher than|below|under|less than|lower than|>=?|<=?)\s*\d+\.?\d*', re.IGNORECASE),
        re.compile(r'\b(?:high|low|positive|negative|good|bad|mixed|neutral)\s+(?:mood|sentiment|audience mood)\b', re.IGNORECASE),
    ]
    for pattern in _NUMERIC_PATTERNS:
        stripped = pattern.sub("", stripped)

    # Strip popularity constraint phrases
    _POPULARITY_PATTERNS = [
        re.compile(r'\b(?:fewer|less|under|more|over|greater)\s+than\s+\d+\s*k\s*(?:ratings?|votes?|members?)\b', re.IGNORECASE),
        re.compile(r'\b(?:underrated|obscure|hidden\s*gems?|niche|overlooked|under.?the.?radar|popular|mainstream|well.?known|widely.?watched|famous)\b', re.IGNORECASE),
    ]
    for pattern in _POPULARITY_PATTERNS:
        stripped = pattern.sub("", stripped)

    # Strip studio constraint phrases (e.g., "by MAPPA", "studio Bones")
    if filters.get("studio"):
        stripped = re.sub(
            r'\b(?:by|from|studio)\s+' + re.escape(filters["studio"]) + r'\b',
            '', stripped, flags=re.IGNORECASE
        )

    # Clean up residual constraint words left after numeric stripping
    stripped = re.sub(r'\b(quality|score|rating|mood|sentiment|audience mood)\b', '', stripped, flags=re.IGNORECASE)
    stripped = re.sub(r'\b(but|with|and a|having|a)\b', '', stripped, flags=re.IGNORECASE)

    stripped = re.sub(r'\s+', ' ', stripped).strip()

    # If stripping removed everything meaningful, fall back to genre terms as the query
    if len(stripped.split()) < 2:
        # Use non-demographic genres for the fallback query
        thematic_genres = [g for g in filters["genres"] if g not in _DEMOGRAPHIC_CONCEPTS]
        stripped = " ".join(thematic_genres) + " anime" if thematic_genres else query

    return stripped


def _filters_for_telemetry(filters):
    """Snapshot the filters dict for the journal/retrieval_meta payload.

    PR C — the exclude_mal_ids set (potentially thousands of entries)
    is replaced with `exclude_mal_ids_count` (int) so the journal row
    stays small and JSON-serializable. The reranker downstream reads
    the count from this field to surface the "user has watched N"
    context to Gemini.
    """
    out = {}
    for k, v in filters.items():
        if k == "has_filters" or not v:
            continue
        if k == "exclude_mal_ids":
            # set → integer count; preserves journal serializability and
            # avoids 10x payload bloat per row at production scale.
            try:
                out["exclude_mal_ids_count"] = len(v)
            except TypeError:
                out["exclude_mal_ids_count"] = 0
        else:
            out[k] = v
    return out


def _empty_pool_or_fallback(filters, reason):
    """Consistent handling for the three zero-result gates in
    `_filtered_vector_search`.

    When any **strict-trust** filter is active, we return `[]` so the
    caller ships an empty pool rather than silently violating the
    contract the user signed up for:
      - `mal_rating_max`      — G-only must never see R
      - `exclude_mal_ids`     — watchlist must never surface a watched show
      - `year_min` / `year_max` — a user who ticks the "Pre-1990" radio
        expects pre-1990 or empty, NOT silent fallback to modern shows.
        Added 2026-10-12 after a production bug: "military drama" +
        Pre-1990 fell through to unfiltered FAISS because pre-1990 shows
        had NULL `genres` (data gap), and the unfiltered path surfaced
        2021-era military dramas. Honest empty > wrong fallback.

    For non-strict filter combos (natural-language genre terms, studio
    LIKE, numeric constraints that leave the pool thin but non-strict),
    return `None` to signal the caller to fall back to unfiltered FAISS.
    `reason` is spliced into the log line for diagnostics.
    """
    strict_reasons = []
    if filters.get("mal_rating_max"):
        strict_reasons.append("rating ceiling")
    if filters.get("exclude_mal_ids"):
        strict_reasons.append("watchlist exclusion")
    if filters.get("year_min") or filters.get("year_max"):
        strict_reasons.append("decade filter")
    if strict_reasons:
        logger.warning(
            f"Filtered search: {reason} under {' + '.join(strict_reasons)}. "
            "Returning empty (no fallback)."
        )
        return []
    logger.warning(f"Filtered search: {reason}. Falling back to unfiltered.")
    return None


def _filtered_vector_search(query, filters, top_k=50):
    """
    Hybrid SQL + FAISS search for queries with structured constraints.

    1. Queries SQLite for all shows matching genre/temporal hard filters
    2. Maps those shows to their FAISS index positions
    3. Reconstructs their vectors and computes similarity against the
       stripped query embedding
    4. Returns results sorted by similarity, ready for composite reranking

    Guarantees 100% recall for the filtered set — no candidates are lost
    to FAISS over-fetch limits.

    Watchlist exclusion (PR C): `filters["exclude_mal_ids"]`, when set,
    is the cluster-expanded set of MAL IDs the user has marked as
    watched/dropped. Applied as a Python-side set-membership filter on
    the SQL row set so the cluster-expanded list (which can run to
    several thousand for heavy MAL users) doesn't run into SQLite's
    999-parameter cap on `NOT IN`. Empty post-exclusion pool returns
    [] regardless of other filters — the user opted into the exclusion
    and falling back to the unfiltered (and unwatched) set would defeat
    the trust contract.
    """
    index, metadata = _load_index()

    # Build title -> FAISS position lookup
    title_to_pos = {m["english_title"]: i for i, m in enumerate(metadata)}

    # Query SQLite for matching shows
    where_clauses = ["consensus_json IS NOT NULL"]
    params = []

    if filters["season"] and filters["year"]:
        where_clauses.append("season = ?")
        params.append(f"{filters['season']}_{filters['year']}")
    elif filters["year"]:
        where_clauses.append("release_year = ?")
        params.append(filters["year"])
    if filters["year_min"]:
        where_clauses.append("release_year >= ?")
        params.append(filters["year_min"])
    if filters["year_max"]:
        where_clauses.append("release_year <= ?")
        params.append(filters["year_max"])

    # Genre filter using the authoritative genres column
    for mal_genre in filters["genre_db_terms"]:
        where_clauses.append("genres LIKE ?")
        params.append(f"%{mal_genre}%")

    # Studio filter
    if filters.get("studio"):
        where_clauses.append("studio LIKE ?")
        params.append(f"%{filters['studio']}%")

    # MAL rating ceiling — closed-set bucket prefix match. Strict-NULL is
    # implicit: NULL and empty-string both fail every `LIKE '<bucket> - %'`
    # clause, so unrated shows are excluded without an explicit IS NOT NULL.
    allowed_buckets = _allowed_rating_buckets(filters.get("mal_rating_max"))
    if allowed_buckets:
        rating_clauses = " OR ".join(["mal_rating LIKE ?"] * len(allowed_buckets))
        where_clauses.append(f"({rating_clauses})")
        params.extend(f"{b} - %" for b in allowed_buckets)

    # Quality score constraints (Bayesian-adjusted, applied post-query)
    quality_min = filters.get("quality_min")
    quality_max = filters.get("quality_max")

    # Sentiment constraints
    sentiment_min = filters.get("sentiment_min")
    sentiment_max = filters.get("sentiment_max")

    # Popularity (scored_by) constraints
    scored_by_min = filters.get("scored_by_min")
    scored_by_max = filters.get("scored_by_max")

    # If any numeric constraints exist, we need extra columns for post-filtering
    needs_numeric_cols = any([quality_min is not None, quality_max is not None,
                             sentiment_min is not None, sentiment_max is not None,
                             scored_by_min is not None, scored_by_max is not None])
    # PR C: always pull `id` so the watchlist-exclusion set membership can
    # filter without a chunked SQL NOT IN (which would hit the 999-param cap).
    if needs_numeric_cols:
        select_cols = "id, english_title, mal_score, scored_by, avg_sentiment"
    else:
        select_cols = "id, english_title"

    with _get_readonly_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT {select_cols} FROM anime_info "
            f"WHERE {' AND '.join(where_clauses)}",
            params,
        )
        rows = cursor.fetchall()

    # PR C — watchlist exclusion. Filter rows whose MAL ID is in the
    # cluster-expanded set the caller supplied. Done Python-side because
    # the set can run to several thousand entries (heaviest MAL users
    # have ~2k+ completed/dropped lists, cluster expansion pushes higher),
    # well past SQLite's 999-parameter cap.
    excluded_ids = filters.get("exclude_mal_ids")
    if excluded_ids:
        before = len(rows)
        rows = [row for row in rows if row[0] not in excluded_ids]
        logger.info(
            f"Watchlist exclusion: {before} -> {len(rows)} candidates "
            f"after dropping {before - len(rows)} watched-cluster IDs."
        )

    if not rows:
        return _empty_pool_or_fallback(filters, "no shows match the constraints")

    # Apply numeric post-filters (quality, sentiment, and popularity)
    if needs_numeric_cols:
        matching_titles = []
        for row in rows:
            # row shape: (id, title, mal_score, scored_by, avg_sentiment)
            title, mal_score, scored_by = row[1], row[2] or 0.0, row[3] or 0
            sentiment = row[4] if len(row) > 4 else 0.0
            # Bayesian-adjusted quality (same formula as _composite_rerank)
            v, m, C = scored_by, _BAYESIAN_MIN_VOTES, _BAYESIAN_GLOBAL_MEAN
            q_score = round((v / (v + m)) * mal_score + (m / (v + m)) * C, 1)
            if quality_min is not None and q_score < quality_min:
                continue
            if quality_max is not None and q_score > quality_max:
                continue
            if sentiment_min is not None and (sentiment or 0.0) < sentiment_min:
                continue
            if sentiment_max is not None and (sentiment or 0.0) > sentiment_max:
                continue
            if scored_by_min is not None and scored_by < scored_by_min:
                continue
            if scored_by_max is not None and scored_by > scored_by_max:
                continue
            matching_titles.append(title)
        logger.info(f"Numeric post-filter: {len(rows)} -> {len(matching_titles)} candidates")
    else:
        # row shape: (id, title)
        matching_titles = [row[1] for row in rows]

    if not matching_titles:
        return _empty_pool_or_fallback(filters, "no shows match numeric constraints")

    # Map to FAISS positions and reconstruct vectors
    valid_titles = []
    positions = []
    for title in matching_titles:
        pos = title_to_pos.get(title)
        if pos is not None:
            valid_titles.append(title)
            positions.append(pos)

    if not valid_titles:
        return _empty_pool_or_fallback(filters, "no FAISS-indexed shows match")

    # Reconstruct vectors for the filtered set
    vectors = np.array(
        [index.reconstruct(pos) for pos in positions],
        dtype=np.float32,
    )

    # Embed the stripped query and compute similarities
    stripped_query = _strip_filter_terms(query, filters)
    query_embedding = _embed([stripped_query])

    similarities = np.dot(vectors, query_embedding[0])

    # Build results sorted by similarity
    results = []
    for i, title in enumerate(valid_titles):
        results.append({
            "title": title,
            "similarity": float(similarities[i]),
        })

    results.sort(key=lambda x: x["similarity"], reverse=True)

    logger.info(
        f"Filtered search: {len(valid_titles)} candidates from SQL "
        f"(query: '{stripped_query}'). "
        f"Filters: genres={filters['genre_db_terms']}, "
        f"year={filters.get('year')}, year_min={filters.get('year_min')}, "
        f"year_max={filters.get('year_max')}"
    )

    return results


# --- QUERY DECOMPOSITION ---

def _detect_concepts(query_lower):
    """
    Detect which genre/theme concepts from _CONCEPT_VOCABULARY appear in
    the query. Uses word-boundary matching to prevent false triggers
    (e.g., "transport" should NOT match "sport").

    Returns a list of canonical concept names (e.g., ["mecha", "romance"]).
    """
    detected = []
    for concept, synonyms in _CONCEPT_VOCABULARY.items():
        for syn in synonyms:
            # Word boundary match: "sport" matches "sport" and "sports"
            # but NOT "transport" or "sportswear"
            if re.search(r'\b' + re.escape(syn) + r'\b', query_lower):
                detected.append(concept)
                break
    return detected


def _decompose_query(query):
    """
    Detect distinct thematic concepts in a user query using keyword matching.
    Returns a list of concept phrases suitable for independent FAISS searches.

    For single-concept queries, returns an empty list (caller should use the
    original query as-is). For multi-concept queries, returns one search
    phrase per detected concept.

    Examples:
        "mecha anime with romance aspects" -> ["mecha robot giant robot anime",
                                                "romance romantic love story anime"]
        "dark psychological thriller"      -> ["dark gritty bleak anime",
                                                "psychological mind game anime",
                                                "thriller suspense anime"]
        "best action anime"                -> [] (single concept, no decomposition)
    """
    query_lower = query.lower()

    # Find all concepts present in the query (word-boundary safe)
    detected = _detect_concepts(query_lower)

    # Single concept or none: no decomposition needed
    if len(detected) <= 1:
        return []

    # Build an expanded sub-query per concept using its synonym list
    sub_queries = []
    for concept in detected:
        synonyms = _CONCEPT_VOCABULARY[concept]
        # Include top synonyms + "anime" to anchor the embedding
        phrase = " ".join(synonyms[:4]) + " anime"
        sub_queries.append(phrase)

    return sub_queries


def _search_faiss_raw(query_embedding, fetch_k):
    """
    Low-level FAISS search returning list of {title, similarity} dicts.
    Shared by both single-query and decomposed-query paths.
    """
    index, metadata = _load_index()
    actual_k = min(fetch_k, index.ntotal)
    scores, indices = index.search(query_embedding, actual_k)

    results = []
    for i, idx in enumerate(indices[0]):
        if 0 <= idx < len(metadata):
            results.append({
                "title": metadata[idx]["english_title"],
                "similarity": float(scores[0][i]),
            })
    return results


def _cross_concept_rerank(original_results, sub_query_embeddings):
    """
    Re-score candidates using AND-semantics: for each candidate in the
    original results, compute its similarity to every sub-query concept
    via the FAISS index, then use min(concept_similarities) as a boost.

    This promotes titles that match ALL query concepts (e.g., mecha AND
    romance) over titles that only match the dominant concept.

    Returns a list of {title, similarity} dicts where similarity is the
    original query similarity (for Phase 2) but ordering reflects
    cross-concept coverage.
    """
    index, metadata = _load_index()

    # Build title -> FAISS vector index lookup
    title_to_idx = {}
    for i, m in enumerate(metadata):
        title_to_idx[m["english_title"]] = i

    # For each candidate, compute similarity to each sub-query concept
    candidate_scores = []
    for r in original_results:
        title = r["title"]
        faiss_idx = title_to_idx.get(title)
        if faiss_idx is None:
            candidate_scores.append({
                "title": title,
                "similarity": r["similarity"],
                "_cross_concept_min": 0.0,
            })
            continue

        # Reconstruct this title's vector from the index
        vec = index.reconstruct(faiss_idx).reshape(1, -1)

        # Compute similarity to each sub-query concept
        concept_sims = []
        for sq_emb in sub_query_embeddings:
            sim = float(np.dot(vec[0], sq_emb[0]))
            concept_sims.append(sim)

        candidate_scores.append({
            "title": title,
            "similarity": r["similarity"],
            "_cross_concept_min": min(concept_sims),
        })

    # Sort by cross-concept minimum (AND-semantics), descending
    candidate_scores.sort(key=lambda x: x["_cross_concept_min"], reverse=True)

    return [
        {"title": c["title"], "similarity": c["similarity"]}
        for c in candidate_scores
    ]


# --- INTENT CLASSIFICATION (COA 1) ---

# Keywords that signal specific user intents beyond genre/theme.
_HIDDEN_GEM_SIGNALS = {"hidden gem", "underrated", "overlooked", "under the radar",
                       "nobody talks about", "slept on", "obscure", "lesser known",
                       "not well known", "underwatched", "unknown",
                       # COA C Option 1 (2026-09-07) — latent hidden-gem signals.
                       # These are query shapes that indicate niche taste without
                       # using the explicit "hidden gem" vocabulary. Surfaced by
                       # the Phase A probe: all four hidden-gems eval entries
                       # classified as `vibe` intent pre-expansion because none
                       # contained the original signal words. Bias toward
                       # distinctive terms — words that could equally describe a
                       # mainstream show (e.g., "quiet", "slow") are deliberately
                       # excluded to avoid misclassifying broad-taste queries as
                       # hidden-gem-seeking.
                       "contemplative", "meditative", "atmospheric",
                       "experimental", "unconventional", "avant-garde",
                       "avant garde", "arthouse", "art house",
                       "understated", "niche", "philosophical", "cerebral"}
_CROWD_PLEASER_SIGNALS = {"crowd pleaser", "universally loved", "safe pick", "safe bet",
                          "everyone likes", "popular", "mainstream", "well known",
                          "highly rated", "top rated", "best rated", "classic"}
_POLARIZING_SIGNALS = {"polarizing", "controversial", "divisive", "love it or hate it",
                       "edgy", "provocative", "boundary pushing", "not for everyone"}

# Narrative signals: character references + plot verbs + narrative connectors
# Narrative signal vocabulary — grouped by signal strength.
#
# STRONG signals (standalone — any one of these triggers narrative):
#   - Connectors: "anime where", "but is actually", etc.
#   - Multi-word plot phrases: "becomes the strongest", "falls in love", etc.
#
# WEAK signals (need to be combined):
#   - Character refs: "MC", "protagonist", "villain"
#   - Single-word plot verbs: "rejected", "betrayed", "trapped"
#   - Weak + Weak = narrative (e.g., "MC" + "betrayed")

_NARRATIVE_CHARACTER_REFS = {"mc", "protagonist", "main character", "hero", "heroine",
                             "villain", "love interest", "best friend", "childhood friend",
                             "childhood friends", "loner", "enemies"}
_NARRATIVE_PLOT_VERBS = {"rejected", "betrayed", "trapped", "discovers", "transforms",
                         "hides", "falsely accused", "adopted", "reincarnated",
                         "murdered", "befriends", "obsessed", "manipulates",
                         "reunite", "reunited", "disguised", "abandoned", "sacrifices"}
# Multi-word phrases strong enough to trigger narrative on their own
_NARRATIVE_STRONG_PHRASES = {"becomes evil", "becomes the strongest", "becomes overpowered",
                             "falls in love", "fall in love", "gets dark", "gets really dark",
                             "someone dies", "turns out to be right", "hides their power",
                             "starts happy", "starts as comedy", "in a time loop",
                             "slowly fall in love", "slowly falls in love"}
# Connectors are strong standalone signals
_NARRATIVE_CONNECTORS = {"but then", "and then", "as he", "as she", "after being",
                         "but as", "who is", "who gets", "where the", "where a",
                         "looking for anime where", "anime where", "show where",
                         "but is actually", "who looks", "who gives",
                         "but secretly"}
_SUPERLATIVE_SIGNALS = {"top", "best", "worst", "highest rated", "lowest rated",
                        "most popular", "ranking", "ranked"}


def _classify_intent(query):
    """
    Classifies a user query into an intent category that drives retrieval
    strategy and composite weight selection.

    Intent categories:
        "hidden_gems"    - User wants underrated/overlooked shows
        "crowd_pleasers" - User wants popular/universally loved shows
        "polarizing"     - User wants controversial/edgy shows
        "genre_combo"    - Multi-concept query (mecha + romance)
        "vibe"           - Single-concept or freeform thematic query

    Returns (intent: str, concepts: list[str]) tuple.
    """
    query_lower = query.lower()

    # Check intent signals (order matters: most specific first)
    for signal in _HIDDEN_GEM_SIGNALS:
        if signal in query_lower:
            return "hidden_gems", []
    for signal in _POLARIZING_SIGNALS:
        if signal in query_lower:
            return "polarizing", []
    for signal in _CROWD_PLEASER_SIGNALS:
        if signal in query_lower:
            return "crowd_pleasers", []

    # Narrative detection (tiered signal strength):
    # Strong signals trigger narrative on their own.
    # Weak signals must be combined (character ref + plot verb).
    has_strong_phrase = any(p in query_lower for p in _NARRATIVE_STRONG_PHRASES)
    has_connector = any(c in query_lower for c in _NARRATIVE_CONNECTORS)
    has_char_ref = any(r in query_lower for r in _NARRATIVE_CHARACTER_REFS)
    has_plot_verb = any(v in query_lower for v in _NARRATIVE_PLOT_VERBS)

    if has_strong_phrase or has_connector or (has_char_ref and has_plot_verb):
        return "narrative", []

    # Detect genre/theme concepts (word-boundary safe)
    detected = _detect_concepts(query_lower)

    if len(detected) > 1:
        return "genre_combo", detected

    return "vibe", detected


# --- ADAPTIVE COMPOSITE WEIGHTS (COA 3) ---

# Weight profiles per intent. Each must sum to 1.0.
# Format: (similarity, quality, popularity, input_rank, recency)
_WEIGHT_PROFILES = {
    # Pure vibe/thematic: trust semantic match heavily.
    #
    # COA C Option 2 (2026-09-07) — popularity and recency zeroed. The
    # Phase A probe traced popularity bias to the composite reranker with
    # this profile: for hidden-gems queries where FAISS retrieval correctly
    # over-represented obscure shows (35.5%), composite reranking crashed
    # them to 15.0% and jumped popular from 14% to 36.7%. The +0.05
    # popularity and +0.05 recency weights were small individually but
    # correlated (recent shows are more popular), and combined with
    # Bayesian quality shrinkage they systematically demoted high-scored
    # obscure shows. Weights redistributed to similarity (+0.05) and
    # input_rank (+0.05) — leans harder on FAISS's own ordering, which
    # is where the correct signal is.
    "vibe": {
        "similarity": 0.60, "quality": 0.20,
        "popularity": 0.00, "input_rank": 0.20, "recency": 0.00,
    },
    # Genre combo: input_rank carries AND-semantics from cross-concept reranking
    "genre_combo": {
        "similarity": 0.40, "quality": 0.20,
        "popularity": 0.05, "input_rank": 0.30, "recency": 0.05,
    },
    # Hidden gems: lean on similarity + quality (signals we can trust for
    # niche shows), apply a mild popularity penalty, keep recency neutral-ish.
    #
    # COA C Phase D (2026-09-07) retuning: the previous profile
    # (popularity -0.15, recency +0.30) was calibrated pre-COA-D and was
    # never load-bearing until Option 1 rerouted latent-hidden-gem queries
    # here. Post-Option-1, the aggressive weights over-corrected — the
    # engine surfaced 2020s shorts with <1000 votes and MAL scores near the
    # 6.0 quality floor (Yoru no Kuni, Undetectable Box, Porte) instead of
    # canonical hidden gems (Mushi-Shi 296k votes/8.65, Kaiba 48k/8.14,
    # Kino's Journey 105k/8.28). The mechanism was: aggressive negative
    # popularity penalized every popular show regardless of quality, and
    # +0.30 recency boosted 2025 obscure over 2005 classics. Tuned profile
    # preserves the direction (hidden gems SHOULD skew less popular) but
    # matches the eval curator's operational definition — critically-loved
    # cult classics, not "nobody has heard of it AND it might be bad."
    "hidden_gems": {
        "similarity": 0.55, "quality": 0.30,
        "popularity": -0.05, "input_rank": 0.15, "recency": 0.05,
    },
    # Crowd pleasers: boost popularity + quality, trust the crowd
    "crowd_pleasers": {
        "similarity": 0.35, "quality": 0.30,
        "popularity": 0.20, "input_rank": 0.10, "recency": 0.05,
    },
    # Polarizing: standard weights — the Phase 2 reranker handles controversy scoring
    "polarizing": {
        "similarity": 0.50, "quality": 0.20,
        "popularity": 0.05, "input_rank": 0.15, "recency": 0.10,
    },
    # Narrative: trust semantic match heavily — plot/character embedding alignment is primary signal
    "narrative": {
        "similarity": 0.60, "quality": 0.15,
        "popularity": 0.05, "input_rank": 0.15, "recency": 0.05,
    },
}

# Default weights (backward-compatible with pre-COA behavior)
_W_DEFAULT = _WEIGHT_PROFILES["vibe"]


# --- METADATA-AWARE RERANKING ---

# Legacy weight constants (used as defaults if no intent profile is provided)
_W_SIMILARITY = 0.50  # Semantic relevance (FAISS cosine)
_W_QUALITY = 0.25     # Bayesian-adjusted MAL score (normalized 0-1)
_W_POPULARITY = 0.05  # Log-scaled vote count (normalized 0-1)
_W_INPUT_RANK = 0.15  # Preserves cross-concept AND-ordering from decomposition
_W_RECENCY = 0.05     # Favors newer shows to counteract old-show embedding bias

# Quality floor: candidates below this raw MAL score are dropped before
# composite scoring.  Non-destructive — DB and FAISS index are untouched.
_QUALITY_FLOOR = 6.0

# Normalization constants (derived from DB statistics)
_MAL_SCORE_MIN = 2.87
_MAL_SCORE_MAX = 9.28
_LOG_VOTES_MAX = np.log1p(3_035_579)  # log(1 + max scored_by)
_YEAR_MIN = 1990
_YEAR_MAX = 2025

# Bayesian constants (must match queries.py)
_BAYESIAN_GLOBAL_MEAN = 7.05
_BAYESIAN_MIN_VOTES = 5000


def _composite_rerank(candidates, weights=None, skip_quality_floor=False):
    """
    Re-scores candidates using a weighted blend of semantic similarity,
    Bayesian-adjusted quality, log-scaled popularity, input rank
    (which reflects cross-concept AND-semantics when decomposition was used),
    and recency.

    Args:
        candidates: List of {title, similarity} dicts from FAISS retrieval.
        weights: Optional dict with keys (similarity, quality, popularity,
                 input_rank, recency). If None, uses legacy default weights.
        skip_quality_floor: If True, skip the quality floor filter. Used when
            the user has specified explicit quality constraints (e.g., "between
            6.5 and 7.5") and the filtered search already enforced the bounds.

    Applies a quality floor first: candidates with raw MAL score below
    _QUALITY_FLOOR are dropped before scoring to prevent low-quality shows
    from consuming ranking slots.

    Returns results sorted by composite score with the original similarity
    preserved for Phase 2.
    """
    if not candidates:
        return candidates

    titles = [r["title"] for r in candidates]
    placeholders = ",".join(["?"] * len(titles))

    with _get_readonly_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT english_title, mal_score, scored_by, release_year FROM anime_info "
            f"WHERE english_title IN ({placeholders})",
            titles,
        )
        db_lookup = {}
        for eng_title, mal_score, scored_by, release_year in cursor.fetchall():
            db_lookup[eng_title] = (mal_score or 0.0, scored_by or 0, release_year or 0)

    # G1: Quality floor — drop candidates below the threshold
    # Skipped when user has specified explicit quality constraints (already enforced)
    if not skip_quality_floor:
        filtered_candidates = []
        dropped = 0
        for r in candidates:
            raw_score = db_lookup.get(r["title"], (0.0, 0, 0))[0]
            if raw_score >= _QUALITY_FLOOR or raw_score == 0.0:
                filtered_candidates.append(r)
            else:
                dropped += 1
        if dropped:
            logger.info(f"Quality floor ({_QUALITY_FLOOR}): dropped {dropped} candidates.")
        candidates = filtered_candidates

    # Resolve weight profile
    w = weights or {
        "similarity": _W_SIMILARITY, "quality": _W_QUALITY,
        "popularity": _W_POPULARITY, "input_rank": _W_INPUT_RANK,
        "recency": _W_RECENCY,
    }

    n = len(candidates)
    reranked = []
    for input_rank, r in enumerate(candidates):
        title = r["title"]
        sim = r["similarity"]
        raw_score, votes, year = db_lookup.get(title, (0.0, 0, 0))

        # Bayesian-adjusted quality, normalized to [0, 1]
        v, m, C = votes, _BAYESIAN_MIN_VOTES, _BAYESIAN_GLOBAL_MEAN
        bayesian = (v / (v + m)) * raw_score + (m / (v + m)) * C
        norm_quality = (bayesian - _MAL_SCORE_MIN) / (_MAL_SCORE_MAX - _MAL_SCORE_MIN)
        norm_quality = max(0.0, min(1.0, norm_quality))

        # Log-scaled popularity, normalized to [0, 1]
        norm_pop = np.log1p(votes) / _LOG_VOTES_MAX
        norm_pop = max(0.0, min(1.0, norm_pop))

        # Input rank signal: preserves cross-concept ordering from
        # _cross_concept_rerank (1.0 for rank 0, decaying toward 0)
        norm_rank = 1.0 - (input_rank / n) if n > 1 else 1.0

        # G4: Recency signal — newer shows get a mild boost (0.0 to 1.0)
        if year and year >= _YEAR_MIN:
            norm_recency = (year - _YEAR_MIN) / (_YEAR_MAX - _YEAR_MIN)
            norm_recency = max(0.0, min(1.0, norm_recency))
        else:
            norm_recency = 0.5  # Neutral for unknown years

        composite = (
            w["similarity"] * sim
            + w["quality"] * norm_quality
            + w["popularity"] * norm_pop
            + w["input_rank"] * norm_rank
            + w["recency"] * norm_recency
        )

        reranked.append({
            "title": title,
            "similarity": sim,
            "_composite": composite,
        })

    reranked.sort(key=lambda x: x["_composite"], reverse=True)

    return [{"title": r["title"], "similarity": r["similarity"]} for r in reranked]


def search(query, top_k=50, year_min=None, year_max=None, rating_max=None,
           exclude_mal_ids=None):
    """
    Embeds the user query, retrieves a large candidate pool from FAISS,
    applies metadata-aware composite reranking (blending semantic similarity
    with Bayesian quality and popularity), and returns a dict with results
    and retrieval metadata for diagnostics.

    If the query contains structured constraints (temporal or genre filters),
    or if UI-level era bounds are provided, routes through the hybrid
    SQL+FAISS filtered path first. Falls back to the standard unfiltered
    FAISS path if no filters are detected or if the filtered search yields
    no results.

    `exclude_mal_ids` (PR C — watchlist exclusion) is a list of MAL IDs the
    user has marked watched/dropped. The engine expands these to full
    series_id clusters (PR #77 fold-in) before applying the exclusion, so
    listing "Naruto" excludes the whole 19-entry Naruto cluster. Exclusion
    forces the filtered path so the SQL-level filter applies (the
    unfiltered FAISS path would either lose query decomposition or require
    post-FAISS dropouts that under-fetch the requested top-k). Empty
    post-exclusion pool returns [] with no fallback — the user opted into
    the exclusion.

    Returns:
        {
            "results": [{"title": str, "similarity": float}, ...],
            "meta": {
                "retrieval_path": str,
                "concepts_detected": list[str] | None,
                "candidates_retrieved": int,
                "filters_applied": dict | None,
            }
        }
    """
    # --- INTENT CLASSIFICATION (COA 1) ---
    intent, _ = _classify_intent(query)
    weights = _WEIGHT_PROFILES.get(intent, _W_DEFAULT)
    logger.info(f"Intent classified: '{intent}' | Weight profile: {weights}")

    # --- FILTERED PATH (COA 2) ---
    filters = _extract_filters(query)

    # Merge UI-level era bounds (radio buttons) into parsed filters.
    # If the query already contains ANY temporal constraint (year, year_min,
    # year_max, or season), the query's intent takes full precedence and UI
    # era bounds are ignored to avoid contradictory filters.
    query_has_temporal = any([filters["year"], filters["year_min"],
                             filters["year_max"], filters["season"]])
    if not query_has_temporal:
        if year_min is not None:
            filters["year_min"] = year_min
            filters["has_filters"] = True
        if year_max is not None:
            filters["year_max"] = year_max
            filters["has_filters"] = True

    # UI-supplied rating ceiling — no equivalent natural-language form, so
    # we don't need a query_has_rating gate. Just merge in if present.
    if rating_max is not None:
        filters["mal_rating_max"] = rating_max
        filters["has_filters"] = True

    # PR C — UI-supplied watchlist exclusion. Expand caller-supplied MAL
    # IDs to the full series_id cluster set (PR #77 fold-in). Forces the
    # filtered path; the exclusion applies as a Python-side set membership
    # filter on the SQL row set inside _filtered_vector_search.
    if exclude_mal_ids:
        from analysis.queries import expand_to_cluster_ids
        excluded_ids_set = expand_to_cluster_ids(exclude_mal_ids)
        if excluded_ids_set:
            filters["exclude_mal_ids"] = excluded_ids_set
            filters["has_filters"] = True
            logger.info(
                f"Watchlist exclusion: {len(exclude_mal_ids)} input IDs -> "
                f"{len(excluded_ids_set)} after cluster expansion."
            )

    if filters["has_filters"]:
        filtered_results = _filtered_vector_search(query, filters, top_k=300)
        if filtered_results is not None:
            logger.info(
                f"Filtered path: {len(filtered_results)} candidates. "
                f"Routing to composite reranking."
            )
            skip_floor = filters.get("has_numeric_constraints", False)
            reranked = _composite_rerank(filtered_results, weights=weights, skip_quality_floor=skip_floor)
            # Build filter description for journal
            filter_parts = []
            if filters.get("year"): filter_parts.append(f"year={filters['year']}")
            if filters.get("year_min"): filter_parts.append(f"after={filters['year_min']}")
            if filters.get("year_max"): filter_parts.append(f"before={filters['year_max']}")
            if filters.get("season"): filter_parts.append(f"season={filters['season']}")
            if filters.get("genre_db_terms"): filter_parts.append(f"genres={filters['genre_db_terms']}")
            if filters.get("quality_min"): filter_parts.append(f"quality_min={filters['quality_min']}")
            if filters.get("quality_max"): filter_parts.append(f"quality_max={filters['quality_max']}")
            if filters.get("sentiment_min") is not None: filter_parts.append(f"sentiment_min={filters['sentiment_min']}")
            if filters.get("sentiment_max") is not None: filter_parts.append(f"sentiment_max={filters['sentiment_max']}")
            if filters.get("scored_by_min"): filter_parts.append(f"scored_by_min={filters['scored_by_min']}")
            if filters.get("scored_by_max"): filter_parts.append(f"scored_by_max={filters['scored_by_max']}")
            if filters.get("studio"): filter_parts.append(f"studio={filters['studio']}")
            if filters.get("mal_rating_max"): filter_parts.append(f"rating_max={filters['mal_rating_max']}")
            # Build human-readable constraint summary for the reranker prompt
            constraint_parts = []
            if filters.get("quality_min") is not None and filters.get("quality_max") is not None:
                constraint_parts.append(f"Quality Score between {filters['quality_min']} and {filters['quality_max']}")
            elif filters.get("quality_min") is not None:
                constraint_parts.append(f"Quality Score above {filters['quality_min']}")
            elif filters.get("quality_max") is not None:
                constraint_parts.append(f"Quality Score below {filters['quality_max']}")
            if filters.get("sentiment_min") is not None:
                constraint_parts.append(f"Audience Mood >= {filters['sentiment_min']} (positive reception)")
            if filters.get("sentiment_max") is not None:
                constraint_parts.append(f"Audience Mood <= {filters['sentiment_max']} (mixed/negative reception)")
            if filters.get("scored_by_min") is not None:
                constraint_parts.append(f"Popularity above {filters['scored_by_min']:,} ratings")
            if filters.get("scored_by_max") is not None:
                constraint_parts.append(f"Popularity below {filters['scored_by_max']:,} ratings")
            if filters.get("studio"):
                constraint_parts.append(f"Studio: {filters['studio']}")

            return {
                "results": reranked[:top_k],
                "meta": {
                    "retrieval_path": "filtered:" + "+".join(filter_parts),
                    "intent": intent,
                    "concepts_detected": filters.get("genres"),
                    "candidates_retrieved": len(filtered_results),
                    "filters_applied": _filters_for_telemetry(filters),
                    "has_numeric_constraints": filters.get("has_numeric_constraints", False),
                    "constraint_summary": "; ".join(constraint_parts) if constraint_parts else None,
                }
            }
        # else: fall through to unfiltered path

    # --- UNFILTERED PATH (standard FAISS retrieval) ---
    # Downstream helpers (`_search_faiss_raw`, `_cross_concept_rerank`)
    # each reload the index singleton themselves — no need to bind
    # here.
    query_embedding = _embed([query])

    # Over-fetch: pull 300 candidates so composite reranking has room to
    # promote high-quality relevant shows after the quality floor filter
    # drops sub-6.0 candidates from the pool.
    fetch_k = 300
    original_results = _search_faiss_raw(query_embedding, fetch_k)

    # Query decomposition: for multi-concept queries, compute each
    # candidate's similarity to every concept independently, then
    # re-rank by min(concept_similarities) to enforce AND-semantics.
    sub_queries = _decompose_query(query)
    concepts_detected = None
    if sub_queries:
        concepts_detected = sub_queries
        sub_query_embeddings = [_embed([sq]) for sq in sub_queries]

        # Multi-pool retrieval — search FAISS with each sub-query
        # independently and merge into the candidate pool.
        seen_titles = {r["title"] for r in original_results}
        injected = 0
        for sq_emb in sub_query_embeddings:
            sq_results = _search_faiss_raw(sq_emb, 150)
            for r in sq_results:
                if r["title"] not in seen_titles:
                    original_results.append(r)
                    seen_titles.add(r["title"])
                    injected += 1
        if injected:
            logger.info(
                f"Multi-pool retrieval: injected {injected} candidates "
                f"from {len(sub_queries)} sub-queries. "
                f"Total pool: {len(original_results)}."
            )

        cross_ranked = _cross_concept_rerank(original_results, sub_query_embeddings)
        logger.info(
            f"Query decomposed into {len(sub_queries)} concepts: "
            f"{sub_queries}. Cross-concept reranked {len(cross_ranked)} candidates."
        )
        retrieval_path = f"unfiltered:decomposed({len(sub_queries)}_concepts)"
    else:
        cross_ranked = original_results
        retrieval_path = "unfiltered:single_embedding"

    # Composite rerank with intent-aware weights (COA 3)
    reranked = _composite_rerank(cross_ranked, weights=weights)

    return {
        "results": reranked[:top_k],
        "meta": {
            "retrieval_path": retrieval_path,
            "intent": intent,
            "concepts_detected": concepts_detected,
            "candidates_retrieved": len(cross_ranked),
            "filters_applied": None,
        }
    }


# --- CENTROID-BASED RETRIEVAL (DNA / Shows Like This) ---

def search_by_centroid(titles, top_k=50, negative_anchors=None,
                       additional_excluded_titles=None, exclude_mal_ids=None):
    """
    Retrieves similar anime by computing the vector centroid of 1+ reference
    shows and searching FAISS directly — no text embedding, no query
    decomposition.

    For multi-show inputs, averages the reference vectors and L2-normalizes
    the centroid. For single-show inputs, uses the show's own vector directly
    (nearest-neighbor lookup).

    When `negative_anchors` is provided (1-2 titles the user wants to avoid
    resembling), an anti-centroid is computed and each candidate's similarity
    is adjusted as `pos_similarity − alpha × neg_similarity`. Alpha defaults
    to 0.5 and is tunable via `DNA_NEGATIVE_ALPHA` env var. Candidates that
    are strongly similar to the negative anchors AND only weakly similar to
    the positives are dropped before the candidate list is built (pre-filter,
    saves Gemini tokens downstream).

    `additional_excluded_titles` (Phase D, Initiative 1) is unioned with the
    default excluded set. The DNA orchestrator uses this to pass every
    sibling-cluster title of the reference shows (computed via series_id),
    structurally preventing the same-series-leak failure mode that the
    prefix-based find_franchise_titles can miss (Naruto → Road of Naruto).

    `exclude_mal_ids` (PR C — watchlist exclusion) is the caller-supplied
    raw MAL ID list from the user's watchlist. The function expands these
    to full series_id clusters (PR #77 fold-in), then looks up their titles
    in anime_info and unions into the excluded set. The MAL-IDs-in /
    titles-out plumbing keeps the centroid pipeline single-keyed on title
    (matching how FAISS metadata is indexed) without leaking MAL IDs into
    the FAISS iteration.

    Returns a dict with results and retrieval metadata:
        {
            "results": [{"title": str, "similarity": float}, ...],
            "meta": {
                "retrieval_path": "centroid",
                "reference_count": int,
                "negative_anchor_count": int,
                "excluded_count": int,
                "prefiltered_count": int,
                "candidates_retrieved": int,
            }
        }
    """
    index, metadata = _load_index()

    # Build title -> FAISS position lookup
    title_to_pos = {m["english_title"]: i for i, m in enumerate(metadata)}

    # Collect positive reference vectors
    ref_vectors = []
    for title in titles:
        pos = title_to_pos.get(title)
        if pos is not None:
            ref_vectors.append(index.reconstruct(pos))
        else:
            logger.warning(f"Centroid search: '{title}' not found in FAISS index.")

    if not ref_vectors:
        logger.error("Centroid search: no valid reference vectors found.")
        return {"results": [], "meta": {"retrieval_path": "centroid", "reference_count": 0, "negative_anchor_count": 0, "excluded_count": 0, "prefiltered_count": 0, "candidates_retrieved": 0}}

    # Positive centroid
    pos_centroid = np.mean(ref_vectors, axis=0)
    pos_centroid = pos_centroid / np.linalg.norm(pos_centroid)
    pos_centroid_query = pos_centroid.reshape(1, -1).astype(np.float32)

    # Anti-centroid (only if negative_anchors provided and at least one resolves)
    neg_centroid = None
    neg_vector_count = 0
    if negative_anchors:
        neg_vectors = []
        for title in negative_anchors:
            pos = title_to_pos.get(title)
            if pos is not None:
                neg_vectors.append(index.reconstruct(pos))
            else:
                logger.warning(f"Centroid search: negative anchor '{title}' not found in FAISS index.")
        if neg_vectors:
            neg_centroid = np.mean(neg_vectors, axis=0)
            neg_centroid = neg_centroid / np.linalg.norm(neg_centroid)
            neg_vector_count = len(neg_vectors)

    # Search FAISS — over-fetch to give composite reranking room
    fetch_k = 300
    scores, indices = index.search(pos_centroid_query, fetch_k)

    # Anti-centroid scoring parameters. Default 0.7 calibrated for the realistic
    # in-neighborhood use case — users add negatives to discriminate between
    # similar-but-different shows (e.g., sincere romcom vs. fanservice romcom),
    # NOT to reject cross-genre shows they wouldn't consider anyway. Higher alpha
    # is needed when positive and negative anchors sit close in vector space.
    alpha = float(os.environ.get("DNA_NEGATIVE_ALPHA", "0.7"))
    PRE_FILTER_NEG_THRESHOLD = 0.70  # candidate must be strongly similar to negatives...
    PRE_FILTER_POS_FLOOR = 0.65      # ...AND only weakly similar to positives to be dropped

    # Build excluded set (don't recommend reference shows, negative anchors, or franchise variants)
    excluded = set(titles)
    if negative_anchors:
        excluded.update(negative_anchors)
    from analysis.queries import find_franchise_titles
    excluded.update(find_franchise_titles(titles))
    if negative_anchors:
        excluded.update(find_franchise_titles(negative_anchors))
    # Phase D — caller-supplied series-cluster exclusions. Structural
    # complement to find_franchise_titles' prefix heuristic; catches
    # franchise members whose titles don't share a prefix with the
    # reference (e.g., Naruto -> "Road of Naruto", "Boruto").
    if additional_excluded_titles:
        excluded.update(additional_excluded_titles)

    # PR C — watchlist exclusion. Cluster-expand the caller's raw MAL ID
    # list, then resolve to titles for the FAISS-iteration filter set.
    # The strict-trust contract (empty pool -> []) lives at the result-
    # building layer below where excluded membership is checked.
    if exclude_mal_ids:
        from analysis.queries import expand_to_cluster_ids
        excluded_ids_set = expand_to_cluster_ids(exclude_mal_ids)
        if excluded_ids_set:
            id_to_title = {m["id"]: m["english_title"] for m in metadata}
            excluded.update(
                id_to_title[mid] for mid in excluded_ids_set if mid in id_to_title
            )
            logger.info(
                f"Centroid search: watchlist exclusion expanded "
                f"{len(exclude_mal_ids)} -> {len(excluded_ids_set)} MAL IDs."
            )

    results = []
    prefiltered_count = 0
    for i in range(fetch_k):
        idx = int(indices[0][i])
        if not (0 <= idx < len(metadata)):
            continue
        title = metadata[idx]["english_title"]
        if title in excluded:
            continue

        pos_similarity = float(scores[0][i])

        if neg_centroid is not None:
            # Reconstruct candidate vector to compute negative similarity
            candidate_vec = index.reconstruct(idx)
            neg_similarity = float(np.dot(candidate_vec, neg_centroid))

            # Pre-filter: drop candidates strongly similar to negatives AND
            # weakly similar to positives. Saves Gemini tokens on obvious
            # misses that the score adjustment alone wouldn't push out of the
            # top-15 rerank slice.
            if neg_similarity > PRE_FILTER_NEG_THRESHOLD and pos_similarity < PRE_FILTER_POS_FLOOR:
                prefiltered_count += 1
                continue

            adjusted_similarity = pos_similarity - alpha * neg_similarity
        else:
            adjusted_similarity = pos_similarity

        results.append({
            "title": title,
            "similarity": adjusted_similarity,
        })

    # Re-sort by adjusted similarity when negatives were applied (FAISS order
    # was by positive similarity only).
    if neg_centroid is not None:
        results.sort(key=lambda r: r["similarity"], reverse=True)

    logger.info(
        f"Centroid search: {len(ref_vectors)} positive reference(s), "
        f"{neg_vector_count} negative anchor(s), "
        f"{len(results)} candidates (excluded {len(excluded)}, "
        f"prefiltered {prefiltered_count})."
    )

    # Apply composite reranking (quality, popularity, recency)
    reranked = _composite_rerank(results)
    return {
        "results": reranked[:top_k],
        "meta": {
            "retrieval_path": "centroid",
            "reference_count": len(ref_vectors),
            "negative_anchor_count": neg_vector_count,
            "excluded_count": len(excluded),
            "prefiltered_count": prefiltered_count,
            "candidates_retrieved": len(results),
        }
    }


# --- CLI ENTRY POINT ---
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("Building FAISS index from anime_intelligence_v2.db...")
    count = build_index()
    print(f"Done. Indexed {count} anime.")
