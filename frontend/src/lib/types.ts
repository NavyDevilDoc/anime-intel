// Mirrors the Pydantic schemas in api/schemas/responses.py

interface DroppedSibling {
  id: number
  title: string
  match_confidence: number
  query_relevant_con: string | null
  contains_spoiler: boolean
}

export interface SeriesContext {
  series_id: string
  series_size: number
  position: number  // 1-indexed chronological position within the cluster
  score_min: number | null
  score_max: number | null
  // PR B (subtype bundle) — true when score_min/score_max reflect a
  // TV+TV-Special+Movie filter that narrowed the visible range vs the
  // full cluster (e.g., Frieren Mini Anime hidden). Drives an optional
  // "TV and theatrical entries only" caption. Optional for backward
  // compatibility — backend defaults to false if not set.
  score_range_filtered?: boolean
  sentiment_min: number | null
  sentiment_max: number | null
  // null for singletons or insufficient score data
  trajectory: 'stable' | 'decline' | 'improve' | 'mixed' | null
  // PR B — "Season X of Y" label support. tv_position is the 1-indexed
  // rank among TV-subtype siblings (TV Special excluded — not a
  // numbered season). tv_count is the number of TV-subtype siblings.
  // Both null when this entry isn't itself TV-subtype (movie/OVA).
  tv_position?: number | null
  tv_count?: number | null
  // Phase D — same-series entries that cleared the rerank floor but
  // were collapsed in favor of the higher-scoring visible result.
  // Empty array on encyclopedia (no rerank) and on standalone shows.
  dropped_siblings: DroppedSibling[]
}

export interface ShowProfile {
  id: number
  title: string
  studio: string
  quality_score: number
  scored_by: number
  audience_sentiment: number
  release_year: number
  synopsis: string
  audience_consensus: string
  pros: string[]
  cons: string[]
  controversy_score: number
  semantic_similarity: number
  // Verbatim MAL content rating from Jikan (e.g. "PG-13 - Teens 13 or older").
  // Empty string when MAL has no rating set; null when not yet backfilled.
  mal_rating: string | null
  // Gemini-distilled 3-word thematic descriptor from consensus_json
  // (e.g. "Sci-Fi, Neo-noir, Space Western"). Powers the ExploreVibeButton
  // CTA. Null when the backend/offline-path hasn't surfaced it.
  thematic_vibe?: string | null
}

export interface ScoredResult {
  ai_reasoning: string
  match_confidence: number
  controversy_warning: string | null
  contains_spoiler: boolean
  // One con verbatim from the show's consensus data, picked by the reranker
  // as most relevant to the user's specific query. Null when no con speaks
  // to the query (anti-stretch), when the show has no cons, or under
  // Objective Rankings (LLM reranker bypassed). Rendered on result cards
  // as a "Heads up:" line below the AI Reasoning, gated by the same
  // contains_spoiler flag.
  query_relevant_con?: string | null
  profile: ShowProfile
  // Cluster-level metadata for the show's series (Initiative 1, Phase C).
  // Null when the engine bypassed hydration. Phase E renders the
  // position pill, trajectory caption, range, and dropped-siblings
  // drawer when present.
  series_context?: SeriesContext | null
}

export interface SearchResponse {
  success: boolean
  pool: ScoredResult[]
  pool_size: number
  search_id: string | null
  error: string | null
  // "I Don't Know" mode flag — true when the engine has weak confidence
  // (top result < 75) or the pool is empty entirely. Frontend renders a
  // yellow advisory banner instead of either silent results or a red
  // error block. Defaults to false on older API versions.
  low_confidence?: boolean
  // COA B / Option C — "composite" when Gemini's LLM reranker was blocked
  // by non-configurable policy (typically PROHIBITED_CONTENT on adult-
  // adjacent queries) and results were built from the pre-rerank composite
  // pool as a graceful-degradation fallback. Null on the normal path.
  // Drives the "Semantic match only" pill on ResultCard and a fallback-
  // aware LowConfidenceBanner copy variant.
  fallback_mode?: 'composite' | null
  // Post-request anon quota state — frontend reads these to sync the
  // AnonBanner display with the real server counter.
  search_remaining: number | null
  dna_remaining: number | null
}

export interface DNAResponse {
  success: boolean
  intersection_summary: string | null
  data: ScoredResult[]
  data_size: number
  error: string | null
  negative_anchors_used: string[] | null
  title_resolution_notes: string[] | null
  // Same semantics as SearchResponse.low_confidence — see comment above.
  low_confidence?: boolean
  // Same semantics as SearchResponse.fallback_mode — see comment above.
  fallback_mode?: 'composite' | null
  search_remaining: number | null
  dna_remaining: number | null
}

// --- Item 4a: Staff/Studio search mode ---

export type StaffRole =
  | 'Director'
  | 'Music'
  | 'Original Creator'
  | 'Series Composition'
  | 'Character Design'

interface StaffAutocompleteRole {
  role: StaffRole
  count: number
}

export interface StaffAutocompletePerson {
  person_mal_id: number
  name_display: string
  total_count: number
  roles: StaffAutocompleteRole[]
}

export interface StaffAutocompleteStudio {
  name: string
  show_count: number
}

export interface StaffAutocompleteResponse {
  persons: StaffAutocompletePerson[]
  studios: StaffAutocompleteStudio[]
}

// Internal chip state carrying display metadata. Serialized to the
// /api/search `staff_filter` wire payload by `toStaffFilterPayload` in
// lib/staffChips.ts. Wire shape: `{ persons: {person_mal_id, role}[],
// studios: string[] }` with persons-OR-persons, studios-OR-studios,
// persons-AND-studios semantics — see analysis/vector_store.staff_studio_search.
export type StaffChip =
  | {
      kind: 'person'
      personMalId: number
      nameDisplay: string
      role: StaffRole | null  // null = "Any role"
    }
  | {
      kind: 'studio'
      name: string
    }


export interface EncyclopediaResponse {
  success: boolean
  matches: string[]
  profile: ShowProfile | null
  error: string | null
  // Position-only series context for the resolved encyclopedia entry
  // (Initiative 1, Phase E). Backend ships the full SeriesContext shape;
  // frontend renders just the "Entry X of N" pill — no trajectory,
  // range, or dropped-siblings drawer on encyclopedia (it's a lookup
  // surface, not a recommendation surface).
  series_context?: SeriesContext | null
}
