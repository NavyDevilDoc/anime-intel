import { describe, it, expect, vi, afterEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from './helpers'
import SimilarShows from '../components/SimilarShows'

const samplePayload = {
  reference_id: 1,
  reference_title: 'Cowboy Bebop',
  generated_at: '2026-09-14T12:00:00Z',
  recommendations: [
    {
      anime_id: 20057,
      title: 'Space Dandy',
      slug: '20057-space-dandy',
      release_year: 2014,
      mal_score: 7.89,
      personnel_hook: 'Also directed by Shinichirou Watanabe',
      thematic_hint: 'Goofy, Sci-Fi, Parody',
    },
    {
      anime_id: 205,
      title: 'Samurai Champloo',
      slug: '205-samurai-champloo',
      release_year: 2004,
      mal_score: 8.52,
      personnel_hook: 'Also directed by Shinichirou Watanabe',
      thematic_hint: 'Action-comedy, Anachronistic, Hip-hop infused',
    },
    {
      anime_id: 202,
      title: "Wolf's Rain",
      slug: '202-wolfs-rain',
      release_year: 2003,
      mal_score: 7.79,
      personnel_hook: 'Music by Yoko Kanno',
      thematic_hint: 'Melancholic, Apocalyptic, Symbolic',
    },
  ],
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('SimilarShows', () => {
  it('shows a loading state while fetching', () => {
    vi.stubGlobal('fetch', vi.fn().mockReturnValue(new Promise(() => {})))
    renderWithProviders(<SimilarShows animeId={1} onSelectShow={() => {}} />)
    expect(screen.getByText(/Loading recommendations/)).toBeInTheDocument()
  })

  it('renders one card per recommendation on success', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: () => Promise.resolve(samplePayload),
      }),
    )
    renderWithProviders(<SimilarShows animeId={1} onSelectShow={() => {}} />)
    await waitFor(() => {
      expect(screen.getByText('Space Dandy')).toBeInTheDocument()
      expect(screen.getByText('Samurai Champloo')).toBeInTheDocument()
      expect(screen.getByText("Wolf's Rain")).toBeInTheDocument()
    })
    // Personnel hook strings visible as primary lines
    expect(screen.getAllByText(/Also directed by Shinichirou Watanabe/)).toHaveLength(2)
    expect(screen.getByText(/Music by Yoko Kanno/)).toBeInTheDocument()
    // Year in parens (with real Unicode paren wrapping)
    expect(screen.getByText('(2014)')).toBeInTheDocument()
    // MAL scores displayed
    expect(screen.getByText('7.89')).toBeInTheDocument()
    expect(screen.getByText('8.52')).toBeInTheDocument()
  })

  it('renders both personnel_hook AND Theme lines when both are present', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve(samplePayload),
      }),
    )
    renderWithProviders(<SimilarShows animeId={1} onSelectShow={() => {}} />)
    await waitFor(() => {
      expect(screen.getByText('Space Dandy')).toBeInTheDocument()
    })
    // Each card with personnel_hook should also show the Theme: line
    expect(screen.getByText(/Theme: Goofy, Sci-Fi, Parody/)).toBeInTheDocument()
    expect(screen.getByText(/Theme: Action-comedy, Anachronistic/)).toBeInTheDocument()
    expect(screen.getByText(/Theme: Melancholic, Apocalyptic, Symbolic/)).toBeInTheDocument()
    // Crucial: the word "Similar" should NOT appear (copy decision — we
    // don't overclaim thematic similarity when the connection is personnel)
    expect(screen.queryByText(/Similar theme/)).not.toBeInTheDocument()
  })

  it('renders Theme as the primary line when personnel_hook is null', async () => {
    const themeOnlyPayload = {
      ...samplePayload,
      recommendations: [
        {
          anime_id: 457,
          title: 'Mushi-Shi',
          slug: '457-mushi-shi',
          release_year: 2005,
          mal_score: 8.65,
          personnel_hook: null,
          thematic_hint: 'Meditative, Supernatural, Environmental',
        },
      ],
    }
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve(themeOnlyPayload),
      }),
    )
    renderWithProviders(<SimilarShows animeId={2} onSelectShow={() => {}} />)
    await waitFor(() => {
      expect(screen.getByText('Mushi-Shi')).toBeInTheDocument()
    })
    // Theme: appears exactly once (as the primary line, no duplication)
    expect(screen.getAllByText(/Theme: Meditative, Supernatural/)).toHaveLength(1)
  })

  it('renders only score when both personnel_hook and thematic_hint are null', async () => {
    const bareRecPayload = {
      ...samplePayload,
      recommendations: [
        {
          anime_id: 999,
          title: 'Something Obscure',
          slug: '999-something-obscure',
          release_year: 2020,
          mal_score: 7.5,
          personnel_hook: null,
          thematic_hint: null,
        },
      ],
    }
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve(bareRecPayload),
      }),
    )
    renderWithProviders(<SimilarShows animeId={3} onSelectShow={() => {}} />)
    await waitFor(() => {
      expect(screen.getByText('Something Obscure')).toBeInTheDocument()
    })
    // No descriptor lines at all — just title + year + score
    expect(screen.getByText('7.5')).toBeInTheDocument()
    expect(screen.queryByText(/Theme:/)).not.toBeInTheDocument()
    expect(screen.queryByText(/directed by/i)).not.toBeInTheDocument()
  })

  it('fetches from the correct URL for the animeId', async () => {
    const fetchSpy = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve(samplePayload),
    })
    vi.stubGlobal('fetch', fetchSpy)
    renderWithProviders(<SimilarShows animeId={9253} onSelectShow={() => {}} />)
    await waitFor(() => {
      expect(fetchSpy).toHaveBeenCalledWith('/data/similar/9253.json')
    })
  })

  it('calls onSelectShow with the show title when a card is clicked', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve(samplePayload),
      }),
    )
    const onSelect = vi.fn()
    const user = userEvent.setup()
    renderWithProviders(<SimilarShows animeId={1} onSelectShow={onSelect} />)
    await waitFor(() => {
      expect(screen.getByText('Space Dandy')).toBeInTheDocument()
    })
    await user.click(screen.getByText('Space Dandy'))
    expect(onSelect).toHaveBeenCalledWith('Space Dandy')
  })

  it('renders nothing when the fetch fails (silent hide)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({ ok: false, status: 404 }),
    )
    const { container } = renderWithProviders(
      <SimilarShows animeId={1} onSelectShow={() => {}} />,
    )
    await waitFor(() => {
      expect(container.firstChild).toBeNull()
    })
    // No error message shown to the user
    expect(screen.queryByText(/error/i)).not.toBeInTheDocument()
  })

  it('renders nothing when the network rejects entirely', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockRejectedValue(new Error('offline')),
    )
    const { container } = renderWithProviders(
      <SimilarShows animeId={1} onSelectShow={() => {}} />,
    )
    await waitFor(() => {
      expect(container.firstChild).toBeNull()
    })
  })

  it('renders nothing when the recommendations array is empty', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve({ ...samplePayload, recommendations: [] }),
      }),
    )
    const { container } = renderWithProviders(
      <SimilarShows animeId={1} onSelectShow={() => {}} />,
    )
    await waitFor(() => {
      expect(container.firstChild).toBeNull()
    })
  })

  it('renders the "You might also like" header on success', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve(samplePayload),
      }),
    )
    renderWithProviders(<SimilarShows animeId={1} onSelectShow={() => {}} />)
    await waitFor(() => {
      expect(screen.getByText('You might also like')).toBeInTheDocument()
    })
  })

  it('re-fetches when animeId prop changes', async () => {
    const fetchSpy = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve(samplePayload),
    })
    vi.stubGlobal('fetch', fetchSpy)
    const { rerender } = renderWithProviders(
      <SimilarShows animeId={1} onSelectShow={() => {}} />,
    )
    await waitFor(() => {
      expect(fetchSpy).toHaveBeenCalledWith('/data/similar/1.json')
    })
    rerender(<SimilarShows animeId={30} onSelectShow={() => {}} />)
    await waitFor(() => {
      expect(fetchSpy).toHaveBeenCalledWith('/data/similar/30.json')
    })
  })
})
