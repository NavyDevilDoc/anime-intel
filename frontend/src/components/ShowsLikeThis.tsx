import { useState, useEffect, useRef, type FormEvent } from 'react'
import { api, ApiError, type AnonLimitDetail } from '../lib/api'
import type { DNAResponse } from '../lib/types'
import ResultCard from './ResultCard'
import AnonLimitModal from './AnonLimitModal'
import LowConfidenceBanner from './LowConfidenceBanner'
import Spinner from './Spinner'

// Build-time rollback flag for the negative-anchor input row. Enabled by
// default; set VITE_DNA_NEGATIVE_ANCHORS_ENABLED=false in Vercel env vars
// to hide the row without redeploying the backend.
const NEGATIVE_ANCHORS_ENABLED =
  import.meta.env.VITE_DNA_NEGATIVE_ANCHORS_ENABLED !== 'false'

// Engine hiatus copy. Defensive early-return: the mode-picker tabs are
// normally disabled while hiatus is on, but direct-URL access to
// /try?mode=dna still routes here.
const HIATUS_MESSAGE =
  'The recommendation engine is on hiatus while we work on improvements ' +
  'to retrieval quality. The anime encyclopedia remains fully available.'

interface ShowsLikeThisProps {
  onShowClick?: (title: string) => void
  seedTitle?: string
  onSeedConsumed?: () => void
  // MAL IDs from the watchlist panel's localStorage cache. Threaded
  // into the DNA request as `exclude_mal_ids`; the engine cluster-
  // expands them server-side.
  excludeMalIds?: number[] | null
}

export default function ShowsLikeThis({ onShowClick, seedTitle, onSeedConsumed, excludeMalIds }: ShowsLikeThisProps) {
  const [titles, setTitles] = useState(['', '', ''])
  const [negativeAnchors, setNegativeAnchors] = useState(['', ''])
  const [results, setResults] = useState<DNAResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [lowConfidence, setLowConfidence] = useState(false)
  // Resolution notes from a failed-result response (success=false) are
  // surfaced alongside the error so the user can see *why* the failure
  // happened (e.g., their input was substituted, then the substitute
  // produced no matches). Cleared on every new request.
  const [errorResolutionNotes, setErrorResolutionNotes] = useState<string[] | null>(null)
  const [anonLimit, setAnonLimit] = useState<AnonLimitDetail | null>(null)
  const [elapsed, setElapsed] = useState<number | null>(null)
  const abortRef = useRef<AbortController | null>(null)

  const setTitle = (idx: number, val: string) => {
    setTitles(prev => prev.map((t, i) => (i === idx ? val : t)))
  }

  const setNegative = (idx: number, val: string) => {
    setNegativeAnchors(prev => prev.map((t, i) => (i === idx ? val : t)))
  }

  const performDnaSearch = async (activeTitles: string[], activeNegatives: string[] = []) => {
    abortRef.current?.abort()
    const controller = new AbortController()
    abortRef.current = controller

    setLoading(true)
    setError('')
    setErrorResolutionNotes(null)
    setLowConfidence(false)
    setElapsed(null)
    const t0 = performance.now()
    try {
      const payload: {
        titles: string[]
        negative_anchors?: string[]
        exclude_mal_ids?: number[]
      } = { titles: activeTitles }
      if (activeNegatives.length > 0) {
        payload.negative_anchors = activeNegatives
      }
      if (excludeMalIds && excludeMalIds.length > 0) {
        payload.exclude_mal_ids = excludeMalIds
      }
      const res = await api.post<DNAResponse>('/api/dna', payload, controller.signal)
      // low_confidence flows alongside both success and failure paths.
      setLowConfidence(res.low_confidence === true)
      if (!res.success) {
        // Yellow advisory replaces the red error block when the engine
        // signals weak confidence; title_resolution_notes still surface
        // separately because they're a different category of disclosure
        // (catalog-substitution transparency, not "I don't know").
        if (res.low_confidence !== true) {
          setError(res.error ?? 'DNA triangulation failed')
        }
        setErrorResolutionNotes(res.title_resolution_notes ?? null)
        setResults(null)
      } else {
        setResults(res)
        setElapsed((performance.now() - t0) / 1000)
      }
    } catch (err) {
      if (err instanceof DOMException && err.name === 'AbortError') {
        return
      }
      if (err instanceof ApiError && err.status === 429 && err.data?.limit_type) {
        setAnonLimit(err.data)
      } else {
        setError(err instanceof Error ? err.message : 'DNA triangulation failed')
      }
      setResults(null)
    } finally {
      if (!controller.signal.aborted) {
        setLoading(false)
      }
    }
  }

  const handleStop = () => {
    abortRef.current?.abort()
    abortRef.current = null
    setLoading(false)
  }

  // Consume seed title from parent (e.g., landing-page hero with mode=dna).
  // Pre-fills the first reference slot but does NOT auto-execute — the
  // 3 reference slots + 2 negative anchors are the whole point of this
  // mode, so the user completes their multi-anchor input at their own
  // pace before consuming their daily SLT quota.
  useEffect(() => {
    if (seedTitle) {
      setTitles([seedTitle, '', ''])
      onSeedConsumed?.()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [seedTitle])

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault()
    const activeTitles = titles.map(t => t.trim()).filter(Boolean)
    if (activeTitles.length === 0) return
    const activeNegatives = NEGATIVE_ANCHORS_ENABLED
      ? negativeAnchors.map(t => t.trim()).filter(Boolean)
      : []
    await performDnaSearch(activeTitles, activeNegatives)
  }

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
        <h2 className="text-xl font-bold mb-1">Find Similar Shows</h2>
        <p className="text-sm text-vault-text-muted">
          Enter up to three shows. The engine will extract their core thematic DNA and find matches.
        </p>
      </div>

      <form onSubmit={handleSubmit} className="space-y-4">
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
          {['Target 1 (Required)', 'Target 2 (Optional)', 'Target 3 (Optional)'].map((label, i) => (
            <div key={i}>
              <label htmlFor={`dna-target-${i}`} className="block text-sm text-vault-text-muted mb-1">{label}</label>
              <div className="relative">
                <input
                  id={`dna-target-${i}`}
                  type="text"
                  value={titles[i]}
                  onChange={e => setTitle(i, e.target.value)}
                  placeholder={i === 0 ? "e.g., '86'" : ''}
                  maxLength={100}
                  required={i === 0}
                  className="w-full px-3 py-2 pr-9 rounded bg-vault-surface border border-vault-border text-vault-text focus:outline-none focus:border-vault-accent"
                />
                {titles[i] && (
                  <button
                    type="button"
                    onClick={() => setTitle(i, '')}
                    aria-label={`Clear ${label}`}
                    className="absolute right-2 top-1/2 -translate-y-1/2 px-1.5 text-vault-text-muted hover:text-vault-text bg-transparent border-none cursor-pointer text-lg leading-none"
                  >
                    &times;
                  </button>
                )}
              </div>
            </div>
          ))}
        </div>

        {NEGATIVE_ANCHORS_ENABLED && (
          <div>
            <p className="text-sm text-vault-text-muted mb-2">
              <span className="font-medium">...but not like these</span>{' '}
              (optional — up to 2 shows you want the results to avoid resembling)
            </p>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              {['Avoid 1', 'Avoid 2'].map((label, i) => (
                <div key={i}>
                  <label htmlFor={`dna-avoid-${i}`} className="sr-only">{label}</label>
                  <div className="relative">
                    <input
                      id={`dna-avoid-${i}`}
                      type="text"
                      value={negativeAnchors[i]}
                      onChange={e => setNegative(i, e.target.value)}
                      placeholder={label}
                      maxLength={100}
                      className="w-full px-3 py-2 pr-9 rounded bg-vault-surface border border-vault-border text-vault-text focus:outline-none focus:border-vault-accent"
                    />
                    {negativeAnchors[i] && (
                      <button
                        type="button"
                        onClick={() => setNegative(i, '')}
                        aria-label={`Clear ${label}`}
                        className="absolute right-2 top-1/2 -translate-y-1/2 px-1.5 text-vault-text-muted hover:text-vault-text bg-transparent border-none cursor-pointer text-lg leading-none"
                      >
                        &times;
                      </button>
                    )}
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        <div className="flex gap-3">
          <button
            type="submit"
            disabled={loading || !titles[0].trim()}
            className="flex-1 py-2.5 rounded bg-vault-accent text-white font-medium hover:bg-vault-accent-hover disabled:opacity-50 transition-colors border-none cursor-pointer"
          >
            {loading ? (
              <span className="inline-flex items-center justify-center gap-2">
                <Spinner /> Synthesizing DNA...
              </span>
            ) : (
              'Synthesize DNA'
            )}
          </button>
          {loading && (
            <button
              type="button"
              onClick={handleStop}
              className="px-5 py-2.5 rounded bg-red-800 text-white font-medium hover:bg-red-700 transition-colors border-none cursor-pointer"
            >
              Stop
            </button>
          )}
        </div>
      </form>

      {lowConfidence && (
        <LowConfidenceBanner
          watchlistExclusionEmptied={
            !!excludeMalIds && excludeMalIds.length > 0 &&
            (!results || results.data.length === 0)
          }
          fallbackMode={results?.fallback_mode ?? null}
        />
      )}

      {errorResolutionNotes && errorResolutionNotes.length > 0 && (
        <div
          className="bg-yellow-950 border border-yellow-700 rounded px-4 py-3 text-yellow-200 text-sm space-y-1"
          data-testid="resolution-notes-banner-error"
        >
          {errorResolutionNotes.map((note, i) => (
            <div key={i}>{note}</div>
          ))}
        </div>
      )}

      {error && (
        <div className="bg-red-950 border border-red-800 rounded px-4 py-3 text-red-200">
          {error}
        </div>
      )}

      {anonLimit && (
        <AnonLimitModal detail={anonLimit} onClose={() => setAnonLimit(null)} />
      )}

      {results && (
        <div className="space-y-4">
          {results.title_resolution_notes && results.title_resolution_notes.length > 0 && (
            <div
              className="bg-yellow-950 border border-yellow-700 rounded px-4 py-3 text-yellow-200 text-sm space-y-1"
              data-testid="resolution-notes-banner"
            >
              {results.title_resolution_notes.map((note, i) => (
                <div key={i}>{note}</div>
              ))}
            </div>
          )}

          {results.intersection_summary && (
            <div className="bg-blue-950 border border-blue-700 rounded px-4 py-3 text-blue-100">
              <span className="font-medium">DNA Synthesis Complete: </span>
              {results.intersection_summary}
            </div>
          )}

          {results.negative_anchors_used && results.negative_anchors_used.length > 0 && (
            <div className="flex flex-wrap gap-2 items-center" data-testid="avoiding-chips">
              <span className="text-sm text-vault-text-muted">Avoiding:</span>
              {results.negative_anchors_used.map((title) => (
                <span
                  key={title}
                  className="inline-block px-2 py-1 bg-red-950 border border-red-800 rounded text-xs text-red-200"
                >
                  {title}
                </span>
              ))}
            </div>
          )}

          <p className="text-sm text-vault-success">
            {results.data.length} match{results.data.length !== 1 ? 'es' : ''} found{elapsed !== null ? ` in ${elapsed.toFixed(1)}s` : ''}.
          </p>

          {results.data.map((r, i) => (
            <ResultCard key={`${r.profile.id}-${i}`} result={r} showConfidence onShowClick={onShowClick} fallbackMode={results.fallback_mode ?? null} />
          ))}
        </div>
      )}
    </div>
  )
}
