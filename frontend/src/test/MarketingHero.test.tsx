import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithRouter } from './helpers'
import MarketingHero from '../components/MarketingHero'

const mockNavigate = vi.fn()
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom')
  return { ...actual, useNavigate: () => mockNavigate }
})

beforeEach(() => {
  vi.restoreAllMocks()
  mockNavigate.mockReset()
})

describe('MarketingHero', () => {
  it('renders the headline and subheadline', () => {
    renderWithRouter(<MarketingHero />)
    expect(screen.getByTestId('marketing-hero-headline')).toHaveTextContent(
      /Anime recommendations that understand what you want to watch/,
    )
    expect(screen.getByTestId('marketing-hero-subheadline')).toHaveTextContent(
      /powered by the full force of the anime community/,
    )
  })

  it('renders all 5 example chips', () => {
    renderWithRouter(<MarketingHero />)
    const chipContainer = screen.getByTestId('marketing-hero-chips')
    expect(chipContainer.children.length).toBe(5)
    // Spot-check two chips are present
    expect(screen.getByText(/slow-burn romance/i)).toBeInTheDocument()
    expect(screen.getByText(/philosophical mecha/i)).toBeInTheDocument()
  })

  it('navigates to /try?q=<encoded> on chip click', async () => {
    const user = userEvent.setup()
    renderWithRouter(<MarketingHero />)
    await user.click(screen.getByText(/hidden gems from the 2000s/i))
    expect(mockNavigate).toHaveBeenCalledWith('/try?q=hidden%20gems%20from%20the%202000s')
  })

  it('navigates to /try?q=<encoded> on form submit with text', async () => {
    const user = userEvent.setup()
    renderWithRouter(<MarketingHero />)
    const input = screen.getByLabelText('Search anime')
    await user.type(input, 'cozy slice of life')
    await user.click(screen.getByRole('button', { name: 'Find a Show' }))
    expect(mockNavigate).toHaveBeenCalledWith('/try?q=cozy%20slice%20of%20life')
  })

  it('navigates to /try (no query) on empty form submit', async () => {
    const user = userEvent.setup()
    renderWithRouter(<MarketingHero />)
    await user.click(screen.getByRole('button', { name: 'Find a Show' }))
    expect(mockNavigate).toHaveBeenCalledWith('/try')
  })

  it('trims whitespace before navigating', async () => {
    const user = userEvent.setup()
    renderWithRouter(<MarketingHero />)
    const input = screen.getByLabelText('Search anime')
    await user.type(input, '   gritty military drama   ')
    await user.click(screen.getByRole('button', { name: 'Find a Show' }))
    expect(mockNavigate).toHaveBeenCalledWith('/try?q=gritty%20military%20drama')
  })

  it('renders three "also try" links to the correct routes', () => {
    renderWithRouter(<MarketingHero />)
    expect(screen.getByRole('link', { name: 'Shows Like This' })).toHaveAttribute(
      'href', '/try?mode=dna',
    )
    expect(screen.getByRole('link', { name: 'Encyclopedia' })).toHaveAttribute(
      'href', '/try?mode=encyclopedia',
    )
    expect(screen.getByRole('link', { name: 'Top Lists' })).toHaveAttribute(
      'href', '/top',
    )
  })

})
