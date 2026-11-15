import { useCallback, useEffect, useMemo, useState } from 'react'
import { api, ApiError } from '../lib/api'

/**
 * PR C / Initiative 2 Phase D — Watchlist Exclusion Panel.
 *
 * Lives above the tabs on the Search page. Lets users supply a public
 * MAL or AniList username; the backend reads their public list and the
 * frontend caches the resulting MAL IDs in localStorage. Search and DNA
 * requests then thread those IDs as `exclude_mal_ids` so the engine
 * filters out shows the user has already finished or dropped.
 *
 * Privacy contract (also documented in HowItWorks + Privacy pages):
 *   - Username is sent to the backend once at fetch time. Never persisted
 *     server-side. The list of MAL IDs lives in YOUR browser's
 *     localStorage and is cleared with the Clear button.
 *   - The backend reads from public MAL/AniList list endpoints using
 *     the project's app-scoped credentials. We never see your account
 *     password (the API doesn't expose it).
 *
 * Rollback: VITE_WATCHLIST_EXCLUSION_ENABLED=false in Vercel env hides
 * the entire component without removing the route or wiring.
 *
 * 7-day soft refresh: the panel surfaces a yellow advisory chip when
 * the cached list is older than 7 days. The chip is non-blocking —
 * the user can keep using the old cache or refresh on demand.
 */

const ENABLED = import.meta.env.VITE_WATCHLIST_EXCLUSION_ENABLED !== 'false'

const LOCAL_STORAGE_KEY = 'anime-intel-watchlist-v1'
const SOFT_REFRESH_DAYS = 7

type Source = 'mal' | 'anilist'
type CanonicalStatus =
  | 'completed'
  | 'dropped'
  | 'watching'
  | 'plan_to_watch'
  | 'on_hold'

const ALL_STATUSES: CanonicalStatus[] = [
  'completed',
  'dropped',
  'watching',
  'plan_to_watch',
  'on_hold',
]

const STATUS_LABELS: Record<CanonicalStatus, string> = {
  completed: 'Completed',
  dropped: 'Dropped',
  watching: 'Watching',
  plan_to_watch: 'Plan to Watch',
  on_hold: 'On Hold',
}

const DEFAULT_STATUSES: CanonicalStatus[] = ['completed', 'dropped']

export interface WatchlistCache {
  source: Source
  username: string
  statuses: CanonicalStatus[]
  mal_ids: number[]
  total_entries: number
  unmappable_count: number
  errors: string[]
  fetched_at: string // ISO8601
}

interface FetchResponseBody {
  source: Source
  mal_ids: number[]
  total_entries: number
  unmappable_entries: number
  errors: string[]
}

function readCache(): WatchlistCache | null {
  if (typeof window === 'undefined') return null
  try {
    const raw = window.localStorage.getItem(LOCAL_STORAGE_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw) as WatchlistCache
    if (
      !parsed ||
      typeof parsed !== 'object' ||
      !Array.isArray(parsed.mal_ids) ||
      !parsed.source ||
      !parsed.fetched_at
    ) {
      return null
    }
    return parsed
  } catch {
    return null
  }
}

function writeCache(cache: WatchlistCache | null) {
  if (typeof window === 'undefined') return
  if (cache === null) {
    window.localStorage.removeItem(LOCAL_STORAGE_KEY)
  } else {
    window.localStorage.setItem(LOCAL_STORAGE_KEY, JSON.stringify(cache))
  }
}

function ageInDays(iso: string): number {
  const then = Date.parse(iso)
  if (Number.isNaN(then)) return Number.POSITIVE_INFINITY
  return (Date.now() - then) / 86_400_000
}

function formatRelativeAge(iso: string): string {
  const days = ageInDays(iso)
  if (!Number.isFinite(days)) return 'just now'
  if (days < 1) return 'today'
  if (days < 2) return 'yesterday'
  return `${Math.floor(days)} days ago`
}

interface Props {
  onListChange: (cache: WatchlistCache | null) => void
}

export default function WatchlistExclusionPanel({ onListChange }: Props) {
  const [expanded, setExpanded] = useState(false)
  const [source, setSource] = useState<Source>('mal')
  const [username, setUsername] = useState('')
  const [statuses, setStatuses] = useState<Set<CanonicalStatus>>(
    new Set(DEFAULT_STATUSES),
  )
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [cache, setCache] = useState<WatchlistCache | null>(null)

  // Hydrate from localStorage on mount and notify parent.
  useEffect(() => {
    const stored = readCache()
    if (stored) {
      setCache(stored)
      setSource(stored.source)
      setUsername(stored.username)
      setStatuses(new Set(stored.statuses))
      onListChange(stored)
    } else {
      onListChange(null)
    }
    // Mount-only — onListChange is stable per Search.tsx's useCallback.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const isStale = useMemo(() => {
    if (!cache) return false
    return ageInDays(cache.fetched_at) >= SOFT_REFRESH_DAYS
  }, [cache])

  const toggleStatus = (status: CanonicalStatus) => {
    setStatuses(prev => {
      const next = new Set(prev)
      if (next.has(status)) {
        next.delete(status)
      } else {
        next.add(status)
      }
      return next
    })
  }

  const handleFetch = useCallback(async () => {
    const trimmed = username.trim()
    if (!trimmed) {
      setError('Username is required.')
      return
    }
    if (statuses.size === 0) {
      setError('Pick at least one status to exclude.')
      return
    }
    setLoading(true)
    setError(null)
    try {
      const body = {
        source,
        username: trimmed,
        statuses: Array.from(statuses),
      }
      const res = await api.post<FetchResponseBody>(
        '/api/fetch-watchlist',
        body,
      )
      const next: WatchlistCache = {
        source: res.source,
        username: trimmed,
        statuses: Array.from(statuses),
        mal_ids: res.mal_ids,
        total_entries: res.total_entries,
        unmappable_count: res.unmappable_entries,
        errors: res.errors,
        fetched_at: new Date().toISOString(),
      }
      setCache(next)
      writeCache(next)
      onListChange(next)
      // Collapse on success so the page returns to the search-first view.
      setExpanded(false)
    } catch (e) {
      if (e instanceof ApiError) {
        // 404 = user-not-found / private list (both sources)
        if (e.status === 404) {
          setError(
            'User not found, or the list is private. Set your list to public and retry.',
          )
        } else if (e.status === 429) {
          setError('Upstream rate limit hit — try again in a minute.')
        } else if (e.status === 503) {
          setError(
            'Watchlist exclusion is temporarily unavailable. Try again later.',
          )
        } else {
          setError(e.message || 'Failed to fetch your watchlist.')
        }
      } else {
        setError('Network error — check your connection and retry.')
      }
    } finally {
      setLoading(false)
    }
  }, [source, username, statuses, onListChange])

  const handleClear = useCallback(() => {
    setCache(null)
    writeCache(null)
    setError(null)
    onListChange(null)
  }, [onListChange])

  if (!ENABLED) return null

  // Collapsed-with-list state: a status chip the user can click to expand
  // and a Clear button. Keeps the search-first surface clean.
  if (!expanded && cache) {
    return (
      <div
        className="flex flex-wrap items-center gap-3 text-xs text-vault-text-muted"
        data-testid="watchlist-panel-collapsed"
      >
        <button
          type="button"
          onClick={() => setExpanded(true)}
          className="inline-flex items-center gap-2 px-3 py-1.5 rounded-full bg-vault-surface border border-vault-border hover:border-vault-accent text-vault-text transition-colors cursor-pointer"
          data-testid="watchlist-panel-expand"
        >
          <span>📋</span>
          <span>
            Excluding <strong>{cache.mal_ids.length}</strong>{' '}
            {cache.mal_ids.length === 1 ? 'show' : 'shows'} from your{' '}
            {cache.source.toUpperCase()} list
          </span>
        </button>
        {cache.unmappable_count > 0 && (
          <span
            className="text-vault-text-muted"
            title={`${cache.unmappable_count} AniList entries didn't have a MAL link, so they can't be excluded.`}
            aria-label={`${cache.unmappable_count} AniList entries without MAL links won't be excluded`}
            data-testid="watchlist-unmappable-caption"
          >
            ({cache.unmappable_count} without MAL links)
          </span>
        )}
        {isStale && (
          <button
            type="button"
            onClick={() => setExpanded(true)}
            className="inline-flex items-center gap-1 px-2 py-1 rounded bg-yellow-900/30 border border-yellow-700/40 text-yellow-200 hover:border-yellow-500 cursor-pointer text-xs"
            data-testid="watchlist-stale-chip"
          >
            ⓘ Last fetched {formatRelativeAge(cache.fetched_at)} — refresh?
          </button>
        )}
        <button
          type="button"
          onClick={handleClear}
          className="text-vault-text-muted hover:text-vault-text underline-offset-2 hover:underline text-xs bg-transparent border-none p-0 cursor-pointer"
          data-testid="watchlist-clear-collapsed"
        >
          Clear
        </button>
      </div>
    )
  }

  // Collapsed-without-list state: a single "Exclude shows you've watched"
  // chip — small affordance, no clutter for users who don't care.
  if (!expanded && !cache) {
    return (
      <button
        type="button"
        onClick={() => setExpanded(true)}
        className="inline-flex items-center gap-2 px-3 py-1.5 rounded-full bg-vault-surface border border-vault-border hover:border-vault-accent text-vault-text-muted hover:text-vault-text transition-colors cursor-pointer text-xs"
        data-testid="watchlist-panel-expand"
      >
        <span>📋</span>
        <span>Exclude shows you've already watched</span>
      </button>
    )
  }

  // Expanded state: the full form.
  return (
    <div
      className="rounded-md border border-vault-border bg-vault-surface p-4 space-y-3 text-sm"
      data-testid="watchlist-panel-expanded"
    >
      <div className="flex items-center justify-between">
        <h2 className="text-base font-semibold text-vault-text">
          Exclude shows you've already watched
        </h2>
        <button
          type="button"
          onClick={() => setExpanded(false)}
          className="text-vault-text-muted hover:text-vault-text bg-transparent border-none cursor-pointer text-sm"
          aria-label="Close watchlist exclusion panel"
        >
          ✕
        </button>
      </div>

      <p className="text-xs text-vault-text-muted">
        Your username is used once to fetch your public list, then discarded.
        The ID list lives in your browser only.
      </p>

      <div className="grid grid-cols-1 sm:grid-cols-[120px_1fr] gap-3 items-center">
        <label
          htmlFor="watchlist-source"
          className="text-xs text-vault-text-muted"
        >
          Source
        </label>
        <select
          id="watchlist-source"
          value={source}
          onChange={e => setSource(e.target.value as Source)}
          className="bg-vault-bg border border-vault-border rounded px-2 py-1.5 text-vault-text"
          data-testid="watchlist-source-select"
        >
          <option value="mal">MyAnimeList</option>
          <option value="anilist">AniList</option>
        </select>

        <label
          htmlFor="watchlist-username"
          className="text-xs text-vault-text-muted"
        >
          Username
        </label>
        <input
          id="watchlist-username"
          type="text"
          value={username}
          onChange={e => setUsername(e.target.value)}
          placeholder={
            source === 'mal' ? 'Your MAL username' : 'Your AniList username'
          }
          autoComplete="off"
          maxLength={50}
          className="bg-vault-bg border border-vault-border rounded px-2 py-1.5 text-vault-text"
          data-testid="watchlist-username-input"
        />
      </div>

      <div>
        <div className="text-xs text-vault-text-muted mb-1.5">
          Exclude shows marked as:
        </div>
        <div className="flex flex-wrap gap-2">
          {ALL_STATUSES.map(status => {
            const checked = statuses.has(status)
            return (
              <label
                key={status}
                className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded border text-xs cursor-pointer transition-colors ${
                  checked
                    ? 'border-vault-accent text-vault-text bg-vault-accent/10'
                    : 'border-vault-border text-vault-text-muted hover:text-vault-text'
                }`}
              >
                <input
                  type="checkbox"
                  checked={checked}
                  onChange={() => toggleStatus(status)}
                  className="accent-vault-accent"
                  data-testid={`watchlist-status-${status}`}
                />
                {STATUS_LABELS[status]}
              </label>
            )
          })}
        </div>
      </div>

      {error && (
        <div
          className="text-xs rounded border border-red-500/40 bg-red-900/20 text-red-200 px-3 py-2"
          data-testid="watchlist-error"
        >
          {error}
        </div>
      )}

      {cache && cache.errors.length > 0 && (
        <div
          className="text-xs rounded border border-yellow-700/40 bg-yellow-900/20 text-yellow-200 px-3 py-2"
          data-testid="watchlist-partial-errors"
        >
          <div className="font-medium">Partial fetch — some statuses failed:</div>
          <ul className="mt-1 list-disc pl-4">
            {cache.errors.map((e, i) => (
              <li key={i}>{e}</li>
            ))}
          </ul>
        </div>
      )}

      <div className="flex flex-wrap items-center gap-3">
        <button
          type="button"
          onClick={handleFetch}
          disabled={loading}
          className="px-4 py-1.5 rounded bg-vault-accent text-vault-bg font-medium text-xs hover:opacity-90 disabled:opacity-50 disabled:cursor-not-allowed cursor-pointer border-none"
          data-testid="watchlist-fetch"
        >
          {loading
            ? 'Fetching…'
            : cache
              ? 'Refresh list'
              : 'Fetch list'}
        </button>
        {cache && (
          <button
            type="button"
            onClick={handleClear}
            className="text-vault-text-muted hover:text-vault-text underline-offset-2 hover:underline text-xs bg-transparent border-none p-0 cursor-pointer"
            data-testid="watchlist-clear"
          >
            Clear my list
          </button>
        )}
        {cache && (
          <span
            className="text-xs text-vault-text-muted"
            data-testid="watchlist-status"
          >
            Excluding <strong>{cache.mal_ids.length}</strong>{' '}
            {cache.mal_ids.length === 1 ? 'show' : 'shows'} from{' '}
            {cache.source.toUpperCase()} (fetched {formatRelativeAge(cache.fetched_at)})
            {cache.unmappable_count > 0 && (
              <>
                {' '}
                <span
                  title="AniList entries without MAL links can't be excluded — they exist in AniList's catalog but not MAL's."
                  data-testid="watchlist-unmappable-expanded-caption"
                >
                  ({cache.unmappable_count} AniList entries without MAL links won't be excluded)
                </span>
              </>
            )}
          </span>
        )}
      </div>
    </div>
  )
}
