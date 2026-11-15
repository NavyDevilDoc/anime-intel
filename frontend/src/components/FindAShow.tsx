import { useState, useEffect, useRef, type FormEvent } from 'react'
import { api, ApiError, type AnonLimitDetail } from '../lib/api'
import { detectTitleMatch } from '../lib/offlineEncyclopedia'
import { detectObjectiveShape } from '../lib/objectiveDetect'
import { detectIntelligentShape } from '../lib/intelligentDetect'
import type { SearchResponse, ScoredResult, StaffChip } from '../lib/types'
import QueryTipsPanel from './QueryTipsPanel'
import ResultCard from './ResultCard'
import SearchFeedback from './SearchFeedback'
import AnonLimitModal from './AnonLimitModal'
import LowConfidenceBanner from './LowConfidenceBanner'
import ObjectiveHint from './ObjectiveHint'
import IntelligentSearchHint from './IntelligentSearchHint'
import Spinner from './Spinner'
import StaffStudioInput from './StaffStudioInput'
import { toStaffFilterPayload } from '../lib/staffChips'
import TitleLookupHint from './TitleLookupHint'

const ERA_OPTIONS: Record<string, { min?: number; max?: number }> = {
  'All': {},
  'Pre-1990': { max: 1989 },
  '1990s': { min: 1990, max: 1999 },
  '2000s': { min: 2000, max: 2009 },
  '2010s': { min: 2010, max: 2019 },
  '2020s': { min: 2020 },
}

const LENS_INFO: Record<string, string> = {
  'Intelligent Search': 'AI-powered semantic search. Understands vibes, themes, and implicit preferences.',
  'Objective Rankings': 'Bypasses AI reasoning to return hard database rankings.',
  'Staff/Studio': 'Filter by director, composer, creator, or studio. Deterministic — no AI reranker.',
}

// Canonical ordering of the segmented control. Intelligent Search
// sits first so it's the default selection on mount.
const LENS_ORDER = ['Intelligent Search', 'Objective Rankings', 'Staff/Studio'] as const

// MAL rating ceilings exposed to the user. Order matters — the <select>
// renders them top-down. Value strings must match the backend's closed-set
// validator (G / PG / PG-13 / R); empty value = no filter applied.
const RATING_OPTIONS: { value: string; label: string }[] = [
  { value: '', label: 'Any rating' },
  { value: 'PG-13', label: 'PG-13 and below' },
  { value: 'PG', label: 'PG and below' },
  { value: 'G', label: 'G only' },
]

const RATING_FILTER_ENABLED =
  import.meta.env.VITE_RATING_FILTER_ENABLED !== 'false'

// Engine hiatus copy. Defensive early-return: the mode-picker tabs are
// normally disabled while hiatus is on, but direct-URL access to
// /try?mode=fas still routes here.
const HIATUS_MESSAGE =
  'The recommendation engine is on hiatus while we work on improvements ' +
  'to retrieval quality. The anime encyclopedia remains fully available.'

const PAGE_SIZE = 5

interface Props {
  onShowClick?: (title: string) => void
  seedQuery?: string
  onSeedConsumed?: () => void
  // MAL IDs from the watchlist panel's localStorage cache. The engine
  // expands these to full series clusters server-side before applying
  // the exclusion. null/undefined = no exclusion active.
  excludeMalIds?: number[] | null
}

export default function FindAShow({ onShowClick, seedQuery, onSeedConsumed, excludeMalIds }: Props) {
  const [query, setQuery] = useState('')
  const [lens, setLens] = useState('Intelligent Search')
  const [era, setEra] = useState('All')
  const [ratingMax, setRatingMax] = useState('')
  // Staff/Studio chip state. Preserved across mode switches so a user
  // picking chips, toggling to IS, and back doesn't lose their work.
  const [staffChips, setStaffChips] = useState<StaffChip[]>([])
  const [pool, setPool] = useState<ScoredResult[]>([])
  const [page, setPage] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [lowConfidence, setLowConfidence] = useState(false)
  // Signals that the pool came from composite fallback instead of full
  // LLM rerank. Threaded to both the banner (variant copy) and per-card
  // pills so users see consistent framing.
  const [fallbackMode, setFallbackMode] = useState<'composite' | null>(null)
  const [elapsed, setElapsed] = useState<number | null>(null)
  const [searchId, setSearchId] = useState<string | null>(null)
  const [anonLimit, setAnonLimit] = useState<AnonLimitDetail | null>(null)
  // Detected canonical title when the user's query is (case-insensitively,
  // punctuation-agnostically) an exact vault title. Populated after every
  // search; drives the TitleLookupHint below. Kept as state (not derived)
  // because the detection is async and shouldn't re-fire on unrelated
  // re-renders (pagination, filter changes without a new search).
  const [detectedTitle, setDetectedTitle] = useState<string | null>(null)
  // Set when the submitted IS query reads as objective-ranking shape
  // (see lib/objectiveDetect). Render also gates on the current lens
  // so switching modes via the hint's button naturally hides it
  // without a manual clear.
  const [objectiveHintVisible, setObjectiveHintVisible] = useState(false)
  // Symmetric counterpart: set when the submitted Objective Rankings
  // query reads as vibe-shape (see lib/intelligentDetect). Same
  // lens-gated render pattern as above.
  const [intelligentHintVisible, setIntelligentHintVisible] = useState(false)
  const abortRef = useRef<AbortController | null>(null)

  // Consume seed query from parent (hero search bar, sample chips).
  // Pre-fills the search box but does NOT auto-execute — the user
  // sees the four pre-flight controls (watchlist exclusion, decade,
  // mode, rating ceiling) before spending one of their daily search
  // quota on a query they'd likely want to refine.
  useEffect(() => {
    if (seedQuery) {
      setQuery(seedQuery)
      onSeedConsumed?.()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [seedQuery])

  const performSearch = async (queryOverride?: string, lensOverride?: string) => {
    // `lensOverride` lets the hint handlers (ObjectiveHint,
    // IntelligentSearchHint) switch mode AND re-run in the new mode
    // atomically. Without it, calling setLens + performSearch
    // in sequence runs the search against the OLD lens (React state
    // updates are batched and asynchronous — performSearch would read
    // the stale `lens` from closure).
    const effectiveLens = lensOverride ?? lens
    const isStaffStudio = effectiveLens === 'Staff/Studio'
    const q = (queryOverride ?? query).trim()
    // Staff/Studio has empty query; chip payload carries intent. All
    // other modes need a non-empty query.
    if (!isStaffStudio && !q) return
    if (isStaffStudio && staffChips.length === 0) return

    abortRef.current?.abort()
    const controller = new AbortController()
    abortRef.current = controller

    setLoading(true)
    setError('')
    setLowConfidence(false)
    setFallbackMode(null)
    setElapsed(null)
    setDetectedTitle(null)
    setObjectiveHintVisible(false)
    setIntelligentHintVisible(false)
    // Objective-shape detection is synchronous (regex sweep, no fetch)
    // so we can decide before firing the search. Only fires on
    // Intelligent Search — OR is the target destination, and
    // Staff/Studio has different UX semantics entirely.
    const objectiveHintEnabled =
      import.meta.env.VITE_FAS_OBJECTIVE_HINT_ENABLED !== 'false' &&
      !isStaffStudio &&
      effectiveLens === 'Intelligent Search'
    if (objectiveHintEnabled && detectObjectiveShape(q).matched) {
      setObjectiveHintVisible(true)
    }
    // Symmetric counterpart: vibe-shape detection on Objective Rankings
    // queries. Same guard rails — never fires in Staff/Studio, and
    // only fires in the mode where IS is the better destination.
    const intelligentHintEnabled =
      import.meta.env.VITE_FAS_INTELLIGENT_HINT_ENABLED !== 'false' &&
      !isStaffStudio &&
      effectiveLens === 'Objective Rankings'
    if (intelligentHintEnabled && detectIntelligentShape(q).matched) {
      setIntelligentHintVisible(true)
    }
    // Fire the title-match detection in parallel with the search — the
    // JSON fetch is cached after the first call, so this is essentially
    // free on subsequent searches. We don't await it until after the
    // main search response lands (results take priority). Skip entirely
    // for Staff/Studio: query is empty, nothing to match.
    const titleHintEnabled =
      import.meta.env.VITE_FAS_TITLE_HINT_ENABLED !== 'false' && !isStaffStudio
    const titleMatchPromise = titleHintEnabled
      ? detectTitleMatch(q).catch(() => ({ matched: false, title: null }))
      : Promise.resolve({ matched: false, title: null })
    const t0 = performance.now()
    try {
      // Era + rating filters only apply in Intelligent Search.
      // Objective Rankings runs a separate SQL path; Staff/Studio runs
      // a deterministic chip-filtered SQL path — neither uses these
      // controls.
      const useFilters = effectiveLens === 'Intelligent Search'
      const eraOpts = useFilters ? ERA_OPTIONS[era] : {}
      const ratingPayload = useFilters && ratingMax ? ratingMax : null
      const payload: Record<string, unknown> = {
        // Staff/Studio sends an empty query — the backend's mode-conditional
        // validator accepts it because staff_filter carries intent.
        query: isStaffStudio ? '' : q,
        lens: effectiveLens,
        year_min: eraOpts.min ?? null,
        year_max: eraOpts.max ?? null,
        era_label: useFilters ? era : null,
        rating_max: ratingPayload,
      }
      // Chip payload only threaded on the Staff/Studio path — keeps
      // the wire body tight on IS/OR and avoids exposing an empty key
      // to downstream shape asserters.
      if (isStaffStudio) {
        payload.staff_filter = toStaffFilterPayload(staffChips)
      }
      // Only thread the exclusion key when non-empty — keeps the wire
      // payload tight for callers that don't opt into the feature.
      if (excludeMalIds && excludeMalIds.length > 0) {
        payload.exclude_mal_ids = excludeMalIds
      }
      const res = await api.post<SearchResponse>('/api/search', payload, controller.signal)
      // low_confidence is independent of success — it flows alongside
      // both the success-with-results and success-with-empty-pool paths.
      setLowConfidence(res.low_confidence === true)
      // Null-safe against older backend responses that omit the key.
      setFallbackMode(res.fallback_mode ?? null)
      if (!res.success) {
        // When the engine signals low_confidence, the banner replaces
        // the red error block (yellow advisory instead of "broken"
        // framing). When it doesn't, the red error still shows.
        if (res.low_confidence !== true) {
          setError(res.error ?? 'Search failed')
        }
        setPool([])
      } else {
        setPool(res.pool)
        setPage(0)
        setSearchId(res.search_id)
        setElapsed((performance.now() - t0) / 1000)
      }
    } catch (err) {
      if (err instanceof DOMException && err.name === 'AbortError') {
        return
      }
      if (err instanceof ApiError && err.status === 429 && err.data?.limit_type) {
        setAnonLimit(err.data)
      } else {
        setError(err instanceof Error ? err.message : 'Search failed')
      }
      setPool([])
    } finally {
      if (!controller.signal.aborted) {
        // Resolve the title-match promise now that the main search is done.
        // Guard on abort so a superseded request doesn't stamp a stale
        // detectedTitle onto the current view.
        const match = await titleMatchPromise
        if (!controller.signal.aborted && match.matched && match.title) {
          setDetectedTitle(match.title)
        }
        setLoading(false)
      }
    }
  }

  const handleSearch = (e: FormEvent) => {
    e.preventDefault()
    performSearch()
  }

  const handleStop = () => {
    abortRef.current?.abort()
    abortRef.current = null
    setLoading(false)
  }

  const start = page * PAGE_SIZE
  const pageResults = pool.slice(start, start + PAGE_SIZE)
  const hasNext = pool.length > start + PAGE_SIZE
  const hasPrev = page > 0
  // Only Intelligent Search shows era/rating filters and per-card
  // confidence. Objective Rankings runs a SQL sort where confidence is
  // meaningless (all 100); Staff/Studio runs a deterministic chip filter
  // for the same reason.
  const isIntelligentSearch = lens === 'Intelligent Search'
  const isStaffStudioMode = lens === 'Staff/Studio'
  const staffStudioEnabled =
    import.meta.env.VITE_STAFF_STUDIO_ENABLED !== 'false'
  const availableLenses = staffStudioEnabled
    ? LENS_ORDER
    : LENS_ORDER.filter(l => l !== 'Staff/Studio')
  // Submit gating differs per mode: Staff/Studio needs chips (query
  // is empty); IS/OR need typed query text.
  const canSubmit = isStaffStudioMode
    ? staffChips.length > 0
    : query.trim().length > 0

  if (import.meta.env.VITE_ENGINE_HIATUS_MODE === 'true') {
    return (
      <div
        role="status"
        data-testid="hiatus-card"
        className="rounded border border-yellow-700 bg-yellow-950 px-4 py-6 text-sm text-yellow-200"
      >
        {HIATUS_MESSAGE}
      </div>
    )
  }

  return (
    <div className="space-y-5">
      <div>
        <h2 className="text-xl font-bold mb-1">AI Vibe Search</h2>
        <p className="text-sm text-vault-text-muted">
          Scan the encyclopedia for specific themes, tones, or objective rankings.
        </p>
      </div>

      <QueryTipsPanel />

      <form onSubmit={handleSearch} className="space-y-4">
        {/* Segmented control above the query row. Persistent visibility
            solves the "users don't know Objective Rankings / Staff/Studio
            exist" gap that a hidden <select> would introduce. */}
        <div
          role="tablist"
          aria-label="Search mode"
          className="flex border-b border-vault-border"
        >
          {availableLenses.map(l => (
            <button
              key={l}
              type="button"
              role="tab"
              aria-selected={lens === l}
              aria-controls="fas-search-form"
              onClick={() => setLens(l)}
              title={LENS_INFO[l]}
              className={`px-5 py-2.5 text-sm font-medium border-b-2 transition-colors cursor-pointer bg-transparent ${
                lens === l
                  ? 'border-vault-accent text-vault-accent'
                  : 'border-transparent text-vault-text-muted hover:text-vault-text'
              }`}
            >
              {l}
            </button>
          ))}
        </div>

        <div id="fas-search-form" className="flex flex-col sm:flex-row gap-3">
          {isStaffStudioMode ? (
            <div className="flex-1">
              <StaffStudioInput
                value={staffChips}
                onChange={setStaffChips}
                disabled={loading}
              />
            </div>
          ) : (
            <div className="relative flex-1">
              <input
                type="text"
                value={query}
                onChange={e => setQuery(e.target.value)}
                placeholder="e.g., 'gritty military drama' or 'anything by MAPPA' or 'Top 5 sports anime'"
                maxLength={500}
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
          )}
          {loading && (
            <button
              type="button"
              onClick={handleStop}
              className="px-4 py-2 rounded bg-red-800 text-white font-medium hover:bg-red-700 transition-colors border-none cursor-pointer"
            >
              Stop
            </button>
          )}
        </div>

        <p className="text-xs text-vault-text-muted italic">{LENS_INFO[lens]}</p>

        {isIntelligentSearch && (
          <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
            <span className="text-sm text-vault-text-muted">Decade:</span>
            {Object.keys(ERA_OPTIONS).map(e => (
              <label key={e} className="flex items-center gap-1.5 text-sm cursor-pointer">
                <input
                  type="radio"
                  name="era"
                  checked={era === e}
                  onChange={() => setEra(e)}
                  className="accent-vault-accent"
                />
                <span className={era === e ? 'text-vault-text' : 'text-vault-text-muted'}>{e}</span>
              </label>
            ))}
          </div>
        )}

        {isIntelligentSearch && RATING_FILTER_ENABLED && (
          <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
            <label htmlFor="rating-max" className="text-sm text-vault-text-muted">
              Rating:
            </label>
            <select
              id="rating-max"
              value={ratingMax}
              onChange={e => setRatingMax(e.target.value)}
              title="Hides shows whose MAL rating is unknown or higher than the selected ceiling."
              aria-label="MAL content rating ceiling"
              className="px-2 py-1 text-sm rounded bg-vault-surface border border-vault-border text-vault-text focus:outline-none focus:border-vault-accent"
            >
              {RATING_OPTIONS.map(opt => (
                <option key={opt.value} value={opt.value}>{opt.label}</option>
              ))}
            </select>
          </div>
        )}

        <button
          type="submit"
          disabled={loading || !canSubmit}
          className="w-full py-2.5 rounded bg-vault-accent text-white font-medium hover:bg-vault-accent-hover disabled:opacity-50 transition-colors border-none cursor-pointer"
        >
          {loading ? (
            <span className="inline-flex items-center justify-center gap-2">
              <Spinner /> Searching...
            </span>
          ) : (
            'Execute Search'
          )}
        </button>
      </form>

      {lowConfidence && (
        <LowConfidenceBanner
          watchlistExclusionEmptied={
            !!excludeMalIds && excludeMalIds.length > 0 && pool.length === 0
          }
          fallbackMode={fallbackMode}
          staffStudioEmpty={isStaffStudioMode && pool.length === 0}
        />
      )}

      {detectedTitle && (
        <TitleLookupHint title={detectedTitle} isEmpty={pool.length === 0} />
      )}

      {intelligentHintVisible && lens === 'Objective Rankings' && (
        <IntelligentSearchHint onSwitchToIntelligentSearch={() => {
          // Switch lens AND re-run the current query atomically — the
          // lensOverride param on performSearch bypasses the stale-
          // closure problem that would otherwise run against the old
          // lens. Without this, the user would see "Objective Result #1"
          // reasoning on results after flipping to Intelligent Search.
          setLens('Intelligent Search')
          performSearch(undefined, 'Intelligent Search')
        }} />
      )}
      {objectiveHintVisible && lens === 'Intelligent Search' && (
        <ObjectiveHint onSwitchToObjectiveRankings={() => {
          // Symmetric to the IntelligentSearchHint handler above —
          // switch + re-run atomically so stale IS reasoning doesn't
          // linger after the lens flip to Objective Rankings.
          setLens('Objective Rankings')
          performSearch(undefined, 'Objective Rankings')
        }} />
      )}

      {error && (
        <div className="bg-red-950 border border-red-800 rounded px-4 py-3 text-red-200">
          {error}
        </div>
      )}

      {anonLimit && (
        <AnonLimitModal detail={anonLimit} onClose={() => setAnonLimit(null)} />
      )}

      {pool.length > 0 && (
        <div className="space-y-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="text-sm text-vault-success">
              Displaying results {start + 1} - {Math.min(start + PAGE_SIZE, pool.length)} of {pool.length} potential targets{elapsed !== null ? ` in ${elapsed.toFixed(1)}s` : ''}.
            </p>
            {searchId && <SearchFeedback searchId={searchId} />}
          </div>

          {pageResults.map((r, i) => (
            <ResultCard key={`${r.profile.id}-${i}`} result={r} showConfidence={isIntelligentSearch} onShowClick={onShowClick} fallbackMode={fallbackMode} />
          ))}

          {(hasPrev || hasNext) && (
            <div className="flex gap-3">
              {hasPrev && (
                <button
                  onClick={() => { setPage(p => p - 1); window.scrollTo({ top: 0, behavior: 'smooth' }) }}
                  className="flex-1 py-2 rounded bg-vault-surface border border-vault-border text-vault-text hover:bg-vault-border transition-colors cursor-pointer"
                >
                  Previous
                </button>
              )}
              {hasNext && (
                <button
                  onClick={() => { setPage(p => p + 1); window.scrollTo({ top: 0, behavior: 'smooth' }) }}
                  className="flex-1 py-2 rounded bg-vault-surface border border-vault-border text-vault-text hover:bg-vault-border transition-colors cursor-pointer"
                >
                  Show Me More
                </button>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
