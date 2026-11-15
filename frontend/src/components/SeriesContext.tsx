import { useState } from 'react'
import type { SeriesContext as SeriesContextData } from '../lib/types'

// Build-time rollback flag. Set VITE_SERIES_CONTEXT_ENABLED=false in
// Vercel env to hide the component site-wide without redeploying the
// backend. Default true on launch.
const SERIES_CONTEXT_ENABLED =
  import.meta.env.VITE_SERIES_CONTEXT_ENABLED !== 'false'

interface Props {
  context: SeriesContextData | null | undefined
  // 'full' renders the position pill, trajectory caption, range, and
  // dropped-siblings drawer (search / DNA result cards).
  // 'minimal' renders only the position pill — used by Encyclopedia,
  // which is a lookup surface, not a recommendation surface.
  mode?: 'full' | 'minimal'
}

const TRAJECTORY_COPY: Record<NonNullable<SeriesContextData['trajectory']>, string> = {
  stable: 'Quality stays consistent across the series.',
  decline: 'Reception declines toward later entries.',
  improve: 'Reception improves across the series.',
  mixed: 'Reception varies across entries.',
}

/**
 * Renders cluster-level series metadata for a recommendation or
 * encyclopedia result. See Initiative 1 in IMPLEMENTATION_PLAN.md for
 * the data lineage; this component owns presentation only.
 *
 * Returns null when:
 *   - VITE_SERIES_CONTEXT_ENABLED is "false" (env rollback)
 *   - context is null/undefined (engine bypassed hydration)
 *   - the cluster is a singleton (series_size === 1 with no useful info)
 */
export default function SeriesContext({ context, mode = 'full' }: Props) {
  const [expanded, setExpanded] = useState(false)

  if (!SERIES_CONTEXT_ENABLED) return null
  if (!context) return null
  if (context.series_size <= 1) return null

  const droppedCount = (context.dropped_siblings ?? []).length
  const showFullDetails = mode === 'full'
  const trajectoryCopy = context.trajectory ? TRAJECTORY_COPY[context.trajectory] : null
  const hasScoreRange =
    context.score_min !== null &&
    context.score_max !== null &&
    context.score_min !== context.score_max

  // PR B (subtype bundle) — when this entry is itself a TV-subtype
  // and the cluster has more than one TV entry, surface "Season X of
  // Y" against the TV-only count, and still expose the full franchise
  // size as the trailing context. Falls back to the legacy "Entry X
  // of N in the franchise" when the entry is a movie/OVA, when the
  // cluster has only one TV entry, or when the backend didn't supply
  // the tv_* fields (pre-PR-B response shape).
  const tvPosition = context.tv_position ?? null
  const tvCount = context.tv_count ?? null
  const useTVLabel = tvPosition !== null && tvCount !== null && tvCount > 1
  const scoreFiltered = context.score_range_filtered === true

  return (
    <div
      className="text-xs text-vault-text-muted"
      data-testid="series-context"
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span
          className="inline-flex items-center gap-1.5"
          data-testid="series-position-pill"
        >
          {useTVLabel ? (
            <>
              <span className="font-medium text-vault-text">
                Season {tvPosition} of {tvCount}
              </span>
              <span>in a {context.series_size}-entry franchise</span>
            </>
          ) : (
            <>
              <span className="font-medium text-vault-text">
                Entry {context.position} of {context.series_size}
              </span>
              <span>in the franchise</span>
            </>
          )}
          <span
            className="cursor-help select-none text-vault-text-muted"
            title="Franchise count includes TV seasons, films, OVAs, and specials — not just numbered seasons."
            aria-label="Franchise count includes TV seasons, films, OVAs, and specials — not just numbered seasons."
            data-testid="series-position-info"
          >
            &#9432;
          </span>
        </span>

        {showFullDetails && hasScoreRange && (
          <span
            data-testid="series-score-range"
            title={scoreFiltered ? 'TV and theatrical entries only' : undefined}
            aria-label={scoreFiltered ? 'Score range covers TV and theatrical entries only' : undefined}
          >
            Scores: {context.score_min!.toFixed(1)} — {context.score_max!.toFixed(1)}
            {scoreFiltered && (
              <span
                className="ml-1 text-vault-text-muted"
                data-testid="series-score-range-filtered-caption"
              >
                (TV and theatrical)
              </span>
            )}
          </span>
        )}

        {showFullDetails && droppedCount > 0 && (
          <button
            type="button"
            onClick={() => setExpanded((v) => !v)}
            className="bg-transparent border-none cursor-pointer text-vault-accent hover:underline text-xs p-0"
            data-testid="series-dropped-toggle"
            aria-expanded={expanded}
          >
            {expanded ? '− hide' : `+ ${droppedCount} other ${droppedCount === 1 ? 'entry' : 'entries'} scored well`}
          </button>
        )}
      </div>

      {showFullDetails && trajectoryCopy && (
        <div className="mt-0.5" data-testid="series-trajectory">
          {trajectoryCopy}
        </div>
      )}

      {showFullDetails && expanded && droppedCount > 0 && (
        <ul
          className="mt-2 space-y-1 border-l-2 border-vault-border pl-3"
          data-testid="series-dropped-list"
        >
          {context.dropped_siblings!.map((sib) => (
            <li key={sib.id} className="flex flex-wrap items-baseline gap-x-2">
              <a
                href={`https://myanimelist.net/anime/${sib.id}`}
                target="_blank"
                rel="noopener noreferrer"
                className="text-vault-text hover:text-vault-accent transition-colors"
              >
                {sib.title}
              </a>
              <span className="text-vault-text-muted">
                {sib.match_confidence}% match
              </span>
              {sib.query_relevant_con && (
                <span className="basis-full text-vault-text-muted italic">
                  {sib.query_relevant_con}
                </span>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
