import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import { renderWithRouter } from './helpers'
import HowItWorks from '../pages/HowItWorks'

describe('HowItWorks', () => {
  it('renders the page heading', () => {
    renderWithRouter(<HowItWorks />)
    expect(screen.getByText('How It Works')).toBeInTheDocument()
  })

  it('describes all five discovery modes', () => {
    renderWithRouter(<HowItWorks />)
    expect(screen.getByText('Intelligent Search')).toBeInTheDocument()
    expect(screen.getByText('Shows Like This')).toBeInTheDocument()
    expect(screen.getAllByText('Objective Rankings').length).toBeGreaterThanOrEqual(1)
    expect(screen.getByText('Staff/Studio')).toBeInTheDocument()
    expect(screen.getByText('Top Lists')).toBeInTheDocument()
  })

  it('renders the About the Data section with all five sub-sections', () => {
    // Sub-section headings use agnostic noun phrases — no first-person
    // plural ("We", "Our"). See chore/voice-and-newsletter-cleanup.
    renderWithRouter(<HowItWorks />)
    expect(screen.getByText('About the Data')).toBeInTheDocument()
    expect(screen.getByText('The Vault')).toBeInTheDocument()
    expect(screen.getByText('Scoring')).toBeInTheDocument()
    expect(screen.getByText('Consensus Distillation')).toBeInTheDocument()
    expect(screen.getByText('Ranking')).toBeInTheDocument()
    expect(screen.getByText('Data Minimization')).toBeInTheDocument()
  })

  it('mentions the query-relevant con surface in Consensus Distillation', () => {
    // Regression guard for the trust-positioning copy added alongside the
    // query-relevant con feature. The page should explicitly tell users
    // that result cards now show a query-relevant con, so the user-facing
    // explanation stays in sync with the actual product behaviour.
    renderWithRouter(<HowItWorks />)
    expect(
      screen.getByText(/picks one con that\s+specifically speaks to what you asked for/i),
    ).toBeInTheDocument()
    expect(screen.getByText(/Encyclopedia entries still show the full/i)).toBeInTheDocument()
  })

  it('uses agnostic voice — no first-person plural in About the Data headings', () => {
    // Regression guard: the voice refactor removed "How We Score",
    // "How We Distill Consensus", "How We Rank", "What We Don't Keep".
    // None of those headings should reappear.
    renderWithRouter(<HowItWorks />)
    expect(screen.queryByText('How We Score')).not.toBeInTheDocument()
    expect(screen.queryByText('How We Distill Consensus')).not.toBeInTheDocument()
    expect(screen.queryByText('How We Rank')).not.toBeInTheDocument()
    expect(screen.queryByText("What We Don't Keep")).not.toBeInTheDocument()
  })

  it('embeds the quadrant chart with descriptive alt text', () => {
    renderWithRouter(<HowItWorks />)
    const img = screen.getByRole('img')
    expect(img).toHaveAttribute('src', '/quadrant_analysis.png')
    // Alt text should describe the chart's data (accessibility + SEO).
    expect(img.getAttribute('alt')).toMatch(/Crowd Pleasers/)
    expect(img.getAttribute('alt')).toMatch(/Hidden Gems/)
  })

  it('surfaces the rating-distribution summary including the surprising data points', () => {
    renderWithRouter(<HowItWorks />)
    // PG-13 dominates the catalog — call-out should be visible in the summary.
    expect(screen.getByText(/PG-13 is by far the largest bucket/)).toBeInTheDocument()
    // Rx is excluded because MAL's TV catalog filters explicit content — must be surfaced.
    expect(screen.getByText(/Rx.*not part of the vault/)).toBeInTheDocument()
  })

  it('cross-links to Privacy from the data-minimization sub-section', () => {
    renderWithRouter(<HowItWorks />)
    // Both the About-the-Data section AND the Open Access section link to
    // /privacy — assert there are at least two such links.
    const privacyLinks = screen.getAllByRole('link', { name: /Privacy|privacy policy/ })
    expect(privacyLinks.length).toBeGreaterThanOrEqual(2)
    privacyLinks.forEach(link => {
      expect(link).toHaveAttribute('href', '/privacy')
    })
  })

  it('no longer renders the removed sections', () => {
    renderWithRouter(<HowItWorks />)
    expect(screen.queryByText('What Makes This Different')).not.toBeInTheDocument()
    expect(screen.queryByText('Query Tips')).not.toBeInTheDocument()
    // "Built for Discovery" content was folded into the intro; the heading
    // itself shouldn't appear anywhere on the page.
    expect(screen.queryByText('Built for Discovery')).not.toBeInTheDocument()
  })

  it('has a Start Searching CTA', () => {
    renderWithRouter(<HowItWorks />)
    const cta = screen.getByText('Start Searching')
    expect(cta.closest('a')).toHaveAttribute('href', '/try')
  })

  it('links to privacy policy from the Open Access section', () => {
    renderWithRouter(<HowItWorks />)
    expect(screen.getByText('privacy policy')).toBeInTheDocument()
  })
})

describe('HowItWorks — dynamic vault count', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('renders the live anime count from /api/health', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      json: () => Promise.resolve({ anime_count: 6342 }),
    }))
    renderWithRouter(<HowItWorks />)
    await waitFor(() => {
      expect(screen.getByText(/6,342 anime titles/)).toBeInTheDocument()
    })
  })

  it('falls back to "thousands of anime titles" when the health call fails', async () => {
    // Load-bearing during hiatus mode: Railway is paused, /api/health times out,
    // and the copy still needs to make sense.
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('backend paused')))
    renderWithRouter(<HowItWorks />)
    await waitFor(() => {
      expect(screen.getByText(/thousands of anime titles/)).toBeInTheDocument()
    })
  })
})
