import { describe, it, expect } from 'vitest'
import { screen } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import Privacy from '../pages/Privacy'

describe('Privacy', () => {
  it('renders privacy policy heading', () => {
    renderWithProviders(<Privacy />)
    expect(screen.getByText('Privacy Policy')).toBeInTheDocument()
  })

  it('includes all required sections', () => {
    renderWithProviders(<Privacy />)
    expect(screen.getByText('What We Collect')).toBeInTheDocument()
    expect(screen.getByText('What We Do NOT Collect')).toBeInTheDocument()
    expect(screen.getByText('How We Use Your Data')).toBeInTheDocument()
    expect(screen.getByText('Data Storage')).toBeInTheDocument()
    expect(screen.getByText('Cookies & Local Storage')).toBeInTheDocument()
    expect(screen.getByText('Your Rights')).toBeInTheDocument()
    expect(screen.getByText('Contact')).toBeInTheDocument()
  })

  it('describes the open-access model with no accounts', () => {
    renderWithProviders(<Privacy />)
    expect(screen.getByText(/no sign-up, no\s+login, and no user accounts/i)).toBeInTheDocument()
  })

  it('explains the salted IP hash mechanism', () => {
    renderWithProviders(<Privacy />)
    // The phrase appears both as a definition term and again later in the
    // "Your Rights" section, so getAllByText is the appropriate matcher.
    expect(screen.getAllByText(/Salted IP hash/i).length).toBeGreaterThan(0)
  })

  it('mentions no data selling', () => {
    renderWithProviders(<Privacy />)
    expect(screen.getByText(/do not sell/i)).toBeInTheDocument()
  })

  it('mentions Supabase for data storage', () => {
    renderWithProviders(<Privacy />)
    expect(screen.getAllByText(/Supabase/).length).toBeGreaterThan(0)
  })

  it('links to contact form', () => {
    renderWithProviders(<Privacy />)
    const link = screen.getByText('contact form')
    expect(link).toBeInTheDocument()
    expect(link).toHaveAttribute('href', '/contact')
  })

  it('no longer references the retired newsletter or editorial content', () => {
    // The newsletter was retired; Privacy language must no longer reference
    // it as a downstream consumer of aggregate telemetry.
    renderWithProviders(<Privacy />)
    expect(screen.queryByText(/newsletter/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/editorial content/i)).not.toBeInTheDocument()
  })

  it('states individual queries are never published in raw form', () => {
    renderWithProviders(<Privacy />)
    expect(screen.getByText(/never published in raw form/i)).toBeInTheDocument()
  })
})
