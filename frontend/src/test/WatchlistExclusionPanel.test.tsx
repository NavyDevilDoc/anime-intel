import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import WatchlistExclusionPanel from '../components/WatchlistExclusionPanel'
import type { WatchlistCache } from '../components/WatchlistExclusionPanel'

const LS_KEY = 'anime-intel-watchlist-v1'

function makeCache(overrides?: Partial<WatchlistCache>): WatchlistCache {
  return {
    source: 'mal',
    username: 'AnimeMathNerd',
    statuses: ['completed', 'dropped'],
    mal_ids: [1, 5, 16498],
    total_entries: 3,
    unmappable_count: 0,
    errors: [],
    fetched_at: new Date().toISOString(),
    ...overrides,
  }
}

const mockPost = vi.fn()
vi.mock('../lib/api', async () => {
  const actual = await vi.importActual<typeof import('../lib/api')>('../lib/api')
  return {
    ...actual,
    api: { ...actual.api, post: (...args: unknown[]) => mockPost(...args) },
  }
})

beforeEach(() => {
  window.localStorage.clear()
  mockPost.mockReset()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('WatchlistExclusionPanel — collapsed states', () => {
  it('renders the "Exclude shows you\'ve watched" chip with no cache', () => {
    const onListChange = vi.fn()
    render(<WatchlistExclusionPanel onListChange={onListChange} />)
    expect(
      screen.getByTestId('watchlist-panel-expand'),
    ).toHaveTextContent(/exclude shows you've already watched/i)
    // Mount effect should have fired once with null
    expect(onListChange).toHaveBeenCalledWith(null)
  })

  it('hydrates from localStorage and shows the collapsed-with-list state', () => {
    const cache = makeCache({ mal_ids: [1, 5, 16498, 20] })
    window.localStorage.setItem(LS_KEY, JSON.stringify(cache))
    const onListChange = vi.fn()
    render(<WatchlistExclusionPanel onListChange={onListChange} />)
    expect(screen.getByTestId('watchlist-panel-collapsed')).toBeInTheDocument()
    expect(screen.getByTestId('watchlist-panel-expand')).toHaveTextContent(/4 shows/i)
    expect(screen.getByTestId('watchlist-panel-expand')).toHaveTextContent(/MAL list/i)
    expect(onListChange).toHaveBeenCalledWith(
      expect.objectContaining({ mal_ids: [1, 5, 16498, 20] }),
    )
  })

  it('shows the unmappable caption when AniList catalog gap is non-zero', () => {
    const cache = makeCache({ source: 'anilist', unmappable_count: 12 })
    window.localStorage.setItem(LS_KEY, JSON.stringify(cache))
    render(<WatchlistExclusionPanel onListChange={vi.fn()} />)
    expect(screen.getByTestId('watchlist-unmappable-caption')).toHaveTextContent(
      /12 without MAL links/i,
    )
  })

  it('shows the stale-list chip when the cache is older than 7 days', () => {
    const eightDaysAgo = new Date(Date.now() - 8 * 86_400_000).toISOString()
    const cache = makeCache({ fetched_at: eightDaysAgo })
    window.localStorage.setItem(LS_KEY, JSON.stringify(cache))
    render(<WatchlistExclusionPanel onListChange={vi.fn()} />)
    expect(screen.getByTestId('watchlist-stale-chip')).toBeInTheDocument()
    expect(screen.getByTestId('watchlist-stale-chip')).toHaveTextContent(/refresh/i)
  })

  it('does not show stale chip when the cache is fresh (under 7 days)', () => {
    const cache = makeCache({
      fetched_at: new Date(Date.now() - 3 * 86_400_000).toISOString(),
    })
    window.localStorage.setItem(LS_KEY, JSON.stringify(cache))
    render(<WatchlistExclusionPanel onListChange={vi.fn()} />)
    expect(screen.queryByTestId('watchlist-stale-chip')).not.toBeInTheDocument()
  })

  it('Clear button removes the cache and fires onListChange(null)', async () => {
    const cache = makeCache()
    window.localStorage.setItem(LS_KEY, JSON.stringify(cache))
    const onListChange = vi.fn()
    render(<WatchlistExclusionPanel onListChange={onListChange} />)
    fireEvent.click(screen.getByTestId('watchlist-clear-collapsed'))
    expect(window.localStorage.getItem(LS_KEY)).toBeNull()
    // Initial mount call + the clear call
    expect(onListChange).toHaveBeenLastCalledWith(null)
  })
})

describe('WatchlistExclusionPanel — expanded form', () => {
  it('clicking the expand chip opens the form', () => {
    render(<WatchlistExclusionPanel onListChange={vi.fn()} />)
    fireEvent.click(screen.getByTestId('watchlist-panel-expand'))
    expect(screen.getByTestId('watchlist-panel-expanded')).toBeInTheDocument()
    expect(screen.getByTestId('watchlist-username-input')).toBeInTheDocument()
    expect(screen.getByTestId('watchlist-source-select')).toBeInTheDocument()
  })

  it('completed + dropped checkboxes are checked by default', () => {
    render(<WatchlistExclusionPanel onListChange={vi.fn()} />)
    fireEvent.click(screen.getByTestId('watchlist-panel-expand'))
    expect(screen.getByTestId('watchlist-status-completed')).toBeChecked()
    expect(screen.getByTestId('watchlist-status-dropped')).toBeChecked()
    expect(screen.getByTestId('watchlist-status-watching')).not.toBeChecked()
    expect(screen.getByTestId('watchlist-status-plan_to_watch')).not.toBeChecked()
    expect(screen.getByTestId('watchlist-status-on_hold')).not.toBeChecked()
  })

  it('empty username surfaces validation error without calling API', async () => {
    render(<WatchlistExclusionPanel onListChange={vi.fn()} />)
    fireEvent.click(screen.getByTestId('watchlist-panel-expand'))
    fireEvent.click(screen.getByTestId('watchlist-fetch'))
    await waitFor(() => {
      expect(screen.getByTestId('watchlist-error')).toHaveTextContent(/username is required/i)
    })
    expect(mockPost).not.toHaveBeenCalled()
  })

  it('zero statuses surfaces validation error without calling API', async () => {
    const user = userEvent.setup()
    render(<WatchlistExclusionPanel onListChange={vi.fn()} />)
    fireEvent.click(screen.getByTestId('watchlist-panel-expand'))
    await user.type(screen.getByTestId('watchlist-username-input'), 'AnimeMathNerd')
    fireEvent.click(screen.getByTestId('watchlist-status-completed'))
    fireEvent.click(screen.getByTestId('watchlist-status-dropped'))
    fireEvent.click(screen.getByTestId('watchlist-fetch'))
    await waitFor(() => {
      expect(screen.getByTestId('watchlist-error')).toHaveTextContent(/at least one status/i)
    })
    expect(mockPost).not.toHaveBeenCalled()
  })

  it('successful fetch writes the cache, fires onListChange, and collapses', async () => {
    const user = userEvent.setup()
    mockPost.mockResolvedValueOnce({
      source: 'mal',
      mal_ids: [41457, 31043, 16498],
      total_entries: 3,
      unmappable_entries: 0,
      errors: [],
    })
    const onListChange = vi.fn()
    render(<WatchlistExclusionPanel onListChange={onListChange} />)
    fireEvent.click(screen.getByTestId('watchlist-panel-expand'))
    await user.type(screen.getByTestId('watchlist-username-input'), 'AnimeMathNerd')
    fireEvent.click(screen.getByTestId('watchlist-fetch'))

    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledWith(
        '/api/fetch-watchlist',
        expect.objectContaining({
          source: 'mal',
          username: 'AnimeMathNerd',
          statuses: expect.arrayContaining(['completed', 'dropped']),
        }),
      )
    })

    // Cache written, onListChange fired with the new cache
    await waitFor(() => {
      const stored = window.localStorage.getItem(LS_KEY)
      expect(stored).toBeTruthy()
      const parsed = JSON.parse(stored as string)
      expect(parsed.mal_ids).toEqual([41457, 31043, 16498])
    })

    // Collapsed back to summary view
    await waitFor(() => {
      expect(screen.getByTestId('watchlist-panel-collapsed')).toBeInTheDocument()
    })
    expect(onListChange).toHaveBeenLastCalledWith(
      expect.objectContaining({ mal_ids: [41457, 31043, 16498] }),
    )
  })

  it('404 from API surfaces user-facing not-found message', async () => {
    const user = userEvent.setup()
    const { ApiError } = await import('../lib/api')
    mockPost.mockRejectedValueOnce(new ApiError(404, 'User not found'))
    render(<WatchlistExclusionPanel onListChange={vi.fn()} />)
    fireEvent.click(screen.getByTestId('watchlist-panel-expand'))
    await user.type(screen.getByTestId('watchlist-username-input'), 'nobody')
    fireEvent.click(screen.getByTestId('watchlist-fetch'))
    await waitFor(() => {
      expect(screen.getByTestId('watchlist-error')).toHaveTextContent(
        /not found.*private/i,
      )
    })
    // No cache written on failure
    expect(window.localStorage.getItem(LS_KEY)).toBeNull()
  })

  it('503 from API surfaces service-unavailable message', async () => {
    const user = userEvent.setup()
    const { ApiError } = await import('../lib/api')
    mockPost.mockRejectedValueOnce(new ApiError(503, 'Token broken'))
    render(<WatchlistExclusionPanel onListChange={vi.fn()} />)
    fireEvent.click(screen.getByTestId('watchlist-panel-expand'))
    await user.type(screen.getByTestId('watchlist-username-input'), 'AnimeMathNerd')
    fireEvent.click(screen.getByTestId('watchlist-fetch'))
    await waitFor(() => {
      expect(screen.getByTestId('watchlist-error')).toHaveTextContent(
        /temporarily unavailable/i,
      )
    })
  })

  it('429 from API surfaces rate-limit message', async () => {
    const user = userEvent.setup()
    const { ApiError } = await import('../lib/api')
    mockPost.mockRejectedValueOnce(new ApiError(429, 'Rate limit'))
    render(<WatchlistExclusionPanel onListChange={vi.fn()} />)
    fireEvent.click(screen.getByTestId('watchlist-panel-expand'))
    await user.type(screen.getByTestId('watchlist-username-input'), 'AnimeMathNerd')
    fireEvent.click(screen.getByTestId('watchlist-fetch'))
    await waitFor(() => {
      expect(screen.getByTestId('watchlist-error')).toHaveTextContent(/rate limit/i)
    })
  })

  it('omitting a default status from the form posts the user-selected subset', async () => {
    const user = userEvent.setup()
    mockPost.mockResolvedValueOnce({
      source: 'mal',
      mal_ids: [1],
      total_entries: 1,
      unmappable_entries: 0,
      errors: [],
    })
    render(<WatchlistExclusionPanel onListChange={vi.fn()} />)
    fireEvent.click(screen.getByTestId('watchlist-panel-expand'))
    await user.type(screen.getByTestId('watchlist-username-input'), 'AnimeMathNerd')
    // Drop "dropped" — keep only completed
    fireEvent.click(screen.getByTestId('watchlist-status-dropped'))
    fireEvent.click(screen.getByTestId('watchlist-fetch'))
    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledWith(
        '/api/fetch-watchlist',
        expect.objectContaining({ statuses: ['completed'] }),
      )
    })
  })
})

describe('WatchlistExclusionPanel — rollback flag', () => {
  it('renders nothing when VITE_WATCHLIST_EXCLUSION_ENABLED is "false"', async () => {
    vi.stubEnv('VITE_WATCHLIST_EXCLUSION_ENABLED', 'false')
    vi.resetModules()
    const Reloaded = (await import('../components/WatchlistExclusionPanel')).default
    const { container } = render(<Reloaded onListChange={vi.fn()} />)
    expect(container).toBeEmptyDOMElement()
    vi.unstubAllEnvs()
  })
})
