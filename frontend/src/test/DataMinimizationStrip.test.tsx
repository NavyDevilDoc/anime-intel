import { describe, it, expect, beforeEach, afterAll } from 'vitest'
import { screen } from '@testing-library/react'
import { renderWithRouter } from './helpers'
import DataMinimizationStrip from '../components/DataMinimizationStrip'

const _originalFlag = import.meta.env.VITE_LANDING_MINIMIZATION_STRIP_ENABLED

beforeEach(() => {
  import.meta.env.VITE_LANDING_MINIMIZATION_STRIP_ENABLED = 'true'
})

afterAll(() => {
  if (_originalFlag === undefined) {
    delete import.meta.env.VITE_LANDING_MINIMIZATION_STRIP_ENABLED
  } else {
    import.meta.env.VITE_LANDING_MINIMIZATION_STRIP_ENABLED = _originalFlag
  }
})

describe('DataMinimizationStrip', () => {
  it('renders null when flag is off', () => {
    import.meta.env.VITE_LANDING_MINIMIZATION_STRIP_ENABLED = 'false'
    const { container } = renderWithRouter(<DataMinimizationStrip />)
    expect(container).toBeEmptyDOMElement()
  })

  it('renders all four trust claims', () => {
    renderWithRouter(<DataMinimizationStrip />)
    expect(screen.getByTestId('data-minimization-strip')).toBeInTheDocument()
    expect(screen.getByText('No accounts')).toBeInTheDocument()
    expect(screen.getByText('No ad tracking')).toBeInTheDocument()
    expect(screen.getByText('Salted-hash-only telemetry')).toBeInTheDocument()
    // Corrected copy — plan's original "No cover art hotlinking" was inaccurate
    // post-PR-#148. Confirm we ship the honest hotlink-with-attribution claim.
    expect(screen.getByText(/Poster art hotlinked from MAL with attribution/)).toBeInTheDocument()
  })

  it('detail link points to /how-it-works#data-minimization', () => {
    renderWithRouter(<DataMinimizationStrip />)
    const link = screen.getByTestId('data-minimization-strip-detail-link')
    expect(link).toHaveAttribute('href', '/how-it-works#data-minimization')
    expect(link).toHaveTextContent(/data-handling contract/i)
  })
})
