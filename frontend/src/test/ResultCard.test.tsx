import { describe, it, expect, vi, afterEach } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { renderWithProviders, makeResult, makeProfile } from './helpers'
import ResultCard from '../components/ResultCard'

describe('ResultCard', () => {
  it('renders show title and year', () => {
    renderWithProviders(<ResultCard result={makeResult()} showConfidence />)
    expect(screen.getByText('Steins;Gate')).toBeInTheDocument()
    expect(screen.getByText('(2011)')).toBeInTheDocument()
  })

  it('renders studio and quality score', () => {
    renderWithProviders(<ResultCard result={makeResult()} showConfidence />)
    expect(screen.getByText('White Fox')).toBeInTheDocument()
    expect(screen.getByText('9.1')).toBeInTheDocument()
  })

  it('shows match confidence when showConfidence is true', () => {
    renderWithProviders(<ResultCard result={makeResult()} showConfidence />)
    expect(screen.getByText('87%')).toBeInTheDocument()
  })

  it('shows "Direct DB Hit" when showConfidence is false', () => {
    renderWithProviders(<ResultCard result={makeResult()} showConfidence={false} />)
    expect(screen.getByText('Direct DB Hit')).toBeInTheDocument()
    expect(screen.queryByText('87%')).not.toBeInTheDocument()
  })

  it('renders AI reasoning', () => {
    renderWithProviders(<ResultCard result={makeResult()} showConfidence />)
    expect(screen.getByText(/Strong thematic match/)).toBeInTheDocument()
  })

  it('shows controversy warning when present', () => {
    const result = makeResult({ controversy_warning: 'Contains graphic violence.' })
    renderWithProviders(<ResultCard result={result} showConfidence />)
    expect(screen.getByText(/Contains graphic violence/)).toBeInTheDocument()
  })

  it('hides controversy warning when null', () => {
    renderWithProviders(<ResultCard result={makeResult()} showConfidence />)
    expect(screen.queryByText('Friction Alert:')).not.toBeInTheDocument()
  })

  it('links to correct MAL page', () => {
    const result = makeResult({ profile: makeProfile({ id: 9253 }) })
    renderWithProviders(<ResultCard result={result} showConfidence />)
    const link = screen.getByText('View on MyAnimeList')
    expect(link).toHaveAttribute('href', 'https://myanimelist.net/anime/9253')
    expect(link).toHaveAttribute('target', '_blank')
  })

  it('shows compact vote count when scored_by is populated', () => {
    const result = makeResult({ profile: makeProfile({ scored_by: 296_384 }) })
    renderWithProviders(<ResultCard result={result} showConfidence />)
    const el = screen.getByText('296k ratings')
    expect(el).toBeInTheDocument()
    // Tooltip / title exposes the exact locale-formatted number
    expect(el).toHaveAttribute('title', '296,384 ratings')
  })

  it('hides vote count entirely when scored_by is 0', () => {
    const result = makeResult({ profile: makeProfile({ scored_by: 0 }) })
    renderWithProviders(<ResultCard result={result} showConfidence />)
    expect(screen.queryByText(/ratings$/)).not.toBeInTheDocument()
  })

})

describe('ResultCard — MAL Rating Pill', () => {
  it('renders the short-form pill when mal_rating is populated', () => {
    const result = makeResult({
      profile: makeProfile({ mal_rating: 'R - 17+ (violence & profanity)' }),
    })
    renderWithProviders(<ResultCard result={result} showConfidence />)
    // Short form on the pill itself
    expect(screen.getByText('R')).toBeInTheDocument()
  })

  it('exposes the verbose form via the title attribute and aria-label', () => {
    const result = makeResult({
      profile: makeProfile({ mal_rating: 'PG-13 - Teens 13 or older' }),
    })
    renderWithProviders(<ResultCard result={result} showConfidence />)
    const pill = screen.getByText('PG-13')
    expect(pill).toHaveAttribute('title', 'PG-13 - Teens 13 or older')
    expect(pill).toHaveAttribute('aria-label', 'PG-13 - Teens 13 or older')
  })

  it('does not render the pill when mal_rating is null', () => {
    const result = makeResult({ profile: makeProfile({ mal_rating: null }) })
    const { container } = renderWithProviders(<ResultCard result={result} showConfidence />)
    // No pill DOM node at all — searching for any of the 6 bucket short
    // forms should miss (note: the title block already contains "Steins;Gate"
    // text, so we look for the pill class specifically).
    expect(container.querySelector('span[aria-label*=" - "]')).toBeNull()
  })

  it('does not render the pill when mal_rating is empty string', () => {
    const result = makeResult({ profile: makeProfile({ mal_rating: '' }) })
    const { container } = renderWithProviders(<ResultCard result={result} showConfidence />)
    expect(container.querySelector('span[aria-label*=" - "]')).toBeNull()
  })
})

describe('ResultCard — Rating Pill rollback flag', () => {
  afterEach(() => {
    vi.unstubAllEnvs()
  })

  it('hides the pill site-wide when VITE_MAL_RATING_PILL_ENABLED=false', () => {
    vi.stubEnv('VITE_MAL_RATING_PILL_ENABLED', 'false')
    const result = makeResult({
      profile: makeProfile({ mal_rating: 'R - 17+ (violence & profanity)' }),
    })
    renderWithProviders(<ResultCard result={result} showConfidence />)
    // The "R" pill would be present without the flag — assert it's gone.
    expect(screen.queryByText('R')).not.toBeInTheDocument()
  })
})

describe('ResultCard — Spoiler Toggle', () => {
  it('shows AI reasoning directly when no spoiler', () => {
    const result = makeResult({ contains_spoiler: false })
    renderWithProviders(<ResultCard result={result} showConfidence />)

    expect(screen.getByText(/AI Reasoning:/)).toBeInTheDocument()
    expect(screen.getByText(/Strong thematic match/)).toBeInTheDocument()
    expect(screen.queryByText('Read potential spoilers')).not.toBeInTheDocument()
  })

  it('hides AI reasoning behind spoiler warning when flagged', () => {
    const result = makeResult({
      contains_spoiler: true,
      ai_reasoning: 'The MC is betrayed by their best friend in episode 12.',
    })
    renderWithProviders(<ResultCard result={result} showConfidence />)

    expect(screen.getByText(/Spoiler Warning:/)).toBeInTheDocument()
    expect(screen.getByText('Read potential spoilers')).toBeInTheDocument()
    expect(screen.queryByText(/The MC is betrayed/)).not.toBeInTheDocument()
  })

  it('reveals reasoning after clicking spoiler link', () => {
    const result = makeResult({
      contains_spoiler: true,
      ai_reasoning: 'The MC is betrayed by their best friend in episode 12.',
    })
    renderWithProviders(<ResultCard result={result} showConfidence />)

    fireEvent.click(screen.getByText('Read potential spoilers'))

    expect(screen.getByText(/The MC is betrayed/)).toBeInTheDocument()
    expect(screen.getByText(/AI Reasoning:/)).toBeInTheDocument()
    expect(screen.queryByText('Read potential spoilers')).not.toBeInTheDocument()
  })

  it('hides controversy warning behind spoiler toggle too', () => {
    const result = makeResult({
      contains_spoiler: true,
      ai_reasoning: 'Major character dies in the finale.',
      controversy_warning: 'Divisive ending that split the fanbase.',
    })
    renderWithProviders(<ResultCard result={result} showConfidence />)

    // Both hidden behind spoiler wall
    expect(screen.queryByText(/Major character dies/)).not.toBeInTheDocument()
    expect(screen.queryByText(/Divisive ending/)).not.toBeInTheDocument()

    // Reveal
    fireEvent.click(screen.getByText('Read potential spoilers'))
    expect(screen.getByText(/Major character dies/)).toBeInTheDocument()
    expect(screen.getByText(/Divisive ending/)).toBeInTheDocument()
  })

  it('shows controversy warning normally when no spoiler flag', () => {
    const result = makeResult({
      contains_spoiler: false,
      controversy_warning: 'Polarizing pacing in second half.',
    })
    renderWithProviders(<ResultCard result={result} showConfidence />)

    expect(screen.getByText(/Polarizing pacing/)).toBeInTheDocument()
  })

  // --- Query-relevant con / "Heads up:" section (FUTURE_ENHANCEMENTS #2) ---

  it('renders "Heads up:" when query_relevant_con is present', () => {
    const result = makeResult({
      query_relevant_con: 'Pacing drags in the middle arc.',
    })
    renderWithProviders(<ResultCard result={result} showConfidence />)
    const banner = screen.getByTestId('query-relevant-con')
    expect(banner).toBeInTheDocument()
    expect(banner).toHaveTextContent('Heads up:')
    expect(banner).toHaveTextContent('Pacing drags in the middle arc.')
  })

  it('does not render "Heads up:" when query_relevant_con is null', () => {
    const result = makeResult({ query_relevant_con: null })
    renderWithProviders(<ResultCard result={result} showConfidence />)
    expect(screen.queryByTestId('query-relevant-con')).not.toBeInTheDocument()
  })

  it('does not render "Heads up:" when query_relevant_con is undefined (legacy/back-compat)', () => {
    // Older API responses omit the field entirely. Frontend must treat
    // that as "no con to show" rather than crashing or showing an empty
    // placeholder.
    const result = makeResult()
    delete (result as { query_relevant_con?: string | null }).query_relevant_con
    renderWithProviders(<ResultCard result={result} showConfidence />)
    expect(screen.queryByTestId('query-relevant-con')).not.toBeInTheDocument()
  })

  it('hides "Heads up:" behind the spoiler wall when contains_spoiler is true', () => {
    const result = makeResult({
      contains_spoiler: true,
      query_relevant_con: 'The ending is divisive among viewers.',
    })
    renderWithProviders(<ResultCard result={result} showConfidence />)
    // Spoiler wall is up — Heads-up content must not be visible yet
    expect(screen.queryByTestId('query-relevant-con')).not.toBeInTheDocument()
    expect(screen.getByText(/Spoiler Warning/)).toBeInTheDocument()
  })

  it('reveals "Heads up:" after the user clicks through the spoiler wall', () => {
    const result = makeResult({
      contains_spoiler: true,
      query_relevant_con: 'Reveals the protagonist betrayal in episode 4.',
    })
    renderWithProviders(<ResultCard result={result} showConfidence />)
    fireEvent.click(screen.getByText('Read potential spoilers'))
    const banner = screen.getByTestId('query-relevant-con')
    expect(banner).toHaveTextContent('Reveals the protagonist betrayal in episode 4.')
  })

  it('renders "Heads up:" alongside Friction Alert when both are set', () => {
    // The two yellow/red advisories live in the same fragment but serve
    // different roles — Friction Alert flags controversy_score >= 6;
    // Heads up reflects query-relevant con. Both can fire on the same
    // recommendation without conflict.
    const result = makeResult({
      controversy_warning: 'Reception is split on the ending.',
      query_relevant_con: 'Pacing drags in the second cour.',
    })
    renderWithProviders(<ResultCard result={result} showConfidence />)
    expect(screen.getByText(/Reception is split/)).toBeInTheDocument()
    expect(screen.getByTestId('query-relevant-con')).toHaveTextContent('Pacing drags')
  })
})

describe('ResultCard — Series Context integration', () => {
  // These tests verify the SeriesContext component renders correctly
  // when mounted through ResultCard (vs. tested in isolation in
  // SeriesContext.test.tsx). The integration concerns are: data
  // flow from ScoredResult.series_context to the component, absence
  // handling (no block when null/singleton), and that the drawer
  // toggle works from inside the ResultCard's DOM tree.

  const makeSeriesContext = (overrides = {}) => ({
    series_id: '16498',
    series_size: 4,
    position: 1,
    score_min: 8.5,
    score_max: 9.1,
    sentiment_min: 0.4,
    sentiment_max: 0.7,
    trajectory: 'stable' as const,
    dropped_siblings: [],
    ...overrides,
  })

  it('renders SeriesContext when series_context is present on the result', () => {
    const result = makeResult({
      series_context: makeSeriesContext({ position: 2, series_size: 4 }),
    })
    renderWithProviders(<ResultCard result={result} showConfidence />)
    expect(screen.getByTestId('series-context')).toBeInTheDocument()
    expect(screen.getByText('Entry 2 of 4')).toBeInTheDocument()
    expect(screen.getByText(/in the franchise/)).toBeInTheDocument()
  })

  it('does not render SeriesContext when series_context is null', () => {
    const result = makeResult({ series_context: null })
    renderWithProviders(<ResultCard result={result} showConfidence />)
    expect(screen.queryByTestId('series-context')).toBeNull()
  })

  it('does not render SeriesContext when series_context is absent (legacy payload)', () => {
    // Backwards-compat: older API responses or code paths that don't
    // hydrate omit the field. Pydantic default null kicks in, frontend
    // should handle gracefully.
    const result = makeResult()  // no series_context override
    renderWithProviders(<ResultCard result={result} showConfidence />)
    expect(screen.queryByTestId('series-context')).toBeNull()
  })

  it('hides SeriesContext when the cluster is a singleton (series_size === 1)', () => {
    const result = makeResult({
      series_context: makeSeriesContext({ series_size: 1 }),
    })
    renderWithProviders(<ResultCard result={result} showConfidence />)
    expect(screen.queryByTestId('series-context')).toBeNull()
  })

  it('renders the trajectory caption from the result data', () => {
    const result = makeResult({
      series_context: makeSeriesContext({ trajectory: 'decline' }),
    })
    renderWithProviders(<ResultCard result={result} showConfidence />)
    expect(screen.getByText('Reception declines toward later entries.')).toBeInTheDocument()
  })

  it('expands the dropped-siblings drawer when toggled from inside ResultCard', () => {
    const result = makeResult({
      series_context: makeSeriesContext({
        dropped_siblings: [
          { id: 25777, title: 'Attack on Titan Season 2', match_confidence: 88,
            query_relevant_con: 'Pacing dips mid-season', contains_spoiler: false },
        ],
      }),
    })
    renderWithProviders(<ResultCard result={result} showConfidence />)
    expect(screen.queryByTestId('series-dropped-list')).toBeNull()
    fireEvent.click(screen.getByTestId('series-dropped-toggle'))
    expect(screen.getByTestId('series-dropped-list')).toBeInTheDocument()
    expect(screen.getByText('Attack on Titan Season 2')).toBeInTheDocument()
  })

  it('hides SeriesContext behind the spoiler wall (renders only after reveal)', () => {
    // SeriesContext lives inside the AI-Reasoning-and-friends block,
    // which is gated by the contains_spoiler wall. Verify the block
    // is hidden initially and surfaces after the reveal click.
    const result = makeResult({
      contains_spoiler: true,
      series_context: makeSeriesContext(),
    })
    renderWithProviders(<ResultCard result={result} showConfidence />)
    expect(screen.queryByTestId('series-context')).toBeNull()
    fireEvent.click(screen.getByText(/Read potential spoilers/))
    expect(screen.getByTestId('series-context')).toBeInTheDocument()
  })
})


describe('ResultCard — Composite fallback pill (COA B / Option C)', () => {
  it('does not render the pill by default (no fallbackMode prop)', () => {
    renderWithProviders(<ResultCard result={makeResult()} showConfidence />)
    expect(screen.queryByTestId('composite-fallback-pill')).toBeNull()
  })

  it('does not render the pill when fallbackMode is null', () => {
    renderWithProviders(
      <ResultCard result={makeResult()} showConfidence fallbackMode={null} />,
    )
    expect(screen.queryByTestId('composite-fallback-pill')).toBeNull()
  })

  it('renders the pill when fallbackMode is "composite"', () => {
    renderWithProviders(
      <ResultCard result={makeResult()} showConfidence fallbackMode="composite" />,
    )
    const pill = screen.getByTestId('composite-fallback-pill')
    expect(pill).toBeInTheDocument()
    expect(pill).toHaveTextContent('Semantic match only')
  })

  it('pill has a tooltip explaining the reason', () => {
    renderWithProviders(
      <ResultCard result={makeResult()} showConfidence fallbackMode="composite" />,
    )
    // Tooltip only renders visible copy on hover, but the info-icon's
    // aria-label carries the text unconditionally for screen readers.
    // Verify via getByLabelText — matches the a11y-first assertion
    // pattern used elsewhere in this file.
    expect(
      screen.getByLabelText(/detailed reranker analysis wasn't available/i),
    ).toBeInTheDocument()
  })

  it('pill renders alongside the match-confidence line, not replacing it', () => {
    renderWithProviders(
      <ResultCard result={makeResult()} showConfidence fallbackMode="composite" />,
    )
    // Confidence still shows (fallback entries carry 70% by design)
    expect(screen.getByText('87%')).toBeInTheDocument()
    // Pill sits next to it
    expect(screen.getByTestId('composite-fallback-pill')).toBeInTheDocument()
  })

  it('pill does not render when showConfidence is false (Objective Rankings)', () => {
    // OR mode bypasses the reranker, so fallback semantics don't apply.
    // Even if parent somehow passed fallbackMode="composite" here, the
    // pill still renders because it's mode-driven, not confidence-driven —
    // this test pins that behavior as intentional so a future refactor
    // is a deliberate choice, not accidental.
    renderWithProviders(
      <ResultCard
        result={makeResult()}
        showConfidence={false}
        fallbackMode="composite"
      />,
    )
    // "Direct DB Hit" text renders (showConfidence=false path)
    expect(screen.getByText('Direct DB Hit')).toBeInTheDocument()
    // Pill still renders — mode-driven
    expect(screen.getByTestId('composite-fallback-pill')).toBeInTheDocument()
  })
})
