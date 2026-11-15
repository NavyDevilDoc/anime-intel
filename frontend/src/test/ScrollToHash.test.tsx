import type { ReactNode } from 'react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import ScrollToHash from '../components/ScrollToHash'

/**
 * jsdom doesn't implement Element.prototype.scrollIntoView. Stubbing it
 * on the prototype lets ScrollToHash's effect call through without
 * throwing, and we can assert what element was targeted.
 */
const scrollIntoViewMock = vi.fn()

beforeEach(() => {
  scrollIntoViewMock.mockReset()
  Element.prototype.scrollIntoView = scrollIntoViewMock
})

function renderWithHash(initialEntry: string, targetElement?: ReactNode) {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <ScrollToHash />
      {targetElement}
    </MemoryRouter>,
  )
}

describe('ScrollToHash', () => {
  it('scrolls to the target element when the URL has a hash', () => {
    renderWithHash('/how-it-works#ranking',
      <div id="ranking">Ranking section</div>,
    )
    expect(scrollIntoViewMock).toHaveBeenCalledTimes(1)
    expect(scrollIntoViewMock).toHaveBeenCalledWith({
      behavior: 'smooth',
      block: 'start',
    })
  })

  it('does nothing when the URL has no hash', () => {
    renderWithHash('/how-it-works',
      <div id="ranking">Ranking section</div>,
    )
    expect(scrollIntoViewMock).not.toHaveBeenCalled()
  })

  it('does nothing when the hash targets a non-existent element', () => {
    renderWithHash('/how-it-works#nonexistent',
      <div id="ranking">Ranking section</div>,
    )
    // querySelector('#nonexistent') returns null; the effect no-ops
    expect(scrollIntoViewMock).not.toHaveBeenCalled()
  })

  it('renders nothing (returns null)', () => {
    const { container } = renderWithHash('/how-it-works')
    expect(container).toBeEmptyDOMElement()
  })
})
