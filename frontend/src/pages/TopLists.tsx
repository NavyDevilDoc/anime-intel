import { useState } from 'react'
import { Helmet } from 'react-helmet-async'
import { useNavigate } from 'react-router-dom'
import DiscoveryTabs, { type Tab } from '../components/DiscoveryTabs'

/**
 * TopLists — browse landing for the 79 curated `/top/{genre}-anime[-{decade}]`
 * static SEO pages.
 *
 * Solves the discoverability gap surfaced 2026-09-18: pre-fix the pages
 * were only reachable via genre tags at the bottom of a show profile.
 * Now they're one tab click away from the primary landing surface.
 *
 * Layout: decade filter row (radio pills) + genre chip grid. Click a
 * genre chip to navigate to `/top/{genre}-anime[-{decade}]`. Decade
 * defaults to "All Time". Each destination is a pre-built static HTML
 * page served by the CDN.
 */

// Kept in sync with GENRE_DEFS in `frontend/scripts/generate-seo-pages.js`
// so every chip resolves to an existing page.
const GENRES = [
  { label: 'Action', slug: 'action' },
  { label: 'Romance', slug: 'romance' },
  { label: 'Comedy', slug: 'comedy' },
  { label: 'Horror', slug: 'horror' },
  { label: 'Mecha', slug: 'mecha' },
  { label: 'Sports', slug: 'sports' },
  { label: 'Fantasy', slug: 'fantasy' },
  { label: 'Sci-Fi', slug: 'sci-fi' },
  { label: 'Thriller', slug: 'thriller' },
  { label: 'Psychological', slug: 'psychological' },
  { label: 'Slice of Life', slug: 'slice-of-life' },
  { label: 'Drama', slug: 'drama' },
  { label: 'Mystery', slug: 'mystery' },
  { label: 'Isekai', slug: 'isekai' },
  { label: 'Adventure', slug: 'adventure' },
  { label: 'Supernatural', slug: 'supernatural' },
] as const

// Kept in sync with DECADES in `frontend/scripts/generate-seo-pages.js`.
const DECADES = [
  { label: 'All Time', slug: '' },
  { label: '1990s', slug: '-1990s' },
  { label: '2000s', slug: '-2000s' },
  { label: '2010s', slug: '-2010s' },
  { label: '2020s', slug: '-2020s' },
] as const

type Decade = typeof DECADES[number]['slug']

export default function TopLists() {
  const navigate = useNavigate()
  const [decade, setDecade] = useState<Decade>('')

  const handleTabChange = (tab: Tab) => {
    // From Top Lists, clicking any of the three engine-driven tabs
    // navigates back to /try with the ?tab= param seeded so the target
    // surface is active on arrival. Clicking Top Lists is a no-op
    // (already here).
    if (tab === 'Top Lists') return
    const tabParam =
      tab === 'Find a Show' ? 'find'
      : tab === 'Shows Like This' ? 'dna'
      : 'encyclopedia'
    navigate(`/try?tab=${tabParam}`)
  }

  const buildHref = (genreSlug: string) => `/top/${genreSlug}-anime${decade}`

  const currentDecadeLabel = DECADES.find(d => d.slug === decade)?.label ?? 'All Time'

  return (
    <div className="space-y-6">
      <Helmet>
        <title>Top Anime Lists — Browse by Genre and Decade | Anime Intel</title>
        <meta
          name="description"
          content="Browse 79 curated top-anime lists across 16 genres and 5 decades. Ranked by community ratings from MyAnimeList, one entry per franchise."
        />
        <link rel="canonical" href="https://animeintel.app/top" />
      </Helmet>

      <div>
        <h1 className="text-3xl font-extrabold mb-1">Top Lists</h1>
        <p className="text-sm text-vault-text-muted">
          Browse curated top-anime rankings by genre and decade. Each list is
          ranked by community ratings with one entry per franchise — no repeat
          seasons crowding the top 20.
        </p>
      </div>

      <DiscoveryTabs activeTab="Top Lists" onTabChange={handleTabChange} />

      <div className="space-y-5">
        {/* Decade filter — radio pills */}
        <div>
          <div
            role="radiogroup"
            aria-label="Filter by decade"
            className="flex flex-wrap items-center gap-2"
          >
            <span className="text-sm text-vault-text-muted mr-1">Decade:</span>
            {DECADES.map(d => (
              <button
                key={d.slug || 'all'}
                type="button"
                role="radio"
                aria-checked={decade === d.slug}
                onClick={() => setDecade(d.slug)}
                className={`px-3 py-1 rounded-full border text-xs transition-colors cursor-pointer ${
                  decade === d.slug
                    ? 'bg-vault-accent text-white border-vault-accent'
                    : 'bg-vault-surface text-vault-text-muted border-vault-border hover:text-vault-text hover:border-vault-accent'
                }`}
              >
                {d.label}
              </button>
            ))}
          </div>
        </div>

        {/* Genre grid */}
        <div>
          <h2 className="text-lg font-semibold mb-2">
            Best {currentDecadeLabel === 'All Time' ? 'of All Time' : `of the ${currentDecadeLabel}`}
          </h2>
          <p className="text-xs text-vault-text-muted mb-3">
            Pick a genre to see the top 20{decade ? ` for the ${currentDecadeLabel}` : ''}.
          </p>
          <div className="flex flex-wrap gap-2" data-testid="top-lists-genre-grid">
            {GENRES.map(g => (
              <a
                key={g.slug}
                href={buildHref(g.slug)}
                className="px-4 py-2 rounded-lg bg-vault-surface border border-vault-border text-sm text-vault-text hover:border-vault-accent hover:text-vault-accent transition-colors"
              >
                {g.label}
              </a>
            ))}
          </div>
        </div>

        <p className="text-xs text-vault-text-muted italic">
          Lists are pre-built and updated with each seasonal ingest — no engine
          call, works during hiatus. Each entry links to the show's Encyclopedia
          profile.
        </p>
      </div>
    </div>
  )
}
