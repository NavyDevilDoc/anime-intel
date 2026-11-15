import { useState, useEffect, useRef } from 'react'
import { api } from '../lib/api'
import { lookupOffline } from '../lib/offlineEncyclopedia'
import type { EncyclopediaResponse, ShowProfile, SeriesContext as SeriesContextData } from '../lib/types'
import { formatVoteCount, formatVoteCountExact } from '../lib/voteCountDisplay'
import RatingPill from './RatingPill'
import SeriesContext from './SeriesContext'
import SimilarShows from './SimilarShows'
import Tooltip from './Tooltip'
import Spinner from './Spinner'
import ExploreVibeButton from './ExploreVibeButton'

function slugify(id: number, title: string): string {
  const slug = title
    .toLowerCase()
    .replace(/[^a-z0-9\s-]/g, '')
    .trim()
    .replace(/[\s]+/g, '-')
    .replace(/-+/g, '-')
    .replace(/^-|-$/g, '')
  return slug ? `${id}-${slug}` : `${id}`
}

interface Props {
  initialTitle?: string
  onTitleConsumed?: () => void
  onBack?: () => void
  backLabel?: string
}

export default function Encyclopedia({ initialTitle, onTitleConsumed, onBack, backLabel }: Props) {
  const [query, setQuery] = useState('')
  const [matches, setMatches] = useState<string[]>([])
  const [profile, setProfile] = useState<ShowProfile | null>(null)
  const [seriesContext, setSeriesContext] = useState<SeriesContextData | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const lastInitialTitle = useRef('')

  useEffect(() => {
    if (initialTitle && initialTitle !== lastInitialTitle.current) {
      lastInitialTitle.current = initialTitle
      setQuery(initialTitle)
      lookup(initialTitle)
      onTitleConsumed?.()
    }
  }, [initialTitle, onTitleConsumed])

  const lookup = async (title: string, opts: { fromDisambig?: boolean } = {}) => {
    setLoading(true)
    setError('')
    setProfile(null)
    setSeriesContext(null)
    setMatches([])
    try {
      // Hiatus mode swaps the backend call for a client-side lookup
      // against the same seo_profiles.json that build-time SEO pages
      // consume. Response shape mirrors EncyclopediaResponse so the
      // state-setting code below doesn't branch. fromDisambig is
      // threaded into the offline path as skipFranchiseDisambig — the
      // backend doesn't have (and doesn't need) the franchise-root
      // heuristic, so the online path ignores the flag.
      const hiatusMode = import.meta.env.VITE_ENGINE_HIATUS_MODE === 'true'
      const res: EncyclopediaResponse = hiatusMode
        ? await lookupOffline(title, { skipFranchiseDisambig: opts.fromDisambig ?? false })
        : await api.get<EncyclopediaResponse>(`/api/show/${encodeURIComponent(title)}`)
      if (!res.success) {
        setError(res.error ?? 'Lookup failed')
      } else if (res.profile) {
        setProfile(res.profile)
        setSeriesContext(res.series_context ?? null)
        setMatches([])
      } else if (res.matches.length > 1) {
        setMatches(res.matches)
      } else {
        setError(`"${title}" not found in the database.`)
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Lookup failed')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="space-y-5">
      {onBack && (
        <button
          onClick={onBack}
          className="text-sm text-vault-accent hover:underline bg-transparent border-none cursor-pointer p-0"
        >
          &larr; Back to {backLabel}
        </button>
      )}

      <div>
        <h2 className="text-xl font-bold mb-1">Anime Encyclopedia</h2>
        <p className="text-sm text-vault-text-muted">
          Access the complete encyclopedia entry for a specific show. Resists typos and partial names.
        </p>
      </div>

      <div className="flex flex-col sm:flex-row gap-3">
        <div className="relative flex-1">
          <input
            type="text"
            value={query}
            onChange={e => setQuery(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter' && query.trim()) lookup(query.trim()) }}
            placeholder="e.g., 'Evangelion' or 'Code Geass'"
            maxLength={100}
            className="w-full px-3 py-2 pr-9 rounded bg-vault-surface border border-vault-border text-vault-text focus:outline-none focus:border-vault-accent"
          />
          {query && (
            <button
              type="button"
              onClick={() => setQuery('')}
              aria-label="Clear search"
              className="absolute right-2 top-1/2 -translate-y-1/2 px-1.5 text-vault-text-muted hover:text-vault-text bg-transparent border-none cursor-pointer text-lg leading-none"
            >
              &times;
            </button>
          )}
        </div>
        <button
          onClick={() => query.trim() && lookup(query.trim())}
          disabled={loading || !query.trim()}
          className="px-5 py-2 rounded bg-vault-accent text-white font-medium hover:bg-vault-accent-hover disabled:opacity-50 transition-colors border-none cursor-pointer"
        >
          {loading ? (
            <span className="inline-flex items-center justify-center gap-2">
              <Spinner /> Looking up...
            </span>
          ) : (
            'Look Up'
          )}
        </button>
      </div>

      {error && (
        <div className="bg-yellow-950 border border-yellow-700 rounded px-4 py-3 text-yellow-200">
          {error}
        </div>
      )}

      {matches.length > 1 && (
        <div className="space-y-2">
          <p className="text-sm text-vault-accent">Multiple shows detected. Please clarify:</p>
          {matches.map(m => (
            <button
              key={m}
              onClick={() => { setQuery(m); lookup(m, { fromDisambig: true }) }}
              className="block w-full text-left px-4 py-2 rounded bg-vault-surface border border-vault-border text-vault-text hover:bg-vault-border transition-colors cursor-pointer"
            >
              {m}
            </button>
          ))}
        </div>
      )}

      {profile && (
        <div className="bg-vault-surface border border-vault-border rounded-lg p-6 space-y-5">
          <div className="text-center mb-2">
            <h3 className="text-2xl font-bold flex flex-wrap items-center justify-center gap-2">
              <span>
                {profile.title}{' '}
                <span className="text-vault-text-muted font-normal">
                  ({profile.release_year || 'Unknown'})
                </span>
              </span>
              <RatingPill rating={profile.mal_rating} />
            </h3>
            {seriesContext && (
              <div className="mt-1 flex justify-center">
                <SeriesContext context={seriesContext} mode="minimal" />
              </div>
            )}
            <div className="mt-1 text-sm text-vault-accent">
              <a
                href={`https://myanimelist.net/anime/${profile.id}`}
                target="_blank"
                rel="noopener noreferrer"
                className="hover:underline"
              >
                View on MyAnimeList
              </a>
              <span className="text-vault-text-muted mx-2">·</span>
              <a
                href={`/anime/${slugify(profile.id, profile.title)}`}
                target="_blank"
                rel="noopener noreferrer"
                className="hover:underline"
              >
                View Full Profile
              </a>
            </div>
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
            <div className="text-center">
              <div className="text-2xl font-bold text-yellow-400">{profile.quality_score}</div>
              <Tooltip text="Bayesian-adjusted MAL score — accounts for vote count so popular shows with many ratings are weighted more accurately.">
                <span className="text-xs text-vault-text-muted">MAL Score</span>
              </Tooltip>
              {formatVoteCount(profile.scored_by) && (
                <div
                  className="text-xs text-vault-text-muted mt-0.5"
                  title={formatVoteCountExact(profile.scored_by) ?? undefined}
                >
                  {formatVoteCount(profile.scored_by)}
                </div>
              )}
            </div>
            <div className="text-center">
              <div className={`text-2xl font-bold ${
                profile.audience_sentiment > 0.05
                  ? 'text-vault-success'
                  : profile.audience_sentiment < -0.05
                  ? 'text-vault-danger'
                  : 'text-vault-text-muted'
              }`}>
                {`${profile.audience_sentiment > 0.05 ? '+' : profile.audience_sentiment < -0.05 ? '-' : ''}${Math.abs(Math.round(profile.audience_sentiment * 100))}%`}
              </div>
              <Tooltip text="% community sentiment from viewer reviews — green is positive, red is negative.">
                <span className="text-xs text-vault-text-muted">Audience Mood</span>
              </Tooltip>
            </div>
            <div className="text-center">
              <div className="text-2xl font-bold">{profile.controversy_score}/10</div>
              <Tooltip text="How divisive this show is among viewers — high scores mean strong love/hate reactions.">
                <span className="text-xs text-vault-text-muted">Controversy</span>
              </Tooltip>
            </div>
          </div>

          <p className="text-sm">
            <span className="text-vault-text-muted">Studio:</span> {profile.studio}
          </p>

          <div className="bg-vault-bg/50 rounded px-4 py-3 text-sm">
            <span className="text-vault-accent font-medium">Audience Consensus: </span>
            {profile.audience_consensus}
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div className="space-y-1">
              <h4 className="text-vault-success font-medium text-sm">The Pros</h4>
              {profile.pros.map((p, i) => (
                <p key={i} className="text-sm text-vault-text-muted">• {p}</p>
              ))}
            </div>
            <div className="space-y-1">
              <h4 className="text-vault-danger font-medium text-sm">The Cons</h4>
              {profile.cons.map((c, i) => (
                <p key={i} className="text-sm text-vault-text-muted">• {c}</p>
              ))}
            </div>
          </div>

          {profile.thematic_vibe && (
            <div className="flex justify-center">
              <ExploreVibeButton vibe={profile.thematic_vibe} />
            </div>
          )}

          <SimilarShows
            key={profile.id}
            animeId={profile.id}
            onSelectShow={(title) => {
              setQuery(title)
              lookup(title)
            }}
          />
        </div>
      )}
    </div>
  )
}
