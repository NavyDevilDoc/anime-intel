import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'

/**
 * HowRecommendationsWork — Landing Redesign PR #3.
 *
 * Three-step horizontal visual explaining the retrieval -> rerank ->
 * reason pipeline. Distilled from HowItWorks' "Ranking" section. Copy
 * intentionally punchy — full detail lives on /how-it-works#ranking.
 *
 * Icons are inline SVG (no icon library dependency; matches the rest of
 * the codebase which uses inline SVG / emoji rather than pulling
 * lucide/heroicons/etc).
 *
 * Feature flag: VITE_LANDING_HOW_STRIP_ENABLED.
 */

interface Step {
  number: string
  title: string
  description: string
  icon: ReactNode
}

const STEPS: readonly Step[] = [
  {
    number: '1',
    title: 'Retrieve',
    description:
      'Semantic search finds candidates by meaning. "Existential mecha" surfaces shows that feel existential and involve mecha — even if those exact words never appear in any description.',
    icon: (
      // Magnifying glass — retrieval
      <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"
           stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
           className="h-6 w-6" aria-hidden="true">
        <circle cx="11" cy="11" r="7" />
        <line x1="16" y1="16" x2="21" y2="21" />
      </svg>
    ),
  },
  {
    number: '2',
    title: 'Rerank',
    description:
      'Gemini reads each candidate\'s full community profile — thematic vibe, pros, cons, controversy — and scores it against your specific query. Different query types get different reranker treatments.',
    icon: (
      // Sliders — reranking / weighting
      <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"
           stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
           className="h-6 w-6" aria-hidden="true">
        <line x1="4" y1="6" x2="20" y2="6" />
        <circle cx="9" cy="6" r="2" fill="currentColor" />
        <line x1="4" y1="12" x2="20" y2="12" />
        <circle cx="15" cy="12" r="2" fill="currentColor" />
        <line x1="4" y1="18" x2="20" y2="18" />
        <circle cx="11" cy="18" r="2" fill="currentColor" />
      </svg>
    ),
  },
  {
    number: '3',
    title: 'Reason',
    description:
      'Every result explains itself: one pro that matches your query, one con that speaks to what you asked for. You see why it matches AND what might not work — in the same glance.',
    icon: (
      // Speech bubble — reasoning / explanation
      <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"
           stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
           className="h-6 w-6" aria-hidden="true">
        <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
      </svg>
    ),
  },
]

export default function HowRecommendationsWork() {
  const enabled = import.meta.env.VITE_LANDING_HOW_STRIP_ENABLED === 'true'
  if (!enabled) return null

  return (
    <section
      data-testid="how-recommendations-work"
      className="mx-auto mt-12 max-w-5xl px-4"
    >
      <div className="mb-6 text-center">
        <h2 className="text-2xl font-bold text-vault-text">
          How recommendations work
        </h2>
        <p className="mt-1 text-sm text-vault-text-muted">
          Every search runs through two AI layers before a result reaches you.
        </p>
      </div>

      <div className="grid grid-cols-1 gap-6 md:grid-cols-3">
        {STEPS.map(step => (
          <div
            key={step.number}
            className="rounded border border-vault-border bg-vault-surface p-5"
          >
            <div className="mb-3 flex items-center gap-3">
              <div className="flex h-10 w-10 items-center justify-center rounded-full bg-vault-accent/10 text-vault-accent">
                {step.icon}
              </div>
              <div>
                <div className="text-xs font-mono text-vault-text-muted">
                  Step {step.number}
                </div>
                <div className="text-lg font-semibold text-vault-text">
                  {step.title}
                </div>
              </div>
            </div>
            <p className="text-sm text-vault-text-muted">
              {step.description}
            </p>
          </div>
        ))}
      </div>

      <div className="mt-4 text-center">
        {/* Link targets the Consensus Distillation section rather than
            Ranking — the pipeline visual above describes retrieval,
            but the "why" that makes the engine interesting is the
            per-show consensus profiles it reranks against. That's the
            more compelling deep-dive destination. */}
        <Link
          to="/how-it-works#consensus"
          data-testid="how-recommendations-work-detail-link"
          className="text-sm text-vault-accent hover:underline"
        >
          Full detail: how we distill 200,000+ reviews into per-show consensus profiles →
        </Link>
      </div>
    </section>
  )
}
