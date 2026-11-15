import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from './helpers'
import SearchFeedback from '../components/SearchFeedback'

vi.mock('../lib/api', () => ({
  api: {
    post: vi.fn(),
  },
}))

import { api } from '../lib/api'
const mockPost = vi.mocked(api.post)

beforeEach(() => {
  vi.clearAllMocks()
})

describe('SearchFeedback', () => {
  it('renders thumbs up and down buttons', () => {
    renderWithProviders(<SearchFeedback searchId="test-123" />)
    expect(screen.getByLabelText('Thumbs up')).toBeInTheDocument()
    expect(screen.getByLabelText('Thumbs down')).toBeInTheDocument()
    expect(screen.getByText('Helpful?')).toBeInTheDocument()
  })

  it('submits thumbs up and shows thank you', async () => {
    mockPost.mockResolvedValueOnce({ success: true })
    const user = userEvent.setup()
    renderWithProviders(<SearchFeedback searchId="test-123" />)

    await user.click(screen.getByLabelText('Thumbs up'))

    await waitFor(() => {
      expect(screen.getByText('Thanks!')).toBeInTheDocument()
    })
    expect(mockPost).toHaveBeenCalledWith('/api/search-feedback', {
      search_id: 'test-123',
      rating: 'up',
    })
  })

  it('submits thumbs down and shows acknowledgment', async () => {
    mockPost.mockResolvedValueOnce({ success: true })
    const user = userEvent.setup()
    renderWithProviders(<SearchFeedback searchId="test-456" />)

    await user.click(screen.getByLabelText('Thumbs down'))

    await waitFor(() => {
      expect(screen.getByText(/we'll improve/i)).toBeInTheDocument()
    })
    expect(mockPost).toHaveBeenCalledWith('/api/search-feedback', {
      search_id: 'test-456',
      rating: 'down',
    })
  })

  it('shows a visible error message on API failure', async () => {
    mockPost.mockRejectedValueOnce(new Error('Network error'))
    const user = userEvent.setup()
    renderWithProviders(<SearchFeedback searchId="test-789" />)

    await user.click(screen.getByLabelText('Thumbs up'))

    await waitFor(() => {
      expect(screen.getByText(/Couldn't save your rating/i)).toBeInTheDocument()
    })
    expect(screen.getByText('Try again')).toBeInTheDocument()
    // Thumbs buttons are hidden in the error state
    expect(screen.queryByLabelText('Thumbs up')).not.toBeInTheDocument()
  })

  it('restores the buttons when "Try again" is clicked after a failure', async () => {
    mockPost.mockRejectedValueOnce(new Error('Network error'))
    const user = userEvent.setup()
    renderWithProviders(<SearchFeedback searchId="test-789" />)

    await user.click(screen.getByLabelText('Thumbs up'))
    await waitFor(() => {
      expect(screen.getByText('Try again')).toBeInTheDocument()
    })

    await user.click(screen.getByText('Try again'))

    expect(screen.getByLabelText('Thumbs up')).toBeInTheDocument()
    expect(screen.getByLabelText('Thumbs down')).toBeInTheDocument()
    expect(screen.queryByText(/Couldn't save/i)).not.toBeInTheDocument()
  })
})
