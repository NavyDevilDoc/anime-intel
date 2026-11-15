import { useState } from 'react'
import type { ScoredResult } from '../lib/types'
import { formatVoteCount, formatVoteCountExact } from '../lib/voteCountDisplay'
import RatingPill from './RatingPill'
import SeriesContext from './SeriesContext'
import Tooltip from './Tooltip'

// Build-time rollback flag for the query-relevant con "Heads up:" section.
// Enabled by default; set VITE_QUERY_RELEVANT_CON_ENABLED=false in Vercel
// env vars to hide the section site-wide without redeploying the backend.
const QUERY_RELEVANT_CON_ENABLED =
  import.meta.env.VITE_QUERY_RELEVANT_CON_ENABLED !== 'false'

interface Props {
  result: ScoredResult
  showConfidence: boolean
  onShowClick?: (title: string) => void
  // Response-level fallback_mode threaded down from the parent. When
  // "composite", the card renders a subtle "Semantic match only" pill
  // next to the match confidence so users see per-card that the LLM
  // reranker's detailed analysis wasn't available for this query.
  // Response-level rather than per-result because the whole pool
  // shares the same mode.
  fallbackMode?: 'composite' | null
}

export default function ResultCard({
  result,
  showConfidence,
  onShowClick,
  fallbackMode = null,
}: Props) {
  const { profile } = result
  const [spoilerRevealed, setSpoilerRevealed] = useState(false)
  const isCompositeFallback = fallbackMode === 'composite'

  return (
    <div className="bg-vault-surface border border-vault-border rounded-lg p-5 space-y-3">
      <h3 className="text-lg font-bold text-vault-text flex flex-wrap items-center gap-2">
        <span>
          {onShowClick ? (
            <button
              onClick={() => onShowClick(profile.title)}
              className="bg-transparent border-none cursor-pointer text-vault-text hover:text-vault-accent transition-colors text-lg font-bold p-0"
            >
              {profile.title}
            </button>
          ) : (
            profile.title
          )}{' '}
          <span className="text-vault-text-muted font-normal">
            ({profile.release_year || 'Unknown'})
          </span>
        </span>
        <RatingPill rating={profile.mal_rating} />
      </h3>

      <div className="flex flex-wrap gap-x-6 gap-y-1 text-sm">
        <span><span className="text-vault-text-muted">Studio:</span> {profile.studio}</span>
        <Tooltip text="Bayesian-adjusted MAL score — accounts for vote count so popular shows with many ratings are weighted more accurately.">
          <span className="text-vault-text-muted">Quality:</span>{' '}
          <span className="text-yellow-400 font-bold">{profile.quality_score}</span>
        </Tooltip>
        {formatVoteCount(profile.scored_by) && (
          <span
            className="text-vault-text-muted"
            title={formatVoteCountExact(profile.scored_by) ?? undefined}
          >
            {formatVoteCount(profile.scored_by)}
          </span>
        )}
        {showConfidence ? (
          <Tooltip text="AI confidence that this show matches your search — based on semantic analysis and community data.">
            <span className="text-vault-text-muted">Match:</span>{' '}
            <span className="font-bold">{result.match_confidence}%</span>
          </Tooltip>
        ) : (
          <span className="text-vault-text-muted">Direct DB Hit</span>
        )}
        {isCompositeFallback && (
          <Tooltip text="Gemini's detailed reranker analysis wasn't available for this query — this show was ranked by content similarity alone. See the advisory banner above.">
            <span
              className="inline-flex items-center px-2 py-0.5 rounded-full text-xs bg-yellow-900/50 text-yellow-200 border border-yellow-700/50"
              data-testid="composite-fallback-pill"
            >
              Semantic match only
            </span>
          </Tooltip>
        )}
      </div>

      {result.contains_spoiler && !spoilerRevealed ? (
        <div className="bg-vault-bg/50 rounded px-3 py-2 text-sm">
          <span className="text-yellow-400 font-medium">Spoiler Warning: </span>
          <span className="text-vault-text-muted">This recommendation may reveal plot details. </span>
          <button
            onClick={() => setSpoilerRevealed(true)}
            className="text-vault-accent hover:underline bg-transparent border-none cursor-pointer p-0 text-sm"
          >
            Read potential spoilers
          </button>
        </div>
      ) : (
        <>
          <div className="bg-vault-bg/50 rounded px-3 py-2 text-sm">
            <span className="text-vault-accent font-medium">AI Reasoning: </span>
            {result.ai_reasoning}
          </div>

          {result.controversy_warning && (
            <div className="bg-red-950 border border-red-800 rounded px-3 py-2 text-sm text-red-200">
              <span className="font-medium">Friction Alert: </span>
              {result.controversy_warning}
            </div>
          )}

          {QUERY_RELEVANT_CON_ENABLED && result.query_relevant_con && (
            <div
              className="bg-vault-bg/50 rounded px-3 py-2 text-sm text-vault-text-muted"
              data-testid="query-relevant-con"
            >
              <span className="font-medium">Heads up: </span>
              {result.query_relevant_con}
            </div>
          )}

          <SeriesContext context={result.series_context} mode="full" />
        </>
      )}

      <div className="flex flex-wrap items-center justify-between gap-4 text-sm">
        <a
          href={`https://myanimelist.net/anime/${profile.id}`}
          target="_blank"
          rel="noopener noreferrer"
          className="text-vault-accent hover:underline"
        >
          View on MyAnimeList
        </a>
      </div>
    </div>
  )
}
