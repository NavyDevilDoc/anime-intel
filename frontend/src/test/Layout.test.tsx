import { describe, it, expect } from 'vitest'
import { screen, render } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import Layout from '../components/Layout'

function renderLayout() {
  // Layout is an Outlet host — render it inside a Routes/Route tree.
  return render(
    <MemoryRouter>
      <Routes>
        <Route element={<Layout />}>
          <Route index element={<div data-testid="child">CHILD</div>} />
        </Route>
      </Routes>
    </MemoryRouter>,
  )
}

describe('Layout', () => {
  it('renders the app title', () => {
    renderLayout()
    expect(screen.getByText('Anime Intel')).toBeInTheDocument()
  })

  it('renders nested route content via Outlet', () => {
    renderLayout()
    expect(screen.getByTestId('child')).toBeInTheDocument()
  })

  it('renders the MAL attribution in the footer', () => {
    renderLayout()
    expect(screen.getByText('MyAnimeList')).toBeInTheDocument()
  })

  it('renders all footer navigation links', () => {
    renderLayout()
    expect(screen.getByText('How It Works')).toBeInTheDocument()
    expect(screen.getByText('Privacy')).toBeInTheDocument()
    expect(screen.getByText('Contact')).toBeInTheDocument()
    expect(screen.getByText('Changelog')).toBeInTheDocument()
  })

  it('does not render the deprecated Newsletter footer link', () => {
    // The newsletter was retired; the footer link should not reappear.
    renderLayout()
    expect(screen.queryByText('Newsletter')).not.toBeInTheDocument()
  })

  it('does not render any auth UI — no Sign In/Out, no Admin link, no Watchlist', () => {
    renderLayout()
    expect(screen.queryByText('Sign In')).not.toBeInTheDocument()
    expect(screen.queryByText('Sign Out')).not.toBeInTheDocument()
    expect(screen.queryByText('Admin')).not.toBeInTheDocument()
    expect(screen.queryByText('Watchlist')).not.toBeInTheDocument()
    expect(screen.queryByText('Saved Searches')).not.toBeInTheDocument()
    expect(screen.queryByText('Discover')).not.toBeInTheDocument()
  })

  it('does not render the hamburger menu', () => {
    renderLayout()
    expect(screen.queryByLabelText('Toggle navigation menu')).not.toBeInTheDocument()
  })
})
