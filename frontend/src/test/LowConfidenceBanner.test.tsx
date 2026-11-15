import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import LowConfidenceBanner from '../components/LowConfidenceBanner'

describe('LowConfidenceBanner', () => {
  it('renders the locked copy', () => {
    render(<LowConfidenceBanner />)
    expect(
      screen.getByText('No strong match — try refining your query.'),
    ).toBeInTheDocument()
  })

  it('exposes a testid for downstream assertions', () => {
    render(<LowConfidenceBanner />)
    expect(screen.getByTestId('low-confidence-banner')).toBeInTheDocument()
  })

  it('uses role="status" for accessibility (advisory, not error)', () => {
    render(<LowConfidenceBanner />)
    expect(screen.getByRole('status')).toBeInTheDocument()
  })

  // PR C — watchlist-exclusion-empty variant
  describe('when watchlist exclusion emptied the pool', () => {
    it('renders the watchlist-specific copy', () => {
      render(<LowConfidenceBanner watchlistExclusionEmptied />)
      expect(
        screen.getByText(/All matches for this query are on your watchlist/i),
      ).toBeInTheDocument()
      expect(
        screen.queryByText('No strong match — try refining your query.'),
      ).not.toBeInTheDocument()
    })

    it('exposes a data-variant attribute for downstream assertions', () => {
      render(<LowConfidenceBanner watchlistExclusionEmptied />)
      expect(screen.getByTestId('low-confidence-banner')).toHaveAttribute(
        'data-variant',
        'watchlist-empty',
      )
    })

    it('default variant carries data-variant="default"', () => {
      render(<LowConfidenceBanner />)
      expect(screen.getByTestId('low-confidence-banner')).toHaveAttribute(
        'data-variant',
        'default',
      )
    })

    it('prop default to false renders the generic copy', () => {
      // Explicit prop=false should match omitted-prop behavior
      render(<LowConfidenceBanner watchlistExclusionEmptied={false} />)
      expect(
        screen.getByText('No strong match — try refining your query.'),
      ).toBeInTheDocument()
    })
  })

  // COA B / Option C — composite fallback copy variant
  describe('when the pool came from composite fallback', () => {
    it('renders the fallback-mode copy explaining LLM analysis was unavailable', () => {
      render(<LowConfidenceBanner fallbackMode="composite" />)
      expect(
        screen.getByText(/Detailed analysis wasn't available for this query/i),
      ).toBeInTheDocument()
      expect(
        screen.getByText(/showing semantic matches only/i),
      ).toBeInTheDocument()
      expect(
        screen.queryByText('No strong match — try refining your query.'),
      ).not.toBeInTheDocument()
    })

    it('exposes data-variant="composite-fallback"', () => {
      render(<LowConfidenceBanner fallbackMode="composite" />)
      expect(screen.getByTestId('low-confidence-banner')).toHaveAttribute(
        'data-variant',
        'composite-fallback',
      )
    })

    it('composite-fallback wins over watchlist-empty when both would apply', () => {
      // Fallback mode is a more specific product-behavior signal than
      // watchlist-empty; the user needs to know analysis was unavailable
      // regardless of exclusion state.
      render(
        <LowConfidenceBanner
          fallbackMode="composite"
          watchlistExclusionEmptied
        />,
      )
      expect(
        screen.getByText(/Detailed analysis wasn't available/i),
      ).toBeInTheDocument()
      expect(
        screen.queryByText(/All matches for this query are on your watchlist/i),
      ).not.toBeInTheDocument()
      expect(screen.getByTestId('low-confidence-banner')).toHaveAttribute(
        'data-variant',
        'composite-fallback',
      )
    })

    it('explicit fallbackMode=null falls through to default copy', () => {
      render(<LowConfidenceBanner fallbackMode={null} />)
      expect(
        screen.getByText('No strong match — try refining your query.'),
      ).toBeInTheDocument()
    })
  })

  // Item 4a — Staff/Studio empty-pool variant
  describe('when a Staff/Studio chip filter returned zero results', () => {
    it('renders the Staff/Studio-specific copy', () => {
      render(<LowConfidenceBanner staffStudioEmpty />)
      expect(
        screen.getByText(/No shows match all filters\. Remove a chip/i),
      ).toBeInTheDocument()
      expect(
        screen.queryByText('No strong match — try refining your query.'),
      ).not.toBeInTheDocument()
    })

    it('exposes data-variant="staff-studio-empty"', () => {
      render(<LowConfidenceBanner staffStudioEmpty />)
      expect(screen.getByTestId('low-confidence-banner')).toHaveAttribute(
        'data-variant',
        'staff-studio-empty',
      )
    })

    it('composite-fallback wins over staff-studio-empty when both would apply', () => {
      render(
        <LowConfidenceBanner fallbackMode="composite" staffStudioEmpty />,
      )
      expect(screen.getByTestId('low-confidence-banner')).toHaveAttribute(
        'data-variant',
        'composite-fallback',
      )
    })

    it('staff-studio-empty wins over watchlist-empty when both would apply', () => {
      // Watchlist exclusion is a no-op in Staff/Studio mode (chip filter
      // path doesn't gate on exclude_mal_ids in v1), but the frontend
      // still passes both props. Staff/Studio copy is more actionable —
      // remove a chip vs. adjust watchlist.
      render(
        <LowConfidenceBanner staffStudioEmpty watchlistExclusionEmptied />,
      )
      expect(screen.getByTestId('low-confidence-banner')).toHaveAttribute(
        'data-variant',
        'staff-studio-empty',
      )
    })
  })
})
