import { detectObjectiveShape } from './objectiveDetect'

/**
 * Vibe-shape detection for Find-a-Show queries submitted in
 * Objective Rankings mode.
 *
 * Symmetric to `detectObjectiveShape`: fires a hint pointing the user
 * at Intelligent Search when their query reads as a vibe / thematic /
 * tonal request that Objective Rankings' SQL-ordered-by-Bayesian-quality
 * sort won't serve well.
 *
 * Hybrid rule: positive vibe signals present AND the query doesn't
 * look objective. The AND half matters — "cozy fantasy from 2010"
 * has both a vibe signal AND a decade; the user clearly wants a
 * filtered ranking and should stay in Objective Rankings. Only
 * fire for pure vibe queries where IS semantic matching would
 * uniquely help.
 *
 * Firing rule (all case-insensitive, after lowercasing):
 *   1. Query contains >=1 vibe adjective (curated list) → vibe signal
 *   2. OR query matches a compound-requirement pattern (`with ...`,
 *      `without ...`, `where the ... [verb]`, `that has/is`) → vibe signal
 *   3. OR query matches a similarity pattern (`similar to`,
 *      `reminds me of`, `in the style of`) → vibe signal
 *   AND detectObjectiveShape(q).matched is FALSE (no objective-shape
 *      signals present).
 *
 * Vocabulary scope: deliberately tight. Grow from search_journal
 * false negatives, not by guessing — same discipline as the
 * objective-detect anti-patterns list.
 */

// Curated vibe adjectives — things that unambiguously signal thematic /
// tonal / emotional intent. Deliberately short; false positives here
// would annoy users browsing by genre word who get told "switch to IS."
const VIBE_ADJECTIVES = [
  // Warm / positive emotional
  'cozy', 'cosy', 'wholesome', 'uplifting', 'heartfelt', 'heartwarming',
  'feel-good', 'feel good',
  // Dark / heavy emotional
  'gritty', 'dark', 'grim', 'bleak', 'melancholic', 'melancholy',
  'tragic', 'depressing', 'brooding',
  // Intellectual / introspective
  'philosophical', 'existential', 'cerebral', 'meditative',
  'thought-provoking', 'thought provoking', 'contemplative',
  'introspective',
  // Pacing / structural tone
  'slow-burn', 'slow burn', 'atmospheric',
  // World / aesthetic tone
  'dystopian', 'utopian', 'surreal', 'nostalgic', 'dreamlike',
  // Narrative mood
  'wholesome', 'chaotic', 'absurd',
] as const

// Compound-requirement patterns — multi-clause query shapes that
// require semantic reasoning (what does "where the villain has a
// point" even mean to a SQL query? nothing). Objective Rankings has
// no way to honor these; IS's reranker reads consensus profiles and
// can.
const COMPOUND_PATTERNS: readonly RegExp[] = [
  /\bwith\s+(?:complex|deep|strong|minimal|subtle)\b/i,  // "with complex worldbuilding"
  /\bwhere\s+(?:the|an?)\s+\w+\s+(?:is|are|has|have|does|do|get|gets|ends|dies|falls|betrays|wins|loses)\b/i,
  /\bwithout\s+(?:the|a|an)?\s*\w+/i,                     // "without romance"
  /\bthat\s+(?:has|features|is|includes|involves)\b/i,    // "that features..."
] as const

// Similarity patterns — explicit "find me more like X" phrasing.
// These are more naturally handled by Shows Like This (DNA
// Triangulation), but IS also handles them acceptably via semantic
// retrieval over the thematic_vibe signal.
const SIMILARITY_PATTERNS: readonly RegExp[] = [
  /\bsimilar\s+to\b/i,
  /\breminds?\s+me\s+of\b/i,
  /\bin\s+the\s+(?:style|vein|spirit)\s+of\b/i,
] as const

// Pre-build the vibe-adjective alternation for cheap word-boundary
// matching. Hyphen/space handled by \s*-?\s* via the replacement.
const VIBE_PATTERN = new RegExp(
  '\\b(' +
    VIBE_ADJECTIVES.map(v => v.replace(/[-\s]/g, '[-\\s]')).join('|') +
  ')\\b',
  'i',
)

interface IntelligentSignals {
  hasVibeAdjective: boolean
  hasCompoundPattern: boolean
  hasSimilarityPattern: boolean
  objectiveShapeMatched: boolean
}

export interface IntelligentDetectResult {
  matched: boolean
  signals: IntelligentSignals
}

/**
 * Scan a query for vibe-shape signals and apply the hybrid firing rule.
 * Pure function — safe to call on every submit without async overhead.
 */
export function detectIntelligentShape(query: string): IntelligentDetectResult {
  const q = query ?? ''

  const hasVibeAdjective = VIBE_PATTERN.test(q)
  const hasCompoundPattern = COMPOUND_PATTERNS.some(p => p.test(q))
  const hasSimilarityPattern = SIMILARITY_PATTERNS.some(p => p.test(q))
  const objectiveShapeMatched = detectObjectiveShape(q).matched

  const signals: IntelligentSignals = {
    hasVibeAdjective,
    hasCompoundPattern,
    hasSimilarityPattern,
    objectiveShapeMatched,
  }

  const hasAnyVibeSignal = hasVibeAdjective || hasCompoundPattern || hasSimilarityPattern

  // Hybrid rule: fire when vibe-signals are present AND the query
  // doesn't ALSO look objective. The AND half prevents firing on
  // mixed-intent queries like "cozy fantasy from 2010" — if the
  // user wanted a filtered ranking, let them stay in Objective Rankings.
  if (hasAnyVibeSignal && !objectiveShapeMatched) {
    return { matched: true, signals }
  }

  return { matched: false, signals }
}
