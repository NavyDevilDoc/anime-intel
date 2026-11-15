import { describe, it, expect, vi, beforeEach, afterAll } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithRouter } from './helpers'
import ControversyLandscape, {
  jitterFor,
  matchesFilters,
  type Filters,
  type LandscapeResponse,
  type LandscapeShow,
} from '../components/ControversyLandscape'

// Mock Observable Plot — jsdom lacks the canvas APIs that Plot's color
// legend requires (fillStyle on CanvasRenderingContext2D), so the real
// Plot.plot() throws in test. The component defends against this with
// try/catch (headings-only degraded render), but mocking at the module
// level keeps tests focused on data-fetch + flag gating + loading state
// rather than Plot's internals. Visual correctness is validated via
// Phase 6 dev-server eyeball.
vi.mock('@observablehq/plot', () => ({
  plot: vi.fn(() => document.createElement('svg')),
  dot: vi.fn(),
}))

const _originalFlag = import.meta.env.VITE_CONTROVERSY_LANDSCAPE_ENABLED

beforeEach(() => {
  vi.restoreAllMocks()
  import.meta.env.VITE_CONTROVERSY_LANDSCAPE_ENABLED = 'true'
})

afterAll(() => {
  if (_originalFlag === undefined) {
    delete import.meta.env.VITE_CONTROVERSY_LANDSCAPE_ENABLED
  } else {
    import.meta.env.VITE_CONTROVERSY_LANDSCAPE_ENABLED = _originalFlag
  }
})

function makeShow(overrides?: Partial<LandscapeShow>): LandscapeShow {
  return {
    mal_id: 1,
    title: 'Cowboy Bebop',
    quality: 8.74,
    controversy_score: 5,
    scored_by: 1_054_562,
    avg_sentiment: 0.89,
    genres: ['Action', 'Sci-Fi'],
    release_year: 1998,
    subtype: 'TV',
    ...overrides,
  }
}

function stubFetchOk(payload: LandscapeResponse) {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    json: () => Promise.resolve(payload),
  }))
}

describe('ControversyLandscape', () => {
  it('renders null when VITE_CONTROVERSY_LANDSCAPE_ENABLED is off', () => {
    import.meta.env.VITE_CONTROVERSY_LANDSCAPE_ENABLED = 'false'
    const { container } = renderWithRouter(<ControversyLandscape />)
    expect(container).toBeEmptyDOMElement()
  })

  it('renders loading skeleton before data arrives', () => {
    vi.stubGlobal('fetch', vi.fn(() => new Promise(() => {})))
    renderWithRouter(<ControversyLandscape />)
    expect(screen.getByTestId('landscape-loading')).toBeInTheDocument()
    expect(screen.getByTestId('controversy-landscape')).toBeInTheDocument()
  })

  it('fetches landscape data from /api/controversy-landscape-data on mount', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ shows: [makeShow()], generated_at: '2026-09-30T00:00:00+00:00' }),
    })
    vi.stubGlobal('fetch', fetchMock)
    renderWithRouter(<ControversyLandscape />)
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalled()
    })
    expect(fetchMock.mock.calls[0][0]).toContain('/api/controversy-landscape-data')
  })

  it('renders the section heading + descriptor copy', async () => {
    stubFetchOk({ shows: [makeShow()], generated_at: '2026-09-30T00:00:00+00:00' })
    renderWithRouter(<ControversyLandscape />)
    await waitFor(() => {
      expect(screen.getByText('The Controversy Landscape')).toBeInTheDocument()
    })
    // Tagline aligned with Y-axis terminology — "controversy" throughout,
    // not the earlier mixed "divisiveness / Controversy".
    expect(screen.getByText(/Every vaulted show plotted by quality and controversy/)).toBeInTheDocument()
  })

  it('renders the term legend with linked definitions', async () => {
    stubFetchOk({ shows: [makeShow()], generated_at: '2026-09-30T00:00:00+00:00' })
    renderWithRouter(<ControversyLandscape />)
    await waitFor(() => {
      expect(screen.getByTestId('landscape-legend')).toBeInTheDocument()
    })
    // Quality definition + link to HowItWorks scoring anchor
    const qualityLink = screen.getByRole('link', { name: 'Quality' })
    expect(qualityLink).toHaveAttribute('href', '/how-it-works#scoring')
    expect(screen.getByText(/community rating adjusted for vote count/)).toBeInTheDocument()
    // Controversy definition + link to HowItWorks consensus anchor
    const controversyLink = screen.getByRole('link', { name: 'Controversy' })
    expect(controversyLink).toHaveAttribute('href', '/how-it-works#consensus')
    expect(screen.getByText(/how divided fan reviews are/)).toBeInTheDocument()
  })

  it('hides loading skeleton once data arrives', async () => {
    stubFetchOk({ shows: [makeShow(), makeShow({ mal_id: 2 })], generated_at: '2026-09-30T00:00:00+00:00' })
    renderWithRouter(<ControversyLandscape />)
    await waitFor(() => {
      expect(screen.queryByTestId('landscape-loading')).not.toBeInTheDocument()
    })
  })

  it('renders null when the fetch fails (silent degradation)', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('network down')))
    const { container } = renderWithRouter(<ControversyLandscape />)
    await waitFor(() => {
      expect(container).toBeEmptyDOMElement()
    })
  })

  // -------------------------------------------------------------------
  // Phase 4 — interactivity
  // -------------------------------------------------------------------

  it('renders the three filter dropdowns + Reset button after data loads', async () => {
    stubFetchOk({
      shows: [
        makeShow({ mal_id: 1, genres: ['Action'], subtype: 'TV' }),
        makeShow({ mal_id: 2, genres: ['Drama'], subtype: 'Movie' }),
      ],
      generated_at: '2026-09-30T00:00:00+00:00',
    })
    renderWithRouter(<ControversyLandscape />)
    await waitFor(() => {
      expect(screen.getByTestId('landscape-filter-genre')).toBeInTheDocument()
    })
    expect(screen.getByTestId('landscape-filter-decade')).toBeInTheDocument()
    expect(screen.getByTestId('landscape-filter-subtype')).toBeInTheDocument()
    // Reset button always rendered (not conditionally hidden); disabled
    // when there's nothing to clear.
    const resetBtn = screen.getByTestId('landscape-clear-filters') as HTMLButtonElement
    expect(resetBtn).toBeInTheDocument()
    expect(resetBtn).toBeDisabled()
    expect(resetBtn).toHaveTextContent(/Reset/i)
  })

  it('enables the Reset button once a filter is selected', async () => {
    stubFetchOk({
      shows: [makeShow({ mal_id: 1, genres: ['Action'] })],
      generated_at: '2026-09-30T00:00:00+00:00',
    })
    const user = userEvent.setup()
    renderWithRouter(<ControversyLandscape />)
    await waitFor(() => {
      expect(screen.getByTestId('landscape-filter-genre')).toBeInTheDocument()
    })
    const genreSelect = screen.getByTestId('landscape-filter-genre') as HTMLSelectElement
    const resetBtn = screen.getByTestId('landscape-clear-filters') as HTMLButtonElement
    expect(resetBtn).toBeDisabled()
    await user.selectOptions(genreSelect, 'Action')
    expect(resetBtn).toBeEnabled()
  })

  it('clear filters button resets all three filters to empty', async () => {
    stubFetchOk({
      shows: [makeShow({ mal_id: 1, genres: ['Action'], subtype: 'TV' })],
      generated_at: '2026-09-30T00:00:00+00:00',
    })
    const user = userEvent.setup()
    renderWithRouter(<ControversyLandscape />)
    await waitFor(() => {
      expect(screen.getByTestId('landscape-filter-genre')).toBeInTheDocument()
    })
    const genreSelect = screen.getByTestId('landscape-filter-genre') as HTMLSelectElement
    const subtypeSelect = screen.getByTestId('landscape-filter-subtype') as HTMLSelectElement
    await user.selectOptions(genreSelect, 'Action')
    await user.selectOptions(subtypeSelect, 'TV')
    expect(genreSelect.value).toBe('Action')
    expect(subtypeSelect.value).toBe('TV')
    await user.click(screen.getByTestId('landscape-clear-filters'))
    expect(genreSelect.value).toBe('')
    expect(subtypeSelect.value).toBe('')
  })

  it('populates genre options from the loaded data', async () => {
    stubFetchOk({
      shows: [
        makeShow({ mal_id: 1, genres: ['Action', 'Drama'] }),
        makeShow({ mal_id: 2, genres: ['Drama', 'Sci-Fi'] }),
        makeShow({ mal_id: 3, genres: ['Comedy'] }),
      ],
      generated_at: '2026-09-30T00:00:00+00:00',
    })
    renderWithRouter(<ControversyLandscape />)
    await waitFor(() => {
      expect(screen.getByTestId('landscape-filter-genre')).toBeInTheDocument()
    })
    const genreSelect = screen.getByTestId('landscape-filter-genre') as HTMLSelectElement
    const optionValues = Array.from(genreSelect.options).map(o => o.value)
    // Includes 'Any' + sorted uniques across all shows
    expect(optionValues).toEqual(['', 'Action', 'Comedy', 'Drama', 'Sci-Fi'])
  })
})

// ---------------------------------------------------------------------
// Pure-function tests — jitter + filter logic
// ---------------------------------------------------------------------

describe('jitterFor', () => {
  it('returns a value in [-0.3, 0.3]', () => {
    for (const malId of [1, 100, 12345, 99999, 123456789]) {
      const j = jitterFor(malId)
      expect(j).toBeGreaterThanOrEqual(-0.3)
      expect(j).toBeLessThanOrEqual(0.3)
    }
  })

  it('is deterministic — same input always returns same value', () => {
    expect(jitterFor(42)).toBe(jitterFor(42))
    expect(jitterFor(9999)).toBe(jitterFor(9999))
  })

  it('different ids typically produce different values', () => {
    // Not a strict guarantee for all pairs, but with 100 adjacent ids
    // the vast majority differ. Catches pathological seed collapse.
    const vals = Array.from({ length: 100 }, (_, i) => jitterFor(i + 1))
    const unique = new Set(vals)
    expect(unique.size).toBeGreaterThan(90)
  })
})

describe('matchesFilters', () => {
  const show: LandscapeShow = {
    mal_id: 1, title: 'Cowboy Bebop', quality: 8.74,
    controversy_score: 5, scored_by: 1_054_562, avg_sentiment: 0.89,
    genres: ['Action', 'Sci-Fi'], release_year: 1998, subtype: 'TV',
  }

  const noFilters: Filters = { genre: '', decade: '', subtype: '' }

  it('matches when all filters empty', () => {
    expect(matchesFilters(show, noFilters)).toBe(true)
  })

  it('matches genre when the show includes it', () => {
    expect(matchesFilters(show, { ...noFilters, genre: 'Action' })).toBe(true)
    expect(matchesFilters(show, { ...noFilters, genre: 'Comedy' })).toBe(false)
  })

  it('matches decade by 10-year window inclusive-exclusive', () => {
    // 1998 is in 1990s
    expect(matchesFilters(show, { ...noFilters, decade: '1990' })).toBe(true)
    expect(matchesFilters(show, { ...noFilters, decade: '2000' })).toBe(false)
    // 2000 starts the 2000s
    const show2000 = { ...show, release_year: 2000 }
    expect(matchesFilters(show2000, { ...noFilters, decade: '2000' })).toBe(true)
    expect(matchesFilters(show2000, { ...noFilters, decade: '1990' })).toBe(false)
  })

  it('matches subtype exactly', () => {
    expect(matchesFilters(show, { ...noFilters, subtype: 'TV' })).toBe(true)
    expect(matchesFilters(show, { ...noFilters, subtype: 'Movie' })).toBe(false)
  })

  it('AND-combines all three filters', () => {
    expect(matchesFilters(show, {
      genre: 'Action', decade: '1990', subtype: 'TV',
    })).toBe(true)
    // Fail on genre
    expect(matchesFilters(show, {
      genre: 'Horror', decade: '1990', subtype: 'TV',
    })).toBe(false)
    // Fail on decade
    expect(matchesFilters(show, {
      genre: 'Action', decade: '2010', subtype: 'TV',
    })).toBe(false)
    // Fail on subtype
    expect(matchesFilters(show, {
      genre: 'Action', decade: '1990', subtype: 'Movie',
    })).toBe(false)
  })
})
