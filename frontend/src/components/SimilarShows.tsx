import { useState, useEffect } from 'react'

/**
 * "You might also like" grid on Encyclopedia profile pages.
 *
 * Lazy-loads per-show JSON from /data/similar/{animeId}.json — files
 * are pre-computed at build/ingest time by tools/build_similar_shows.py
 * so there is zero backend involvement (works during hiatus mode) and
 * zero per-visit computation cost. Each file is ~1.2 KB.
 *
 * On card click, invokes onSelectShow(title) so the parent Encyclopedia
 * can trigger a fresh lookup — driving users deeper into the Encyclopedia
 * tab rather than shipping them off to the static SEO page.
 */

interface Recommendation {
  anime_id: number
  title: string
  slug: string
  release_year: number
  mal_score: number
  // personnel_hook: "Also directed by X" / "Music by Y" / "Adapted from a
  // work by Z" — null when reference + candidate share no personnel in
  // the Director / Music / Original Creator tier.
  personnel_hook: string | null
  // thematic_hint: bare comma-separated theme descriptor for the candidate
  // (e.g., "Melancholic, Apocalyptic, Symbolic"). Rendered with a "Theme:"
  // label. Null when the candidate has no distilled thematic_vibe.
  //
  // Copy decision: deliberately no "Similar" in the label — we don't want
  // to overclaim thematic similarity when the connection is really a
  // personnel signal. "Theme:" states what the show IS about and lets
  // the user judge fit.
  thematic_hint: string | null
}

interface SimilarShowsPayload {
  reference_id: number
  reference_title: string
  generated_at: string
  recommendations: Recommendation[]
}

interface Props {
  animeId: number
  onSelectShow: (title: string) => void
}

export default function SimilarShows({ animeId, onSelectShow }: Props) {
  const [recs, setRecs] = useState<Recommendation[] | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(false)

  // Encyclopedia passes `key={animeId}` so a show change remounts this
  // component with fresh initial state — no need to reset in the
  // effect body. Tests that rerender in place without a key still
  // exercise the animeId-dep refetch path (`.then` writes new state).
  useEffect(() => {
    let cancelled = false

    fetch(`/data/similar/${animeId}.json`)
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        return res.json() as Promise<SimilarShowsPayload>
      })
      .then((payload) => {
        if (cancelled) return
        setRecs(payload.recommendations)
        setLoading(false)
      })
      .catch(() => {
        if (cancelled) return
        setError(true)
        setLoading(false)
      })

    return () => {
      cancelled = true
    }
  }, [animeId])

  // Silently hide the section on error — most likely cause is a show
  // added post-ingest without a similar-shows file yet. Better UX to
  // omit the section than to display an error users can't act on.
  if (error || (recs && recs.length === 0)) {
    return null
  }

  return (
    <div className="border-t border-vault-border pt-5">
      <h4 className="text-vault-accent font-medium text-sm mb-3">
        You might also like
      </h4>
      {loading ? (
        <div className="text-sm text-vault-text-muted">Loading recommendations...</div>
      ) : (
        <div
          className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3"
          data-testid="similar-shows-grid"
        >
          {recs?.map((rec) => {
            // Line-composition rules:
            //   1. Primary line always shows mal_score + a descriptor.
            //   2. If personnel_hook is set, it's the primary descriptor.
            //      Below it we ALSO show "Theme: {thematic_hint}" so the
            //      user sees both the creative connection AND the show's
            //      own theme without us asserting thematic similarity.
            //   3. If personnel_hook is null, thematic_hint alone becomes
            //      the primary descriptor (as "Theme: {hint}"). No
            //      duplicated secondary line.
            //   4. If neither is set (rare), we show just score + title.
            const hasPersonnel = rec.personnel_hook !== null
            const primaryText = hasPersonnel
              ? rec.personnel_hook
              : rec.thematic_hint
                ? `Theme: ${rec.thematic_hint}`
                : null
            const secondaryText = hasPersonnel && rec.thematic_hint
              ? `Theme: ${rec.thematic_hint}`
              : null
            return (
              <button
                key={rec.anime_id}
                type="button"
                onClick={() => onSelectShow(rec.title)}
                className="text-left bg-vault-bg/50 rounded px-3 py-2 hover:bg-vault-bg transition-colors border border-transparent hover:border-vault-border focus:outline-none focus:border-vault-accent"
                data-testid="similar-shows-card"
              >
                <div className="flex items-baseline gap-2">
                  <span className="font-medium text-sm">{rec.title}</span>
                  <span className="text-xs text-vault-text-muted">
                    ({rec.release_year || 'Unknown'})
                  </span>
                </div>
                <div className="text-xs mt-1 flex items-center gap-2">
                  <span className="text-yellow-400 font-bold">{rec.mal_score}</span>
                  {primaryText && (
                    <>
                      <span className="text-vault-text-muted">·</span>
                      <span className="text-vault-text-muted italic">{primaryText}</span>
                    </>
                  )}
                </div>
                {secondaryText && (
                  <div className="text-xs mt-0.5 text-vault-text-muted italic">
                    {secondaryText}
                  </div>
                )}
              </button>
            )
          })}
        </div>
      )}
    </div>
  )
}
