import { describe, it, expect } from 'vitest'
import { screen } from '@testing-library/react'
import { renderWithRouter } from './helpers'
import ExploreVibeButton from '../components/ExploreVibeButton'

describe('ExploreVibeButton', () => {
  it('renders a link with "Explore this vibe" copy', () => {
    renderWithRouter(<ExploreVibeButton vibe="Sci-Fi, Thriller, Time Travel" />)
    const link = screen.getByRole('link', { name: 'Explore this vibe' })
    expect(link).toBeInTheDocument()
  })

  it('routes to /try with mode=find and the URL-encoded vibe triplet as q param', () => {
    renderWithRouter(<ExploreVibeButton vibe="Sci-Fi, Thriller, Time Travel" />)
    const link = screen.getByRole('link', { name: 'Explore this vibe' })
    // mode=find is load-bearing when clicking from inside /try (e.g.
    // Encyclopedia tab) — without it, Search.tsx keeps the active tab
    // and the vibe triplet becomes a dangling q param.
    expect(link).toHaveAttribute(
      'href',
      '/try?mode=find&q=Sci-Fi%2C%20Thriller%2C%20Time%20Travel',
    )
  })

  it('suppresses render when vibe is null (defensive guard matches SEO page)', () => {
    const { container } = renderWithRouter(<ExploreVibeButton vibe={null} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('suppresses render when vibe is undefined', () => {
    const { container } = renderWithRouter(<ExploreVibeButton vibe={undefined} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('suppresses render when vibe is empty string', () => {
    const { container } = renderWithRouter(<ExploreVibeButton vibe="" />)
    expect(container).toBeEmptyDOMElement()
  })

  it('suppresses render when vibe is whitespace-only', () => {
    const { container } = renderWithRouter(<ExploreVibeButton vibe="   " />)
    expect(container).toBeEmptyDOMElement()
  })

  it('trims surrounding whitespace from the vibe before encoding', () => {
    renderWithRouter(<ExploreVibeButton vibe="  Sci-Fi  " />)
    const link = screen.getByRole('link', { name: 'Explore this vibe' })
    expect(link).toHaveAttribute('href', '/try?mode=find&q=Sci-Fi')
  })

  it('accepts a className override', () => {
    renderWithRouter(
      <ExploreVibeButton vibe="Mecha, Military, Drama" className="custom-class-x" />,
    )
    const link = screen.getByRole('link', { name: 'Explore this vibe' })
    expect(link).toHaveClass('custom-class-x')
  })

  it('applies the default outlined-accent CTA class when no override given', () => {
    renderWithRouter(<ExploreVibeButton vibe="Vibe" />)
    const link = screen.getByRole('link', { name: 'Explore this vibe' })
    // Spot-check the load-bearing styling tokens — border + accent color +
    // outlined (bg-transparent) match the PR #175 cta-button-secondary.
    expect(link).toHaveClass('border-vault-accent')
    expect(link).toHaveClass('text-vault-accent')
    expect(link).toHaveClass('bg-transparent')
  })
})
