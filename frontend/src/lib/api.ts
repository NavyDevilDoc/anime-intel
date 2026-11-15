const API_BASE = import.meta.env.VITE_API_BASE_URL as string ?? ''

// Block HTTP API calls in production (HTTPS only) — but exempt localhost
// so `npm run preview` against a local backend still works. The regex is
// anchored: the host portion must be exactly localhost / 127.0.0.1
// followed by `:`, `/`, or end-of-string — so `http://localhost.evil.com`
// and `http://evil.com/localhost` do NOT match and still fire the guard.
const isLocalhostHttp = /^http:\/\/(localhost|127\.0\.0\.1)(?::|\/|$)/.test(API_BASE)
if (import.meta.env.PROD && API_BASE.startsWith('http://') && !isLocalhostHttp) {
  throw new Error('VITE_API_BASE_URL must use HTTPS in production')
}

// --- Anonymous usage tracking (server-authoritative) ---
//
// Seeded from GET /api/anon-usage on mount and kept in sync by reading
// the search_remaining / dna_remaining fields the backend threads into
// every /api/search and /api/dna response. On a 429 the relevant
// counter is zeroed locally so the AnonBanner immediately shows
// "0 remaining" without an extra round-trip.

export interface AnonUsage {
  searchesRemaining: number
  dnaRemaining: number
}

interface AnonUsageResponse {
  search_remaining: number
  dna_remaining: number
}

// null = not yet loaded from the server. Consumers (the AnonBanner)
// render a placeholder until the first fetch resolves.
let _anonUsage: AnonUsage | null = null
let _anonListeners: Array<(u: AnonUsage | null) => void> = []

export function getAnonUsage(): AnonUsage | null { return _anonUsage }
export function onAnonUsageChange(fn: (u: AnonUsage | null) => void) {
  _anonListeners.push(fn)
  return () => { _anonListeners = _anonListeners.filter(l => l !== fn) }
}

function _setAnonUsage(next: AnonUsage | null) {
  _anonUsage = next
  _anonListeners.forEach(fn => fn(_anonUsage))
}

// Read remaining-count fields off a successful POST response body and
// update the local counter. The fields are nullable in the API contract
// because some non-anon code paths might omit them in the future — we
// only update when they're present.
function _updateAnonFromResponse(body: unknown) {
  if (!body || typeof body !== 'object') return
  const obj = body as Record<string, unknown>
  const searchRemaining = obj.search_remaining
  const dnaRemaining = obj.dna_remaining
  if (typeof searchRemaining === 'number' && typeof dnaRemaining === 'number') {
    _setAnonUsage({
      searchesRemaining: searchRemaining,
      dnaRemaining: dnaRemaining,
    })
  }
}

/**
 * Fetch the caller's true remaining anon quota from the server. Call once
 * on app mount; subsequent updates happen automatically via POST response
 * piggybacking. Failures are swallowed — the banner falls back to "—" and
 * the next successful POST will resolve it.
 */
export async function fetchAnonUsage(): Promise<void> {
  try {
    const res = await fetch(`${API_BASE}/api/anon-usage`, { credentials: 'include' })
    if (!res.ok) return  // 401 (kill switch) or network error — leave as null
    const body = await res.json() as Partial<AnonUsageResponse>
    // Defensive — only apply when the response has the expected numeric
    // fields. Guards against test environments that stub fetch globally
    // with a different payload, and against future schema drift.
    if (typeof body.search_remaining === 'number' && typeof body.dna_remaining === 'number') {
      _setAnonUsage({
        searchesRemaining: body.search_remaining,
        dnaRemaining: body.dna_remaining,
      })
    }
  } catch {
    // Network failure on mount is non-fatal; the next POST will sync.
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = {
    'Content-Type': 'application/json',
    ...init?.headers,
  }
  // credentials: 'include' lets the browser send cached Basic Auth credentials
  // on cross-origin requests to the admin endpoints (Railway is a different
  // origin from Vercel in production). Anonymous endpoints don't care.
  const res = await fetch(`${API_BASE}${path}`, { ...init, headers, credentials: 'include' })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    const detail = body.detail
    const message = typeof detail === 'string' ? detail : detail?.detail ?? res.statusText
    // On a 429 from the anon quota, the relevant counter is at zero —
    // surface that immediately so the banner reflects reality without a
    // refetch. The other counter is left alone (we don't know its state).
    if (res.status === 429 && typeof detail === 'object' && detail !== null) {
      const limitType = (detail as { limit_type?: string }).limit_type
      if (limitType === 'search' || limitType === 'dna') {
        const current = _anonUsage ?? { searchesRemaining: 0, dnaRemaining: 0 }
        _setAnonUsage({
          searchesRemaining: limitType === 'search' ? 0 : current.searchesRemaining,
          dnaRemaining: limitType === 'dna' ? 0 : current.dnaRemaining,
        })
      }
    }
    throw new ApiError(res.status, message, typeof detail === 'object' ? detail : undefined)
  }

  const body = await res.json() as T
  if (init?.method === 'POST') {
    _updateAnonFromResponse(body)
  }
  return body
}

export interface AnonLimitDetail {
  detail: string
  limit_type: string
  cap: number
  reset_at: string
  upgrade_hint: string
}

export class ApiError extends Error {
  status: number
  data?: AnonLimitDetail
  constructor(status: number, message: string, data?: AnonLimitDetail) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.data = data
  }
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body: unknown, signal?: AbortSignal) =>
    request<T>(path, { method: 'POST', body: JSON.stringify(body), signal }),
  patch: <T>(path: string, body: unknown) =>
    request<T>(path, { method: 'PATCH', body: JSON.stringify(body) }),
  delete: <T>(path: string) => request<T>(path, { method: 'DELETE' }),
}
