import { parseRating, ratingPillClasses } from '../lib/ratingDisplay'

interface Props {
  rating: string | null | undefined
}

/**
 * Color-coded MAL content rating pill. Displays short form ("PG-13"),
 * exposes the verbose form via native HTML title attribute on hover and
 * aria-label for screen readers.
 *
 * Returns null when:
 *   - rating is null / empty (DB hasn't been backfilled, or MAL has no
 *     rating set for the show)
 *   - VITE_MAL_RATING_PILL_ENABLED is set to "false" in the Vercel env
 *     (rollback flag; pill disappears site-wide without a code redeploy)
 *   - rating doesn't parse to a known bucket (defensive — should never
 *     happen given Phase A's locked format contract)
 */
export default function RatingPill({ rating }: Props) {
  if (import.meta.env.VITE_MAL_RATING_PILL_ENABLED === 'false') return null

  const parsed = parseRating(rating)
  if (!parsed) return null

  return (
    <span
      title={parsed.full}
      aria-label={parsed.full}
      className={`inline-block px-2 py-0.5 text-xs font-bold rounded ${ratingPillClasses(parsed.bucket)}`}
    >
      {parsed.short}
    </span>
  )
}
