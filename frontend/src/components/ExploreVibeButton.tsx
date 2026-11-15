import { Link } from 'react-router-dom'

interface Props {
  /** Gemini-distilled 3-word thematic descriptor (e.g. "Sci-Fi, Neo-noir, Space Western").
   *  Button suppresses itself when this is null/empty/whitespace. */
  vibe: string | null | undefined
  /** Optional className override — defaults to the outlined-accent secondary-CTA
   *  style that mirrors the SEO page's .cta-button-secondary (PR #175). */
  className?: string
}

/**
 * Secondary CTA that routes to Find a Show pre-filled with a show's
 * thematic vibe triplet as the semantic query. Companion to the SEO
 * page button shipped in PR #175 — same copy, same URL contract, same
 * visual language (outlined accent).
 *
 * Renders inside the SPA so navigation uses react-router's Link (preserves
 * client-side routing); the SEO page's variant uses a plain <a> since those
 * pages are standalone HTML.
 */
export default function ExploreVibeButton({ vibe, className }: Props) {
  const trimmed = vibe?.trim()
  if (!trimmed) return null

  const defaultClass =
    'inline-block bg-transparent text-vault-accent font-medium text-sm px-5 py-2 rounded border border-vault-accent hover:bg-vault-accent hover:text-white transition-colors no-underline cursor-pointer'

  // `mode=find` is load-bearing when the user is already on `/try` (e.g.
  // clicking from inside the Encyclopedia widget) — without it,
  // Search.tsx's useEffect at line ~73 hits the "No modeParam = don't
  // change" branch and the activeTab stays on Encyclopedia. The vibe
  // triplet ends up as a dangling `q` param that nothing consumes.
  // Fine on full-route navigations (/ → /try) because the Search
  // useState initializers run fresh on mount, but we can't rely on that
  // from any surface already inside /try.
  return (
    <Link to={`/try?mode=find&q=${encodeURIComponent(trimmed)}`} className={className ?? defaultClass}>
      Explore this vibe
    </Link>
  )
}
