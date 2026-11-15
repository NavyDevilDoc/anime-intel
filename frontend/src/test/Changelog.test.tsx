import { describe, it, expect } from 'vitest'
import { screen } from '@testing-library/react'
import { renderWithRouter } from './helpers'
import Changelog from '../pages/Changelog'

describe('Changelog', () => {
  it('renders the page heading', () => {
    renderWithRouter(<Changelog />)
    expect(screen.getByText('Changelog')).toBeInTheDocument()
  })

  it('renders the most-recent month section', () => {
    renderWithRouter(<Changelog />)
    expect(screen.getByText('September 2026')).toBeInTheDocument()
  })

  it('mentions the open-access pivot', () => {
    renderWithRouter(<Changelog />)
    expect(screen.getByText(/Open-access pivot/i)).toBeInTheDocument()
  })

  it('summarizes earlier milestones', () => {
    renderWithRouter(<Changelog />)
    expect(screen.getByText(/FAISS semantic search/i)).toBeInTheDocument()
  })
})
