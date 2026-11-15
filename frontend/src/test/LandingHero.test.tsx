import { describe, it, expect, vi, beforeEach, afterAll } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithRouter } from './helpers'
import LandingHero from '../components/LandingHero'

const mockNavigate = vi.fn()
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom')
  return { ...actual, useNavigate: () => mockNavigate }
})

// Snapshot the developer's local `.env` value once, then force it OFF for
// the main describe (so pre-existing tests aren't affected by a local
// `VITE_ENGINE_HIATUS_MODE=true`) and back to the original in the file-
// level afterAll. Direct mutation on `import.meta.env` — vi.stubEnv is
// unreliable for VITE_-prefixed vars because Vite's plugin transforms
// them. This mirrors the SeriesContext.test.tsx pattern.
const _originalHiatusEnv = import.meta.env.VITE_ENGINE_HIATUS_MODE

beforeEach(() => {
  vi.restoreAllMocks()
  mockNavigate.mockReset()
  import.meta.env.VITE_ENGINE_HIATUS_MODE = ''
})

afterAll(() => {
  if (_originalHiatusEnv === undefined) {
    delete import.meta.env.VITE_ENGINE_HIATUS_MODE
  } else {
    import.meta.env.VITE_ENGINE_HIATUS_MODE = _originalHiatusEnv
  }
})

describe('LandingHero', () => {
  it('renders the heading', () => {
    renderWithRouter(<LandingHero />)
    expect(screen.getByText('AI-Powered Anime Discovery')).toBeInTheDocument()
  })

  it('renders all four tabs including Top Lists', () => {
    renderWithRouter(<LandingHero />)
    // Tabs and feature cards both contain these labels — getByRole('tab')
    // scopes to the tab bar so we don't collide with card titles.
    expect(screen.getByRole('tab', { name: 'Find a Show' })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: 'Shows Like This' })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: 'Encyclopedia' })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: 'Top Lists' })).toBeInTheDocument()
  })

  it('defaults to Find a Show mode with vibe placeholder', () => {
    renderWithRouter(<LandingHero />)
    const input = screen.getByLabelText('Search anime')
    expect(input).toHaveAttribute('placeholder', "Describe a vibe, theme, or show you'd like...")
    expect(screen.getByRole('button', { name: 'Find a Show' })).toBeInTheDocument()
  })

  it('switches placeholder and button when Shows Like This tab is selected', async () => {
    const user = userEvent.setup()
    renderWithRouter(<LandingHero />)
    await user.click(screen.getByRole('tab', { name: 'Shows Like This' }))
    const input = screen.getByLabelText('Search anime')
    expect(input).toHaveAttribute('placeholder', 'Enter a show you already like...')
    expect(screen.getByRole('button', { name: 'Find Similar Shows' })).toBeInTheDocument()
  })

  it('switches placeholder and button when Encyclopedia tab is selected', async () => {
    const user = userEvent.setup()
    renderWithRouter(<LandingHero />)
    await user.click(screen.getByRole('tab', { name: 'Encyclopedia' }))
    const input = screen.getByLabelText('Search anime')
    expect(input).toHaveAttribute('placeholder', 'Look up any anime by title...')
    expect(screen.getByRole('button', { name: 'Look Up' })).toBeInTheDocument()
  })

  it('navigates to /top when the Top Lists tab is clicked', async () => {
    const user = userEvent.setup()
    renderWithRouter(<LandingHero />)
    await user.click(screen.getByRole('tab', { name: 'Top Lists' }))
    expect(mockNavigate).toHaveBeenCalledWith('/top')
  })

  it('renders a Top Lists descriptor card that also navigates to /top', async () => {
    const user = userEvent.setup()
    renderWithRouter(<LandingHero />)
    // The 4 cards are rendered — 3 for the form modes + 1 for Top Lists.
    // The Top Lists card is a button (clickable navigation), unlike the
    // static <div> cards for the form modes.
    const card = screen.getByRole('button', { name: /Top Lists.*Browse curated rankings/i })
    await user.click(card)
    expect(mockNavigate).toHaveBeenCalledWith('/top')
  })

  it('navigates to /try?q=<encoded> on Find a Show submit with text', async () => {
    const user = userEvent.setup()
    renderWithRouter(<LandingHero />)
    const input = screen.getByLabelText('Search anime')
    await user.type(input, 'gritty military drama')
    await user.click(screen.getByRole('button', { name: 'Find a Show' }))
    expect(mockNavigate).toHaveBeenCalledWith('/try?q=gritty%20military%20drama')
  })

  it('navigates to /try?mode=dna&q= on Shows Like This submit', async () => {
    const user = userEvent.setup()
    renderWithRouter(<LandingHero />)
    await user.click(screen.getByRole('tab', { name: 'Shows Like This' }))
    const input = screen.getByLabelText('Search anime')
    await user.type(input, 'Attack on Titan')
    await user.click(screen.getByRole('button', { name: 'Find Similar Shows' }))
    expect(mockNavigate).toHaveBeenCalledWith('/try?mode=dna&q=Attack%20on%20Titan')
  })

  it('navigates to /try?mode=encyclopedia&q= on Encyclopedia submit', async () => {
    const user = userEvent.setup()
    renderWithRouter(<LandingHero />)
    await user.click(screen.getByRole('tab', { name: 'Encyclopedia' }))
    const input = screen.getByLabelText('Search anime')
    await user.type(input, 'Cowboy Bebop')
    await user.click(screen.getByRole('button', { name: 'Look Up' }))
    expect(mockNavigate).toHaveBeenCalledWith('/try?mode=encyclopedia&q=Cowboy%20Bebop')
  })

  it('navigates to /try (no query) on empty Find a Show submit', async () => {
    const user = userEvent.setup()
    renderWithRouter(<LandingHero />)
    await user.click(screen.getByRole('button', { name: 'Find a Show' }))
    expect(mockNavigate).toHaveBeenCalledWith('/try')
  })

  it('trims whitespace before navigating', async () => {
    const user = userEvent.setup()
    renderWithRouter(<LandingHero />)
    const input = screen.getByLabelText('Search anime')
    await user.type(input, '   cozy slice of life   ')
    await user.click(screen.getByRole('button', { name: 'Find a Show' }))
    expect(mockNavigate).toHaveBeenCalledWith('/try?q=cozy%20slice%20of%20life')
  })

  it('displays anime count from health endpoint', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      json: () => Promise.resolve({ anime_count: 6293 }),
    }))

    renderWithRouter(<LandingHero />)

    await waitFor(() => {
      expect(screen.getByText(/6,293 titles indexed/)).toBeInTheDocument()
    })
  })

  it('shows fallback text when health endpoint fails', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('Network error')))

    renderWithRouter(<LandingHero />)

    await waitFor(() => {
      expect(screen.getByText(/Thousands of titles indexed/)).toBeInTheDocument()
    })
  })

  it('renders sample query chips for the default Find a Show mode', () => {
    renderWithRouter(<LandingHero />)
    expect(screen.getByText(/"gritty military drama"/)).toBeInTheDocument()
    expect(screen.getByText(/"cozy slice of life"/)).toBeInTheDocument()
    expect(screen.getByText(/"top sports anime from 2023"/)).toBeInTheDocument()
  })

  it('switches sample chips when mode changes', async () => {
    const user = userEvent.setup()
    renderWithRouter(<LandingHero />)
    await user.click(screen.getByRole('tab', { name: 'Shows Like This' }))
    expect(screen.getByText(/"Attack on Titan"/)).toBeInTheDocument()
    expect(screen.queryByText(/"gritty military drama"/)).not.toBeInTheDocument()
  })

  it('links to How It Works page', () => {
    renderWithRouter(<LandingHero />)
    const link = screen.getByText('Learn how it works')
    expect(link.closest('a')).toHaveAttribute('href', '/how-it-works')
  })

  it('renders three feature cards describing each mode', () => {
    renderWithRouter(<LandingHero />)
    // Each mode label appears in both the tab and the card heading — assert
    // by counting occurrences (>=2) rather than by uniqueness.
    expect(screen.getAllByText('Find a Show').length).toBeGreaterThanOrEqual(2)
    expect(screen.getAllByText('Shows Like This').length).toBeGreaterThanOrEqual(2)
    expect(screen.getAllByText('Encyclopedia').length).toBeGreaterThanOrEqual(2)
  })

  it('does not render the hiatus notice when VITE_ENGINE_HIATUS_MODE is unset (default)', () => {
    renderWithRouter(<LandingHero />)
    expect(screen.queryByTestId('hiatus-notice')).not.toBeInTheDocument()
    // FaS/SLT tabs are not disabled in default mode
    expect(screen.getByRole('tab', { name: 'Find a Show' })).not.toBeDisabled()
    expect(screen.getByRole('tab', { name: 'Shows Like This' })).not.toBeDisabled()
  })
})

describe('LandingHero — hiatus mode', () => {
  // File-level beforeEach forced the flag to '' — flip it on here so every
  // test in this block sees the hiatus branch. afterAll at file scope
  // restores whatever the developer had in .env.
  beforeEach(() => {
    import.meta.env.VITE_ENGINE_HIATUS_MODE = 'true'
  })

  it('shows the hiatus notice above the tab bar', () => {
    renderWithRouter(<LandingHero />)
    const notice = screen.getByTestId('hiatus-notice')
    expect(notice).toBeInTheDocument()
    expect(notice).toHaveTextContent(/recommendation engine is on hiatus/i)
  })

  it('renders Find a Show and Shows Like This tabs as disabled', () => {
    renderWithRouter(<LandingHero />)
    const fasTab = screen.getByRole('tab', { name: /Find a Show/i })
    const sltTab = screen.getByRole('tab', { name: /Shows Like This/i })
    expect(fasTab).toBeDisabled()
    expect(fasTab).toHaveAttribute('aria-disabled', 'true')
    expect(sltTab).toBeDisabled()
    expect(sltTab).toHaveAttribute('aria-disabled', 'true')
  })

  it('leaves the Encyclopedia tab fully interactive', () => {
    renderWithRouter(<LandingHero />)
    const encTab = screen.getByRole('tab', { name: 'Encyclopedia' })
    expect(encTab).not.toBeDisabled()
    expect(encTab).not.toHaveAttribute('aria-disabled', 'true')
  })

  it('defaults to Encyclopedia mode (its placeholder + button show)', () => {
    renderWithRouter(<LandingHero />)
    const input = screen.getByLabelText('Search anime')
    expect(input).toHaveAttribute('placeholder', 'Look up any anime by title...')
    expect(screen.getByRole('button', { name: 'Look Up' })).toBeInTheDocument()
  })

  it('clicking a disabled tab does not switch active mode', async () => {
    const user = userEvent.setup()
    renderWithRouter(<LandingHero />)
    // userEvent respects `disabled` on the button, so the click is a no-op;
    // input placeholder stays on Encyclopedia's.
    await user.click(screen.getByRole('tab', { name: /Find a Show/i }))
    const input = screen.getByLabelText('Search anime')
    expect(input).toHaveAttribute('placeholder', 'Look up any anime by title...')
  })
})
