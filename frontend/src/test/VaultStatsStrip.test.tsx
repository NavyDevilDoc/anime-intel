import { describe, it, expect, beforeEach, afterAll } from 'vitest'
import { screen } from '@testing-library/react'
import { renderWithRouter } from './helpers'
import VaultStatsStrip from '../components/VaultStatsStrip'

const _originalFlag = import.meta.env.VITE_LANDING_STATS_STRIP_ENABLED

beforeEach(() => {
  import.meta.env.VITE_LANDING_STATS_STRIP_ENABLED = 'true'
})

afterAll(() => {
  if (_originalFlag === undefined) {
    delete import.meta.env.VITE_LANDING_STATS_STRIP_ENABLED
  } else {
    import.meta.env.VITE_LANDING_STATS_STRIP_ENABLED = _originalFlag
  }
})

describe('VaultStatsStrip', () => {
  it('renders null when flag is off', () => {
    import.meta.env.VITE_LANDING_STATS_STRIP_ENABLED = 'false'
    const { container } = renderWithRouter(<VaultStatsStrip />)
    expect(container).toBeEmptyDOMElement()
  })

  it('renders all four stat cards with expected values', () => {
    renderWithRouter(<VaultStatsStrip />)
    expect(screen.getByTestId('vault-stats-strip')).toBeInTheDocument()
    // Spot-check each stat's headline number
    expect(screen.getByText('6,428')).toBeInTheDocument()
    expect(screen.getByText('200,000+')).toBeInTheDocument()
    expect(screen.getByText('955')).toBeInTheDocument()
    expect(screen.getByText('28,162')).toBeInTheDocument()
  })

  it('renders the popularity-drowns-out-obscure-shows pull quote', () => {
    renderWithRouter(<VaultStatsStrip />)
    const bq = screen.getByTestId('vault-stats-quadrant-blockquote')
    expect(bq).toBeInTheDocument()
    // Third-iteration framing: popularity DROWNS OUT equally-great
    // obscure shows (rather than "popular = bad", which contradicts any
    // popular-and-great show like Frieren / MHA / Naruto). Ends on the
    // "buried treasure" hook.
    expect(bq).toHaveTextContent(/Popular anime and the hype around them tend to drown out/)
    expect(bq).toHaveTextContent(/The community is vocal about what works/)
    expect(bq).toHaveTextContent(/uncover buried treasure/)
    expect(bq).toHaveTextContent(/see what the data actually shows/)
    // Blockquote is itself a link to the scoring section
    expect(bq).toHaveAttribute('href', '/how-it-works#scoring')
  })

  it('stat cards link to the correct HowItWorks anchors', () => {
    renderWithRouter(<VaultStatsStrip />)
    // Each of the 4 numeric values is inside a Link — verify hrefs
    // by finding the link ancestor of each headline.
    const vaultSize = screen.getByText('6,428').closest('a')
    expect(vaultSize).toHaveAttribute('href', '/how-it-works#vault')

    const reviews = screen.getByText('200,000+').closest('a')
    expect(reviews).toHaveAttribute('href', '/how-it-works#scoring')

    const franchises = screen.getByText('955').closest('a')
    expect(franchises).toHaveAttribute('href', '/how-it-works#vault')

    const staff = screen.getByText('28,162').closest('a')
    expect(staff).toHaveAttribute('href', '/how-it-works#vault')
  })
})
