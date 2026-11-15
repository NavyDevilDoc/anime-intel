import { describe, it, expect } from 'vitest'
import { screen } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import NotFound from '../pages/NotFound'

describe('NotFound', () => {
  it('renders 404 heading', () => {
    renderWithProviders(<NotFound />)
    expect(screen.getByText('404')).toBeInTheDocument()
  })

  it('shows page not found message', () => {
    renderWithProviders(<NotFound />)
    expect(screen.getByText('Page not found')).toBeInTheDocument()
  })

  it('has a link back to search', () => {
    renderWithProviders(<NotFound />)
    const link = screen.getByText('Back to Search')
    expect(link).toBeInTheDocument()
    expect(link).toHaveAttribute('href', '/')
  })
})
