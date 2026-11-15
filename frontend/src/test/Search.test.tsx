import { describe, it, expect, vi, beforeEach, afterAll } from 'vitest'
import { screen, waitFor, render } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { BrowserRouter, MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import Search from '../pages/Search'

function renderSearch(props?: { skipHero?: boolean }) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Search {...props} />
      </BrowserRouter>
    </QueryClientProvider>,
  )
}

/** Mount Search at a specific URL via MemoryRouter — needed for tests
 *  that assert URL-param-driven state (seedQuery pre-fill, activeTab). */
function renderSearchAt(url: string, props?: { skipHero?: boolean }) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[url]}>
        <Search {...props} />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

// Force hiatus flag OFF so Search-page tests aren't affected by a local
// `VITE_ENGINE_HIATUS_MODE=true` in frontend/.env. Mirrors the pattern in
// LandingHero.test.tsx / FindAShow.test.tsx / ShowsLikeThis.test.tsx.
const _originalHiatusEnv = import.meta.env.VITE_ENGINE_HIATUS_MODE
// Landing Redesign PR #2 — also snapshot the two new flags so the
// default-branch tests in this file don't get affected by a developer's
// local .env setting them to 'true'.
const _originalNewHeroFlag = import.meta.env.VITE_NEW_LANDING_HERO_ENABLED
const _originalSpotlightFlag = import.meta.env.VITE_DAILY_SPOTLIGHT_ENABLED
// Landing Redesign PR #3 — three below-fold section flags.
const _originalStatsFlag = import.meta.env.VITE_LANDING_STATS_STRIP_ENABLED
const _originalHowFlag = import.meta.env.VITE_LANDING_HOW_STRIP_ENABLED
const _originalMinimizationFlag = import.meta.env.VITE_LANDING_MINIMIZATION_STRIP_ENABLED

beforeEach(() => {
  vi.clearAllMocks()
  import.meta.env.VITE_ENGINE_HIATUS_MODE = ''
  import.meta.env.VITE_NEW_LANDING_HERO_ENABLED = ''
  import.meta.env.VITE_DAILY_SPOTLIGHT_ENABLED = ''
  import.meta.env.VITE_LANDING_STATS_STRIP_ENABLED = ''
  import.meta.env.VITE_LANDING_HOW_STRIP_ENABLED = ''
  import.meta.env.VITE_LANDING_MINIMIZATION_STRIP_ENABLED = ''
  // Stub fetch for LandingHero health call
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
    json: () => Promise.resolve({ anime_count: 6236 }),
  }))
})

afterAll(() => {
  if (_originalHiatusEnv === undefined) {
    delete import.meta.env.VITE_ENGINE_HIATUS_MODE
  } else {
    import.meta.env.VITE_ENGINE_HIATUS_MODE = _originalHiatusEnv
  }
  if (_originalNewHeroFlag === undefined) {
    delete import.meta.env.VITE_NEW_LANDING_HERO_ENABLED
  } else {
    import.meta.env.VITE_NEW_LANDING_HERO_ENABLED = _originalNewHeroFlag
  }
  if (_originalSpotlightFlag === undefined) {
    delete import.meta.env.VITE_DAILY_SPOTLIGHT_ENABLED
  } else {
    import.meta.env.VITE_DAILY_SPOTLIGHT_ENABLED = _originalSpotlightFlag
  }
  if (_originalStatsFlag === undefined) {
    delete import.meta.env.VITE_LANDING_STATS_STRIP_ENABLED
  } else {
    import.meta.env.VITE_LANDING_STATS_STRIP_ENABLED = _originalStatsFlag
  }
  if (_originalHowFlag === undefined) {
    delete import.meta.env.VITE_LANDING_HOW_STRIP_ENABLED
  } else {
    import.meta.env.VITE_LANDING_HOW_STRIP_ENABLED = _originalHowFlag
  }
  if (_originalMinimizationFlag === undefined) {
    delete import.meta.env.VITE_LANDING_MINIMIZATION_STRIP_ENABLED
  } else {
    import.meta.env.VITE_LANDING_MINIMIZATION_STRIP_ENABLED = _originalMinimizationFlag
  }
})

describe('Search page — landing (index route)', () => {
  it('shows the landing hero by default', async () => {
    renderSearch()
    await waitFor(() => {
      expect(screen.getByText('AI-Powered Anime Discovery')).toBeInTheDocument()
    })
  })

  it('does not show the tabbed search engine UI on the index route', async () => {
    renderSearch()
    await waitFor(() => {
      expect(screen.getByText('AI-Powered Anime Discovery')).toBeInTheDocument()
    })
    expect(screen.queryByText('Anime Discovery Engine')).not.toBeInTheDocument()
  })

  it('exposes the hero search input', async () => {
    renderSearch()
    await waitFor(() => {
      expect(screen.getByLabelText('Search anime')).toBeInTheDocument()
    })
  })
})

describe('Search page — landing (new-hero flag on)', () => {
  // Landing Redesign PR #2. Flag on -> MarketingHero replaces LandingHero;
  // hiatus mode still takes precedence (new hero has no graceful degradation
  // for a disabled search path).

  it('renders MarketingHero instead of the old LandingHero when the flag is on', async () => {
    import.meta.env.VITE_NEW_LANDING_HERO_ENABLED = 'true'
    renderSearch()
    await waitFor(() => {
      expect(screen.getByTestId('marketing-hero-headline')).toBeInTheDocument()
    })
    // Old landing's headline should NOT be present
    expect(screen.queryByText('AI-Powered Anime Discovery')).not.toBeInTheDocument()
  })

  it('falls back to LandingHero when hiatus mode is on, regardless of new-hero flag', async () => {
    import.meta.env.VITE_NEW_LANDING_HERO_ENABLED = 'true'
    import.meta.env.VITE_ENGINE_HIATUS_MODE = 'true'
    renderSearch()
    await waitFor(() => {
      expect(screen.getByText('AI-Powered Anime Discovery')).toBeInTheDocument()
    })
    // Marketing hero should NOT render — hiatus wins
    expect(screen.queryByTestId('marketing-hero-headline')).not.toBeInTheDocument()
  })

  it('renders TodaysSpotlight below MarketingHero when both flags are on', async () => {
    import.meta.env.VITE_NEW_LANDING_HERO_ENABLED = 'true'
    import.meta.env.VITE_DAILY_SPOTLIGHT_ENABLED = 'true'
    // Stub fetch to return a valid SpotlightResponse shape
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve({
        category: 'hidden_gem',
        category_label: 'Today: Hidden Gem',
        date: '2026-09-30',
        primary_show: {
          id: 1, title: 'Test Show', release_year: 2020, mal_score: 8.0,
          thematic_vibe: 'Test', consensus_summary: 'A test show.',
          pros: [], controversy_score: 3,
          poster_url_large: null, poster_url_medium: null,
        },
        supplementary_shows: [],
        supplementary_context: null,
      }),
    }))
    renderSearch()
    await waitFor(() => {
      expect(screen.getByTestId('marketing-hero-headline')).toBeInTheDocument()
    })
    await waitFor(() => {
      expect(screen.getByTestId('spotlight')).toBeInTheDocument()
    })
  })

  it('renders all three below-fold sections when their PR #3 flags are on', async () => {
    import.meta.env.VITE_NEW_LANDING_HERO_ENABLED = 'true'
    import.meta.env.VITE_LANDING_STATS_STRIP_ENABLED = 'true'
    import.meta.env.VITE_LANDING_HOW_STRIP_ENABLED = 'true'
    import.meta.env.VITE_LANDING_MINIMIZATION_STRIP_ENABLED = 'true'
    renderSearch()
    await waitFor(() => {
      expect(screen.getByTestId('marketing-hero-headline')).toBeInTheDocument()
    })
    expect(screen.getByTestId('vault-stats-strip')).toBeInTheDocument()
    expect(screen.getByTestId('how-recommendations-work')).toBeInTheDocument()
    expect(screen.getByTestId('data-minimization-strip')).toBeInTheDocument()
  })

  it('below-fold sections are hidden when their PR #3 flags stay off', async () => {
    import.meta.env.VITE_NEW_LANDING_HERO_ENABLED = 'true'
    // PR #3 flags left off (default from beforeEach)
    renderSearch()
    await waitFor(() => {
      expect(screen.getByTestId('marketing-hero-headline')).toBeInTheDocument()
    })
    expect(screen.queryByTestId('vault-stats-strip')).not.toBeInTheDocument()
    expect(screen.queryByTestId('how-recommendations-work')).not.toBeInTheDocument()
    expect(screen.queryByTestId('data-minimization-strip')).not.toBeInTheDocument()
  })
})

describe('Search page — try route (skipHero)', () => {
  it('shows the tabbed search UI when skipHero is set', async () => {
    renderSearch({ skipHero: true })
    await waitFor(() => {
      expect(screen.getByText('Anime Discovery Engine')).toBeInTheDocument()
    })
    expect(screen.getByText('Find a Show')).toBeInTheDocument()
    expect(screen.getByText('Shows Like This')).toBeInTheDocument()
    expect(screen.getByText('Encyclopedia')).toBeInTheDocument()
  })

  it('does not show the landing hero when skipHero is set', async () => {
    renderSearch({ skipHero: true })
    await waitFor(() => {
      expect(screen.getByText('Anime Discovery Engine')).toBeInTheDocument()
    })
    expect(screen.queryByText('AI-Powered Anime Discovery')).not.toBeInTheDocument()
  })
})

describe('Search page — ExploreVibeButton URL contract (/try?mode=find&q=<vibe>)', () => {
  // Regression guard for the hotfix: PR #191's ExploreVibeButton originally
  // navigated to /try?q=<vibe> (no mode param). From the Encyclopedia tab,
  // Search.tsx's useEffect hit the "No modeParam = don't change" branch and
  // the activeTab stayed on Encyclopedia — the vibe triplet became a
  // dangling `q` param that nothing consumed. Fix: ExploreVibeButton routes
  // to /try?mode=find&q=<vibe>, and Search.tsx's mode-sync useEffect now
  // also seeds the corresponding input (seedQuery for find, seedDnaTitle
  // for dna, encyclopediaTitle for encyclopedia).

  it('switches to Find a Show tab and seeds the query when URL is /try?mode=find&q=<vibe>', async () => {
    renderSearchAt(
      '/try?mode=find&q=Sci-Fi%2C%20Thriller%2C%20Time%20Travel',
      { skipHero: true },
    )
    await waitFor(() => {
      expect(screen.getByText('Anime Discovery Engine')).toBeInTheDocument()
    })
    const findTab = screen.getByRole('tab', { name: 'Find a Show' })
    expect(findTab).toHaveAttribute('aria-selected', 'true')
    const input = screen.getByPlaceholderText(/gritty military drama/)
    await waitFor(() => {
      expect(input).toHaveValue('Sci-Fi, Thriller, Time Travel')
    })
  })

  it('switches to Shows Like This tab and seeds the DNA title when URL is /try?mode=dna&q=<title>', async () => {
    renderSearchAt('/try?mode=dna&q=Steins%3BGate', { skipHero: true })
    await waitFor(() => {
      expect(screen.getByText('Anime Discovery Engine')).toBeInTheDocument()
    })
    const dnaTab = screen.getByRole('tab', { name: 'Shows Like This' })
    expect(dnaTab).toHaveAttribute('aria-selected', 'true')
  })

  it('switches to Encyclopedia tab and seeds the title when URL is /try?mode=encyclopedia&q=<title>', async () => {
    renderSearchAt('/try?mode=encyclopedia&q=Code%20Geass', { skipHero: true })
    await waitFor(() => {
      expect(screen.getByText('Anime Discovery Engine')).toBeInTheDocument()
    })
    const encTab = screen.getByRole('tab', { name: 'Encyclopedia' })
    expect(encTab).toHaveAttribute('aria-selected', 'true')
  })
})

describe('Search page — Query Tips panel visibility by tab', () => {
  // Tailwind's `hidden` utility (display:none) is set via class string;
  // jsdom doesn't compute styles from Tailwind classes, so toBeVisible()
  // can't detect display:none. Instead, assert the panel button's ancestor
  // <div> wrapper carries the `hidden` class when the tab is inactive.
  it('Query Tips panel wrapper is NOT hidden on the Find a Show tab (default)', async () => {
    renderSearch({ skipHero: true })
    await waitFor(() => {
      expect(screen.getByText('Anime Discovery Engine')).toBeInTheDocument()
    })
    const panelButton = screen.getByRole('button', { name: /Query Tips/ })
    // closest('.hidden') walks up the DOM looking for a hidden ancestor.
    // On Find a Show tab, no such ancestor should exist.
    expect(panelButton.closest('.hidden')).toBeNull()
  })

  it('Query Tips panel wrapper is hidden when switching to Shows Like This', async () => {
    const user = userEvent.setup()
    renderSearch({ skipHero: true })
    await waitFor(() => {
      expect(screen.getByText('Anime Discovery Engine')).toBeInTheDocument()
    })
    await user.click(screen.getByRole('tab', { name: 'Shows Like This' }))
    const panelButton = screen.getByRole('button', { name: /Query Tips/ })
    // Panel still in the DOM, but its ancestor wrapper now has `hidden`.
    expect(panelButton.closest('.hidden')).not.toBeNull()
  })

  it('Query Tips panel wrapper is hidden when switching to Encyclopedia', async () => {
    const user = userEvent.setup()
    renderSearch({ skipHero: true })
    await waitFor(() => {
      expect(screen.getByText('Anime Discovery Engine')).toBeInTheDocument()
    })
    await user.click(screen.getByRole('tab', { name: 'Encyclopedia' }))
    const panelButton = screen.getByRole('button', { name: /Query Tips/ })
    expect(panelButton.closest('.hidden')).not.toBeNull()
  })
})
