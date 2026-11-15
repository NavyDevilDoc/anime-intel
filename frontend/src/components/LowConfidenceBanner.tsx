/**
 * "I Don't Know" mode advisory banner.
 *
 * Renders when the backend signals low_confidence — either the top
 * result fell below the 75 threshold or the pool came back empty.
 * Shared by FindAShow and ShowsLikeThis; same copy in both surfaces
 * because the trust message is the same regardless of mode.
 *
 * Yellow tone (not red) — the engine isn't broken, it's being honest
 * about uncertainty. Red would read as an error; yellow reads as an
 * advisory.
 *
 * PR C — when the user has an active watchlist exclusion AND the pool
 * came back empty, the more useful next-step is "try a different angle
 * or temporarily disable exclusion" rather than the generic "refine
 * your query" copy. The watchlist-empty case is structurally different
 * from a low-confidence-but-populated pool — refining the query won't
 * help if the engine had matches but all of them were on the user's
 * watchlist.
 *
 * COA B / Option C — when the engine returns fallbackMode="composite"
 * (LLM reranker was blocked by Gemini policy and results came from the
 * pre-rerank composite pool), the banner copy shifts to explain WHY
 * confidence is low — the reranker's detailed per-show analysis wasn't
 * available, not that no matches exist. Precedence: fallback-mode copy
 * wins over watchlist-empty copy when both would apply (fallback is a
 * more specific product-behavior signal).
 */
interface Props {
  watchlistExclusionEmptied?: boolean
  fallbackMode?: 'composite' | null
  // Item 4a — true when the current empty pool came from a Staff/Studio
  // chip filter (deterministic SQL returned zero matches). Different
  // remediation than a low-confidence vibe query: the user should either
  // remove a chip or switch modes, not refine query text.
  staffStudioEmpty?: boolean
}

export default function LowConfidenceBanner({
  watchlistExclusionEmptied = false,
  fallbackMode = null,
  staffStudioEmpty = false,
}: Props) {
  let message: string
  let variant: string
  if (fallbackMode === 'composite') {
    message =
      "Detailed analysis wasn't available for this query — showing semantic matches only. Results are ranked by content similarity, not by the deeper per-show reasoning you'd normally see."
    variant = 'composite-fallback'
  } else if (staffStudioEmpty) {
    // Precedence: fallback and watchlist copy don't apply to
    // Staff/Studio (fallback is Gemini-blocked; watchlist doesn't gate
    // this mode's SQL path). Copy points at the two available actions:
    // narrow the chip set (remove one) or switch modes to search
    // thematically instead of by attribution.
    message =
      "No shows match all filters. Remove a chip, or switch to Intelligent Search for thematic queries."
    variant = 'staff-studio-empty'
  } else if (watchlistExclusionEmptied) {
    message =
      "All matches for this query are on your watchlist — try a different angle, or temporarily disable exclusion above."
    variant = 'watchlist-empty'
  } else {
    message = "No strong match — try refining your query."
    variant = 'default'
  }

  return (
    <div
      className="bg-yellow-950 border border-yellow-700 rounded px-4 py-3 text-yellow-200 text-sm"
      role="status"
      data-testid="low-confidence-banner"
      data-variant={variant}
    >
      {message}
    </div>
  )
}
