import { Link } from 'react-router-dom'

interface Props {
  /** Fired when the user clicks "Switch to Intelligent Search". Parent
   *  is expected to setLens('Intelligent Search'); the query text
   *  survives the mode switch on its own (same pattern as the
   *  ObjectiveHint symmetric sibling). */
  onSwitchToIntelligentSearch: () => void
}

/**
 * Yellow advisory rendered by Find-a-Show when the user's Objective
 * Rankings query has vibe-shape (see lib/intelligentDetect.ts for the
 * firing rule). Non-blocking — Objective Rankings still runs and
 * returns SQL-sorted results below; the hint just points the user at
 * two more precise surfaces they may not have known about:
 *
 *   1. Intelligent Search mode (same page, one click) — AI-powered
 *      semantic matching with reranker-driven reasoning.
 *   2. Shows Like This (/try?mode=dna) — if the user has a specific
 *      title in mind rather than a pure vibe.
 *
 * Symmetric mirror of ObjectiveHint — same visual language, same
 * two-link structure, opposite trigger + opposite target. Keeping the
 * UX pattern aligned so returning users recognize the advisory shape
 * instantly.
 */
export default function IntelligentSearchHint({ onSwitchToIntelligentSearch }: Props) {
  return (
    <div
      role="status"
      data-testid="intelligent-search-hint"
      className="bg-yellow-950 border border-yellow-700 rounded px-4 py-3 text-yellow-200 text-sm"
    >
      This looks like a vibe query.{' '}
      <button
        type="button"
        onClick={onSwitchToIntelligentSearch}
        className="font-medium underline hover:text-yellow-100 bg-transparent border-none p-0 cursor-pointer text-yellow-200"
      >
        Switch to Intelligent Search
      </button>{' '}
      for AI-powered reasoning, or try{' '}
      <Link to="/try?mode=dna" className="font-medium underline hover:text-yellow-100">
        Shows Like This →
      </Link>{' '}
      if you have a specific title in mind.
    </div>
  )
}
