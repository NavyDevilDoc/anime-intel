import { useState } from 'react'

/**
 * Reference panel of query patterns supported by Intelligent Search.
 *
 * Lives in the left margin of the Find-a-Show search surface (collapsed by
 * default, click to expand). Replaces the standalone "Query Tips" section
 * that used to live on /how-it-works — users get the examples at the point
 * of use (typing a query) rather than behind a navigation hop.
 *
 * Mode-scope: rendered only on the Find a Show tab (Encyclopedia is
 * self-explanatory; Shows Like This has sufficient on-screen text).
 *
 * Panel state resets to collapsed on each page load — no localStorage —
 * to keep the search box prominent on first arrival.
 */

interface Category {
  label: string
  examples: string[]
}

const CATEGORIES: Category[] = [
  {
    label: 'Vibe & Theme',
    examples: ['gritty military drama', 'wholesome found family', 'existential mecha'],
  },
  {
    label: 'Studio Filter',
    examples: ['anything by MAPPA', 'Madhouse anime', 'studio Bones shows'],
  },
  {
    label: 'Quality & Popularity',
    examples: ['mecha with quality above 8', 'underrated romance', 'score between 6.5 and 7.5'],
  },
  {
    label: 'Mood Filter',
    examples: ['comedy with high mood', 'dark anime with negative sentiment'],
  },
  {
    label: 'Plot & Narrative',
    examples: [
      'MC gets rejected and the girl becomes obsessed',
      'protagonist is betrayed by a friend',
      'anime where the hero hides their power',
    ],
  },
  {
    label: 'Character Archetype',
    examples: [
      'cynical loner MC with great monologues',
      'scary-looking protagonist who is secretly kind',
    ],
  },
  {
    label: 'Objective Rankings',
    examples: ['top 10 sports anime', 'best seinen from the 2000s', 'top 5 by Sunrise'],
  },
]

interface Props {
  className?: string
}

export default function QueryTipsPanel({ className = '' }: Props) {
  const [expanded, setExpanded] = useState(false)

  return (
    <div className={className}>
      <div className="flex justify-end">
        <button
          type="button"
          onClick={() => setExpanded(v => !v)}
          aria-expanded={expanded}
          aria-controls="query-tips-content"
          className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded text-sm text-vault-text-muted hover:text-vault-text hover:bg-vault-surface border border-transparent hover:border-vault-border transition-colors cursor-pointer"
        >
          <span className="font-medium">Query Tips</span>
          <span
            aria-hidden="true"
            className={`transition-transform ${expanded ? 'rotate-180' : ''}`}
          >
            ▾
          </span>
        </button>
      </div>
      {expanded && (
        <div
          id="query-tips-content"
          className="mt-3 grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2"
        >
          {CATEGORIES.map(cat => (
            <div
              key={cat.label}
              className="p-2.5 rounded bg-vault-surface border border-vault-border"
            >
              <p className="font-medium text-vault-text text-xs mb-1">{cat.label}</p>
              <p className="text-xs text-vault-text-muted leading-snug">
                {cat.examples.map((ex, i) => (
                  <span key={ex}>
                    {i > 0 && <span className="mx-1 text-vault-text-muted">·</span>}
                    &ldquo;{ex}&rdquo;
                  </span>
                ))}
              </p>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
