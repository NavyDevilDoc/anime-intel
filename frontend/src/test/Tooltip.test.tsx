import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import Tooltip from '../components/Tooltip'

describe('Tooltip', () => {
  it('renders children text', () => {
    render(<Tooltip text="Help text"><span>Quality:</span></Tooltip>)
    expect(screen.getByText('Quality:')).toBeInTheDocument()
  })

  it('renders info icon with aria-label', () => {
    render(<Tooltip text="Help text"><span>Label</span></Tooltip>)
    expect(screen.getByLabelText('Help text')).toBeInTheDocument()
  })

  it('does not show tooltip text by default', () => {
    render(<Tooltip text="Hidden help"><span>Label</span></Tooltip>)
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
  })

  it('shows tooltip on hover', async () => {
    const user = userEvent.setup()
    render(<Tooltip text="Bayesian-adjusted score"><span>Quality:</span></Tooltip>)

    const wrapper = screen.getByText('Quality:').closest('span[class*="relative"]')!
    await user.hover(wrapper)

    expect(screen.getByRole('tooltip')).toBeInTheDocument()
    expect(screen.getByText('Bayesian-adjusted score')).toBeInTheDocument()
  })

  it('hides tooltip on unhover', async () => {
    const user = userEvent.setup()
    render(<Tooltip text="Help text"><span>Label</span></Tooltip>)

    const wrapper = screen.getByText('Label').closest('span[class*="relative"]')!
    await user.hover(wrapper)
    expect(screen.getByRole('tooltip')).toBeInTheDocument()

    await user.unhover(wrapper)
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
  })
})
