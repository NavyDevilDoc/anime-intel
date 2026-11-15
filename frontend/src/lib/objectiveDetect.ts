/**
 * Objective-shape detection for Find-a-Show queries.
 *
 * Fires a hint on the Intelligent Search results surface when the user's
 * query reads as a ranking / temporal-filter request that Objective
 * Rankings mode (or the /top browse surface) would answer more precisely
 * than IS's vibe-matching pipeline.
 *
 * Pure synchronous regex sweep — no fetches, no async. The Python
 * authoritative parser lives in analysis/vector_store._parse_objective_query;
 * this is a deliberately looser frontend classifier scoped to the subset
 * of signals that unambiguously indicate objective-shape intent.
 *
 * Firing rule (all case-insensitive):
 *   1. Explicit top-N ("top 5", "top 10") -> fire
 *   2. Superlative + (genre | decade | year | starts-with-superlative) -> fire
 *   3. Year, year range, or decade present alone -> fire
 *   4. Anti-pattern hit -> hard block regardless of positive signals
 *
 * Season alone ("spring anime") does NOT fire — too ambiguous with vibe
 * queries ("spring vibes"). Genre alone does NOT fire — too common in
 * vibe queries ("mecha shows with strong character arcs").
 */

// Superlative vocabulary — signals ranking intent when paired with a
// qualifier (or when they open the query). Order doesn't matter; the
// scanner returns on first match.
const SUPERLATIVES = [
  'best',
  'top',
  'greatest',
  'worst',
  'highest rated',
  'top rated',
  'top-rated',
  'most popular',
  'most acclaimed',
] as const

// Coarse genre-word list drawn from _CONCEPT_VOCABULARY keys in
// analysis/vector_store.py. Only the canonical keys plus a couple of
// high-frequency synonyms — full synonym expansion is the backend's
// job. Purpose here is corroboration signal for a superlative, not
// authoritative genre extraction.
const GENRE_WORDS = [
  'mecha', 'romance', 'action', 'comedy', 'horror', 'psychological',
  'thriller', 'sports', 'fantasy', 'sci-fi', 'science fiction', 'drama',
  'slice of life', 'mystery', 'supernatural', 'military', 'music',
  'school', 'adventure', 'harem', 'ecchi', 'shounen', 'shonen', 'seinen',
  'josei', 'isekai', 'romcom', 'rom-com',
] as const

// Anti-patterns — figurative constructions where a superlative loses
// its objective-ranking sense. Hard block if any of these match. Grow
// this list from real search_journal false positives, not by guessing.
const ANTI_PATTERNS: readonly RegExp[] = [
  /\bof all time\b/i,       // "greatest villain of all time" — abstract praise
  /\btop[- ]tier\b/i,        // "top-tier animation" — qualitative adjective
  /\bbest girl\b/i,          // fandom slang for waifu-appeal, thematic intent
]

// Explicit top-N: "top 5", "top 10", "top 100". Bounded to 1-3 digits
// so runaway numbers ("top 12345") don't match.
const TOP_N_PATTERN = /\btop\s+(\d{1,3})\b/i

// Year (1900-2099). Standalone token via word boundaries so "2010s" is
// caught by the decade pattern, not this one.
const YEAR_PATTERN = /\b(19|20)\d{2}\b/

// Year range: "2010-2019", "2010 to 2019", "between 2010 and 2019".
const YEAR_RANGE_PATTERNS: readonly RegExp[] = [
  /\bbetween\s+(?:19|20)\d{2}\s+and\s+(?:19|20)\d{2}\b/i,
  /\b(?:19|20)\d{2}\s*(?:to|-|–|—)\s*(?:19|20)\d{2}\b/,
  /\b(?:after|since|before)\s+(?:19|20)\d{2}\b/i,
]

// Decade: "90s", "2010s", "nineties". Two-digit shorthand ("90s") only
// matches when preceded by non-digit — "1990s" is caught by the 20nn0s
// arm, and "2090s" hasn't happened yet.
const DECADE_PATTERN =
  /\b(?:19|20)\d0s\b|\b\d0s\b|\b(?:nineties|eighties|seventies|noughties|aughts)\b/i

// Word-boundary check for a superlative anywhere in the query. Uses a
// pre-built alternation instead of iterating — cheaper on the hot path.
const SUPERLATIVE_PATTERN = new RegExp(
  '\\b(' + SUPERLATIVES.map(s => s.replace(/[-\s]/g, '[-\\s]')).join('|') + ')\\b',
  'i',
)

const GENRE_PATTERN = new RegExp(
  '\\b(' + GENRE_WORDS.map(g => g.replace(/[-\s]/g, '[-\\s]')).join('|') + ')\\b',
  'i',
)

// Superlative at query start (after any leading whitespace). Signals a
// ranking query even without further qualifier — "best romance" reads
// as a ranking request.
const SUPERLATIVE_AT_START = new RegExp(
  '^\\s*(' + SUPERLATIVES.map(s => s.replace(/[-\s]/g, '[-\\s]')).join('|') + ')\\b',
  'i',
)

interface ObjectiveSignals {
  topN: number | null
  hasYear: boolean
  hasYearRange: boolean
  hasDecade: boolean
  hasSuperlative: boolean
  superlativeAtStart: boolean
  hasGenre: boolean
  antiPatternHit: boolean
}

export interface ObjectiveDetectResult {
  matched: boolean
  signals: ObjectiveSignals
}

/**
 * Scan a query for objective-shape signals and apply the firing rule.
 * Pure function — safe to call on every keystroke if a future caller
 * wants live feedback (currently invoked once per search submit).
 */
export function detectObjectiveShape(query: string): ObjectiveDetectResult {
  const q = query ?? ''
  const antiPatternHit = ANTI_PATTERNS.some(p => p.test(q))

  const topNMatch = q.match(TOP_N_PATTERN)
  const topN = topNMatch ? parseInt(topNMatch[1], 10) : null
  const hasYear = YEAR_PATTERN.test(q)
  const hasYearRange = YEAR_RANGE_PATTERNS.some(p => p.test(q))
  const hasDecade = DECADE_PATTERN.test(q)
  const hasSuperlative = SUPERLATIVE_PATTERN.test(q)
  const superlativeAtStart = SUPERLATIVE_AT_START.test(q)
  const hasGenre = GENRE_PATTERN.test(q)

  const signals: ObjectiveSignals = {
    topN,
    hasYear,
    hasYearRange,
    hasDecade,
    hasSuperlative,
    superlativeAtStart,
    hasGenre,
    antiPatternHit,
  }

  if (antiPatternHit) {
    return { matched: false, signals }
  }

  // Rule 1: explicit top-N always fires.
  if (topN !== null) {
    return { matched: true, signals }
  }

  // Rule 2: superlative + qualifier (genre / decade / year / start position).
  if (hasSuperlative && (hasGenre || hasDecade || hasYear || hasYearRange || superlativeAtStart)) {
    return { matched: true, signals }
  }

  // Rule 3: year / year-range / decade present alone — temporal filter
  // that OR handles precisely and IS handles imprecisely.
  if (hasYear || hasYearRange || hasDecade) {
    return { matched: true, signals }
  }

  return { matched: false, signals }
}
