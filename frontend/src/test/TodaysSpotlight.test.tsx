import { describe, it, expect, vi, beforeEach, afterAll } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithRouter } from './helpers'
import TodaysSpotlight, { type SpotlightResponse, type SpotlightShow } from '../components/TodaysSpotlight'

const mockNavigate = vi.fn()
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom')
  return { ...actual, useNavigate: () => mockNavigate }
})

// Env-var save/restore pattern (see LandingHero.test.tsx / PosterImage.test.tsx).
const _originalSpotlightFlag = import.meta.env.VITE_DAILY_SPOTLIGHT_ENABLED
const _originalCoverArtFlag = import.meta.env.VITE_COVER_ART_ENABLED

beforeEach(() => {
  vi.restoreAllMocks()
  mockNavigate.mockReset()
  // Both flags on by default so we exercise the full render path;
  // individual tests flip either off as needed.
  import.meta.env.VITE_DAILY_SPOTLIGHT_ENABLED = 'true'
  import.meta.env.VITE_COVER_ART_ENABLED = 'true'
})

afterAll(() => {
  if (_originalSpotlightFlag === undefined) {
    delete import.meta.env.VITE_DAILY_SPOTLIGHT_ENABLED
  } else {
    import.meta.env.VITE_DAILY_SPOTLIGHT_ENABLED = _originalSpotlightFlag
  }
  if (_originalCoverArtFlag === undefined) {
    delete import.meta.env.VITE_COVER_ART_ENABLED
  } else {
    import.meta.env.VITE_COVER_ART_ENABLED = _originalCoverArtFlag
  }
})

function makeShow(overrides?: Partial<SpotlightShow>): SpotlightShow {
  return {
    id: 1,
    title: 'Cowboy Bebop',
    release_year: 1998,
    mal_score: 8.75,
    thematic_vibe: 'Neo-Noir, Space Western',
    consensus_summary: 'A stylish, jazz-scored space western with unforgettable characters.',
    pros: ['Iconic soundtrack', 'Standalone episodes', 'Yoko Kanno'],
    controversy_score: 2,
    poster_url_large: 'https://cdn.myanimelist.net/images/anime/4/19644l.jpg',
    poster_url_medium: 'https://cdn.myanimelist.net/images/anime/4/19644.jpg',
    ...overrides,
  }
}

function makeSpotlight(overrides?: Partial<SpotlightResponse>): SpotlightResponse {
  return {
    category: 'hidden_gem',
    category_label: 'Today: Hidden Gem',
    date: '2026-09-28',
    primary_show: makeShow(),
    supplementary_shows: [],
    supplementary_context: null,
    ...overrides,
  }
}

function stubFetchOk(payload: SpotlightResponse) {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    json: () => Promise.resolve(payload),
  }))
}

describe('TodaysSpotlight', () => {
  it('renders null when VITE_DAILY_SPOTLIGHT_ENABLED is off', () => {
    import.meta.env.VITE_DAILY_SPOTLIGHT_ENABLED = 'false'
    const { container } = renderWithRouter(<TodaysSpotlight />)
    expect(container).toBeEmptyDOMElement()
  })

  it('shows loading skeleton before data arrives', () => {
    // Fetch that never resolves — component stays in loading state
    vi.stubGlobal('fetch', vi.fn(() => new Promise(() => {})))
    renderWithRouter(<TodaysSpotlight />)
    expect(screen.getByTestId('spotlight-loading')).toBeInTheDocument()
  })

  it('renders category label, title, quote, pros, and controversy after fetch', async () => {
    stubFetchOk(makeSpotlight())
    renderWithRouter(<TodaysSpotlight />)
    await waitFor(() => {
      expect(screen.getByTestId('spotlight')).toBeInTheDocument()
    })
    expect(screen.getByTestId('spotlight-category-label')).toHaveTextContent('Today: Hidden Gem')
    expect(screen.getByText('Cowboy Bebop')).toBeInTheDocument()
    expect(screen.getByText(/1998.*MAL score 8\.75/)).toBeInTheDocument()
    expect(screen.getByText(/jazz-scored space western/i)).toBeInTheDocument()
    expect(screen.getByText('Iconic soundtrack')).toBeInTheDocument()
    // Controversy bar renders with the score
    expect(screen.getByLabelText(/Controversy score 2 of 10/)).toBeInTheDocument()
  })

  it('CTA navigates to /try?q=<vibe> to preload Find-a-Show with the thematic vibe', async () => {
    stubFetchOk(makeSpotlight())
    const user = userEvent.setup()
    renderWithRouter(<TodaysSpotlight />)
    await waitFor(() => {
      expect(screen.getByTestId('spotlight-cta')).toBeInTheDocument()
    })
    await user.click(screen.getByTestId('spotlight-cta'))
    // Fixture vibe = "Neo-Noir, Space Western"
    expect(mockNavigate).toHaveBeenCalledWith('/try?q=Neo-Noir%2C%20Space%20Western')
  })

  it('CTA falls back to the show title when thematic_vibe is empty', async () => {
    stubFetchOk(makeSpotlight({
      primary_show: makeShow({ thematic_vibe: '', title: 'Cowboy Bebop' }),
    }))
    const user = userEvent.setup()
    renderWithRouter(<TodaysSpotlight />)
    await waitFor(() => {
      expect(screen.getByTestId('spotlight-cta')).toBeInTheDocument()
    })
    await user.click(screen.getByTestId('spotlight-cta'))
    expect(mockNavigate).toHaveBeenCalledWith('/try?q=Cowboy%20Bebop')
  })

  it('renders supplementary shows when the category provides them', async () => {
    stubFetchOk(makeSpotlight({
      category: 'franchise_spotlight',
      category_label: 'Today: Franchise Spotlight',
      primary_show: makeShow({ id: 6948, title: 'Monogatari Series' }),
      supplementary_shows: [
        makeShow({ id: 12345, title: 'Bakemonogatari' }),
        makeShow({ id: 67890, title: 'Nisemonogatari' }),
      ],
      supplementary_context: { series_id: '6948', series_size: 17 },
    }))
    renderWithRouter(<TodaysSpotlight />)
    await waitFor(() => {
      expect(screen.getByTestId('spotlight-supplementary')).toBeInTheDocument()
    })
    expect(screen.getByText('Bakemonogatari')).toBeInTheDocument()
    expect(screen.getByText('Nisemonogatari')).toBeInTheDocument()
    // Franchise caption from supplementary_context
    expect(screen.getByText(/17-entry franchise/)).toBeInTheDocument()
  })

  it('surfaces person + role caption on behind-the-scenes category', async () => {
    stubFetchOk(makeSpotlight({
      category: 'behind_the_scenes',
      category_label: 'Today: Behind the Scenes',
      supplementary_context: { person_name: 'Shinichirou Watanabe', role: 'Director' },
    }))
    renderWithRouter(<TodaysSpotlight />)
    await waitFor(() => {
      expect(screen.getByText('Shinichirou Watanabe')).toBeInTheDocument()
    })
    expect(screen.getByText(/Director/)).toBeInTheDocument()
  })

  it('renders null when the fetch fails (silent degradation)', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('network down')))
    const { container } = renderWithRouter(<TodaysSpotlight />)
    // Loading initially; then failure resolves and unmounts everything
    await waitFor(() => {
      expect(container).toBeEmptyDOMElement()
    })
  })
})
