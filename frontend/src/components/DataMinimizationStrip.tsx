import { Link } from 'react-router-dom'

/**
 * DataMinimizationStrip — Landing Redesign PR #3.
 *
 * Small trust-signal band below HowRecommendationsWork. Four short
 * claims rendered as pill-style tags with an anchor into HowItWorks'
 * Data Minimization section for the full contract.
 *
 * Copy note: the plan's original draft included "No cover art
 * hotlinking" which became inaccurate after PR #1 shipped MAL CDN
 * hotlinking. Corrected copy explicitly names the hotlinking practice
 * with attribution — matches ecosystem norms and doesn't try to hide
 * what we do.
 *
 * Feature flag: VITE_LANDING_MINIMIZATION_STRIP_ENABLED.
 */

const CLAIMS: readonly string[] = [
  'No accounts',
  'No ad tracking',
  'Salted-hash-only telemetry',
  'Poster art hotlinked from MAL with attribution',
]

export default function DataMinimizationStrip() {
  const enabled = import.meta.env.VITE_LANDING_MINIMIZATION_STRIP_ENABLED === 'true'
  if (!enabled) return null

  return (
    <section
      data-testid="data-minimization-strip"
      className="mx-auto mt-12 max-w-5xl px-4"
    >
      <div className="rounded border border-vault-border bg-vault-surface p-5 text-center">
        <div className="mb-3 text-xs font-medium uppercase tracking-wide text-vault-text-muted">
          Privacy at a glance
        </div>

        <div className="flex flex-wrap justify-center gap-2">
          {CLAIMS.map(claim => (
            <span
              key={claim}
              className="rounded-full border border-vault-border bg-vault-bg px-3 py-1 text-xs text-vault-text"
            >
              {claim}
            </span>
          ))}
        </div>

        <div className="mt-4">
          <Link
            to="/how-it-works#data-minimization"
            data-testid="data-minimization-strip-detail-link"
            className="text-xs text-vault-accent hover:underline"
          >
            Full data-handling contract →
          </Link>
        </div>
      </div>
    </section>
  )
}
