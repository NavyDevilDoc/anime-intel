import { Link } from 'react-router-dom'

/**
 * VaultStatsStrip — Landing Redesign PR #3.
 *
 * Below-fold marketing surface: four stat cards + the "72/28" quadrant
 * pull-quote from HowItWorks. Cards link to specific HowItWorks anchors
 * so users can dive into the underlying explanation without hunting.
 *
 * Numbers are hardcoded intentionally. The vault grows seasonally (one
 * major ingest per season), so refreshing four numbers by hand every
 * couple months is cheaper than the fetch + loading-state + cache-
 * invalidation complexity of a live /api/vault-stats endpoint. Refresh
 * cadence: quarterly, or whenever a major initiative (ingest sweep,
 * enrichment pass) changes a number by more than ~5%.
 *
 * Verified against production DB 2026-09-30 (post-PR-148 vault):
 *   Vault size:            6,428  (COUNT(*) FROM anime_info)
 *   Consensus profiles:    6,427  (~100% coverage)
 *   Multi-entry franchises:  955  (series_id clusters with 2+ entries)
 *   Staff attributions:   28,162  (COUNT(*) FROM anime_staff)
 *   Reviews synthesized:  ~200K  (ceiling 6,428 × 50 = 321K; avg
 *                                  ~30-40 realistic per PR #143 Phase A)
 *
 * Feature flag: VITE_LANDING_STATS_STRIP_ENABLED. Also gates the
 * quadrant blockquote per plan (keeps landing flag count manageable).
 */

interface StatCard {
  value: string
  label: string
  detail: string
  anchor: string  // /how-it-works#<id> — headings have matching id attributes
}

const STATS: readonly StatCard[] = [
  {
    value: '6,428',
    label: 'anime titles',
    detail: 'TV, films, OVAs, specials — 1990 to today',
    anchor: '/how-it-works#vault',
  },
  {
    value: '200,000+',
    label: 'reviews synthesized',
    detail: 'distilled into per-show consensus profiles',
    anchor: '/how-it-works#scoring',
  },
  {
    value: '955',
    label: 'franchises',
    detail: 'multi-entry series unified across sequels + films + OVAs',
    anchor: '/how-it-works#vault',
  },
  {
    value: '28,162',
    label: 'staff credits',
    detail: 'director, composer, creator, series composition, character design',
    anchor: '/how-it-works#vault',
  },
]

export default function VaultStatsStrip() {
  const enabled = import.meta.env.VITE_LANDING_STATS_STRIP_ENABLED === 'true'
  if (!enabled) return null

  return (
    <section
      data-testid="vault-stats-strip"
      className="mx-auto mt-12 max-w-5xl px-4"
    >
      {/* Quadrant pull-quote — the HowItWorks audit called this the
          strongest single asset. Reframed for the landing after two
          copy iterations: the original quality-vs-popularity framing
          leaned on the chart directly above it in HowItWorks, and a
          simpler "popularity != quality" rewrite got contradicted by
          any show like Bleach / Naruto / MHA / Frieren (popular AND
          great). Final framing: popularity DROWNS OUT equally-great
          obscure shows, and the recommender surfaces the signal buried
          under that hype. Matches the actual product thesis. */}
      <Link
        to="/how-it-works#scoring"
        data-testid="vault-stats-quadrant-blockquote"
        className="block mb-8 no-underline"
      >
        <blockquote className="border-l-4 border-vault-accent pl-4 py-2 text-lg italic text-vault-text hover:text-vault-accent transition-colors">
          Popular anime and the hype around them tend to drown out
          equally great shows that never got the same marketing push.
          The community is vocal about what works — and what doesn't.
          Follow that signal and you might uncover buried treasure.
          <span className="mt-2 block text-xs not-italic text-vault-text-muted">
            → see what the data actually shows
          </span>
        </blockquote>
      </Link>

      <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
        {STATS.map(stat => (
          <Link
            key={stat.label}
            to={stat.anchor}
            className="group rounded border border-vault-border bg-vault-surface p-4 no-underline transition-colors hover:border-vault-accent"
          >
            <div className="text-3xl font-extrabold text-vault-accent group-hover:text-vault-accent-hover">
              {stat.value}
            </div>
            <div className="mt-1 text-sm font-medium text-vault-text">
              {stat.label}
            </div>
            <div className="mt-2 text-xs text-vault-text-muted">
              {stat.detail}
            </div>
          </Link>
        ))}
      </div>
    </section>
  )
}
