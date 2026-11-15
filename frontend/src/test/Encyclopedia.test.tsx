import { describe, it, expect, vi, beforeEach, afterAll } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders, makeProfile } from './helpers'
import Encyclopedia from '../components/Encyclopedia'
import * as offlineEncyclopedia from '../lib/offlineEncyclopedia'

vi.mock('../lib/api', () => ({
  api: {
    get: vi.fn(),
  },
}))

import { api } from '../lib/api'
const mockGet = vi.mocked(api.get)

// Snapshot the developer's local .env value; force OFF for the main
// describes so tests aren't affected by a local VITE_ENGINE_HIATUS_MODE=true.
// vi.stubEnv is unreliable for VITE_-prefixed vars because Vite transforms
// them; direct mutation is what actually works. Mirrors the pattern in
// LandingHero.test.tsx / FindAShow.test.tsx / etc.
const _originalHiatusEnv = import.meta.env.VITE_ENGINE_HIATUS_MODE

beforeEach(() => {
  vi.clearAllMocks()
  import.meta.env.VITE_ENGINE_HIATUS_MODE = ''
})

afterAll(() => {
  if (_originalHiatusEnv === undefined) {
    delete import.meta.env.VITE_ENGINE_HIATUS_MODE
  } else {
    import.meta.env.VITE_ENGINE_HIATUS_MODE = _originalHiatusEnv
  }
})

describe('Encyclopedia', () => {
  it('renders lookup input and button', () => {
    renderWithProviders(<Encyclopedia />)
    expect(screen.getByPlaceholderText(/Evangelion/)).toBeInTheDocument()
    expect(screen.getByText('Look Up')).toBeInTheDocument()
  })

  it('disables button when input is empty', () => {
    renderWithProviders(<Encyclopedia />)
    expect(screen.getByText('Look Up')).toBeDisabled()
  })

  it('displays full profile on exact match', async () => {
    const profile = makeProfile()
    mockGet.mockResolvedValueOnce({
      success: true,
      matches: ['Steins;Gate'],
      profile,
      error: null,
    })
    const user = userEvent.setup()
    renderWithProviders(<Encyclopedia />)
    await user.type(screen.getByPlaceholderText(/Evangelion/), 'Steins;Gate')
    await user.click(screen.getByText('Look Up'))

    await waitFor(() => {
      expect(screen.getByText('Steins;Gate')).toBeInTheDocument()
      expect(screen.getByText('9.1')).toBeInTheDocument()
      expect(screen.getByText('+85%')).toBeInTheDocument()
      expect(screen.getByText('2/10')).toBeInTheDocument()
      expect(screen.getByText('White Fox')).toBeInTheDocument()
      expect(screen.getByText(/masterful sci-fi thriller/)).toBeInTheDocument()
      expect(screen.getByText(/Brilliant plot twists/)).toBeInTheDocument()
      expect(screen.getByText(/Slow start/)).toBeInTheDocument()
      // Vote count caption under MAL Score (default fixture: 1,200,000)
      const votes = screen.getByText('1.2M ratings')
      expect(votes).toBeInTheDocument()
      expect(votes).toHaveAttribute('title', '1,200,000 ratings')
    })
  })

  it('renders the Explore-this-vibe CTA when profile has thematic_vibe', async () => {
    // The default makeProfile fixture populates thematic_vibe; this
    // verifies the vibe-triplet CTA is wired into the profile card and
    // routes to /try?q=<URL-encoded triplet> per the PR #175 contract.
    mockGet.mockResolvedValueOnce({
      success: true,
      matches: ['Steins;Gate'],
      profile: makeProfile(),
      error: null,
    })
    const user = userEvent.setup()
    renderWithProviders(<Encyclopedia />)
    await user.type(screen.getByPlaceholderText(/Evangelion/), 'Steins;Gate')
    await user.click(screen.getByText('Look Up'))

    await waitFor(() => {
      const vibeLink = screen.getByRole('link', { name: 'Explore this vibe' })
      expect(vibeLink).toBeInTheDocument()
      expect(vibeLink).toHaveAttribute(
        'href',
        '/try?mode=find&q=Sci-Fi%2C%20Thriller%2C%20Time%20Travel',
      )
    })
  })

  it('suppresses the Explore-this-vibe CTA when thematic_vibe is empty', async () => {
    mockGet.mockResolvedValueOnce({
      success: true,
      matches: ['Steins;Gate'],
      profile: makeProfile({ thematic_vibe: '' }),
      error: null,
    })
    const user = userEvent.setup()
    renderWithProviders(<Encyclopedia />)
    await user.type(screen.getByPlaceholderText(/Evangelion/), 'Steins;Gate')
    await user.click(screen.getByText('Look Up'))

    await waitFor(() => {
      expect(screen.getByText('Steins;Gate')).toBeInTheDocument()
    })
    expect(screen.queryByRole('link', { name: 'Explore this vibe' })).not.toBeInTheDocument()
  })

  it('hides vote count caption when scored_by is 0', async () => {
    mockGet.mockResolvedValueOnce({
      success: true,
      matches: ['Steins;Gate'],
      profile: makeProfile({ scored_by: 0 }),
      error: null,
    })
    const user = userEvent.setup()
    renderWithProviders(<Encyclopedia />)
    await user.type(screen.getByPlaceholderText(/Evangelion/), 'Steins;Gate')
    await user.click(screen.getByText('Look Up'))

    await waitFor(() => {
      expect(screen.getByText('MAL Score')).toBeInTheDocument()
    })
    expect(screen.queryByText(/ratings$/)).not.toBeInTheDocument()
  })

  it('shows disambiguation when multiple matches', async () => {
    mockGet.mockResolvedValueOnce({
      success: true,
      matches: ['Code Geass: Lelouch of the Rebellion', 'Code Geass: Lelouch of the Rebellion R2'],
      profile: null,
      error: null,
    })
    const user = userEvent.setup()
    renderWithProviders(<Encyclopedia />)
    await user.type(screen.getByPlaceholderText(/Evangelion/), 'Code Geass')
    await user.click(screen.getByText('Look Up'))

    await waitFor(() => {
      expect(screen.getByText('Multiple shows detected. Please clarify:')).toBeInTheDocument()
      expect(screen.getByText('Code Geass: Lelouch of the Rebellion')).toBeInTheDocument()
      expect(screen.getByText('Code Geass: Lelouch of the Rebellion R2')).toBeInTheDocument()
    })
  })

  it('selects a disambiguation option and fetches profile', async () => {
    mockGet
      .mockResolvedValueOnce({
        success: true,
        matches: ['Code Geass: Lelouch of the Rebellion', 'Code Geass R2'],
        profile: null,
        error: null,
      })
      .mockResolvedValueOnce({
        success: true,
        matches: ['Code Geass: Lelouch of the Rebellion'],
        profile: makeProfile({ title: 'Code Geass: Lelouch of the Rebellion', id: 1575 }),
        error: null,
      })

    const user = userEvent.setup()
    renderWithProviders(<Encyclopedia />)
    await user.type(screen.getByPlaceholderText(/Evangelion/), 'Code Geass')
    await user.click(screen.getByText('Look Up'))

    await waitFor(() => {
      expect(screen.getByText('Code Geass: Lelouch of the Rebellion')).toBeInTheDocument()
    })
    await user.click(screen.getByText('Code Geass: Lelouch of the Rebellion'))

    await waitFor(() => {
      expect(mockGet).toHaveBeenCalledTimes(2)
    })
  })

  it('shows error when show not found', async () => {
    mockGet.mockResolvedValueOnce({
      success: true,
      matches: [],
      profile: null,
      error: null,
    })
    const user = userEvent.setup()
    renderWithProviders(<Encyclopedia />)
    await user.type(screen.getByPlaceholderText(/Evangelion/), 'xyznotreal')
    await user.click(screen.getByText('Look Up'))

    await waitFor(() => {
      expect(screen.getByText(/not found in the database/)).toBeInTheDocument()
    })
  })

  it('shows loading state', async () => {
    mockGet.mockImplementation(() => new Promise(() => {}))
    const user = userEvent.setup()
    renderWithProviders(<Encyclopedia />)
    await user.type(screen.getByPlaceholderText(/Evangelion/), 'test')
    await user.click(screen.getByText('Look Up'))
    expect(screen.getByText('Looking up...')).toBeInTheDocument()
  })

  it('has MAL link in profile view', async () => {
    mockGet.mockResolvedValueOnce({
      success: true,
      matches: ['Steins;Gate'],
      profile: makeProfile(),
      error: null,
    })
    const user = userEvent.setup()
    renderWithProviders(<Encyclopedia />)
    await user.type(screen.getByPlaceholderText(/Evangelion/), 'Steins;Gate')
    await user.click(screen.getByText('Look Up'))

    await waitFor(() => {
      const link = screen.getByText('View on MyAnimeList')
      expect(link).toHaveAttribute('href', 'https://myanimelist.net/anime/1')
    })
  })

})

describe('Encyclopedia — MAL Rating Pill', () => {
  it('renders the short-form pill when mal_rating is populated', async () => {
    mockGet.mockResolvedValueOnce({
      success: true,
      matches: ['Attack on Titan'],
      profile: makeProfile({
        title: 'Attack on Titan',
        mal_rating: 'R - 17+ (violence & profanity)',
      }),
      error: null,
    })
    const user = userEvent.setup()
    renderWithProviders(<Encyclopedia />)
    await user.type(screen.getByPlaceholderText(/Evangelion/), 'Attack on Titan')
    await user.click(screen.getByText('Look Up'))

    await waitFor(() => {
      const pill = screen.getByText('R')
      expect(pill).toBeInTheDocument()
      expect(pill).toHaveAttribute('title', 'R - 17+ (violence & profanity)')
    })
  })

  it('does not render the pill when mal_rating is null', async () => {
    mockGet.mockResolvedValueOnce({
      success: true,
      matches: ['Some Obscure Show'],
      profile: makeProfile({ title: 'Some Obscure Show', mal_rating: null }),
      error: null,
    })
    const user = userEvent.setup()
    const { container } = renderWithProviders(<Encyclopedia />)
    await user.type(screen.getByPlaceholderText(/Evangelion/), 'Some Obscure Show')
    await user.click(screen.getByText('Look Up'))

    await waitFor(() => {
      expect(screen.getByText('Some Obscure Show')).toBeInTheDocument()
    })
    // No pill DOM node — assert no element carries an aria-label that
    // matches the verbose rating format.
    expect(container.querySelector('span[aria-label*=" - "]')).toBeNull()
  })

  it('does not render the pill when mal_rating is empty string', async () => {
    mockGet.mockResolvedValueOnce({
      success: true,
      matches: ['Unrated Show'],
      profile: makeProfile({ title: 'Unrated Show', mal_rating: '' }),
      error: null,
    })
    const user = userEvent.setup()
    const { container } = renderWithProviders(<Encyclopedia />)
    await user.type(screen.getByPlaceholderText(/Evangelion/), 'Unrated Show')
    await user.click(screen.getByText('Look Up'))

    await waitFor(() => {
      expect(screen.getByText('Unrated Show')).toBeInTheDocument()
    })
    expect(container.querySelector('span[aria-label*=" - "]')).toBeNull()
  })
})

describe('Encyclopedia — hiatus mode', () => {
  beforeEach(() => {
    import.meta.env.VITE_ENGINE_HIATUS_MODE = 'true'
  })

  it('uses the offline lookup path instead of the backend when flag is on', async () => {
    const spy = vi.spyOn(offlineEncyclopedia, 'lookupOffline').mockResolvedValueOnce({
      success: true,
      matches: ['Cowboy Bebop'],
      profile: makeProfile({ title: 'Cowboy Bebop', id: 1 }),
      error: null,
      series_context: null,
    })
    const user = userEvent.setup()
    renderWithProviders(<Encyclopedia />)
    await user.type(screen.getByPlaceholderText(/Evangelion/), 'Cowboy Bebop')
    await user.click(screen.getByText('Look Up'))

    await waitFor(() => {
      expect(spy).toHaveBeenCalledWith('Cowboy Bebop', { skipFranchiseDisambig: false })
      expect(screen.getByText('Cowboy Bebop')).toBeInTheDocument()
    })
    // Load-bearing: the backend was NOT called
    expect(mockGet).not.toHaveBeenCalled()
  })

  it('surfaces the offline lookup error message', async () => {
    vi.spyOn(offlineEncyclopedia, 'lookupOffline').mockResolvedValueOnce({
      success: false,
      matches: [],
      profile: null,
      error: 'Could not identify "xyznotreal" in the vault.',
    })
    const user = userEvent.setup()
    renderWithProviders(<Encyclopedia />)
    await user.type(screen.getByPlaceholderText(/Evangelion/), 'xyznotreal')
    await user.click(screen.getByText('Look Up'))

    await waitFor(() => {
      expect(screen.getByText(/Could not identify "xyznotreal"/)).toBeInTheDocument()
    })
  })

  it('renders the disambiguation list from the offline lookup', async () => {
    vi.spyOn(offlineEncyclopedia, 'lookupOffline').mockResolvedValueOnce({
      success: true,
      matches: ['Naruto', 'Naruto: Shippuden'],
      profile: null,
      error: null,
    })
    const user = userEvent.setup()
    renderWithProviders(<Encyclopedia />)
    await user.type(screen.getByPlaceholderText(/Evangelion/), 'Naruto')
    await user.click(screen.getByText('Look Up'))

    await waitFor(() => {
      expect(screen.getByText('Multiple shows detected. Please clarify:')).toBeInTheDocument()
      expect(screen.getByText('Naruto')).toBeInTheDocument()
      expect(screen.getByText('Naruto: Shippuden')).toBeInTheDocument()
    })
  })

  it('clicking a disambig item calls lookupOffline with skipFranchiseDisambig=true', async () => {
    // Regression guard: without this flag, clicking the exact-match item
    // at the top of the franchise-root disambig list re-triggers the
    // heuristic and re-renders the same list, looking to the user like
    // nothing happened.
    const spy = vi.spyOn(offlineEncyclopedia, 'lookupOffline')
      // First call — user searches "Naruto", franchise heuristic fires
      .mockResolvedValueOnce({
        success: true,
        matches: ['Naruto', 'Naruto: Shippuden', 'Boruto: Naruto Next Generations'],
        profile: null,
        error: null,
      })
      // Second call — user clicks "Naruto" in the list, should resolve
      .mockResolvedValueOnce({
        success: true,
        matches: ['Naruto'],
        profile: makeProfile({ title: 'Naruto', id: 20 }),
        error: null,
        series_context: null,
      })
    const user = userEvent.setup()
    renderWithProviders(<Encyclopedia />)
    await user.type(screen.getByPlaceholderText(/Evangelion/), 'Naruto')
    await user.click(screen.getByText('Look Up'))

    await waitFor(() => {
      expect(screen.getByText('Boruto: Naruto Next Generations')).toBeInTheDocument()
    })

    // Click the "Naruto" button in the disambig list. getByRole with
    // exact name lands on the button unambiguously (input value doesn't
    // create a matching text node).
    await user.click(screen.getByRole('button', { name: 'Naruto' }))

    await waitFor(() => {
      expect(spy).toHaveBeenCalledTimes(2)
    })
    // First call: user's raw search — no opts / opts undefined, so
    // the heuristic can fire. Second call: from the disambig click,
    // MUST carry skipFranchiseDisambig=true so the heuristic doesn't
    // re-fire and re-render the same list.
    expect(spy.mock.calls[0][1]).toEqual({ skipFranchiseDisambig: false })
    expect(spy.mock.calls[1][1]).toEqual({ skipFranchiseDisambig: true })
  })
})
