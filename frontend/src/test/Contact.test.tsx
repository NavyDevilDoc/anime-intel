import { describe, it, expect } from 'vitest'
import { screen } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import Contact from '../pages/Contact'

describe('Contact', () => {
  it('renders contact form heading', () => {
    renderWithProviders(<Contact />)
    expect(screen.getByText('Contact Us')).toBeInTheDocument()
  })

  it('has name, email, and message fields', () => {
    renderWithProviders(<Contact />)
    expect(screen.getByLabelText('Name')).toBeInTheDocument()
    expect(screen.getByLabelText('Email')).toBeInTheDocument()
    expect(screen.getByLabelText('Message')).toBeInTheDocument()
  })

  it('has a send button', () => {
    renderWithProviders(<Contact />)
    expect(screen.getByText('Send Message')).toBeInTheDocument()
  })

  it('shows character count for message', () => {
    renderWithProviders(<Contact />)
    expect(screen.getByText('0 / 2,000')).toBeInTheDocument()
  })

  it('send button is disabled when form is empty', () => {
    renderWithProviders(<Contact />)
    const button = screen.getByText('Send Message')
    expect(button).toBeDisabled()
  })
})
