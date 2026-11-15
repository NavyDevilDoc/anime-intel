import { describe, it, expect, vi } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { renderWithRouter } from './helpers'
import ObjectiveHint from '../components/ObjectiveHint'

describe('ObjectiveHint', () => {
  it('renders the advisory copy', () => {
    renderWithRouter(<ObjectiveHint onSwitchToObjectiveRankings={vi.fn()} />)
    const hint = screen.getByTestId('objective-hint')
    expect(hint).toBeInTheDocument()
    expect(hint).toHaveTextContent(/ranking query/i)
    expect(hint).toHaveTextContent(/precise top-N results/i)
  })

  it('renders the mode-switch as a button (not a link)', () => {
    renderWithRouter(<ObjectiveHint onSwitchToObjectiveRankings={vi.fn()} />)
    const button = screen.getByRole('button', { name: /Switch to Objective Rankings/i })
    expect(button).toBeInTheDocument()
    expect(button.tagName).toBe('BUTTON')
  })

  it('calls the mode-switch callback when the button is clicked', () => {
    const onSwitch = vi.fn()
    renderWithRouter(<ObjectiveHint onSwitchToObjectiveRankings={onSwitch} />)
    fireEvent.click(screen.getByRole('button', { name: /Switch to Objective Rankings/i }))
    expect(onSwitch).toHaveBeenCalledTimes(1)
  })

  it('renders a Top Lists link pointing at /top', () => {
    renderWithRouter(<ObjectiveHint onSwitchToObjectiveRankings={vi.fn()} />)
    const link = screen.getByRole('link', { name: /Top Lists/i })
    expect(link).toHaveAttribute('href', '/top')
  })

  it('uses role="status" for accessibility (non-blocking advisory)', () => {
    renderWithRouter(<ObjectiveHint onSwitchToObjectiveRankings={vi.fn()} />)
    expect(screen.getByRole('status')).toBe(screen.getByTestId('objective-hint'))
  })
})
