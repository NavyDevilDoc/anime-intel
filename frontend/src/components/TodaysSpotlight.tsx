import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import PosterImage from './PosterImage'

const API_BASE = import.meta.env.VITE_API_BASE_URL as string ?? ''

/**
 * TodaysSpotlight — Landing Redesign PR #2.
 *
 * Fetches GET /api/spotlight and renders the algorithmic daily pick as
 * a card below the marketing hero. The backend rotates category by
 * weekday (Mon Hidden Gem -> Sun Vibe of the Week) and returns a show
 * plus optional supplementary shows for the multi-entry categories
 * (franchise / behind-the-scenes / vibe).
 *
 * Feature flag: `VITE_DAILY_SPOTLIGHT_ENABLED`. Off => renders null so
 * the surface can be pulled instantly from Vercel without redeploy.
 *
 * Failure posture: silent. If the fetch fails (backend down, 5xx,
 * network) the component renders null rather than a broken card — this
 * is a decorative surface, not a critical path.
 */

// -------------------------------------------------------------------
// Types — mirror the backend SpotlightResponse shape from
// api/routes/spotlight.py. Exported so tests can build fixtures.
// -------------------------------------------------------------------

export interface SpotlightShow {
  id: number
  title: string
  release_year: number
  mal_score: number
  thematic_vibe: string
  consensus_summary: string
  pros: string[]
  controversy_score: number
  poster_url_large: string | null
  poster_url_medium: string | null
}

export interface SpotlightResponse {
  category: string
  category_label: string
  date: string
  primary_show: SpotlightShow
  supplementary_shows: SpotlightShow[]
  supplementary_context: Record<string, unknown> | null
}

// -------------------------------------------------------------------
// Sub-components
// -------------------------------------------------------------------

function ControversyBar({ score }: { score: number }) {
  // Score is 0-10 (integer). Render a 10-cell horizontal bar with
  // score-many filled. Color scales up: green (low) -> yellow (mid) ->
  // orange (high). Purely decorative — the score itself renders next
  // to it for accessibility.
  const clamped = Math.max(0, Math.min(10, Math.round(score)))
  const barColor =
    clamped <= 3 ? 'bg-green-500' :
    clamped <= 6 ? 'bg-yellow-500' :
    'bg-orange-500'
  return (
    <div className="flex items-center gap-2 text-xs text-vault-text-muted">
      <span>Controversy</span>
      <div className="flex gap-0.5" aria-label={`Controversy score ${clamped} of 10`}>
        {Array.from({ length: 10 }, (_, i) => (
          <div
            key={i}
            className={`h-2 w-2 rounded-sm ${i < clamped ? barColor : 'bg-vault-border'}`}
          />
        ))}
      </div>
      <span className="font-mono">{clamped}/10</span>
    </div>
  )
}

function SupplementaryContextCaption({ ctx }: { ctx: Record<string, unknown> | null }) {
  if (!ctx) return null
  // Category-specific caption. Kept as small render logic in one place
  // so adding a new category means one edit here + one in the backend.
  if (typeof ctx.person_name === 'string' && typeof ctx.role === 'string') {
    return (
      <p className="text-xs text-vault-text-muted">
        Featuring the work of{' '}
        <span className="text-vault-text">{ctx.person_name}</span>
        {' '}({ctx.role})
      </p>
    )
  }
  if (typeof ctx.vibe === 'string') {
    return (
      <p className="text-xs text-vault-text-muted">
        Vibe: <span className="text-vault-text">{ctx.vibe}</span>
      </p>
    )
  }
  if (typeof ctx.series_size === 'number') {
    return (
      <p className="text-xs text-vault-text-muted">
        Part of a{' '}
        <span className="text-vault-text">{ctx.series_size}-entry franchise</span>
      </p>
    )
  }
  return null
}

// -------------------------------------------------------------------
// Main component
// -------------------------------------------------------------------

export default function TodaysSpotlight() {
  const enabled = import.meta.env.VITE_DAILY_SPOTLIGHT_ENABLED === 'true'
  const navigate = useNavigate()
  const [data, setData] = useState<SpotlightResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    if (!enabled) {
      setLoading(false)
      return
    }
    let cancelled = false
    fetch(`${API_BASE}/api/spotlight`)
      .then(res => {
        if (!res.ok) throw new Error(`spotlight fetch failed: ${res.status}`)
        return res.json()
      })
      .then((body: SpotlightResponse) => {
        if (!cancelled) {
          setData(body)
          setLoading(false)
        }
      })
      .catch(() => {
        if (!cancelled) {
          setFailed(true)
          setLoading(false)
        }
      })
    return () => { cancelled = true }
  }, [enabled])

  if (!enabled) return null
  if (failed) return null

  if (loading) {
    return (
      <div
        data-testid="spotlight-loading"
        className="mx-auto mt-8 max-w-3xl animate-pulse rounded border border-vault-border bg-vault-surface p-6"
      >
        <div className="h-4 w-32 rounded bg-vault-border mb-4" />
        <div className="flex gap-4">
          <div className="h-64 w-44 rounded bg-vault-border" />
          <div className="flex-1 space-y-2">
            <div className="h-6 w-3/4 rounded bg-vault-border" />
            <div className="h-4 w-1/2 rounded bg-vault-border" />
            <div className="h-16 w-full rounded bg-vault-border" />
          </div>
        </div>
      </div>
    )
  }

  if (!data) return null

  const { category_label, primary_show, supplementary_shows, supplementary_context } = data

  const handleExploreVibe = () => {
    // Preload the Find-a-Show query with the spotlight's thematic vibe
    // (e.g., "Neo-Noir, Space Western") — the show itself is one example
    // of that vibe, so seeding the search with the vibe surfaces more
    // shows matching the same tone. Fall back to the title if vibe is
    // empty (rare — consensus_json is required by the pool builders).
    const seed = primary_show.thematic_vibe || primary_show.title
    navigate(`/try?q=${encodeURIComponent(seed)}`)
  }

  return (
    <section
      data-testid="spotlight"
      className="mx-auto mt-8 max-w-3xl rounded border border-vault-border bg-vault-surface p-6"
    >
      <div className="mb-4 flex items-center justify-between">
        <span
          data-testid="spotlight-category-label"
          className="rounded-full bg-vault-accent/10 px-3 py-1 text-xs font-medium uppercase tracking-wide text-vault-accent"
        >
          {category_label}
        </span>
        <span className="text-xs text-vault-text-muted">{data.date}</span>
      </div>

      <div className="flex flex-col gap-6 sm:flex-row">
        <div className="flex-shrink-0">
          <PosterImage
            src={primary_show.poster_url_large}
            alt={primary_show.title}
            size="large"
            malId={primary_show.id}
          />
        </div>

        <div className="flex-1 space-y-3">
          <div>
            <h2 className="text-2xl font-bold text-vault-text">{primary_show.title}</h2>
            <p className="text-sm text-vault-text-muted">
              {primary_show.release_year > 0 && <>{primary_show.release_year} · </>}
              MAL score {primary_show.mal_score.toFixed(2)}
            </p>
          </div>

          {primary_show.consensus_summary && (
            <blockquote className="border-l-2 border-vault-accent pl-3 text-sm italic text-vault-text">
              "{primary_show.consensus_summary}"
            </blockquote>
          )}

          {primary_show.pros.length > 0 && (
            <div className="flex flex-wrap gap-2">
              {primary_show.pros.slice(0, 3).map(pro => (
                <span
                  key={pro}
                  className="rounded-full bg-vault-border/30 px-2.5 py-1 text-xs text-vault-text"
                >
                  {pro}
                </span>
              ))}
            </div>
          )}

          <ControversyBar score={primary_show.controversy_score} />

          <SupplementaryContextCaption ctx={supplementary_context} />

          <button
            type="button"
            onClick={handleExploreVibe}
            data-testid="spotlight-cta"
            className="mt-2 inline-flex items-center gap-1 rounded bg-vault-accent px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-vault-accent-hover cursor-pointer border-none"
          >
            Explore this vibe →
          </button>
        </div>
      </div>

      {supplementary_shows.length > 0 && (
        <div
          data-testid="spotlight-supplementary"
          className="mt-6 border-t border-vault-border pt-4"
        >
          <div className="flex flex-wrap gap-4">
            {supplementary_shows.map(show => (
              <div key={show.id} className="w-24 text-center">
                <PosterImage
                  src={show.poster_url_medium}
                  alt={show.title}
                  size="medium"
                  malId={show.id}
                  className="mx-auto max-w-[96px]"
                />
                <p className="mt-1 truncate text-xs text-vault-text-muted" title={show.title}>
                  {show.title}
                </p>
              </div>
            ))}
          </div>
        </div>
      )}
    </section>
  )
}
