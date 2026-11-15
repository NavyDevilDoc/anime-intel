import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import SeriesContext from '../components/SeriesContext'
import type { SeriesContext as SeriesContextData } from '../lib/types'

function makeContext(overrides?: Partial<SeriesContextData>): SeriesContextData {
  return {
    series_id: '16498',
    series_size: 4,
    position: 1,
    score_min: 8.5,
    score_max: 9.1,
    sentiment_min: 0.4,
    sentiment_max: 0.7,
    trajectory: 'stable',
    dropped_siblings: [],
    ...overrides,
  }
}

describe('SeriesContext — position pill', () => {
  it('renders the position label "Entry X of N in the franchise"', () => {
    render(<SeriesContext context={makeContext({ position: 2, series_size: 5 })} />)
    expect(screen.getByText('Entry 2 of 5')).toBeInTheDocument()
    expect(screen.getByText(/in the franchise/)).toBeInTheDocument()
  })

  it('renders an info icon explaining what the franchise count includes', () => {
    render(<SeriesContext context={makeContext()} />)
    const info = screen.getByTestId('series-position-info')
    expect(info).toBeInTheDocument()
    // Native title + aria-label (PR #69 convention for compact UI).
    // Tooltip text explicitly names the non-TV entries that pad the count
    // so users aren't surprised by large numbers on franchises like
    // Psycho-Pass (TV + films + spinoffs = 9 entries).
    expect(info).toHaveAttribute(
      'title',
      expect.stringContaining('Franchise count includes TV seasons, films, OVAs, and specials'),
    )
    expect(info).toHaveAttribute(
      'aria-label',
      expect.stringContaining('Franchise count includes TV seasons, films, OVAs, and specials'),
    )
  })

  it('renders info icon in minimal mode too (encyclopedia surface still needs the explainer)', () => {
    render(<SeriesContext mode="minimal" context={makeContext()} />)
    expect(screen.getByTestId('series-position-info')).toBeInTheDocument()
  })

  it('returns null for a singleton cluster (series_size === 1)', () => {
    const { container } = render(<SeriesContext context={makeContext({ series_size: 1 })} />)
    expect(container.querySelector('[data-testid="series-context"]')).toBeNull()
  })

  it('returns null when context is null', () => {
    const { container } = render(<SeriesContext context={null} />)
    expect(container.querySelector('[data-testid="series-context"]')).toBeNull()
  })

  it('returns null when context is undefined', () => {
    const { container } = render(<SeriesContext context={undefined} />)
    expect(container.querySelector('[data-testid="series-context"]')).toBeNull()
  })
})

describe('SeriesContext — trajectory caption', () => {
  it('renders the stable copy when trajectory is "stable"', () => {
    render(<SeriesContext context={makeContext({ trajectory: 'stable' })} />)
    expect(screen.getByText('Quality stays consistent across the series.')).toBeInTheDocument()
  })

  it('renders the decline copy when trajectory is "decline"', () => {
    render(<SeriesContext context={makeContext({ trajectory: 'decline' })} />)
    expect(screen.getByText('Reception declines toward later entries.')).toBeInTheDocument()
  })

  it('renders the improve copy when trajectory is "improve"', () => {
    render(<SeriesContext context={makeContext({ trajectory: 'improve' })} />)
    expect(screen.getByText('Reception improves across the series.')).toBeInTheDocument()
  })

  it('renders the mixed copy when trajectory is "mixed"', () => {
    render(<SeriesContext context={makeContext({ trajectory: 'mixed' })} />)
    expect(screen.getByText('Reception varies across entries.')).toBeInTheDocument()
  })

  it('hides the trajectory caption when trajectory is null', () => {
    render(<SeriesContext context={makeContext({ trajectory: null })} />)
    expect(screen.queryByTestId('series-trajectory')).toBeNull()
  })
})

describe('SeriesContext — score range', () => {
  it('renders the score range to 1 decimal when min !== max', () => {
    render(<SeriesContext context={makeContext({ score_min: 7.36, score_max: 8.94 })} />)
    expect(screen.getByText(/Scores: 7\.4 — 8\.9/)).toBeInTheDocument()
  })

  it('hides the score range when min === max (no meaningful range)', () => {
    render(<SeriesContext context={makeContext({ score_min: 8.5, score_max: 8.5 })} />)
    expect(screen.queryByTestId('series-score-range')).toBeNull()
  })

  it('hides the score range when score_min is null', () => {
    render(<SeriesContext context={makeContext({ score_min: null, score_max: 8.5 })} />)
    expect(screen.queryByTestId('series-score-range')).toBeNull()
  })

  it('hides the score range when score_max is null', () => {
    render(<SeriesContext context={makeContext({ score_min: 8.0, score_max: null })} />)
    expect(screen.queryByTestId('series-score-range')).toBeNull()
  })
})

describe('SeriesContext — dropped siblings drawer', () => {
  const droppedTwo = [
    { id: 25777, title: 'Attack on Titan Season 2', match_confidence: 88,
      query_relevant_con: 'Pacing dips mid-season', contains_spoiler: false },
    { id: 35760, title: 'Attack on Titan Season 3', match_confidence: 82,
      query_relevant_con: null, contains_spoiler: false },
  ]

  it('shows the toggle button with correct count when dropped_siblings present', () => {
    render(<SeriesContext context={makeContext({ dropped_siblings: droppedTwo })} />)
    expect(screen.getByTestId('series-dropped-toggle')).toHaveTextContent(
      '+ 2 other entries scored well',
    )
  })

  it('uses singular copy for a single dropped sibling', () => {
    render(<SeriesContext context={makeContext({ dropped_siblings: [droppedTwo[0]] })} />)
    expect(screen.getByTestId('series-dropped-toggle')).toHaveTextContent(
      '+ 1 other entry scored well',
    )
  })

  it('drawer is collapsed by default (list is not rendered)', () => {
    render(<SeriesContext context={makeContext({ dropped_siblings: droppedTwo })} />)
    expect(screen.queryByTestId('series-dropped-list')).toBeNull()
  })

  it('expands the drawer on toggle click and shows sibling rows', () => {
    render(<SeriesContext context={makeContext({ dropped_siblings: droppedTwo })} />)
    fireEvent.click(screen.getByTestId('series-dropped-toggle'))
    const list = screen.getByTestId('series-dropped-list')
    expect(list).toBeInTheDocument()
    expect(list.textContent).toContain('Attack on Titan Season 2')
    expect(list.textContent).toContain('88% match')
    expect(list.textContent).toContain('Pacing dips mid-season')
  })

  it('toggle copy flips to "− hide" after expanding', () => {
    render(<SeriesContext context={makeContext({ dropped_siblings: droppedTwo })} />)
    fireEvent.click(screen.getByTestId('series-dropped-toggle'))
    expect(screen.getByTestId('series-dropped-toggle')).toHaveTextContent('− hide')
  })

  it('sibling rows link to MyAnimeList by id', () => {
    render(<SeriesContext context={makeContext({ dropped_siblings: droppedTwo })} />)
    fireEvent.click(screen.getByTestId('series-dropped-toggle'))
    const link = screen.getByText('Attack on Titan Season 2')
    expect(link).toHaveAttribute('href', 'https://myanimelist.net/anime/25777')
    expect(link).toHaveAttribute('target', '_blank')
  })

  it('hides the dropped-siblings toggle when the list is empty', () => {
    render(<SeriesContext context={makeContext({ dropped_siblings: [] })} />)
    expect(screen.queryByTestId('series-dropped-toggle')).toBeNull()
  })
})

describe('SeriesContext — minimal mode (encyclopedia)', () => {
  it('renders the position pill', () => {
    render(<SeriesContext mode="minimal" context={makeContext({ position: 2, series_size: 5 })} />)
    expect(screen.getByText('Entry 2 of 5')).toBeInTheDocument()
  })

  it('hides the trajectory caption', () => {
    render(
      <SeriesContext
        mode="minimal"
        context={makeContext({ trajectory: 'decline' })}
      />,
    )
    expect(screen.queryByTestId('series-trajectory')).toBeNull()
  })

  it('hides the score range', () => {
    render(
      <SeriesContext
        mode="minimal"
        context={makeContext({ score_min: 7.5, score_max: 9.0 })}
      />,
    )
    expect(screen.queryByTestId('series-score-range')).toBeNull()
  })

  it('hides the dropped-siblings toggle even when siblings are present', () => {
    render(
      <SeriesContext
        mode="minimal"
        context={makeContext({
          dropped_siblings: [
            { id: 1, title: 'Sibling', match_confidence: 90, query_relevant_con: null, contains_spoiler: false },
          ],
        })}
      />,
    )
    expect(screen.queryByTestId('series-dropped-toggle')).toBeNull()
  })
})

describe('SeriesContext — rollback flag', () => {
  const originalEnv = import.meta.env.VITE_SERIES_CONTEXT_ENABLED

  afterEach(() => {
    vi.unstubAllEnvs()
    if (originalEnv !== undefined) {
      import.meta.env.VITE_SERIES_CONTEXT_ENABLED = originalEnv
    }
  })

  it('renders nothing when VITE_SERIES_CONTEXT_ENABLED is "false"', async () => {
    vi.stubEnv('VITE_SERIES_CONTEXT_ENABLED', 'false')
    // Re-import the component so the new env value is picked up at module
    // load. import.meta.env is read at top-level inside SeriesContext.
    vi.resetModules()
    const SeriesContextReloaded = (await import('../components/SeriesContext')).default
    const { container } = render(<SeriesContextReloaded context={makeContext()} />)
    expect(container.querySelector('[data-testid="series-context"]')).toBeNull()
  })
})

// =====================================================================
// PR B (subtype bundle): "Season X of Y" label + score-range caption
// =====================================================================

describe('SeriesContext — TV-aware position label', () => {
  it('renders "Season X of Y in a Z-entry franchise" when tv_position + tv_count > 1 are set', () => {
    render(
      <SeriesContext
        context={makeContext({
          series_size: 14,  // AoT-shaped: 4 TV + movies + OVA + recaps
          position: 1,
          tv_position: 1,
          tv_count: 4,
        })}
      />,
    )
    expect(screen.getByText('Season 1 of 4')).toBeInTheDocument()
    expect(screen.getByText(/in a 14-entry franchise/)).toBeInTheDocument()
    // The legacy label must NOT appear when TV label fires.
    expect(screen.queryByText('Entry 1 of 14')).not.toBeInTheDocument()
  })

  it('falls back to "Entry X of N in the franchise" when tv_position is null (target is a movie)', () => {
    render(
      <SeriesContext
        context={makeContext({
          series_size: 14,
          position: 7,
          tv_position: null,  // movie — no TV-relative position
          tv_count: null,
        })}
      />,
    )
    expect(screen.getByText('Entry 7 of 14')).toBeInTheDocument()
    expect(screen.getByText(/in the franchise/)).toBeInTheDocument()
    expect(screen.queryByText(/Season /)).not.toBeInTheDocument()
  })

  it('falls back when tv_count === 1 (single TV entry — "Season 1 of 1" reads weird)', () => {
    render(
      <SeriesContext
        context={makeContext({
          series_size: 5,  // 1 TV + 4 OVA/Movie
          position: 1,
          tv_position: 1,
          tv_count: 1,
        })}
      />,
    )
    expect(screen.getByText('Entry 1 of 5')).toBeInTheDocument()
    expect(screen.queryByText(/Season 1 of 1/)).not.toBeInTheDocument()
  })

  it('falls back when tv_position/tv_count are absent (pre-PR-B backend response)', () => {
    render(<SeriesContext context={makeContext({ series_size: 5, position: 2 })} />)
    expect(screen.getByText('Entry 2 of 5')).toBeInTheDocument()
    expect(screen.queryByText(/Season /)).not.toBeInTheDocument()
  })

  it('info icon still renders alongside the TV label', () => {
    render(
      <SeriesContext
        context={makeContext({
          tv_position: 2,
          tv_count: 4,
        })}
      />,
    )
    expect(screen.getByTestId('series-position-info')).toBeInTheDocument()
  })

  it('TV label respects minimal mode (Encyclopedia surface)', () => {
    render(
      <SeriesContext
        mode="minimal"
        context={makeContext({
          series_size: 14,
          tv_position: 3,
          tv_count: 4,
        })}
      />,
    )
    expect(screen.getByText('Season 3 of 4')).toBeInTheDocument()
    expect(screen.getByText(/in a 14-entry franchise/)).toBeInTheDocument()
    // Minimal mode hides the score range and trajectory — but the
    // TV-aware position pill still renders.
    expect(screen.queryByTestId('series-score-range')).not.toBeInTheDocument()
  })
})

describe('SeriesContext — score-range filtered caption', () => {
  it('shows the "(TV and theatrical)" caption when score_range_filtered is true', () => {
    render(
      <SeriesContext
        context={makeContext({
          score_min: 8.8,
          score_max: 9.3,
          score_range_filtered: true,
        })}
      />,
    )
    const range = screen.getByTestId('series-score-range')
    expect(range).toHaveTextContent('Scores: 8.8 — 9.3')
    expect(screen.getByTestId('series-score-range-filtered-caption')).toBeInTheDocument()
    // Native title + aria-label for accessibility (PR #69 convention).
    expect(range).toHaveAttribute('title', 'TV and theatrical entries only')
    expect(range).toHaveAttribute(
      'aria-label',
      expect.stringContaining('TV and theatrical entries only'),
    )
  })

  it('omits the caption when score_range_filtered is false', () => {
    render(
      <SeriesContext
        context={makeContext({
          score_min: 8.0,
          score_max: 9.0,
          score_range_filtered: false,
        })}
      />,
    )
    expect(screen.queryByTestId('series-score-range-filtered-caption')).not.toBeInTheDocument()
    const range = screen.getByTestId('series-score-range')
    expect(range).not.toHaveAttribute('title')
  })

  it('omits the caption when score_range_filtered is absent (pre-PR-B backend)', () => {
    render(<SeriesContext context={makeContext()} />)
    expect(screen.queryByTestId('series-score-range-filtered-caption')).not.toBeInTheDocument()
  })

  it('caption is hidden in minimal mode (no score range there at all)', () => {
    render(
      <SeriesContext
        mode="minimal"
        context={makeContext({
          score_min: 8.8,
          score_max: 9.3,
          score_range_filtered: true,
        })}
      />,
    )
    expect(screen.queryByTestId('series-score-range-filtered-caption')).not.toBeInTheDocument()
  })
})
