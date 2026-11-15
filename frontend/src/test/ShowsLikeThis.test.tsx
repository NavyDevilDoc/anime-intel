import { describe, it, expect, vi, beforeEach, afterAll } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders, makeResult, makeDNAResponse } from './helpers'
import ShowsLikeThis from '../components/ShowsLikeThis'

vi.mock('../lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/api')>()
  return {
    ...actual,
    api: {
      post: vi.fn(),
    },
  }
})

import { api } from '../lib/api'
const mockPost = vi.mocked(api.post)

// Snapshot the developer's local .env value; force OFF for the main describe
// (mirrors SeriesContext.test.tsx). vi.stubEnv is unreliable for VITE_-prefixed
// vars because Vite transforms them; direct mutation is what actually works.
const _originalHiatusEnv = import.meta.env.VITE_ENGINE_HIATUS_MODE

beforeEach(() => {
  vi.clearAllMocks()
  import.meta.env.VITE_ENGINE_HIATUS_MODE = ''
})

afterAll(() => {
  if (_originalHiatusEnv === undefined) {
    delete import.meta.env.VITE_ENGINE_HIATUS_MODE
  } else {
    import.meta.env.VITE_ENGINE_HIATUS_MODE = _originalHiatusEnv
  }
})

describe('ShowsLikeThis', () => {
  it('renders three title inputs', () => {
    renderWithProviders(<ShowsLikeThis />)
    expect(screen.getByLabelText('Target 1 (Required)')).toBeInTheDocument()
    expect(screen.getByLabelText('Target 2 (Optional)')).toBeInTheDocument()
    expect(screen.getByLabelText('Target 3 (Optional)')).toBeInTheDocument()
  })

  it('pre-fills slot 1 from seedTitle without auto-firing the DNA search', async () => {
    // Regression guard: a seedTitle (e.g., /try?mode=dna&q=Attack%20on%20Titan)
    // must only pre-fill slot 1. The 3 reference slots + 2 negative
    // anchors are the whole point of this mode; a single-anchor auto-fire
    // would waste the user's daily SLT quota on a suboptimal query.
    const onSeedConsumed = vi.fn()
    renderWithProviders(<ShowsLikeThis seedTitle="Attack on Titan" onSeedConsumed={onSeedConsumed} />)

    // Slot 1 gets the seeded value.
    const slot1 = screen.getByLabelText('Target 1 (Required)') as HTMLInputElement
    expect(slot1.value).toBe('Attack on Titan')

    // Slots 2 and 3 stay empty so the user can add refs.
    expect((screen.getByLabelText('Target 2 (Optional)') as HTMLInputElement).value).toBe('')
    expect((screen.getByLabelText('Target 3 (Optional)') as HTMLInputElement).value).toBe('')

    // Parent gets notified so the seed can be cleared from its state.
    expect(onSeedConsumed).toHaveBeenCalledTimes(1)

    // Load-bearing: no API call was made. Quota preserved.
    expect(mockPost).not.toHaveBeenCalled()
  })

  it('disables submit when first target is empty', () => {
    renderWithProviders(<ShowsLikeThis />)
    expect(screen.getByText('Synthesize DNA')).toBeDisabled()
  })

  it('enables submit when first target has text', async () => {
    const user = userEvent.setup()
    renderWithProviders(<ShowsLikeThis />)
    await user.type(screen.getByLabelText('Target 1 (Required)'), '86')
    expect(screen.getByText('Synthesize DNA')).toBeEnabled()
  })

  it('sends only non-empty titles', async () => {
    mockPost.mockResolvedValueOnce(makeDNAResponse())
    const user = userEvent.setup()
    renderWithProviders(<ShowsLikeThis />)
    await user.type(screen.getByLabelText('Target 1 (Required)'), '86')
    await user.type(screen.getByLabelText('Target 3 (Optional)'), 'Code Geass')
    await user.click(screen.getByText('Synthesize DNA'))

    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledWith('/api/dna', { titles: ['86', 'Code Geass'] }, expect.any(AbortSignal))
    })
  })

  it('displays DNA synthesis summary', async () => {
    mockPost.mockResolvedValueOnce(makeDNAResponse({
      data: [makeResult()],
      data_size: 1,
      intersection_summary: 'Both share themes of war, sacrifice, and military duty.',
    }))
    const user = userEvent.setup()
    renderWithProviders(<ShowsLikeThis />)
    await user.type(screen.getByLabelText('Target 1 (Required)'), '86')
    await user.click(screen.getByText('Synthesize DNA'))

    await waitFor(() => {
      expect(screen.getByText(/war, sacrifice, and military duty/)).toBeInTheDocument()
      expect(screen.getByText('Steins;Gate')).toBeInTheDocument()
    })
  })

  it('shows loading state', async () => {
    mockPost.mockImplementation(() => new Promise(() => {}))
    const user = userEvent.setup()
    renderWithProviders(<ShowsLikeThis />)
    await user.type(screen.getByLabelText('Target 1 (Required)'), 'test')
    await user.click(screen.getByText('Synthesize DNA'))
    expect(screen.getByText('Synthesizing DNA...')).toBeInTheDocument()
  })

  it('displays result count and timing after successful search', async () => {
    mockPost.mockResolvedValueOnce(makeDNAResponse({
      data: [makeResult(), makeResult({ profile: { ...makeResult().profile, id: 2, title: 'Psycho-Pass' } })],
      data_size: 2,
      intersection_summary: 'Both share sci-fi themes.',
    }))
    const user = userEvent.setup()
    renderWithProviders(<ShowsLikeThis />)
    await user.type(screen.getByLabelText('Target 1 (Required)'), 'Steins;Gate')
    await user.click(screen.getByText('Synthesize DNA'))

    await waitFor(() => {
      expect(screen.getByText(/2 matches found in \d+\.\d+s/)).toBeInTheDocument()
    })
  })

  it('displays error on failure', async () => {
    mockPost.mockResolvedValueOnce(makeDNAResponse({
      success: false,
      error: 'Could not find "asdf" in the database.',
    }))
    const user = userEvent.setup()
    renderWithProviders(<ShowsLikeThis />)
    await user.type(screen.getByLabelText('Target 1 (Required)'), 'asdf')
    await user.click(screen.getByText('Synthesize DNA'))

    await waitFor(() => {
      expect(screen.getByText(/Could not find "asdf"/)).toBeInTheDocument()
    })
  })

  // --- negative anchors ---

  it('renders the two negative anchor inputs by default', () => {
    renderWithProviders(<ShowsLikeThis />)
    expect(screen.getByLabelText('Avoid 1')).toBeInTheDocument()
    expect(screen.getByLabelText('Avoid 2')).toBeInTheDocument()
  })

  it('shows the "...but not like these" helper copy', () => {
    renderWithProviders(<ShowsLikeThis />)
    expect(screen.getByText(/\.\.\.but not like these/)).toBeInTheDocument()
  })

  it('sends negative_anchors in payload when avoid fields are filled', async () => {
    mockPost.mockResolvedValueOnce(makeDNAResponse())
    const user = userEvent.setup()
    renderWithProviders(<ShowsLikeThis />)
    await user.type(screen.getByLabelText('Target 1 (Required)'), '86')
    await user.type(screen.getByLabelText('Avoid 1'), 'Naruto')
    await user.type(screen.getByLabelText('Avoid 2'), 'Bleach')
    await user.click(screen.getByText('Synthesize DNA'))

    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledWith(
        '/api/dna',
        { titles: ['86'], negative_anchors: ['Naruto', 'Bleach'] },
        expect.any(AbortSignal),
      )
    })
  })

  it('omits negative_anchors from payload when avoid fields are empty', async () => {
    // Verifies the existing API contract is preserved when the new feature is unused.
    mockPost.mockResolvedValueOnce(makeDNAResponse())
    const user = userEvent.setup()
    renderWithProviders(<ShowsLikeThis />)
    await user.type(screen.getByLabelText('Target 1 (Required)'), '86')
    await user.click(screen.getByText('Synthesize DNA'))

    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledWith(
        '/api/dna',
        { titles: ['86'] },
        expect.any(AbortSignal),
      )
    })
  })

  it('only sends non-empty negative anchors', async () => {
    // Fill Avoid 2 only, leave Avoid 1 empty — payload should contain just the one entry.
    mockPost.mockResolvedValueOnce(makeDNAResponse())
    const user = userEvent.setup()
    renderWithProviders(<ShowsLikeThis />)
    await user.type(screen.getByLabelText('Target 1 (Required)'), '86')
    await user.type(screen.getByLabelText('Avoid 2'), 'Bleach')
    await user.click(screen.getByText('Synthesize DNA'))

    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledWith(
        '/api/dna',
        { titles: ['86'], negative_anchors: ['Bleach'] },
        expect.any(AbortSignal),
      )
    })
  })

  it('clears a negative anchor when its × button is clicked', async () => {
    const user = userEvent.setup()
    renderWithProviders(<ShowsLikeThis />)
    const avoidInput = screen.getByLabelText('Avoid 1') as HTMLInputElement
    await user.type(avoidInput, 'Naruto')
    expect(avoidInput.value).toBe('Naruto')

    await user.click(screen.getByLabelText('Clear Avoid 1'))
    expect(avoidInput.value).toBe('')
  })

  it('renders the Avoiding chip when the response echoes negative_anchors_used', async () => {
    mockPost.mockResolvedValueOnce(makeDNAResponse({
      data: [makeResult()],
      data_size: 1,
      intersection_summary: 'Both shows share sincere character work.',
      negative_anchors_used: ['My Girlfriend is a Gal'],
    }))
    const user = userEvent.setup()
    renderWithProviders(<ShowsLikeThis />)
    await user.type(screen.getByLabelText('Target 1 (Required)'), 'Kaguya-sama')
    await user.type(screen.getByLabelText('Avoid 1'), 'My Girlfriend is a Gal')
    await user.click(screen.getByText('Synthesize DNA'))

    await waitFor(() => {
      const chips = screen.getByTestId('avoiding-chips')
      expect(chips).toBeInTheDocument()
      expect(chips).toHaveTextContent('Avoiding:')
      expect(chips).toHaveTextContent('My Girlfriend is a Gal')
    })
  })

  it('does not render the Avoiding chip when negative_anchors_used is null', async () => {
    mockPost.mockResolvedValueOnce(makeDNAResponse({
      data: [makeResult()],
      data_size: 1,
      intersection_summary: 'Both shows share themes.',
    }))
    const user = userEvent.setup()
    renderWithProviders(<ShowsLikeThis />)
    await user.type(screen.getByLabelText('Target 1 (Required)'), 'Kaguya-sama')
    await user.click(screen.getByText('Synthesize DNA'))

    await waitFor(() => {
      expect(screen.getByText('Steins;Gate')).toBeInTheDocument()
    })
    expect(screen.queryByTestId('avoiding-chips')).not.toBeInTheDocument()
  })

  it('does not render the Avoiding chip when negative_anchors_used is an empty array', async () => {
    // Edge case: all submitted negatives were filtered out (collided with positives
    // or failed to resolve). Backend returns []; frontend should hide the chip.
    mockPost.mockResolvedValueOnce(makeDNAResponse({
      data: [makeResult()],
      data_size: 1,
      intersection_summary: 'Both shows share themes.',
      negative_anchors_used: [],
    }))
    const user = userEvent.setup()
    renderWithProviders(<ShowsLikeThis />)
    await user.type(screen.getByLabelText('Target 1 (Required)'), 'Kaguya-sama')
    await user.click(screen.getByText('Synthesize DNA'))

    await waitFor(() => {
      expect(screen.getByText('Steins;Gate')).toBeInTheDocument()
    })
    expect(screen.queryByTestId('avoiding-chips')).not.toBeInTheDocument()
  })

  // --- title resolution notes ---

  it('renders the resolution-notes banner on success when notes are present', async () => {
    mockPost.mockResolvedValueOnce(makeDNAResponse({
      data: [makeResult()],
      data_size: 1,
      intersection_summary: 'Both shows share themes.',
      negative_anchors_used: ['Girl Friend Beta'],
      title_resolution_notes: [
        "\"my girlfriend is a gal\" isn't in the vault. Used the closest match — \"Girl Friend Beta\" — instead.",
      ],
    }))
    const user = userEvent.setup()
    renderWithProviders(<ShowsLikeThis />)
    await user.type(screen.getByLabelText('Target 1 (Required)'), 'Kaguya-sama')
    await user.type(screen.getByLabelText('Avoid 1'), 'my girlfriend is a gal')
    await user.click(screen.getByText('Synthesize DNA'))

    await waitFor(() => {
      const banner = screen.getByTestId('resolution-notes-banner')
      expect(banner).toBeInTheDocument()
      expect(banner).toHaveTextContent(/isn't in the vault/)
      expect(banner).toHaveTextContent('Girl Friend Beta')
    })
  })

  it('renders the resolution-notes banner on error when notes are present', async () => {
    // Failure path: the substituted title produced no centroid matches.
    // User needs to see WHY they got "no matches" — likely because their
    // input was substituted with something that doesn't exist in FAISS.
    mockPost.mockResolvedValueOnce(makeDNAResponse({
      success: false,
      error: 'No matching targets found for this specific DNA blend.',
      title_resolution_notes: [
        "\"something obscure\" isn't in the vault. Used the closest match — \"Bogus Title\" — instead.",
      ],
    }))
    const user = userEvent.setup()
    renderWithProviders(<ShowsLikeThis />)
    await user.type(screen.getByLabelText('Target 1 (Required)'), 'something obscure')
    await user.click(screen.getByText('Synthesize DNA'))

    await waitFor(() => {
      const banner = screen.getByTestId('resolution-notes-banner-error')
      expect(banner).toBeInTheDocument()
      expect(banner).toHaveTextContent(/isn't in the vault/)
      // Error message itself should also be present
      expect(screen.getByText(/No matching targets found/)).toBeInTheDocument()
    })
  })

  it('does not render the resolution-notes banner when notes are null', async () => {
    mockPost.mockResolvedValueOnce(makeDNAResponse({
      data: [makeResult()],
      data_size: 1,
      intersection_summary: 'Both shows share themes.',
    }))
    const user = userEvent.setup()
    renderWithProviders(<ShowsLikeThis />)
    await user.type(screen.getByLabelText('Target 1 (Required)'), 'Kaguya-sama')
    await user.click(screen.getByText('Synthesize DNA'))

    await waitFor(() => {
      expect(screen.getByText('Steins;Gate')).toBeInTheDocument()
    })
    expect(screen.queryByTestId('resolution-notes-banner')).not.toBeInTheDocument()
    expect(screen.queryByTestId('resolution-notes-banner-error')).not.toBeInTheDocument()
  })

  it('renders multiple resolution notes as separate lines', async () => {
    mockPost.mockResolvedValueOnce(makeDNAResponse({
      data: [makeResult()],
      data_size: 1,
      intersection_summary: 'Both shows share themes.',
      negative_anchors_used: ['Show A'],
      title_resolution_notes: [
        "\"input A\" isn't in the vault. Used the closest match — \"Show A\" — instead.",
        "\"unknown B\" isn't in the vault and didn't match any title. Skipped from avoid list.",
      ],
    }))
    const user = userEvent.setup()
    renderWithProviders(<ShowsLikeThis />)
    await user.type(screen.getByLabelText('Target 1 (Required)'), 'input A')
    await user.type(screen.getByLabelText('Avoid 1'), 'unknown B')
    await user.click(screen.getByText('Synthesize DNA'))

    await waitFor(() => {
      const banner = screen.getByTestId('resolution-notes-banner')
      expect(banner).toHaveTextContent(/input A/)
      expect(banner).toHaveTextContent(/Skipped from avoid list/)
    })
  })

  // --- "I Don't Know" mode banner ---

  it('renders LowConfidenceBanner when DNA blend has no matching targets', async () => {
    mockPost.mockResolvedValueOnce(makeDNAResponse({
      success: false,
      low_confidence: true,
      error: 'No matching targets found for this specific DNA blend.',
    }))
    const user = userEvent.setup()
    renderWithProviders(<ShowsLikeThis />)
    await user.type(screen.getByLabelText('Target 1 (Required)'), 'obscure reference')
    await user.click(screen.getByText('Synthesize DNA'))

    await waitFor(() => {
      expect(screen.getByTestId('low-confidence-banner')).toBeInTheDocument()
    })
    // Yellow advisory replaces the red error block
    expect(
      screen.queryByText('No matching targets found for this specific DNA blend.'),
    ).not.toBeInTheDocument()
  })

  it('does not render LowConfidenceBanner on a confident success response', async () => {
    mockPost.mockResolvedValueOnce(makeDNAResponse({
      data: [makeResult({ match_confidence: 95 })],
      data_size: 1,
      intersection_summary: 'Shared themes of resilience.',
      low_confidence: false,
    }))
    const user = userEvent.setup()
    renderWithProviders(<ShowsLikeThis />)
    await user.type(screen.getByLabelText('Target 1 (Required)'), 'Cowboy Bebop')
    await user.click(screen.getByText('Synthesize DNA'))

    await waitFor(() => {
      expect(screen.getByText('Steins;Gate')).toBeInTheDocument()
    })
    expect(screen.queryByTestId('low-confidence-banner')).not.toBeInTheDocument()
  })

  it('still shows red error block on title-resolution (Target MIA) failure', async () => {
    // Target MIA is NOT a low_confidence case — backend explicitly leaves
    // the flag false because it's a different kind of failure (we don't
    // know what show you mean, not "we don't know strong matches").
    mockPost.mockResolvedValueOnce(makeDNAResponse({
      success: false,
      low_confidence: false,
      error: "Target MIA: Could not identify 'NEVER_EXISTS' in the vault.",
    }))
    const user = userEvent.setup()
    renderWithProviders(<ShowsLikeThis />)
    await user.type(screen.getByLabelText('Target 1 (Required)'), 'NEVER_EXISTS')
    await user.click(screen.getByText('Synthesize DNA'))

    await waitFor(() => {
      expect(screen.getByText(/Target MIA/)).toBeInTheDocument()
    })
    expect(screen.queryByTestId('low-confidence-banner')).not.toBeInTheDocument()
  })

  it('renders LowConfidenceBanner above results when in-band low_confidence is true', async () => {
    // The in-band case has been historically inert for DNA but the
    // symmetric implementation handles it if reranker recalibration
    // ever produces lower top-result confidences.
    mockPost.mockResolvedValueOnce(makeDNAResponse({
      data: [makeResult({ match_confidence: 70 })],
      data_size: 1,
      intersection_summary: 'Loose thematic overlap',
      low_confidence: true,
    }))
    const user = userEvent.setup()
    renderWithProviders(<ShowsLikeThis />)
    await user.type(screen.getByLabelText('Target 1 (Required)'), 'fuzzy reference')
    await user.click(screen.getByText('Synthesize DNA'))

    await waitFor(() => {
      expect(screen.getByTestId('low-confidence-banner')).toBeInTheDocument()
    })
    // Results still render
    expect(screen.getByText('Steins;Gate')).toBeInTheDocument()
  })

  it('does not render the hiatus card when VITE_ENGINE_HIATUS_MODE is unset (default)', () => {
    renderWithProviders(<ShowsLikeThis />)
    expect(screen.queryByTestId('hiatus-card')).not.toBeInTheDocument()
    // Normal DNA UI is present
    expect(screen.getByText('Synthesize DNA')).toBeInTheDocument()
  })
})

describe('ShowsLikeThis — hiatus mode', () => {
  beforeEach(() => {
    import.meta.env.VITE_ENGINE_HIATUS_MODE = 'true'
  })

  it('renders the hiatus card and hides the DNA form', () => {
    renderWithProviders(<ShowsLikeThis />)
    const card = screen.getByTestId('hiatus-card')
    expect(card).toBeInTheDocument()
    expect(card).toHaveTextContent(/recommendation engine is on hiatus/i)
    // DNA form is gone
    expect(screen.queryByText('Synthesize DNA')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Target 1 (Required)')).not.toBeInTheDocument()
  })
})
