import { describe, it, expect, vi } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { renderWithRouter } from './helpers'
import IntelligentSearchHint from '../components/IntelligentSearchHint'

describe('IntelligentSearchHint', () => {
  it('renders the advisory copy', () => {
    renderWithRouter(<IntelligentSearchHint onSwitchToIntelligentSearch={vi.fn()} />)
    const hint = screen.getByTestId('intelligent-search-hint')
    expect(hint).toBeInTheDocument()
    expect(hint).toHaveTextContent(/vibe query/i)
    expect(hint).toHaveTextContent(/AI-powered reasoning/i)
  })

  it('renders the mode-switch as a button (not a link)', () => {
    renderWithRouter(<IntelligentSearchHint onSwitchToIntelligentSearch={vi.fn()} />)
    const button = screen.getByRole('button', { name: /Switch to Intelligent Search/i })
    expect(button).toBeInTheDocument()
    expect(button.tagName).toBe('BUTTON')
  })

  it('calls the mode-switch callback when the button is clicked', () => {
    const onSwitch = vi.fn()
    renderWithRouter(<IntelligentSearchHint onSwitchToIntelligentSearch={onSwitch} />)
    fireEvent.click(screen.getByRole('button', { name: /Switch to Intelligent Search/i }))
    expect(onSwitch).toHaveBeenCalledTimes(1)
  })

  it('renders a Shows Like This link pointing at /try?mode=dna', () => {
    renderWithRouter(<IntelligentSearchHint onSwitchToIntelligentSearch={vi.fn()} />)
    const link = screen.getByRole('link', { name: /Shows Like This/i })
    expect(link).toHaveAttribute('href', '/try?mode=dna')
  })

  it('uses role="status" for accessibility (non-blocking advisory)', () => {
    renderWithRouter(<IntelligentSearchHint onSwitchToIntelligentSearch={vi.fn()} />)
    expect(screen.getByRole('status')).toBe(screen.getByTestId('intelligent-search-hint'))
  })
})
