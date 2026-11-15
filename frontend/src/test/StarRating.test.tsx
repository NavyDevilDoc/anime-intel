import { describe, it, expect, vi } from 'vitest'
import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithRouter } from './helpers'
import StarRating from '../components/StarRating'

describe('StarRating', () => {
  it('renders 5 star buttons', () => {
    renderWithRouter(<StarRating avgRating={0} ratingCount={0} userRating={null} />)
    const stars = screen.getAllByText('★')
    expect(stars).toHaveLength(5)
  })

  it('displays average rating and count', () => {
    renderWithRouter(<StarRating avgRating={4.2} ratingCount={7} userRating={null} />)
    expect(screen.getByText('4.2 (7)')).toBeInTheDocument()
  })

  it('shows "No ratings" when count is 0', () => {
    renderWithRouter(<StarRating avgRating={0} ratingCount={0} userRating={null} />)
    expect(screen.getByText('No ratings')).toBeInTheDocument()
  })

  it('calls onRate when star is clicked', async () => {
    const onRate = vi.fn()
    const user = userEvent.setup()
    renderWithRouter(<StarRating avgRating={0} ratingCount={0} userRating={null} onRate={onRate} />)

    const stars = screen.getAllByText('★')
    await user.click(stars[3]) // Click 4th star
    expect(onRate).toHaveBeenCalledWith(4)
  })

  it('does not call onRate when disabled', async () => {
    const onRate = vi.fn()
    const user = userEvent.setup()
    renderWithRouter(<StarRating avgRating={0} ratingCount={0} userRating={null} onRate={onRate} disabled />)

    const stars = screen.getAllByText('★')
    await user.click(stars[0])
    expect(onRate).not.toHaveBeenCalled()
  })
})
