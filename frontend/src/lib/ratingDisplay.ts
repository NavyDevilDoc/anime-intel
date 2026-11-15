/**
 * Parser + palette for MAL content ratings.
 *
 * Phase A of the rating retrofit (2026-05-26) confirmed Jikan always
 * returns the verbatim format "<SHORT> - <DETAIL>" with a literal " - "
 * delimiter, and the SHORT prefix is a closed set of six values. So no
 * regex needed — split on the first " - " and look the prefix up.
 *
 * Empty string from the backend means "MAL has no rating set" (typical
 * of upcoming/unreleased shows). null means "not yet backfilled." Both
 * map to parseRating() returning null so the caller hides the pill.
 */

export type RatingBucket = 'G' | 'PG' | 'PG-13' | 'R' | 'R+' | 'Rx'

export interface ParsedRating {
  short: RatingBucket
  full: string
  bucket: RatingBucket
}

const KNOWN_BUCKETS: ReadonlySet<RatingBucket> = new Set([
  'G',
  'PG',
  'PG-13',
  'R',
  'R+',
  'Rx',
])

export function parseRating(raw: string | null | undefined): ParsedRating | null {
  if (!raw) return null
  const idx = raw.indexOf(' - ')
  if (idx === -1) return null
  const short = raw.slice(0, idx) as RatingBucket
  if (!KNOWN_BUCKETS.has(short)) return null
  return { short, full: raw, bucket: short }
}

/**
 * Tailwind class string for the rating pill background + text color.
 * Calibrated for the dark vault theme:
 *   - G  / PG    -> green tones ("safe")
 *   - PG-13      -> yellow ("warning")
 *   - R   / R+   -> orange / red ("caution")
 *   - Rx         -> dark red ("restricted")
 */
export function ratingPillClasses(bucket: RatingBucket): string {
  switch (bucket) {
    case 'G':
      return 'bg-green-700 text-white'
    case 'PG':
      return 'bg-green-500 text-white'
    case 'PG-13':
      return 'bg-yellow-600 text-white'
    case 'R':
      return 'bg-orange-600 text-white'
    case 'R+':
      return 'bg-red-700 text-white'
    case 'Rx':
      return 'bg-red-900 text-white'
  }
}
