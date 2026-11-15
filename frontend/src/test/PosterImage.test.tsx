import { describe, it, expect, beforeEach, afterAll } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import PosterImage from '../components/PosterImage'

// Env-var save/restore pattern mirrors LandingHero.test.tsx — vi.stubEnv
// is unreliable for VITE_-prefixed vars, so we mutate import.meta.env
// directly and restore in afterAll.
const _originalFlag = import.meta.env.VITE_COVER_ART_ENABLED

beforeEach(() => {
  import.meta.env.VITE_COVER_ART_ENABLED = 'true'
})

afterAll(() => {
  if (_originalFlag === undefined) {
    delete import.meta.env.VITE_COVER_ART_ENABLED
  } else {
    import.meta.env.VITE_COVER_ART_ENABLED = _originalFlag
  }
})

describe('PosterImage', () => {
  it('renders the image when flag is on and src is provided', () => {
    render(<PosterImage src="https://cdn.myanimelist.net/images/anime/4/19644l.jpg"
                        alt="Cowboy Bebop" size="large" />)
    const img = screen.getByRole('img', { name: 'Cowboy Bebop' })
    expect(img).toHaveAttribute('src', 'https://cdn.myanimelist.net/images/anime/4/19644l.jpg')
    expect(img).toHaveAttribute('loading', 'lazy')
    expect(screen.queryByTestId('poster-fallback')).not.toBeInTheDocument()
  })

  it('renders fallback when VITE_COVER_ART_ENABLED is not "true"', () => {
    import.meta.env.VITE_COVER_ART_ENABLED = 'false'
    render(<PosterImage src="https://cdn.myanimelist.net/images/anime/4/19644l.jpg"
                        alt="Cowboy Bebop" size="large" />)
    expect(screen.getByTestId('poster-fallback')).toBeInTheDocument()
    // The alt text serves as the title inside the fallback card
    expect(screen.getByRole('img', { name: 'Cowboy Bebop' })).toBeInTheDocument()
  })

  it('renders fallback when src is null / empty', () => {
    render(<PosterImage src={null} alt="Missing Show" size="medium" />)
    expect(screen.getByTestId('poster-fallback')).toBeInTheDocument()
    expect(screen.getByRole('img', { name: 'Missing Show' })).toBeInTheDocument()
  })

  it('swaps to fallback when the image fires onError', () => {
    render(<PosterImage src="https://cdn.myanimelist.net/broken.jpg"
                        alt="Broken Show" size="large" />)
    // Initial render — real <img>, no fallback
    const img = screen.getByRole('img', { name: 'Broken Show' })
    expect(img.tagName).toBe('IMG')
    // Simulate MAL CDN 404 / network error
    fireEvent.error(img)
    // Fallback takes over
    expect(screen.getByTestId('poster-fallback')).toBeInTheDocument()
    expect(screen.queryByRole('img', { name: 'Broken Show' })?.tagName).not.toBe('IMG')
  })

  it('attribution links to MAL show page when malId is provided', () => {
    render(<PosterImage src="https://cdn.myanimelist.net/images/anime/4/19644l.jpg"
                        alt="Cowboy Bebop" size="large" malId={1} />)
    const attribution = screen.getByTestId('poster-attribution')
    expect(attribution.tagName).toBe('A')
    expect(attribution).toHaveAttribute('href', 'https://myanimelist.net/anime/1')
    expect(attribution).toHaveAttribute('target', '_blank')
    expect(attribution).toHaveAttribute('rel', 'noopener noreferrer')
    expect(attribution).toHaveTextContent('© MAL')
  })

  it('attribution renders as plain text when malId is omitted', () => {
    render(<PosterImage src="https://cdn.myanimelist.net/images/anime/4/19644l.jpg"
                        alt="Cowboy Bebop" size="large" />)
    const attribution = screen.getByTestId('poster-attribution')
    expect(attribution.tagName).toBe('SPAN')
    expect(attribution).toHaveTextContent('© MAL')
  })

  it('renders custom fallback element when one is provided', () => {
    import.meta.env.VITE_COVER_ART_ENABLED = 'false'
    render(<PosterImage
      src="https://cdn.myanimelist.net/images/anime/4/19644l.jpg"
      alt="Cowboy Bebop"
      size="large"
      fallback={<div data-testid="custom-fallback">CUSTOM</div>}
    />)
    expect(screen.getByTestId('custom-fallback')).toBeInTheDocument()
    // Default fallback should NOT render when custom is provided
    expect(screen.queryByTestId('poster-fallback')).not.toBeInTheDocument()
  })
})
