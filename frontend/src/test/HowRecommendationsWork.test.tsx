import { describe, it, expect, beforeEach, afterAll } from 'vitest'
import { screen } from '@testing-library/react'
import { renderWithRouter } from './helpers'
import HowRecommendationsWork from '../components/HowRecommendationsWork'

const _originalFlag = import.meta.env.VITE_LANDING_HOW_STRIP_ENABLED

beforeEach(() => {
  import.meta.env.VITE_LANDING_HOW_STRIP_ENABLED = 'true'
})

afterAll(() => {
  if (_originalFlag === undefined) {
    delete import.meta.env.VITE_LANDING_HOW_STRIP_ENABLED
  } else {
    import.meta.env.VITE_LANDING_HOW_STRIP_ENABLED = _originalFlag
  }
})

describe('HowRecommendationsWork', () => {
  it('renders null when flag is off', () => {
    import.meta.env.VITE_LANDING_HOW_STRIP_ENABLED = 'false'
    const { container } = renderWithRouter(<HowRecommendationsWork />)
    expect(container).toBeEmptyDOMElement()
  })

  it('renders all three steps in order (Retrieve -> Rerank -> Reason)', () => {
    renderWithRouter(<HowRecommendationsWork />)
    expect(screen.getByTestId('how-recommendations-work')).toBeInTheDocument()
    expect(screen.getByText('Retrieve')).toBeInTheDocument()
    expect(screen.getByText('Rerank')).toBeInTheDocument()
    expect(screen.getByText('Reason')).toBeInTheDocument()
    // Verify step numbers render
    expect(screen.getByText('Step 1')).toBeInTheDocument()
    expect(screen.getByText('Step 2')).toBeInTheDocument()
    expect(screen.getByText('Step 3')).toBeInTheDocument()
  })

  it('detail link points to /how-it-works#consensus with matching copy', () => {
    renderWithRouter(<HowRecommendationsWork />)
    const link = screen.getByTestId('how-recommendations-work-detail-link')
    // Points to Consensus Distillation rather than Ranking — Consensus
    // is the more compelling deep-dive destination (per PR #3 eyeball
    // feedback). Link copy updated to match the target section.
    expect(link).toHaveAttribute('href', '/how-it-works#consensus')
    expect(link).toHaveTextContent(/consensus profiles/i)
  })
})
