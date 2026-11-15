import { useState, type FormEvent } from 'react'
import { Link, useNavigate } from 'react-router-dom'

/**
 * MarketingHero — Landing Redesign PR #2.
 *
 * The redesigned marketing-shaped landing hero. Replaces the current
 * <LandingHero> tab-selector layout with soft progressive disclosure
 * (COA C from the 2026-09-28 design conversation):
 *
 *   - Prominent Find-a-Show input only (no tab bar on the landing)
 *   - 5 example query chips that navigate to /try?q=<chip>
 *   - Soft "Also try:" text links to the other three surfaces
 *
 * The full tab-selector layout continues to live on /try (Search.tsx
 * with skipHero) so power users retain the familiar bar. The old
 * <LandingHero> is preserved as the flag-off + hiatus fallback until
 * the new hero fully ships; the deletion-and-rename happens after the
 * flag permanently flips on.
 *
 * Feature flag lives at the caller (Search.tsx) — this component itself
 * always renders when invoked. Keeps the flag-off return simple: don't
 * invoke.
 *
 * Copy is intentionally draft-tier — tune during Phase 7 dev-server
 * eyeball rather than iterating on strings pre-implementation.
 */

const HEADLINE = 'Anime recommendations that understand what you want to watch.'
const SUBHEADLINE = 'Get thoughtful matches with real reasoning — powered by the full force of the anime community.'

// Chips exercise the semantic-search strengths the engine was built for:
// vibe-first, subject-nuanced, decade-scoped. Kept to 5 so the hero
// doesn't sprawl.
const EXAMPLE_CHIPS: readonly string[] = [
  'slow-burn romance with complex worldbuilding',
  'hidden gems from the 2000s',
  'psychological thrillers where the villain has a point',
  'cozy fantasy without romance',
  'philosophical mecha with soul',
]

// Also-try links map to the other three surfaces. Uses ?mode= (not
// ?tab=) so the /try Search.tsx URL-param branch resolves them to the
// right active tab on landing.
const ALSO_TRY_LINKS: ReadonlyArray<{ label: string; to: string }> = [
  { label: 'Shows Like This', to: '/try?mode=dna' },
  { label: 'Encyclopedia', to: '/try?mode=encyclopedia' },
  { label: 'Top Lists', to: '/top' },
]

export default function MarketingHero() {
  const navigate = useNavigate()
  const [query, setQuery] = useState('')

  const handleSubmit = (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault()
    const trimmed = query.trim()
    navigate(trimmed ? `/try?q=${encodeURIComponent(trimmed)}` : '/try')
  }

  const handleChipClick = (chip: string) => {
    navigate(`/try?q=${encodeURIComponent(chip)}`)
  }

  return (
    <div className="mx-auto flex max-w-3xl flex-col items-center gap-6 py-16 px-4 text-center">
      <h1
        data-testid="marketing-hero-headline"
        className="text-3xl font-extrabold tracking-tight text-vault-text sm:text-4xl md:text-5xl"
      >
        {HEADLINE}
      </h1>

      <p
        data-testid="marketing-hero-subheadline"
        className="max-w-2xl text-base text-vault-text-muted sm:text-lg"
      >
        {SUBHEADLINE}
      </p>

      <form onSubmit={handleSubmit} className="mt-4 flex w-full max-w-2xl flex-col gap-2 sm:flex-row">
        <input
          type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Describe a vibe, theme, or show you'd like..."
          aria-label="Search anime"
          className="flex-1 rounded border border-vault-border bg-vault-surface px-4 py-3 text-vault-text placeholder-vault-text-muted transition-colors focus:outline-none focus:border-vault-accent"
        />
        <button
          type="submit"
          className="rounded border-none bg-vault-accent px-6 py-3 font-medium text-white transition-colors hover:bg-vault-accent-hover cursor-pointer"
        >
          Find a Show
        </button>
      </form>

      <div
        data-testid="marketing-hero-chips"
        className="flex flex-wrap justify-center gap-2 pt-2"
      >
        {EXAMPLE_CHIPS.map(chip => (
          <button
            key={chip}
            type="button"
            onClick={() => handleChipClick(chip)}
            className="rounded-full border border-vault-border bg-vault-surface px-3 py-1.5 text-xs text-vault-text-muted transition-colors hover:border-vault-accent hover:text-vault-accent cursor-pointer"
          >
            "{chip}"
          </button>
        ))}
      </div>

      <div className="pt-4 text-sm text-vault-text-muted">
        <span>Also try: </span>
        {ALSO_TRY_LINKS.map((link, i) => (
          <span key={link.to}>
            <Link
              to={link.to}
              className="text-vault-accent hover:underline"
            >
              {link.label}
            </Link>
            {i < ALSO_TRY_LINKS.length - 1 && <span className="mx-2">·</span>}
          </span>
        ))}
      </div>
    </div>
  )
}
