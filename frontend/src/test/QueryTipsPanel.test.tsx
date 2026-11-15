import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import QueryTipsPanel from '../components/QueryTipsPanel'

describe('QueryTipsPanel', () => {
  it('renders the toggle button collapsed by default', () => {
    render(<QueryTipsPanel />)
    const button = screen.getByRole('button', { name: /Query Tips/ })
    expect(button).toBeInTheDocument()
    expect(button).toHaveAttribute('aria-expanded', 'false')
    // None of the category labels should be visible when collapsed.
    expect(screen.queryByText('Vibe & Theme')).not.toBeInTheDocument()
    expect(screen.queryByText('Plot & Narrative')).not.toBeInTheDocument()
  })

  it('expands all 7 categories when the toggle is clicked', async () => {
    const user = userEvent.setup()
    render(<QueryTipsPanel />)
    await user.click(screen.getByRole('button', { name: /Query Tips/ }))

    expect(screen.getByText('Vibe & Theme')).toBeInTheDocument()
    expect(screen.getByText('Studio Filter')).toBeInTheDocument()
    expect(screen.getByText('Quality & Popularity')).toBeInTheDocument()
    expect(screen.getByText('Mood Filter')).toBeInTheDocument()
    expect(screen.getByText('Plot & Narrative')).toBeInTheDocument()
    expect(screen.getByText('Character Archetype')).toBeInTheDocument()
    expect(screen.getByText('Objective Rankings')).toBeInTheDocument()
  })

  it('flips aria-expanded as the panel toggles', async () => {
    const user = userEvent.setup()
    render(<QueryTipsPanel />)
    const button = screen.getByRole('button', { name: /Query Tips/ })

    expect(button).toHaveAttribute('aria-expanded', 'false')
    await user.click(button)
    expect(button).toHaveAttribute('aria-expanded', 'true')
    await user.click(button)
    expect(button).toHaveAttribute('aria-expanded', 'false')
  })

  it('collapses category content when toggled closed again', async () => {
    const user = userEvent.setup()
    render(<QueryTipsPanel />)
    const button = screen.getByRole('button', { name: /Query Tips/ })

    await user.click(button)
    expect(screen.getByText('Vibe & Theme')).toBeInTheDocument()

    await user.click(button)
    expect(screen.queryByText('Vibe & Theme')).not.toBeInTheDocument()
  })
})
