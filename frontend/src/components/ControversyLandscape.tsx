import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import * as Plot from '@observablehq/plot'

/**
 * ControversyLandscape — Landing Redesign PR #4 signature viz.
 *
 * 2D scatter of every vaulted show, plotted as:
 *   X = Bayesian smoothed quality score (1-10)
 *   Y = controversy_score from consensus_json (0-10, jittered)
 *   fill = log scored_by (brighter = more popular)
 *
 * Phase 4 interactive layer: hover tooltip, click-through to the
 * Encyclopedia for the show you clicked, and three filter dropdowns
 * (genre, decade, subtype). Filters DIM non-matching dots to opacity
 * 0.1 rather than removing them — preserves the "look at the whole
 * vault" context the viz is built around.
 *
 * Y-axis jitter: controversy_score is a 0-10 integer, so without
 * jitter the dots stack into visible horizontal bands. Jitter is
 * seeded by mal_id (deterministic hash) so dots don't shuffle on
 * filter changes — the same show lands at the same position
 * forever, which is important for stable hover + click targeting.
 *
 * Data source: GET /api/controversy-landscape-data (cached 24h at the
 * edge). ~6,413 shows, ~237 KB gzipped.
 *
 * Rendering: Observable Plot with SVG default. SVG-at-6K-points proved
 * smooth on first-paint eyeball; if interactivity (hover/filter re-
 * renders) tanks perf in Phase 6, fallback is to switch to raster/
 * canvas via Plot.raster or roll a custom canvas renderer.
 *
 * Feature flag: VITE_CONTROVERSY_LANDSCAPE_ENABLED. Off => returns null.
 * Lazy-loaded by the parent (Search.tsx) via React.lazy — the Plot
 * bundle (~100 KB gzipped with transitive D3 deps) only downloads when
 * the user scrolls to the viz.
 */

const API_BASE = import.meta.env.VITE_API_BASE_URL as string ?? ''

// ---------------------------------------------------------------------
// Types — mirror the backend LandscapeResponse shape from
// api/routes/controversy_landscape.py.
// ---------------------------------------------------------------------

export interface LandscapeShow {
  mal_id: number
  title: string
  quality: number
  controversy_score: number
  scored_by: number
  avg_sentiment: number
  genres: string[]
  release_year: number
  subtype: string
}

export interface LandscapeResponse {
  shows: LandscapeShow[]
  generated_at: string
}

// Internal row type — adds precomputed deterministic jitter.
interface PlottedShow extends LandscapeShow {
  _jittered_y: number
}

// ---------------------------------------------------------------------
// Jitter + filter helpers (exported so tests can exercise them directly)
// ---------------------------------------------------------------------

/**
 * Deterministic jitter seeded by mal_id. Returns a value in [-0.3, 0.3]
 * — chosen so dots within the same integer band spread visibly without
 * crossing into adjacent bands. Linear congruential generator with
 * typical constants; cheap, repeatable, visually random enough.
 */
export function jitterFor(malId: number): number {
  const seed = (malId * 9301 + 49297) % 233280
  return (seed / 233280 - 0.5) * 0.6
}

export interface Filters {
  genre: string         // '' = no filter
  decade: string        // '' = no filter; otherwise '1990' / '2000' / '2010' / '2020'
  subtype: string       // '' = no filter
}

export function matchesFilters(show: LandscapeShow, filters: Filters): boolean {
  if (filters.genre && !show.genres.includes(filters.genre)) return false
  if (filters.decade) {
    const start = parseInt(filters.decade, 10)
    if (!Number.isFinite(start)) return false
    if (show.release_year < start || show.release_year >= start + 10) return false
  }
  if (filters.subtype && show.subtype !== filters.subtype) return false
  return true
}

const DECADES: readonly string[] = ['1990', '2000', '2010', '2020']

// ---------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------

export default function ControversyLandscape() {
  const enabled = import.meta.env.VITE_CONTROVERSY_LANDSCAPE_ENABLED === 'true'
  const navigate = useNavigate()
  const containerRef = useRef<HTMLDivElement>(null)
  const [data, setData] = useState<LandscapeShow[] | null>(null)
  const [failed, setFailed] = useState(false)
  const [filters, setFilters] = useState<Filters>({ genre: '', decade: '', subtype: '' })

  // Fetch landscape data on mount
  useEffect(() => {
    if (!enabled) return
    let cancelled = false
    fetch(`${API_BASE}/api/controversy-landscape-data`)
      .then(res => {
        if (!res.ok) throw new Error(`landscape fetch failed: ${res.status}`)
        return res.json()
      })
      .then((body: LandscapeResponse) => {
        if (!cancelled) setData(body.shows)
      })
      .catch(() => {
        if (!cancelled) setFailed(true)
      })
    return () => { cancelled = true }
  }, [enabled])

  // Precompute jittered rows once per data load. Memoized so re-renders
  // on filter changes don't re-jitter (would shuffle dots mid-interaction).
  const plottedData = useMemo<PlottedShow[]>(() => {
    if (!data) return []
    return data.map(d => ({ ...d, _jittered_y: d.controversy_score + jitterFor(d.mal_id) }))
  }, [data])

  // Unique filter options derived from the loaded data.
  const uniqueGenres = useMemo<string[]>(() => {
    const set = new Set<string>()
    plottedData.forEach(d => d.genres.forEach(g => set.add(g)))
    return Array.from(set).sort()
  }, [plottedData])

  const uniqueSubtypes = useMemo<string[]>(() => {
    const set = new Set<string>()
    plottedData.forEach(d => { if (d.subtype) set.add(d.subtype) })
    return Array.from(set).sort()
  }, [plottedData])

  const hasActiveFilter = filters.genre || filters.decade || filters.subtype

  // Partition data into dim (non-matching) and bright (matching) sets
  // once per filter change. Memoized so the Plot render effect only
  // re-runs when the partition actually changes.
  const { dimData, brightData } = useMemo(() => {
    const dim: PlottedShow[] = []
    const bright: PlottedShow[] = []
    plottedData.forEach(d => {
      if (matchesFilters(d, filters)) bright.push(d)
      else dim.push(d)
    })
    return { dimData: dim, brightData: bright }
  }, [plottedData, filters])

  // Render Plot after data arrives and on filter change. Cleanup removes
  // the appended chart element so re-renders don't stack. Click handler
  // attached to the chart element reads __data__ bound by Plot/d3.
  //
  // Try/catch isn't paranoia: Plot can fail at runtime in environments
  // without canvas (jsdom tests, some older browsers) because the color
  // legend uses canvas for the gradient ramp. We'd rather render the
  // section headings without the chart than crash the whole component
  // tree.
  useEffect(() => {
    if (!data || !containerRef.current || plottedData.length === 0) return

    const container = containerRef.current
    const width = container.clientWidth || 800

    let chart: (Element & { remove: () => void }) | null = null
    try {
      chart = Plot.plot({
        width,
        height: Math.min(500, Math.round(width * 0.6)),
        marginLeft: 50,
        marginBottom: 50,
        style: {
          background: 'transparent',
          color: 'currentColor',
          fontSize: '12px',
        },
        x: {
          label: 'Quality (Bayesian smoothed) →',
          domain: [1, 10],
          grid: true,
        },
        y: {
          label: '↑ Controversy',
          domain: [-0.5, 10.5],  // Padding past integer range to accommodate jitter
          grid: true,
          ticks: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
        },
        color: {
          type: 'log',
          scheme: 'blues',
          label: 'Popularity (scored_by)',
          legend: true,
        },
        marks: [
          // Dim mark — non-matching dots as decorative background. No
          // tip, no click target. Plot's pointer interactor (used by
          // `tip: true`) finds the nearest datum by cursor position
          // regardless of CSS pointer-events, so splitting into two
          // separate marks is the only way to keep dim dots out of the
          // hover + click surface. Empty when no filter is active.
          Plot.dot(dimData, {
            x: 'quality',
            y: '_jittered_y',
            fill: 'scored_by',
            r: 2.5,
            fillOpacity: 0.08,
            stroke: 'none',
            pointerEvents: 'none',
          }),
          // Bright mark — matching dots with full interactivity.
          Plot.dot(brightData, {
            x: 'quality',
            y: '_jittered_y',
            fill: 'scored_by',
            r: 2.5,
            fillOpacity: 0.6,
            stroke: 'none',
            // Channel declaration order drives tooltip row order —
            // "Show" first, metadata following.
            channels: {
              Show: { value: 'title', label: 'Show' },
              Quality: { value: (d: PlottedShow) => d.quality.toFixed(2), label: 'Quality' },
              Controversy: { value: 'controversy_score', label: 'Controversy' },
              Popularity: { value: 'scored_by', label: 'Popularity (votes)' },
              // Year rendered as a plain string so Plot doesn't apply its
              // default number format (which inserted a thousands
              // separator and turned 2018 into "2,018").
              Year: { value: (d: PlottedShow) => String(d.release_year), label: 'Year' },
            },
            tip: {
              // Dark fill matches the vault surface theme; without this
              // the tooltip renders light-text-on-white because the
              // chart's color is currentColor (light on a dark page).
              fill: '#1a1f2e',
              stroke: '#2a3142',
              // Hide auto channels that duplicate declared ones or leak
              // internal state. x = Quality, y = jittered noise, fill =
              // Popularity. fillOpacity would otherwise expose the
              // "match / no-match" signal — reads as noise to users.
              format: {
                x: null,
                y: null,
                fill: null,
                fillOpacity: null,
              },
            },
          }),
        ],
      }) as Element & { remove: () => void }
      container.append(chart)

      // Click handler via event delegation. Plot/d3 binds the datum to
      // the SVG element's __data__ property; we read it back on click
      // and navigate to Encyclopedia for the clicked show.
      const onClick = (e: Event) => {
        const target = e.target as (SVGElement & { __data__?: PlottedShow }) | null
        const datum = target?.__data__
        if (datum && typeof datum === 'object' && 'mal_id' in datum && 'title' in datum) {
          navigate(`/try?mode=encyclopedia&q=${encodeURIComponent(datum.title)}`)
        }
      }
      chart.addEventListener('click', onClick)
      // Return cleanup includes the listener removal
      return () => {
        chart?.removeEventListener('click', onClick)
        chart?.remove()
      }
    } catch (e) {
      // Degraded render path — section headings still render, but no plot.
      console.warn('ControversyLandscape: Plot render failed, degrading to headings-only.', e)
    }
    return () => {
      chart?.remove()
    }
  }, [dimData, brightData, data, navigate])

  const clearFilters = () => setFilters({ genre: '', decade: '', subtype: '' })

  if (!enabled || failed) return null

  return (
    <section
      data-testid="controversy-landscape"
      className="mx-auto mt-12 max-w-5xl px-4"
    >
      <div className="mb-6 text-center">
        <h2 className="text-2xl font-bold text-vault-text">
          The Controversy Landscape
        </h2>
        <p className="mt-1 text-sm text-vault-text-muted">
          Every vaulted show plotted by quality and controversy. Brighter dots are more popular. Hover a dot for the show, click to explore.
        </p>
        {/* Compact term legend — short definitions anchor the two
            metrics so users aren't guessing at "quality" or
            "controversy". Each term links to its full HowItWorks
            section for the deeper explanation. */}
        <div
          data-testid="landscape-legend"
          className="mt-4 flex flex-wrap items-center justify-center gap-x-6 gap-y-2 text-xs text-vault-text-muted"
        >
          <span>
            <Link
              to="/how-it-works#scoring"
              className="font-semibold text-vault-text hover:text-vault-accent transition-colors"
            >
              Quality
            </Link>
            {' — community rating adjusted for vote count'}
          </span>
          <span>
            <Link
              to="/how-it-works#consensus"
              className="font-semibold text-vault-text hover:text-vault-accent transition-colors"
            >
              Controversy
            </Link>
            {' — how divided fan reviews are'}
          </span>
        </div>
      </div>

      {/* Filter controls */}
      {data !== null && (
        <div
          data-testid="landscape-filters"
          className="mb-4 flex flex-wrap items-center justify-center gap-3 text-sm"
        >
          <label className="flex items-center gap-2 text-vault-text-muted">
            Genre
            <select
              data-testid="landscape-filter-genre"
              value={filters.genre}
              onChange={e => setFilters(f => ({ ...f, genre: e.target.value }))}
              className="rounded border border-vault-border bg-vault-surface px-2 py-1 text-vault-text"
            >
              <option value="">Any</option>
              {uniqueGenres.map(g => (
                <option key={g} value={g}>{g}</option>
              ))}
            </select>
          </label>

          <label className="flex items-center gap-2 text-vault-text-muted">
            Decade
            <select
              data-testid="landscape-filter-decade"
              value={filters.decade}
              onChange={e => setFilters(f => ({ ...f, decade: e.target.value }))}
              className="rounded border border-vault-border bg-vault-surface px-2 py-1 text-vault-text"
            >
              <option value="">Any</option>
              {DECADES.map(d => (
                <option key={d} value={d}>{d}s</option>
              ))}
            </select>
          </label>

          <label className="flex items-center gap-2 text-vault-text-muted">
            Subtype
            <select
              data-testid="landscape-filter-subtype"
              value={filters.subtype}
              onChange={e => setFilters(f => ({ ...f, subtype: e.target.value }))}
              className="rounded border border-vault-border bg-vault-surface px-2 py-1 text-vault-text"
            >
              <option value="">Any</option>
              {uniqueSubtypes.map(s => (
                <option key={s} value={s}>{s}</option>
              ))}
            </select>
          </label>

          {/* Reset button always rendered — disabled when there's
              nothing to clear. On mobile the filter dropdowns wrap and
              the button is easy to lose if it only appears
              conditionally, so always-visible-but-maybe-disabled gives
              a stable, discoverable control. */}
          <button
            type="button"
            data-testid="landscape-clear-filters"
            onClick={clearFilters}
            disabled={!hasActiveFilter}
            className="rounded border border-vault-border bg-vault-surface px-3 py-1 text-xs text-vault-text-muted hover:border-vault-accent hover:text-vault-accent transition-colors cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed disabled:hover:border-vault-border disabled:hover:text-vault-text-muted"
          >
            Reset
          </button>
        </div>
      )}

      {data === null && (
        <div
          data-testid="landscape-loading"
          className="mx-auto h-96 w-full animate-pulse rounded border border-vault-border bg-vault-surface"
          role="status"
          aria-label="Loading landscape data"
        />
      )}

      <div ref={containerRef} className="w-full overflow-x-auto" />
    </section>
  )
}
