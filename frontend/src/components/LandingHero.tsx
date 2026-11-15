import { useState, useEffect, type FormEvent } from 'react'
import { Link, useNavigate } from 'react-router-dom'

const API_BASE = import.meta.env.VITE_API_BASE_URL as string ?? ''

// Engine hiatus flag. When true, greys out Find-a-Show + Shows-Like-This
// tab buttons, shows an advisory notice, and defaults the active mode to
// Encyclopedia (the only tab that remains functional). Set to `true` in
// Vercel production env during the offline transition; leave unset locally
// so `npm run dev` retains full functionality. Read at render time (not
// module load) so tests can vi.stubEnv without cache gymnastics.

const HIATUS_MESSAGE =
  'The recommendation engine is on hiatus while we work on improvements ' +
  'to retrieval quality. The anime encyclopedia below remains fully available.'

type Mode = 'find' | 'dna' | 'encyclopedia'
const HIATUS_DISABLED_MODES: Mode[] = ['find', 'dna']

const MODE_CONFIG: Record<Mode, {
  label: string
  placeholder: string
  buttonText: string
  cardTitle: string
  cardDesc: string
  samples: string[]
  samplesIntro: string
  routeFor: (q: string) => string
}> = {
  find: {
    label: 'Find a Show',
    placeholder: "Describe a vibe, theme, or show you'd like...",
    // Button text matches the tab label rather than the generic
    // "Search" — makes the tab→outcome link explicit ("this button
    // will do the Find a Show thing") rather than leaving users to
    // infer it. Same rationale for the other two modes below.
    buttonText: 'Find a Show',
    cardTitle: 'Find a Show',
    cardDesc: 'Describe a vibe, theme, or mood. The AI finds matching shows from 6,000+ titles.',
    samples: ['gritty military drama', 'cozy slice of life', 'top sports anime from 2023'],
    samplesIntro: 'Try queries like:',
    routeFor: (q: string) => q ? `/try?q=${encodeURIComponent(q)}` : '/try',
  },
  dna: {
    label: 'Shows Like This',
    placeholder: 'Enter a show you already like...',
    buttonText: 'Find Similar Shows',
    cardTitle: 'Shows Like This',
    cardDesc: 'Enter a show you love and we surface others with similar vibes, themes, and tone.',
    samples: ['Attack on Titan', 'Spy x Family', 'Steins;Gate'],
    samplesIntro: 'Try a show like:',
    routeFor: (q: string) => q ? `/try?mode=dna&q=${encodeURIComponent(q)}` : '/try?mode=dna',
  },
  encyclopedia: {
    label: 'Encyclopedia',
    placeholder: 'Look up any anime by title...',
    buttonText: 'Look Up',
    cardTitle: 'Encyclopedia',
    // cardDesc is composed dynamically below from animeCount so the count
    // stays in sync with the "N titles indexed." headline. Placeholder kept
    // for shape parity with the other modes.
    cardDesc: '',
    samples: ['Cowboy Bebop', 'Death Note', 'Frieren'],
    samplesIntro: 'Try looking up:',
    routeFor: (q: string) => q ? `/try?mode=encyclopedia&q=${encodeURIComponent(q)}` : '/try?mode=encyclopedia',
  },
}

const MODES: Mode[] = ['find', 'dna', 'encyclopedia']

// Top Lists sits alongside the three form-driven Modes in the tab bar but
// is structurally different: clicking it navigates to /top rather than
// switching the input placeholder. Same pattern as Search.tsx's
// DiscoveryTabs. Tracked as a separate constant so the click handler can
// branch on it without threading a fourth Mode enum entry into the form
// logic (Top Lists has no query, no button, no samples).
const TOP_LISTS_TAB = {
  id: 'toplists' as const,
  label: 'Top Lists',
  route: '/top',
  cardTitle: 'Top Lists',
  cardDesc:
    'Browse curated rankings by genre and decade — 79 pre-built lists, one entry per franchise.',
}

export default function LandingHero() {
  const hiatusMode = import.meta.env.VITE_ENGINE_HIATUS_MODE === 'true'
  const navigate = useNavigate()
  const [animeCount, setAnimeCount] = useState<number | null>(null)
  const [query, setQuery] = useState('')
  const [mode, setMode] = useState<Mode>(hiatusMode ? 'encyclopedia' : 'find')

  useEffect(() => {
    fetch(`${API_BASE}/api/health`)
      .then(res => res.json())
      .then(data => setAnimeCount(data.anime_count))
      .catch(() => {}) // Silently fail — fallback text handles it
  }, [])

  const handleSubmit = (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault()
    navigate(MODE_CONFIG[mode].routeFor(query.trim()))
  }

  const config = MODE_CONFIG[mode]

  return (
    <div className="flex flex-col items-center justify-center py-20 space-y-8 text-center">
      <h1 className="text-4xl font-extrabold tracking-tight sm:text-5xl">
        AI-Powered Anime Discovery
      </h1>

      <p className="max-w-xl text-lg text-vault-text-muted">
        Find your next favorite show using semantic search, community intelligence,
        and AI reranking.{' '}
        {animeCount
          ? `${animeCount.toLocaleString()} titles indexed.`
          : 'Thousands of titles indexed.'}
      </p>

      <div className="w-full max-w-xl px-4">
        {hiatusMode && (
          <div
            role="status"
            data-testid="hiatus-notice"
            className="mb-4 rounded border border-yellow-700 bg-yellow-950 px-4 py-3 text-sm text-yellow-200"
          >
            {HIATUS_MESSAGE}
          </div>
        )}
        <div className="flex flex-wrap justify-center border-b border-vault-border" role="tablist">
          {MODES.map(m => {
            const isDisabled = hiatusMode && HIATUS_DISABLED_MODES.includes(m)
            return (
              <button
                key={m}
                type="button"
                role="tab"
                aria-selected={mode === m}
                aria-disabled={isDisabled || undefined}
                disabled={isDisabled}
                onClick={() => { if (!isDisabled) setMode(m) }}
                title={isDisabled ? HIATUS_MESSAGE : undefined}
                aria-label={isDisabled ? `${MODE_CONFIG[m].label} (on hiatus)` : undefined}
                className={`px-5 py-2.5 text-sm font-medium border-b-2 transition-colors bg-transparent ${
                  isDisabled
                    ? 'border-transparent text-vault-text-muted opacity-40 cursor-not-allowed'
                    : mode === m
                    ? 'border-vault-accent text-vault-accent cursor-pointer'
                    : 'border-transparent text-vault-text-muted hover:text-vault-text cursor-pointer'
                }`}
              >
                {MODE_CONFIG[m].label}
              </button>
            )
          })}
          {/* Top Lists tab — navigates to /top instead of switching form mode. */}
          <button
            key={TOP_LISTS_TAB.id}
            type="button"
            role="tab"
            aria-selected={false}
            onClick={() => navigate(TOP_LISTS_TAB.route)}
            className="px-5 py-2.5 text-sm font-medium border-b-2 border-transparent text-vault-text-muted hover:text-vault-text transition-colors bg-transparent cursor-pointer"
          >
            {TOP_LISTS_TAB.label}
          </button>
        </div>

        <form onSubmit={handleSubmit} className="flex flex-col sm:flex-row gap-2 mt-4">
          <input
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={config.placeholder}
            aria-label="Search anime"
            className="flex-1 px-4 py-3 rounded bg-vault-surface border border-vault-border text-vault-text placeholder-vault-text-muted focus:outline-none focus:border-vault-accent transition-colors"
          />
          <button
            type="submit"
            className="px-6 py-3 rounded bg-vault-accent text-white font-medium hover:bg-vault-accent-hover transition-colors cursor-pointer border-none"
          >
            {config.buttonText}
          </button>
        </form>
      </div>

      <div className="max-w-3xl grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 pt-4 text-left">
        {MODES.map(m => {
          const desc =
            m === 'encyclopedia'
              ? `Look up any of ${
                  animeCount ? animeCount.toLocaleString() : 'thousands of'
                } titles for the full profile, audience consensus, and streaming links.`
              : MODE_CONFIG[m].cardDesc
          return (
            <div key={m} className="p-4 rounded bg-vault-surface border border-vault-border">
              <h3 className="font-semibold text-sm mb-1">{MODE_CONFIG[m].cardTitle}</h3>
              <p className="text-xs text-vault-text-muted">{desc}</p>
            </div>
          )
        })}
        {/* Parallel card for the Top Lists tab — click navigates to /top. */}
        <button
          type="button"
          onClick={() => navigate(TOP_LISTS_TAB.route)}
          className="p-4 rounded bg-vault-surface border border-vault-border text-left hover:border-vault-accent transition-colors cursor-pointer"
        >
          <h3 className="font-semibold text-sm mb-1">{TOP_LISTS_TAB.cardTitle}</h3>
          <p className="text-xs text-vault-text-muted">{TOP_LISTS_TAB.cardDesc}</p>
        </button>
      </div>

      <div className="text-sm text-vault-text-muted space-y-2 pt-2">
        <p>{config.samplesIntro}</p>
        <div className="flex flex-wrap justify-center gap-2">
          {config.samples.map(s => (
            <button
              key={s}
              onClick={() => navigate(config.routeFor(s))}
              className="px-3 py-1 rounded-full bg-vault-surface border border-vault-border text-xs hover:border-vault-accent hover:text-vault-accent transition-colors cursor-pointer"
            >
              "{s}"
            </button>
          ))}
        </div>
        <p className="pt-2">
          <Link to="/how-it-works" className="text-vault-accent hover:underline">
            Learn how it works
          </Link>
        </p>
      </div>
    </div>
  )
}
