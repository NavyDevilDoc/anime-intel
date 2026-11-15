import { describe, it, expect, vi, beforeEach, afterAll } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders, makeResult, makeSearchResponse } from './helpers'
import FindAShow from '../components/FindAShow'

// Mock the api module — must re-export ApiError since FindAShow
// imports it for structured 429 handling.
vi.mock('../lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/api')>()
  return {
    ...actual,
    api: {
      post: vi.fn(),
    },
  }
})

// Default-mock the title-detection helper to matched:false — most tests
// don't care about it and this keeps the "hint should NOT appear" branch
// exercised without extra setup per test. Tests that DO care mock a
// matched result explicitly.
vi.mock('../lib/offlineEncyclopedia', () => ({
  detectTitleMatch: vi.fn().mockResolvedValue({ matched: false, title: null }),
}))

import { api } from '../lib/api'
import { detectTitleMatch } from '../lib/offlineEncyclopedia'
const mockPost = vi.mocked(api.post)
const mockDetectTitle = vi.mocked(detectTitleMatch)

// Snapshot the developer's local .env values; force known baselines for
// the main describe (mirrors SeriesContext.test.tsx). vi.stubEnv is
// unreliable for VITE_-prefixed vars because Vite transforms them; direct
// mutation is what actually works.
const _originalHiatusEnv = import.meta.env.VITE_ENGINE_HIATUS_MODE
const _originalTitleHintEnv = import.meta.env.VITE_FAS_TITLE_HINT_ENABLED
const _originalObjectiveHintEnv = import.meta.env.VITE_FAS_OBJECTIVE_HINT_ENABLED

beforeEach(() => {
  vi.clearAllMocks()
  // Reset the persistent detectTitleMatch implementation (clearAllMocks
  // clears call history but not the mocked return value).
  mockDetectTitle.mockResolvedValue({ matched: false, title: null })
  import.meta.env.VITE_ENGINE_HIATUS_MODE = ''
  // Force both hints enabled — the default. Only overridden by the
  // explicit "rollback flag disables hint" tests.
  delete import.meta.env.VITE_FAS_TITLE_HINT_ENABLED
  delete import.meta.env.VITE_FAS_OBJECTIVE_HINT_ENABLED
})

afterAll(() => {
  if (_originalHiatusEnv === undefined) {
    delete import.meta.env.VITE_ENGINE_HIATUS_MODE
  } else {
    import.meta.env.VITE_ENGINE_HIATUS_MODE = _originalHiatusEnv
  }
  if (_originalTitleHintEnv === undefined) {
    delete import.meta.env.VITE_FAS_TITLE_HINT_ENABLED
  } else {
    import.meta.env.VITE_FAS_TITLE_HINT_ENABLED = _originalTitleHintEnv
  }
  if (_originalObjectiveHintEnv === undefined) {
    delete import.meta.env.VITE_FAS_OBJECTIVE_HINT_ENABLED
  } else {
    import.meta.env.VITE_FAS_OBJECTIVE_HINT_ENABLED = _originalObjectiveHintEnv
  }
})

describe('FindAShow', () => {
  it('renders search form with all controls', () => {
    renderWithProviders(<FindAShow />)
    expect(screen.getByPlaceholderText(/gritty military drama/)).toBeInTheDocument()
    expect(screen.getByText('Execute Search')).toBeInTheDocument()
    expect(screen.getByText('All')).toBeInTheDocument()
    expect(screen.getByText('Pre-1990')).toBeInTheDocument()
    expect(screen.getByText('1990s')).toBeInTheDocument()
    expect(screen.getByText('2000s')).toBeInTheDocument()
    expect(screen.getByText('2010s')).toBeInTheDocument()
    expect(screen.getByText('2020s')).toBeInTheDocument()
  })

  it('pre-fills search box from seedQuery without auto-firing the search', async () => {
    // Regression guard: seedQuery from /try?q=... must only pre-fill
    // the input. The pre-flight controls (watchlist exclusion, decade,
    // mode, rating ceiling) need to be visible before the user commits
    // one of their daily-quota searches.
    const onSeedConsumed = vi.fn()
    renderWithProviders(<FindAShow seedQuery="cozy slice of life" onSeedConsumed={onSeedConsumed} />)

    // Search box is pre-filled with the seed value.
    const searchBox = screen.getByPlaceholderText(/gritty military drama/) as HTMLInputElement
    expect(searchBox.value).toBe('cozy slice of life')

    // Parent gets notified so the seed can be cleared from its state.
    expect(onSeedConsumed).toHaveBeenCalledTimes(1)

    // Load-bearing: no API call was made. Quota preserved.
    expect(mockPost).not.toHaveBeenCalled()
  })

  it('disables submit button when query is empty', () => {
    renderWithProviders(<FindAShow />)
    expect(screen.getByText('Execute Search')).toBeDisabled()
  })

  it('enables submit button when query is entered', async () => {
    const user = userEvent.setup()
    renderWithProviders(<FindAShow />)
    await user.type(screen.getByPlaceholderText(/gritty military drama/), 'sci-fi thriller')
    expect(screen.getByText('Execute Search')).toBeEnabled()
  })

  it('shows a clear button when the input has text and empties it on click', async () => {
    const user = userEvent.setup()
    renderWithProviders(<FindAShow />)
    const input = screen.getByPlaceholderText(/gritty military drama/) as HTMLInputElement

    // Empty input — no clear button
    expect(screen.queryByLabelText('Clear search')).not.toBeInTheDocument()

    // Type something — clear button appears
    await user.type(input, 'sci-fi thriller')
    const clearBtn = screen.getByLabelText('Clear search')
    expect(clearBtn).toBeInTheDocument()
    expect(input.value).toBe('sci-fi thriller')

    // Click clear — input empties and button disappears
    await user.click(clearBtn)
    expect(input.value).toBe('')
    expect(screen.queryByLabelText('Clear search')).not.toBeInTheDocument()
  })

  it('shows loading state during search', async () => {
    // Make the post hang so we can observe loading state
    mockPost.mockImplementation(() => new Promise(() => {}))
    const user = userEvent.setup()
    renderWithProviders(<FindAShow />)
    await user.type(screen.getByPlaceholderText(/gritty military drama/), 'test query')
    await user.click(screen.getByText('Execute Search'))
    expect(screen.getByText('Searching...')).toBeInTheDocument()
  })

  it('displays results on successful search', async () => {
    mockPost.mockResolvedValueOnce(makeSearchResponse({
      pool: [makeResult(), makeResult({ profile: { ...makeResult().profile, id: 2, title: 'Psycho-Pass' } })],
      pool_size: 2,
    }))
    const user = userEvent.setup()
    renderWithProviders(<FindAShow />)
    await user.type(screen.getByPlaceholderText(/gritty military drama/), 'dystopian thriller')
    await user.click(screen.getByText('Execute Search'))

    await waitFor(() => {
      expect(screen.getByText('Steins;Gate')).toBeInTheDocument()
      expect(screen.getByText('Psycho-Pass')).toBeInTheDocument()
    })
  })

  it('displays error on failed search', async () => {
    mockPost.mockResolvedValueOnce(makeSearchResponse({
      success: false,
      error: 'Query too vague',
    }))
    const user = userEvent.setup()
    renderWithProviders(<FindAShow />)
    await user.type(screen.getByPlaceholderText(/gritty military drama/), 'asdf')
    await user.click(screen.getByText('Execute Search'))

    await waitFor(() => {
      expect(screen.getByText('Query too vague')).toBeInTheDocument()
    })
  })

  it('displays error on network failure', async () => {
    mockPost.mockRejectedValueOnce(new Error('Network error'))
    const user = userEvent.setup()
    renderWithProviders(<FindAShow />)
    await user.type(screen.getByPlaceholderText(/gritty military drama/), 'test')
    await user.click(screen.getByText('Execute Search'))

    await waitFor(() => {
      expect(screen.getByText('Network error')).toBeInTheDocument()
    })
  })

  it('sends correct lens and era in request', async () => {
    mockPost.mockResolvedValueOnce(makeSearchResponse())
    const user = userEvent.setup()
    renderWithProviders(<FindAShow />)
    await user.type(screen.getByPlaceholderText(/gritty military drama/), 'sports anime')
    await user.click(screen.getByLabelText('2010s'))
    await user.click(screen.getByText('Execute Search'))

    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledWith('/api/search', {
        query: 'sports anime',
        lens: 'Intelligent Search',
        year_min: 2010,
        year_max: 2019,
        era_label: '2010s',
        rating_max: null,
      }, expect.anything())
    })
  })

  it('hides era filter and omits era when Objective Rankings selected', async () => {
    mockPost.mockResolvedValueOnce(makeSearchResponse())
    const user = userEvent.setup()
    renderWithProviders(<FindAShow />)
    await user.type(screen.getByPlaceholderText(/gritty military drama/), 'top romance')
    await user.click(screen.getByRole('tab', { name: 'Objective Rankings' }))

    // Decade radio buttons should be hidden in objective mode
    expect(screen.queryByText('1990s')).not.toBeInTheDocument()
    expect(screen.queryByText('2000s')).not.toBeInTheDocument()

    await user.click(screen.getByText('Execute Search'))

    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledWith('/api/search', {
        query: 'top romance',
        lens: 'Objective Rankings',
        year_min: null,
        year_max: null,
        era_label: null,
        rating_max: null,
      }, expect.anything())
    })
  })

  it('shows pagination when results exceed page size', async () => {
    const pool = Array.from({ length: 8 }, (_, i) =>
      makeResult({ profile: { ...makeResult().profile, id: i + 1, title: `Show ${i + 1}` } })
    )
    mockPost.mockResolvedValueOnce(makeSearchResponse({ pool, pool_size: 8 }))
    const user = userEvent.setup()
    renderWithProviders(<FindAShow />)
    await user.type(screen.getByPlaceholderText(/gritty military drama/), 'test')
    await user.click(screen.getByText('Execute Search'))

    await waitFor(() => {
      expect(screen.getByText('Show 1')).toBeInTheDocument()
      expect(screen.getByText('Show Me More')).toBeInTheDocument()
      expect(screen.queryByText('Previous')).not.toBeInTheDocument()
    })
  })

  it('paginates forward and backward', async () => {
    const pool = Array.from({ length: 8 }, (_, i) =>
      makeResult({ profile: { ...makeResult().profile, id: i + 1, title: `Show ${i + 1}` } })
    )
    mockPost.mockResolvedValueOnce(makeSearchResponse({ pool, pool_size: 8 }))
    const user = userEvent.setup()
    renderWithProviders(<FindAShow />)
    await user.type(screen.getByPlaceholderText(/gritty military drama/), 'test')
    await user.click(screen.getByText('Execute Search'))

    // Page 1: shows 1-5
    await waitFor(() => expect(screen.getByText('Show 1')).toBeInTheDocument())
    expect(screen.getByText(/1 - 5 of 8/)).toBeInTheDocument()

    // Go to page 2
    await user.click(screen.getByText('Show Me More'))
    expect(screen.getByText('Show 6')).toBeInTheDocument()
    expect(screen.queryByText('Show 1')).not.toBeInTheDocument()
    expect(screen.getByText('Previous')).toBeInTheDocument()
    expect(screen.queryByText('Show Me More')).not.toBeInTheDocument()

    // Go back to page 1
    await user.click(screen.getByText('Previous'))
    expect(screen.getByText('Show 1')).toBeInTheDocument()
  })

  it('displays search timing after successful search', async () => {
    mockPost.mockResolvedValueOnce(makeSearchResponse({
      pool: [makeResult()],
      pool_size: 1,
    }))
    const user = userEvent.setup()
    renderWithProviders(<FindAShow />)
    await user.type(screen.getByPlaceholderText(/gritty military drama/), 'mecha anime')
    await user.click(screen.getByText('Execute Search'))

    await waitFor(() => {
      // Timing text appears with format "in X.Xs"
      expect(screen.getByText(/in \d+\.\d+s/)).toBeInTheDocument()
    })
  })

  it('hides match confidence for Objective Rankings', async () => {
    mockPost.mockResolvedValueOnce(makeSearchResponse({
      pool: [makeResult()],
      pool_size: 1,
    }))
    const user = userEvent.setup()
    renderWithProviders(<FindAShow />)
    await user.type(screen.getByPlaceholderText(/gritty military drama/), 'top sports')
    await user.click(screen.getByRole('tab', { name: 'Objective Rankings' }))
    await user.click(screen.getByText('Execute Search'))

    await waitFor(() => {
      expect(screen.getByText('Direct DB Hit')).toBeInTheDocument()
      expect(screen.queryByText('87%')).not.toBeInTheDocument()
    })
  })

  // --- MAL rating filter ---

  it('renders the rating dropdown with all four options', () => {
    renderWithProviders(<FindAShow />)
    const select = screen.getByLabelText('MAL content rating ceiling') as HTMLSelectElement
    expect(select).toBeInTheDocument()
    const optionValues = Array.from(select.options).map(o => o.value)
    expect(optionValues).toEqual(['', 'PG-13', 'PG', 'G'])
    // Default is "Any rating" (empty value)
    expect(select.value).toBe('')
  })

  it('threads selected rating ceiling into the request body', async () => {
    mockPost.mockResolvedValueOnce(makeSearchResponse())
    const user = userEvent.setup()
    renderWithProviders(<FindAShow />)
    await user.type(screen.getByPlaceholderText(/gritty military drama/), 'wholesome adventure')
    await user.selectOptions(screen.getByLabelText('MAL content rating ceiling'), 'PG')
    await user.click(screen.getByText('Execute Search'))

    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledWith('/api/search', expect.objectContaining({
        rating_max: 'PG',
      }), expect.anything())
    })
  })

  it('omits rating_max (sends null) when "Any rating" is selected', async () => {
    mockPost.mockResolvedValueOnce(makeSearchResponse())
    const user = userEvent.setup()
    renderWithProviders(<FindAShow />)
    await user.type(screen.getByPlaceholderText(/gritty military drama/), 'test')
    // Select rating then revert to "Any rating"
    await user.selectOptions(screen.getByLabelText('MAL content rating ceiling'), 'G')
    await user.selectOptions(screen.getByLabelText('MAL content rating ceiling'), 'Any rating')
    await user.click(screen.getByText('Execute Search'))

    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledWith('/api/search', expect.objectContaining({
        rating_max: null,
      }), expect.anything())
    })
  })

  it('hides rating dropdown when Objective Rankings selected', async () => {
    const user = userEvent.setup()
    renderWithProviders(<FindAShow />)
    expect(screen.getByLabelText('MAL content rating ceiling')).toBeInTheDocument()

    await user.click(screen.getByRole('tab', { name: 'Objective Rankings' }))

    expect(screen.queryByLabelText('MAL content rating ceiling')).not.toBeInTheDocument()
  })

  it('drops rating_max from payload when switching to Objective Rankings after selection', async () => {
    mockPost.mockResolvedValueOnce(makeSearchResponse())
    const user = userEvent.setup()
    renderWithProviders(<FindAShow />)
    await user.type(screen.getByPlaceholderText(/gritty military drama/), 'top sports')
    // Pick a rating, then switch lens (dropdown disappears but state persists)
    await user.selectOptions(screen.getByLabelText('MAL content rating ceiling'), 'PG')
    await user.click(screen.getByRole('tab', { name: 'Objective Rankings' }))
    await user.click(screen.getByText('Execute Search'))

    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledWith('/api/search', expect.objectContaining({
        lens: 'Objective Rankings',
        rating_max: null,
      }), expect.anything())
    })
  })

  // --- "I Don't Know" mode banner ---

  it('renders LowConfidenceBanner when backend signals low_confidence on success-with-results', async () => {
    mockPost.mockResolvedValueOnce(makeSearchResponse({
      pool: [makeResult({ match_confidence: 70 })],
      pool_size: 1,
      low_confidence: true,
    }))
    const user = userEvent.setup()
    renderWithProviders(<FindAShow />)
    await user.type(screen.getByPlaceholderText(/gritty military drama/), 'tough query')
    await user.click(screen.getByText('Execute Search'))

    await waitFor(() => {
      expect(screen.getByTestId('low-confidence-banner')).toBeInTheDocument()
    })
    // Results still render alongside the banner
    expect(screen.getByText('Steins;Gate')).toBeInTheDocument()
  })

  it('renders LowConfidenceBanner instead of red error block on empty-result low_confidence response', async () => {
    mockPost.mockResolvedValueOnce(makeSearchResponse({
      success: false,
      low_confidence: true,
      error: 'No classified intelligence matched...',
    }))
    const user = userEvent.setup()
    renderWithProviders(<FindAShow />)
    await user.type(screen.getByPlaceholderText(/gritty military drama/), 'gibberish xyzzy')
    await user.click(screen.getByText('Execute Search'))

    await waitFor(() => {
      expect(screen.getByTestId('low-confidence-banner')).toBeInTheDocument()
    })
    // Yellow banner replaces red error block — the engine isn't broken,
    // it's being honest about uncertainty
    expect(screen.queryByText('No classified intelligence matched...')).not.toBeInTheDocument()
  })

  it('still shows red error block when failure has no low_confidence flag', async () => {
    mockPost.mockResolvedValueOnce(makeSearchResponse({
      success: false,
      low_confidence: false,
      error: 'System cooling down. Try again in 60s.',
    }))
    const user = userEvent.setup()
    renderWithProviders(<FindAShow />)
    await user.type(screen.getByPlaceholderText(/gritty military drama/), 'rate-limited query')
    await user.click(screen.getByText('Execute Search'))

    await waitFor(() => {
      expect(screen.getByText('System cooling down. Try again in 60s.')).toBeInTheDocument()
    })
    expect(screen.queryByTestId('low-confidence-banner')).not.toBeInTheDocument()
  })

  it('does not render LowConfidenceBanner when backend low_confidence is false', async () => {
    mockPost.mockResolvedValueOnce(makeSearchResponse({
      pool: [makeResult({ match_confidence: 98 })],
      pool_size: 1,
      low_confidence: false,
    }))
    const user = userEvent.setup()
    renderWithProviders(<FindAShow />)
    await user.type(screen.getByPlaceholderText(/gritty military drama/), 'strong query')
    await user.click(screen.getByText('Execute Search'))

    await waitFor(() => {
      expect(screen.getByText('Steins;Gate')).toBeInTheDocument()
    })
    expect(screen.queryByTestId('low-confidence-banner')).not.toBeInTheDocument()
  })

  it('does not render LowConfidenceBanner when backend omits the flag (backwards compat)', async () => {
    mockPost.mockResolvedValueOnce(makeSearchResponse({
      pool: [makeResult()],
      pool_size: 1,
    }))
    const user = userEvent.setup()
    renderWithProviders(<FindAShow />)
    await user.type(screen.getByPlaceholderText(/gritty military drama/), 'query')
    await user.click(screen.getByText('Execute Search'))

    await waitFor(() => {
      expect(screen.getByText('Steins;Gate')).toBeInTheDocument()
    })
    expect(screen.queryByTestId('low-confidence-banner')).not.toBeInTheDocument()
  })

  it('does not render the hiatus card when VITE_ENGINE_HIATUS_MODE is unset (default)', () => {
    renderWithProviders(<FindAShow />)
    expect(screen.queryByTestId('hiatus-card')).not.toBeInTheDocument()
    // Normal search UI is present
    expect(screen.getByText('Execute Search')).toBeInTheDocument()
  })

  it('clears the banner on subsequent successful search', async () => {
    mockPost.mockResolvedValueOnce(makeSearchResponse({
      success: false,
      low_confidence: true,
      error: 'nothing found',
    }))
    const user = userEvent.setup()
    renderWithProviders(<FindAShow />)
    await user.type(screen.getByPlaceholderText(/gritty military drama/), 'weak')
    await user.click(screen.getByText('Execute Search'))
    await waitFor(() => {
      expect(screen.getByTestId('low-confidence-banner')).toBeInTheDocument()
    })

    mockPost.mockResolvedValueOnce(makeSearchResponse({
      pool: [makeResult()],
      pool_size: 1,
      low_confidence: false,
    }))
    await user.clear(screen.getByPlaceholderText(/gritty military drama/))
    await user.type(screen.getByPlaceholderText(/gritty military drama/), 'strong query')
    await user.click(screen.getByText('Execute Search'))

    await waitFor(() => {
      expect(screen.queryByTestId('low-confidence-banner')).not.toBeInTheDocument()
    })
  })
})

describe('FindAShow — hiatus mode', () => {
  beforeEach(() => {
    import.meta.env.VITE_ENGINE_HIATUS_MODE = 'true'
  })

  it('renders the hiatus card and hides the search UI', () => {
    renderWithProviders(<FindAShow />)
    const card = screen.getByTestId('hiatus-card')
    expect(card).toBeInTheDocument()
    expect(card).toHaveTextContent(/recommendation engine is on hiatus/i)
    // Search form is gone
    expect(screen.queryByText('Execute Search')).not.toBeInTheDocument()
    expect(screen.queryByPlaceholderText(/gritty military drama/)).not.toBeInTheDocument()
  })
})

// --- Title-lookup hint ---

describe('FindAShow — title-lookup hint', () => {
  it('renders the hint with non-empty copy when the query is a title AND results are returned', async () => {
    mockDetectTitle.mockResolvedValueOnce({ matched: true, title: 'Fullmetal Alchemist: Brotherhood' })
    mockPost.mockResolvedValueOnce(makeSearchResponse({
      pool: [makeResult({ profile: { ...makeResult().profile, title: 'Some Related Show' } })],
      pool_size: 1,
    }))
    const user = userEvent.setup()
    renderWithProviders(<FindAShow />)
    await user.type(screen.getByPlaceholderText(/gritty military drama/), 'Fullmetal Alchemist: Brotherhood')
    await user.click(screen.getByText('Execute Search'))

    await waitFor(() => {
      const hint = screen.getByTestId('title-lookup-hint')
      expect(hint).toBeInTheDocument()
      expect(hint).toHaveTextContent(/looks like a specific show/)
      expect(hint).toHaveTextContent(/for the full profile/)
    })
    // Non-empty variant should NOT include the "No matches" empty-state copy
    expect(screen.getByTestId('title-lookup-hint')).not.toHaveTextContent(/No matches/)
  })

  it('renders the hint with empty-state copy when the query is a title AND pool is empty', async () => {
    mockDetectTitle.mockResolvedValueOnce({ matched: true, title: 'Gokudo' })
    mockPost.mockResolvedValueOnce(makeSearchResponse())
    const user = userEvent.setup()
    renderWithProviders(<FindAShow />)
    await user.type(screen.getByPlaceholderText(/gritty military drama/), 'Gokudo')
    await user.click(screen.getByText('Execute Search'))

    await waitFor(() => {
      const hint = screen.getByTestId('title-lookup-hint')
      expect(hint).toHaveTextContent(/No matches for/)
      expect(hint).toHaveTextContent(/as a vibe search/)
      expect(hint).toHaveTextContent(/Gokudo/)
    })
  })

  it('does NOT render the hint when the query is not a title match', async () => {
    // Default mock returns matched:false — this is the vast majority
    // of real user queries (vibe/mood/genre searches).
    mockPost.mockResolvedValueOnce(makeSearchResponse({
      pool: [makeResult()],
      pool_size: 1,
    }))
    const user = userEvent.setup()
    renderWithProviders(<FindAShow />)
    await user.type(screen.getByPlaceholderText(/gritty military drama/), 'gritty military drama')
    await user.click(screen.getByText('Execute Search'))

    await waitFor(() => {
      expect(screen.getByText('Steins;Gate')).toBeInTheDocument()
    })
    expect(screen.queryByTestId('title-lookup-hint')).not.toBeInTheDocument()
  })

  it('links the hint to the Encyclopedia route with the canonical title', async () => {
    // Load-bearing: link should carry the CANONICAL title from the vault
    // (not the raw user input), so casing/punctuation matches the show
    // as stored. Encyclopedia auto-fires on the seedTitle from the URL.
    mockDetectTitle.mockResolvedValueOnce({ matched: true, title: 'Spirited Away' })
    mockPost.mockResolvedValueOnce(makeSearchResponse())
    const user = userEvent.setup()
    renderWithProviders(<FindAShow />)
    // User types lowercase; canonical title comes back title-cased
    await user.type(screen.getByPlaceholderText(/gritty military drama/), 'spirited away')
    await user.click(screen.getByText('Execute Search'))

    await waitFor(() => {
      const link = screen.getByRole('link', { name: /Encyclopedia/i })
      expect(link).toHaveAttribute('href', '/try?mode=encyclopedia&q=Spirited%20Away')
    })
  })

  it('clears a stale hint on the next successful search', async () => {
    // Regression guard: state should reset per search, not linger.
    // First search — title match.
    mockDetectTitle.mockResolvedValueOnce({ matched: true, title: 'Gokudo' })
    mockPost.mockResolvedValueOnce(makeSearchResponse())
    const user = userEvent.setup()
    renderWithProviders(<FindAShow />)
    await user.type(screen.getByPlaceholderText(/gritty military drama/), 'Gokudo')
    await user.click(screen.getByText('Execute Search'))
    await waitFor(() => {
      expect(screen.getByTestId('title-lookup-hint')).toBeInTheDocument()
    })

    // Second search — genuine vibe query, no title match.
    mockDetectTitle.mockResolvedValueOnce({ matched: false, title: null })
    mockPost.mockResolvedValueOnce(makeSearchResponse({
      pool: [makeResult()],
      pool_size: 1,
    }))
    await user.clear(screen.getByPlaceholderText(/gritty military drama/))
    await user.type(screen.getByPlaceholderText(/gritty military drama/), 'cozy slice of life')
    await user.click(screen.getByText('Execute Search'))

    await waitFor(() => {
      expect(screen.queryByTestId('title-lookup-hint')).not.toBeInTheDocument()
    })
  })

  it('does NOT fire detection when VITE_FAS_TITLE_HINT_ENABLED is false', async () => {
    // Rollback flag disables the whole feature — detection skipped
    // entirely (no wasted JSON fetch), hint never renders.
    import.meta.env.VITE_FAS_TITLE_HINT_ENABLED = 'false'
    mockPost.mockResolvedValueOnce(makeSearchResponse())
    const user = userEvent.setup()
    renderWithProviders(<FindAShow />)
    await user.type(screen.getByPlaceholderText(/gritty military drama/), 'Fullmetal Alchemist: Brotherhood')
    await user.click(screen.getByText('Execute Search'))

    // Wait for the search to complete (via the pool-count message)
    await waitFor(() => {
      expect(mockPost).toHaveBeenCalled()
    })
    expect(mockDetectTitle).not.toHaveBeenCalled()
    expect(screen.queryByTestId('title-lookup-hint')).not.toBeInTheDocument()
  })

  // --- Segmented control mode selector ---

  it('renders three segmented control buttons in a tablist', () => {
    renderWithProviders(<FindAShow />)
    const tabs = screen.getAllByRole('tab')
    expect(tabs).toHaveLength(3)
    expect(tabs[0]).toHaveTextContent('Intelligent Search')
    expect(tabs[1]).toHaveTextContent('Objective Rankings')
    expect(tabs[2]).toHaveTextContent('Staff/Studio')
  })

  it('defaults Intelligent Search as selected on mount', () => {
    renderWithProviders(<FindAShow />)
    expect(screen.getByRole('tab', { name: 'Intelligent Search' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('tab', { name: 'Objective Rankings' })).toHaveAttribute('aria-selected', 'false')
    expect(screen.getByRole('tab', { name: 'Staff/Studio' })).toHaveAttribute('aria-selected', 'false')
  })

  it('toggles aria-selected when clicking a different segment', async () => {
    const user = userEvent.setup()
    renderWithProviders(<FindAShow />)
    await user.click(screen.getByRole('tab', { name: 'Staff/Studio' }))
    expect(screen.getByRole('tab', { name: 'Staff/Studio' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('tab', { name: 'Intelligent Search' })).toHaveAttribute('aria-selected', 'false')
  })

  it('preserves query text when switching modes', async () => {
    const user = userEvent.setup()
    renderWithProviders(<FindAShow />)
    const input = screen.getByPlaceholderText(/gritty military drama/) as HTMLInputElement
    await user.type(input, 'shows by Watanabe')
    expect(input.value).toBe('shows by Watanabe')
    await user.click(screen.getByRole('tab', { name: 'Objective Rankings' }))
    expect(input.value).toBe('shows by Watanabe')
    await user.click(screen.getByRole('tab', { name: 'Staff/Studio' }))
    expect(input.value).toBe('shows by Watanabe')
    await user.click(screen.getByRole('tab', { name: 'Intelligent Search' }))
    expect(input.value).toBe('shows by Watanabe')
  })

  it('hides era filter and rating dropdown when Staff/Studio is selected', async () => {
    const user = userEvent.setup()
    renderWithProviders(<FindAShow />)
    // Sanity: filters visible on Intelligent Search
    expect(screen.getByText('1990s')).toBeInTheDocument()
    expect(screen.getByLabelText('MAL content rating ceiling')).toBeInTheDocument()
    // Switch to Staff/Studio
    await user.click(screen.getByRole('tab', { name: 'Staff/Studio' }))
    // Both hidden
    expect(screen.queryByText('1990s')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('MAL content rating ceiling')).not.toBeInTheDocument()
  })

  it('surfaces mode description in title tooltip on each segment', () => {
    renderWithProviders(<FindAShow />)
    expect(screen.getByRole('tab', { name: 'Intelligent Search' })).toHaveAttribute(
      'title',
      expect.stringContaining('AI-powered semantic search'),
    )
    expect(screen.getByRole('tab', { name: 'Staff/Studio' })).toHaveAttribute(
      'title',
      expect.stringContaining('Filter by director'),
    )
  })

  it('hides Staff/Studio segment when VITE_STAFF_STUDIO_ENABLED is false', () => {
    // Rollback flag — masks the third mode entirely; existing two modes
    // and their behavior are untouched.
    import.meta.env.VITE_STAFF_STUDIO_ENABLED = 'false'
    try {
      renderWithProviders(<FindAShow />)
      const tabs = screen.getAllByRole('tab')
      expect(tabs).toHaveLength(2)
      expect(screen.queryByRole('tab', { name: 'Staff/Studio' })).not.toBeInTheDocument()
    } finally {
      delete import.meta.env.VITE_STAFF_STUDIO_ENABLED
    }
  })

  it('swaps query input for chip input when Staff/Studio is active', async () => {
    const user = userEvent.setup()
    renderWithProviders(<FindAShow />)
    // Baseline: query input present, chip input absent
    expect(screen.getByPlaceholderText(/gritty military drama/)).toBeInTheDocument()
    expect(screen.queryByPlaceholderText(/Add a director/)).not.toBeInTheDocument()
    // Switch to Staff/Studio
    await user.click(screen.getByRole('tab', { name: 'Staff/Studio' }))
    // Query input gone; chip input placeholder present
    expect(screen.queryByPlaceholderText(/gritty military drama/)).not.toBeInTheDocument()
    expect(screen.getByPlaceholderText(/Add a director/)).toBeInTheDocument()
  })

  it('submit is disabled on Staff/Studio until at least one chip is added', async () => {
    // Mock autocomplete response so typing produces a suggestion.
    mockPost.mockResolvedValueOnce({
      persons: [{
        person_mal_id: 2009,
        name_display: 'Shinichirou Watanabe',
        total_count: 10,
        roles: [{ role: 'Director', count: 10 }],
      }],
      studios: [],
    })
    const user = userEvent.setup()
    renderWithProviders(<FindAShow />)
    await user.click(screen.getByRole('tab', { name: 'Staff/Studio' }))
    // No chips yet — submit disabled
    expect(screen.getByText('Execute Search')).toBeDisabled()

    // Type into chip input, wait for autocomplete, click a suggestion
    const chipInput = screen.getByPlaceholderText(/Add a director/)
    await user.type(chipInput, 'watanabe')
    // Wait for the pinned "Any role" option — accessible name concatenates
    // prefix + label + count so we match against the full text.
    const anyRoleOption = await screen.findByRole('option', {
      name: /Any role.*Shinichirou Watanabe/,
    })
    await user.click(anyRoleOption)
    // Chip now present — submit should be enabled
    expect(screen.getByText('Execute Search')).toBeEnabled()
  })

  it('sends staff_filter payload with chip data when Staff/Studio submits', async () => {
    mockPost
      // First call: autocomplete
      .mockResolvedValueOnce({
        persons: [{
          person_mal_id: 2009,
          name_display: 'Shinichirou Watanabe',
          total_count: 10,
          roles: [{ role: 'Director', count: 10 }],
        }],
        studios: [],
      })
      // Second call: /api/search
      .mockResolvedValueOnce(makeSearchResponse())

    const user = userEvent.setup()
    renderWithProviders(<FindAShow />)
    await user.click(screen.getByRole('tab', { name: 'Staff/Studio' }))
    await user.type(screen.getByPlaceholderText(/Add a director/), 'watanabe')
    // Click the role-scoped Director option (accessible name matches
    // "Director · Watanabe (10 shows)")
    const directorOption = await screen.findByRole('option', {
      name: /^Director .*Shinichirou Watanabe/,
    })
    await user.click(directorOption)
    await user.click(screen.getByText('Execute Search'))

    await waitFor(() => {
      // Find the /api/search call specifically
      const searchCall = mockPost.mock.calls.find(c => c[0] === '/api/search')
      expect(searchCall).toBeDefined()
      const body = searchCall![1] as Record<string, unknown>
      expect(body.lens).toBe('Staff/Studio')
      expect(body.query).toBe('')
      expect(body.staff_filter).toEqual({
        persons: [{ person_mal_id: 2009, role: 'Director' }],
        studios: [],
      })
      // Era + rating are dropped in Staff/Studio mode
      expect(body.year_min).toBeNull()
      expect(body.year_max).toBeNull()
      expect(body.era_label).toBeNull()
      expect(body.rating_max).toBeNull()
    })
  })

  it('preserves chips across mode switches', async () => {
    mockPost.mockResolvedValueOnce({
      persons: [{
        person_mal_id: 508,
        name_display: 'Yoko Kanno',
        total_count: 29,
        roles: [{ role: 'Music', count: 29 }],
      }],
      studios: [],
    })
    const user = userEvent.setup()
    renderWithProviders(<FindAShow />)
    await user.click(screen.getByRole('tab', { name: 'Staff/Studio' }))
    await user.type(screen.getByPlaceholderText(/Add a director/), 'kanno')
    const anyRoleOption = await screen.findByRole('option', {
      name: /Any role.*Yoko Kanno/,
    })
    await user.click(anyRoleOption)
    // Chip now visible with name displayed
    expect(screen.getByText('Yoko Kanno')).toBeInTheDocument()
    // Switch away, then back — chip should persist
    await user.click(screen.getByRole('tab', { name: 'Intelligent Search' }))
    expect(screen.queryByText('Yoko Kanno')).not.toBeInTheDocument()  // hidden while off-mode
    await user.click(screen.getByRole('tab', { name: 'Staff/Studio' }))
    expect(screen.getByText('Yoko Kanno')).toBeInTheDocument()
  })

  it('removes a chip when the × is clicked', async () => {
    mockPost.mockResolvedValueOnce({
      persons: [{
        person_mal_id: 508,
        name_display: 'Yoko Kanno',
        total_count: 29,
        roles: [{ role: 'Music', count: 29 }],
      }],
      studios: [],
    })
    const user = userEvent.setup()
    renderWithProviders(<FindAShow />)
    await user.click(screen.getByRole('tab', { name: 'Staff/Studio' }))
    await user.type(screen.getByPlaceholderText(/Add a director/), 'kanno')
    const anyRoleOption = await screen.findByRole('option', {
      name: /Any role.*Yoko Kanno/,
    })
    await user.click(anyRoleOption)
    expect(screen.getByText('Yoko Kanno')).toBeInTheDocument()
    // Click the remove-chip button
    const removeButton = screen.getByLabelText('Remove Yoko Kanno')
    await user.click(removeButton)
    expect(screen.queryByText('Yoko Kanno')).not.toBeInTheDocument()
    expect(screen.getByText('Execute Search')).toBeDisabled()
  })
})

// --- Objective-shape hint on Intelligent Search ---

describe('FindAShow — objective-shape hint', () => {
  it('renders the hint when the query looks like a ranking request', async () => {
    mockPost.mockResolvedValueOnce(makeSearchResponse({
      pool: [makeResult()],
      pool_size: 1,
    }))
    const user = userEvent.setup()
    renderWithProviders(<FindAShow />)
    await user.type(screen.getByPlaceholderText(/gritty military drama/), 'top 10 sports anime')
    await user.click(screen.getByText('Execute Search'))

    await waitFor(() => {
      const hint = screen.getByTestId('objective-hint')
      expect(hint).toBeInTheDocument()
      expect(hint).toHaveTextContent(/ranking query/i)
    })
    // Both affordances present.
    expect(screen.getByRole('button', { name: /Switch to Objective Rankings/i })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /Top Lists/i })).toHaveAttribute('href', '/top')
  })

  it('renders the hint on a superlative + qualifier query', async () => {
    mockPost.mockResolvedValueOnce(makeSearchResponse({
      pool: [makeResult()],
      pool_size: 1,
    }))
    const user = userEvent.setup()
    renderWithProviders(<FindAShow />)
    await user.type(screen.getByPlaceholderText(/gritty military drama/), 'best mecha of the 90s')
    await user.click(screen.getByText('Execute Search'))

    await waitFor(() => {
      expect(screen.getByTestId('objective-hint')).toBeInTheDocument()
    })
  })

  it('does NOT render the hint on a vibe query', async () => {
    mockPost.mockResolvedValueOnce(makeSearchResponse({
      pool: [makeResult()],
      pool_size: 1,
    }))
    const user = userEvent.setup()
    renderWithProviders(<FindAShow />)
    await user.type(screen.getByPlaceholderText(/gritty military drama/), 'sincere romcom without fanservice')
    await user.click(screen.getByText('Execute Search'))

    await waitFor(() => {
      expect(screen.getByText('Steins;Gate')).toBeInTheDocument()
    })
    expect(screen.queryByTestId('objective-hint')).not.toBeInTheDocument()
  })

  it('does NOT render the hint when an anti-pattern is present', async () => {
    // Regression guard for the anti-list — "greatest villain of all time"
    // has a superlative but reads as a vibe query about character trait.
    mockPost.mockResolvedValueOnce(makeSearchResponse({
      pool: [makeResult()],
      pool_size: 1,
    }))
    const user = userEvent.setup()
    renderWithProviders(<FindAShow />)
    await user.type(screen.getByPlaceholderText(/gritty military drama/), 'greatest villain of all time')
    await user.click(screen.getByText('Execute Search'))

    await waitFor(() => {
      expect(screen.getByText('Steins;Gate')).toBeInTheDocument()
    })
    expect(screen.queryByTestId('objective-hint')).not.toBeInTheDocument()
  })

  it('switches lens to Objective Rankings when the button is clicked, preserving query text', async () => {
    // Load-bearing: the mode switch preserves query text so the user's
    // typed query survives the toggle. The hint then hides because it's
    // gated on lens === 'Intelligent Search' at render time.
    mockPost.mockResolvedValueOnce(makeSearchResponse({
      pool: [makeResult()],
      pool_size: 1,
    }))
    const user = userEvent.setup()
    renderWithProviders(<FindAShow />)
    const input = screen.getByPlaceholderText(/gritty military drama/) as HTMLInputElement
    await user.type(input, 'top 10 sports anime')
    await user.click(screen.getByText('Execute Search'))

    await waitFor(() => {
      expect(screen.getByTestId('objective-hint')).toBeInTheDocument()
    })

    // Click the mode-switch button inside the hint.
    await user.click(screen.getByRole('button', { name: /Switch to Objective Rankings/i }))

    // Objective Rankings tab is now selected.
    const objectiveTab = screen.getByRole('tab', { name: 'Objective Rankings' })
    expect(objectiveTab).toHaveAttribute('aria-selected', 'true')

    // Query text is preserved across the mode switch.
    expect(input.value).toBe('top 10 sports anime')

    // The hint hides because the render gate checks the current lens.
    expect(screen.queryByTestId('objective-hint')).not.toBeInTheDocument()
  })

  it('does NOT render the hint when the user submits from Objective Rankings mode', async () => {
    // OR mode is the target destination — no point suggesting they
    // switch to it when they're already there.
    mockPost.mockResolvedValueOnce(makeSearchResponse({
      pool: [makeResult()],
      pool_size: 1,
    }))
    const user = userEvent.setup()
    renderWithProviders(<FindAShow />)
    await user.click(screen.getByRole('tab', { name: 'Objective Rankings' }))
    await user.type(screen.getByPlaceholderText(/gritty military drama/), 'top 10 sports anime')
    await user.click(screen.getByText('Execute Search'))

    await waitFor(() => {
      expect(screen.getByText('Steins;Gate')).toBeInTheDocument()
    })
    expect(screen.queryByTestId('objective-hint')).not.toBeInTheDocument()
  })

  it('clears a stale hint on the next successful search', async () => {
    // Regression guard: state should reset per search, not linger.
    mockPost.mockResolvedValue(makeSearchResponse({
      pool: [makeResult()],
      pool_size: 1,
    }))
    const user = userEvent.setup()
    renderWithProviders(<FindAShow />)
    const input = screen.getByPlaceholderText(/gritty military drama/)

    // First search: ranking-shaped → hint fires.
    await user.type(input, 'top 10 sports anime')
    await user.click(screen.getByText('Execute Search'))
    await waitFor(() => {
      expect(screen.getByTestId('objective-hint')).toBeInTheDocument()
    })

    // Second search: vibe query → hint clears.
    await user.clear(input)
    await user.type(input, 'cozy slice of life')
    await user.click(screen.getByText('Execute Search'))
    await waitFor(() => {
      expect(screen.queryByTestId('objective-hint')).not.toBeInTheDocument()
    })
  })

  it('does NOT fire detection when VITE_FAS_OBJECTIVE_HINT_ENABLED is false', async () => {
    import.meta.env.VITE_FAS_OBJECTIVE_HINT_ENABLED = 'false'
    mockPost.mockResolvedValueOnce(makeSearchResponse({
      pool: [makeResult()],
      pool_size: 1,
    }))
    const user = userEvent.setup()
    renderWithProviders(<FindAShow />)
    await user.type(screen.getByPlaceholderText(/gritty military drama/), 'top 10 sports anime')
    await user.click(screen.getByText('Execute Search'))

    await waitFor(() => {
      expect(mockPost).toHaveBeenCalled()
    })
    expect(screen.queryByTestId('objective-hint')).not.toBeInTheDocument()
  })
})
