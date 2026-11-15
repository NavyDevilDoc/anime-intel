/**
 * Format an anime's MAL vote count for compact display on result cards
 * and Encyclopedia profiles.
 *
 * Rationale (2026-09-14): raw MAL scores are much more trustworthy when
 * backed by thousands of ratings than when backed by dozens — the
 * Bayesian shrinkage in `_composite_rerank` already handles this in
 * ranking, but until now the user had no visible signal for it. Showing
 * the vote count alongside the score lets users self-assess "should I
 * trust this score" without any advisory language.
 *
 * Format returned by `formatVoteCount`:
 *   < 1000        -> "384 ratings"           (rare edge case; still shown)
 *   1000-9999     -> "1.2k ratings"
 *   10000-999999  -> "296k ratings"
 *   >= 1M         -> "3.0M ratings"
 *   0 or missing  -> null (caller hides the element)
 *
 * The exact number is expected to be surfaced via a tooltip / title
 * attribute on the display element; the caller decides how.
 */

export function formatVoteCount(count: number | null | undefined): string | null {
  if (count == null || count <= 0) return null

  if (count < 1000) {
    return `${count} ratings`
  }
  if (count < 10_000) {
    // 1.2k, 9.8k — one decimal place for the low end where each 100 matters
    const k = Math.round(count / 100) / 10
    return `${k}k ratings`
  }
  if (count < 1_000_000) {
    // 10k, 296k — whole numbers for the mid range
    const k = Math.round(count / 1000)
    return `${k}k ratings`
  }
  // 1.2M, 3.0M — one decimal for the top end
  const m = Math.round(count / 100_000) / 10
  return `${m}M ratings`
}

/**
 * Exact-count string for use in tooltip / aria-label, e.g. "3,035,579 ratings".
 * Uses locale-aware thousands separators via toLocaleString.
 */
export function formatVoteCountExact(count: number | null | undefined): string | null {
  if (count == null || count <= 0) return null
  return `${count.toLocaleString()} ratings`
}
