import { Link } from 'react-router-dom'

interface Props {
  /** The detected title (canonical form from the vault, not the raw
   *  user input — so casing/punctuation matches the show as we store it). */
  title: string
  /** Whether the Find-a-Show pool was empty. Drives the copy variant:
   *  empty gets a direct "no matches" framing, non-empty gets a lighter
   *  "you might have meant this" nudge alongside the actual results. */
  isEmpty: boolean
}

/**
 * Small yellow advisory rendered by Find-a-Show when the user's query
 * turns out to be an exact show title. Nudges them toward the Encyclopedia
 * (where a title lookup returns the full profile) without blocking the
 * FaS results. Detection is exact-match only via `detectTitleMatch()` in
 * `lib/offlineEncyclopedia.ts` — see that module for the higher-precision
 * rationale.
 */
export default function TitleLookupHint({ title, isEmpty }: Props) {
  const target = `/try?mode=encyclopedia&q=${encodeURIComponent(title)}`
  return (
    <div
      role="status"
      data-testid="title-lookup-hint"
      className="bg-yellow-950 border border-yellow-700 rounded px-4 py-3 text-yellow-200 text-sm"
    >
      {isEmpty ? (
        <>
          No matches for <span className="font-medium">&quot;{title}&quot;</span> as a
          vibe search. But it looks like a specific show —{' '}
          <Link to={target} className="font-medium underline hover:text-yellow-100">
            look it up in the Encyclopedia →
          </Link>
        </>
      ) : (
        <>
          <span className="font-medium">&quot;{title}&quot;</span> looks like a specific
          show —{' '}
          <Link to={target} className="font-medium underline hover:text-yellow-100">
            look it up in the Encyclopedia
          </Link>{' '}
          for the full profile.
        </>
      )}
    </div>
  )
}
