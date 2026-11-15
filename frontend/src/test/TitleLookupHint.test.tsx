import { describe, it, expect } from 'vitest'
import { screen } from '@testing-library/react'
import { renderWithRouter } from './helpers'
import TitleLookupHint from '../components/TitleLookupHint'

describe('TitleLookupHint', () => {
  it('renders the empty-state copy when isEmpty is true', () => {
    renderWithRouter(<TitleLookupHint title="Spirited Away" isEmpty={true} />)
    const hint = screen.getByTestId('title-lookup-hint')
    expect(hint).toBeInTheDocument()
    expect(hint).toHaveTextContent(/No matches for/)
    expect(hint).toHaveTextContent(/as a vibe search/)
    expect(hint).toHaveTextContent(/Spirited Away/)
  })

  it('renders the non-empty copy when isEmpty is false', () => {
    renderWithRouter(<TitleLookupHint title="Spirited Away" isEmpty={false} />)
    const hint = screen.getByTestId('title-lookup-hint')
    expect(hint).toHaveTextContent(/looks like a specific show/)
    expect(hint).toHaveTextContent(/for the full profile/)
    expect(hint).toHaveTextContent(/Spirited Away/)
    expect(hint).not.toHaveTextContent(/No matches/)
  })

  it('renders an Encyclopedia link with the URL-encoded title', () => {
    renderWithRouter(<TitleLookupHint title="Made in Abyss" isEmpty={true} />)
    const link = screen.getByRole('link', { name: /Encyclopedia/i })
    expect(link).toHaveAttribute('href', '/try?mode=encyclopedia&q=Made%20in%20Abyss')
  })

  it('URL-encodes special characters in the title', () => {
    renderWithRouter(<TitleLookupHint title="Steins;Gate" isEmpty={false} />)
    const link = screen.getByRole('link', { name: /Encyclopedia/i })
    expect(link).toHaveAttribute('href', '/try?mode=encyclopedia&q=Steins%3BGate')
  })
})
