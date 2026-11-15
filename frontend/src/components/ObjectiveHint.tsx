import { Link } from 'react-router-dom'

interface Props {
  /** Fired when the user clicks "Switch to Objective Rankings". Parent
   *  is expected to setLens('Objective Rankings'); the query text
   *  survives the mode switch on its own. */
  onSwitchToObjectiveRankings: () => void
}

/**
 * Yellow advisory rendered by Find-a-Show when the user's Intelligent
 * Search query has objective-ranking shape (see lib/objectiveDetect.ts
 * for the firing rule). Non-blocking — IS still runs and returns vibe
 * results below; the hint just points the user at two more precise
 * surfaces they may not have known about:
 *
 *   1. Objective Rankings mode (same page, one click) — deterministic
 *      SQL sort by Bayesian quality with year / genre / top-N filters.
 *   2. Top Lists (/top) — pre-computed curated lists for
 *      genre-by-decade browsing.
 */
export default function ObjectiveHint({ onSwitchToObjectiveRankings }: Props) {
  return (
    <div
      role="status"
      data-testid="objective-hint"
      className="bg-yellow-950 border border-yellow-700 rounded px-4 py-3 text-yellow-200 text-sm"
    >
      This looks like a ranking query.{' '}
      <button
        type="button"
        onClick={onSwitchToObjectiveRankings}
        className="font-medium underline hover:text-yellow-100 bg-transparent border-none p-0 cursor-pointer text-yellow-200"
      >
        Switch to Objective Rankings
      </button>{' '}
      for precise top-N results, or{' '}
      <Link to="/top" className="font-medium underline hover:text-yellow-100">
        browse the Top Lists →
      </Link>
    </div>
  )
}
