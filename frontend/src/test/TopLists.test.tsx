import { describe, it, expect } from 'vitest'
import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { HelmetProvider } from 'react-helmet-async'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { render } from '@testing-library/react'
import TopLists from '../pages/TopLists'

// Renders TopLists inside a router so useNavigate() works. A trailing
// catch-all route with a data-testid lets us assert where navigation
// landed after tab-bar clicks.
function renderTopLists(initialEntry = '/top') {
  return render(
    <HelmetProvider>
      <MemoryRouter initialEntries={[initialEntry]}>
        <Routes>
          <Route path="/top" element={<TopLists />} />
          <Route path="/try" element={<div data-testid="try-page">TRY</div>} />
        </Routes>
      </MemoryRouter>
    </HelmetProvider>,
  )
}

describe('TopLists', () => {
  it('renders the Top Lists heading and description', () => {
    renderTopLists()
    // "Top Lists" appears twice (heading + tab); scope to the h1 heading.
    expect(screen.getByRole('heading', { level: 1, name: 'Top Lists' })).toBeInTheDocument()
    expect(screen.getByText(/Browse curated top-anime rankings/i)).toBeInTheDocument()
  })

  it('renders all four discovery tabs with Top Lists active', () => {
    renderTopLists()
    expect(screen.getByRole('tab', { name: 'Find a Show' })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: 'Shows Like This' })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: 'Encyclopedia' })).toBeInTheDocument()
    const topListsTab = screen.getByRole('tab', { name: 'Top Lists' })
    expect(topListsTab).toHaveAttribute('aria-selected', 'true')
  })

  it('renders all 16 genre chips', () => {
    renderTopLists()
    const grid = screen.getByTestId('top-lists-genre-grid')
    const anchors = within(grid).getAllByRole('link')
    expect(anchors).toHaveLength(16)
  })

  it('renders 5 decade filter radios with All Time active by default', () => {
    renderTopLists()
    const radios = screen.getAllByRole('radio')
    expect(radios).toHaveLength(5)
    const allTime = screen.getByRole('radio', { name: 'All Time' })
    expect(allTime).toHaveAttribute('aria-checked', 'true')
  })

  it('chip hrefs default to All Time (no decade suffix)', () => {
    renderTopLists()
    const actionChip = screen.getByRole('link', { name: 'Action' })
    expect(actionChip).toHaveAttribute('href', '/top/action-anime')
  })

  it('changing decade updates chip hrefs to include the decade slug', async () => {
    const user = userEvent.setup()
    renderTopLists()
    await user.click(screen.getByRole('radio', { name: '2020s' }))
    const actionChip = screen.getByRole('link', { name: 'Action' })
    expect(actionChip).toHaveAttribute('href', '/top/action-anime-2020s')
    // And the aria-checked flips
    expect(screen.getByRole('radio', { name: '2020s' })).toHaveAttribute('aria-checked', 'true')
    expect(screen.getByRole('radio', { name: 'All Time' })).toHaveAttribute('aria-checked', 'false')
  })

  it('changing decade updates the subheading', async () => {
    const user = userEvent.setup()
    renderTopLists()
    expect(screen.getByText('Best of All Time')).toBeInTheDocument()
    await user.click(screen.getByRole('radio', { name: '2010s' }))
    expect(screen.getByText('Best of the 2010s')).toBeInTheDocument()
  })

  it('clicking Find a Show tab navigates to /try?tab=find', async () => {
    const user = userEvent.setup()
    renderTopLists()
    await user.click(screen.getByRole('tab', { name: 'Find a Show' }))
    expect(screen.getByTestId('try-page')).toBeInTheDocument()
  })

  it('clicking Shows Like This tab navigates to /try?tab=dna', async () => {
    const user = userEvent.setup()
    renderTopLists()
    await user.click(screen.getByRole('tab', { name: 'Shows Like This' }))
    expect(screen.getByTestId('try-page')).toBeInTheDocument()
  })

  it('clicking Encyclopedia tab navigates to /try?tab=encyclopedia', async () => {
    const user = userEvent.setup()
    renderTopLists()
    await user.click(screen.getByRole('tab', { name: 'Encyclopedia' }))
    expect(screen.getByTestId('try-page')).toBeInTheDocument()
  })

  it('clicking Top Lists tab while already on Top Lists is a no-op', async () => {
    const user = userEvent.setup()
    renderTopLists()
    await user.click(screen.getByRole('tab', { name: 'Top Lists' }))
    expect(screen.queryByTestId('try-page')).not.toBeInTheDocument()
    // Still on Top Lists — heading present
    expect(screen.getByRole('heading', { level: 1, name: 'Top Lists' })).toBeInTheDocument()
  })
})
