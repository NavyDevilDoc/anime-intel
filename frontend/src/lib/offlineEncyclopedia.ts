// Client-side Encyclopedia lookup for when the backend is paused.
//
// Reads frontend/public/data/seo_profiles.json (built by
// tools/export_seo_data.py) and does exact / substring / fuzzy matching
// against the show titles in-browser. Return shape mirrors the backend's
// EncyclopediaResponse so Encyclopedia.tsx only needs to branch on the
// network call, not on the response shape.
//
// Wired into Encyclopedia.tsx via the VITE_ENGINE_HIATUS_MODE flag —
// when true, this module handles lookups instead of /api/show/{title}.
// Dormant (never fetched, no bundle-size penalty) when the flag is off.

import type { EncyclopediaResponse, SeriesContext, ShowProfile } from './types'

// Shape of raw entries in seo_profiles.json. Field names match the export
// tool's dict; we normalize to ShowProfile below.
interface RawSeoProfile {
  id: number
  title: string
  romaji_title: string | null
  slug: string
  studio: string | null
  quality_score: number
  raw_mal_score: number
  scored_by: number
  avg_sentiment: number
  release_year: number | null
  season: string | null
  genres: string | null
  synopsis: string | null
  consensus_summary: string | null
  pros: string[]
  cons: string[]
  controversy_score: number | null
  thematic_vibe: string | null
  has_narrative: boolean
  mal_rating: string | null
  // compute_series_context's return shape (no dropped_siblings — that's
  // populated only by the recommendation engine at rerank time).
  series_context: Omit<SeriesContext, 'dropped_siblings'> | null
}

interface SeoProfilesFile {
  anime: RawSeoProfile[]
  generated_at: string
  count: number
}

const SEO_PROFILES_URL = '/data/seo_profiles.json'
const MAX_MATCHES = 10
// Similarity ratio above which we treat a fuzzy hit as a real match rather
// than a "did you mean" candidate. Backend fuzzy resolver is more permissive
// (RapidFuzz with 60), but the client-side path errs on the side of a
// stricter threshold — false-positive matches surface the wrong show, which
// is worse than a "not found" that prompts the user to refine.
const FUZZY_MATCH_THRESHOLD = 0.75
// Ratio above which we include a candidate in the disambiguation list even
// if no single title crossed FUZZY_MATCH_THRESHOLD. Lets close-but-not-exact
// input still produce a helpful "did you mean" list.
const FUZZY_DISAMBIG_THRESHOLD = 0.6

let profilesCache: Promise<RawSeoProfile[]> | null = null

/** Test-only: reset the module-level fetch cache. */
export function _resetCache(): void {
  profilesCache = null
}

/** Fetch and cache the encyclopedia dataset. Subsequent calls share the
 *  same Promise. On error, the cache is cleared so a retry re-fetches. */
export async function fetchProfiles(): Promise<RawSeoProfile[]> {
  if (!profilesCache) {
    profilesCache = fetch(SEO_PROFILES_URL)
      .then(async res => {
        if (!res.ok) {
          throw new Error(`Failed to load encyclopedia data (HTTP ${res.status})`)
        }
        const data = (await res.json()) as SeoProfilesFile
        return data.anime
      })
      .catch(err => {
        profilesCache = null
        throw err
      })
  }
  return profilesCache
}

function normalize(s: string | null | undefined): string {
  if (!s) return ''
  return s.toLowerCase().replace(/[^a-z0-9\s]/g, ' ').replace(/\s+/g, ' ').trim()
}

/** Standard Levenshtein distance with rolling arrays. */
function levenshtein(a: string, b: string): number {
  if (a === b) return 0
  const m = a.length
  const n = b.length
  if (m === 0) return n
  if (n === 0) return m

  let prev = new Array<number>(n + 1)
  let curr = new Array<number>(n + 1)
  for (let j = 0; j <= n; j++) prev[j] = j

  for (let i = 1; i <= m; i++) {
    curr[0] = i
    for (let j = 1; j <= n; j++) {
      const cost = a.charCodeAt(i - 1) === b.charCodeAt(j - 1) ? 0 : 1
      curr[j] = Math.min(
        prev[j] + 1,          // delete from a
        curr[j - 1] + 1,      // insert into a
        prev[j - 1] + cost,   // substitute / match
      )
    }
    ;[prev, curr] = [curr, prev]
  }
  return prev[n]
}

function similarityRatio(a: string, b: string): number {
  if (!a && !b) return 1
  const maxLen = Math.max(a.length, b.length)
  if (maxLen === 0) return 1
  return 1 - levenshtein(a, b) / maxLen
}

/** Map the raw JSON shape to the frontend's ShowProfile type. Renames the
 *  two mismatched field names (avg_sentiment / consensus_summary) and
 *  fills in defaults for fields the SEO export doesn't carry. */
function toShowProfile(raw: RawSeoProfile): ShowProfile {
  return {
    id: raw.id,
    title: raw.title,
    studio: raw.studio ?? '',
    quality_score: raw.quality_score,
    scored_by: raw.scored_by,
    audience_sentiment: raw.avg_sentiment,
    release_year: raw.release_year ?? 0,
    synopsis: raw.synopsis ?? '',
    audience_consensus: raw.consensus_summary ?? '',
    pros: raw.pros ?? [],
    cons: raw.cons ?? [],
    controversy_score: raw.controversy_score ?? 0,
    // Encyclopedia doesn't display semantic_similarity — the field only
    // exists on ShowProfile because search results share the same type.
    // Default to 0 so the type contract holds.
    semantic_similarity: 0,
    mal_rating: raw.mal_rating,
  }
}

/** Bring the raw series_context up to the frontend's SeriesContext type by
 *  filling in the reranker-only fields with empty defaults. Encyclopedia
 *  renders in "minimal" mode which only reads position/size/tv_* fields,
 *  so the empty dropped_siblings has no visible effect. */
function toSeriesContext(
  raw: Omit<SeriesContext, 'dropped_siblings'> | null | undefined,
): SeriesContext | null {
  if (!raw) return null
  return { ...raw, dropped_siblings: [] }
}

/** Result shape for detectTitleMatch — a focused exact-title predicate
 *  for the FaS "did the user type a show title?" hint. Not a general
 *  lookup — intentionally skips substring + fuzzy so we don't
 *  false-positive on vibe queries that happen to contain a title word. */
export interface TitleMatchResult {
  matched: boolean
  title: string | null
}

/** Detect whether the query is (case-insensitively, punctuation-agnostic)
 *  an exact title or romaji_title in the vault. Used by Find-a-Show to
 *  offer an Encyclopedia-lookup hint when users type a specific show
 *  name into a vibe-search field.
 *
 *  Higher-precision, lower-recall than lookupOffline: only fires on true
 *  exact matches. A false-positive here surfaces a "did you mean" hint
 *  for a query that wasn't actually a title, which erodes trust in the
 *  affordance — so we err strict.
 */
export async function detectTitleMatch(query: string): Promise<TitleMatchResult> {
  const trimmed = query.trim()
  if (!trimmed || trimmed.length > 100) {
    return { matched: false, title: null }
  }
  let profiles: RawSeoProfile[]
  try {
    profiles = await fetchProfiles()
  } catch {
    // Silent failure — this is a soft affordance, not load-bearing UX.
    // If the JSON can't load, we just don't show the hint.
    return { matched: false, title: null }
  }
  const q = normalize(trimmed)
  const hit = profiles.find(
    p => normalize(p.title) === q || normalize(p.romaji_title) === q,
  )
  return hit ? { matched: true, title: hit.title } : { matched: false, title: null }
}

/** Options for lookupOffline callers. */
export interface LookupOptions {
  /** Skip the franchise-root disambig heuristic and honor an exact match
   *  directly. Pass true when the lookup originates from a disambig-list
   *  click — the user has already narrowed to a specific title and wants
   *  the profile, not another round of "did you mean". */
  skipFranchiseDisambig?: boolean
}

/** Main entry point. Returns the same shape as the backend
 *  EncyclopediaResponse so Encyclopedia.tsx doesn't need to case-split. */
export async function lookupOffline(
  title: string,
  opts: LookupOptions = {},
): Promise<EncyclopediaResponse> {
  const trimmed = title.trim()
  if (!trimmed) {
    return { success: false, matches: [], profile: null, error: 'Title is required.' }
  }
  if (trimmed.length > 100) {
    return {
      success: false,
      matches: [],
      profile: null,
      error: 'Title must be 100 characters or fewer.',
    }
  }

  let profiles: RawSeoProfile[]
  try {
    profiles = await fetchProfiles()
  } catch (err) {
    return {
      success: false,
      matches: [],
      profile: null,
      error: err instanceof Error ? err.message : 'Failed to load encyclopedia data.',
    }
  }

  const q = normalize(trimmed)

  // 1) Exact match on either normalized title or normalized romaji_title.
  const exact = profiles.filter(
    p => normalize(p.title) === q || normalize(p.romaji_title) === q,
  )
  // 2) Substring match — computed alongside so we can detect "franchise
  //    root" queries where the exact hit is only one of many family members.
  const substring = profiles.filter(
    p => normalize(p.title).includes(q) || normalize(p.romaji_title).includes(q),
  )

  if (exact.length === 1) {
    const exactId = exact[0].id
    const otherSubstring = substring.filter(p => p.id !== exactId)
    // Franchise-root heuristic: if the exact hit has 2+ siblings whose
    // titles also contain the query (e.g., "Naruto" → Shippuden, Boruto,
    // Road of Naruto, ...), surface the family as a disambiguation list
    // with the exact match first, so the user can click the canonical
    // entry OR pick a sibling in one interaction. Threshold of 2 keeps
    // this from firing on incidental one-off matches (e.g., "Cowboy
    // Bebop" would still resolve directly with just its movie as the
    // only other hit). Deliberately diverges from the backend's Tier-1
    // behavior in analysis/queries.resolve_show_title, which
    // short-circuits on any exact hit — the UX gain is worth the
    // divergence in the hiatus path.
    //
    // Skipped when opts.skipFranchiseDisambig is true — the caller
    // (typically a disambig-list click handler) is telling us the user
    // has already narrowed to this specific title and wants the profile
    // now, not another round of "did you mean". Without this bypass,
    // clicking the exact match at the top of the disambig list would
    // re-fire the heuristic and re-render the same list, looking to the
    // user like nothing happened.
    if (!opts.skipFranchiseDisambig && otherSubstring.length >= 2) {
      const sortedOthers = otherSubstring.sort((a, b) => a.title.length - b.title.length)
      return {
        success: true,
        matches: [
          exact[0].title,
          ...sortedOthers.slice(0, MAX_MATCHES - 1).map(p => p.title),
        ],
        profile: null,
        error: null,
      }
    }
    return {
      success: true,
      matches: [exact[0].title],
      profile: toShowProfile(exact[0]),
      series_context: toSeriesContext(exact[0].series_context),
      error: null,
    }
  }
  if (exact.length > 1) {
    return {
      success: true,
      matches: exact.slice(0, MAX_MATCHES).map(p => p.title),
      profile: null,
      error: null,
    }
  }

  // 3) Substring-only match (no exact hit). Common case for partial titles
  //    ("evangelion", "code geass"). One hit → profile; many hits → list.
  if (substring.length === 1) {
    return {
      success: true,
      matches: [substring[0].title],
      profile: toShowProfile(substring[0]),
      series_context: toSeriesContext(substring[0].series_context),
      error: null,
    }
  }
  if (substring.length > 1) {
    // Sort by title length (shorter titles tend to be canonical entries;
    // longer titles are sequels/spinoffs whose containment of the query
    // is coincidental). Cap at MAX_MATCHES so the UI list stays scannable.
    const sorted = substring.sort((a, b) => a.title.length - b.title.length)
    return {
      success: true,
      matches: sorted.slice(0, MAX_MATCHES).map(p => p.title),
      profile: null,
      error: null,
    }
  }

  // 4) Fuzzy match. Score every profile's title (and romaji_title where
  //    it differs) against the query, then keep candidates above the
  //    disambig threshold. If the best one crosses FUZZY_MATCH_THRESHOLD
  //    and no other candidate is within 0.05 of it, treat as a confident
  //    single-hit; otherwise return the top N as a "did you mean" list.
  interface Scored { profile: RawSeoProfile; score: number }
  const scored: Scored[] = []
  for (const p of profiles) {
    const titleScore = similarityRatio(normalize(p.title), q)
    const romajiScore = p.romaji_title
      ? similarityRatio(normalize(p.romaji_title), q)
      : 0
    const best = Math.max(titleScore, romajiScore)
    if (best >= FUZZY_DISAMBIG_THRESHOLD) {
      scored.push({ profile: p, score: best })
    }
  }
  scored.sort((a, b) => b.score - a.score)

  if (scored.length === 0) {
    return {
      success: false,
      matches: [],
      profile: null,
      error: `Could not identify "${title}" in the vault.`,
    }
  }

  const top = scored[0]
  const runnerUp = scored[1]
  const confidentSingle =
    top.score >= FUZZY_MATCH_THRESHOLD &&
    (!runnerUp || top.score - runnerUp.score >= 0.05)

  if (confidentSingle) {
    return {
      success: true,
      matches: [top.profile.title],
      profile: toShowProfile(top.profile),
      series_context: toSeriesContext(top.profile.series_context),
      error: null,
    }
  }

  return {
    success: true,
    matches: scored.slice(0, MAX_MATCHES).map(s => s.profile.title),
    profile: null,
    error: null,
  }
}
