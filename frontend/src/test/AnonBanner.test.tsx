/**
 * AnonBanner — verifies the server-authoritative remaining-count display
 * introduced 2026-05-16.
 *
 * Pre-fix behavior was: banner hardcoded to {30, 10} on mount, decremented
 * locally on each POST. Lied across tabs (each tab had its own counter)
 * and across reloads (fresh page = fresh 30/10 regardless of real usage).
 *
 * Post-fix behavior — verified here:
 *   1. Banner renders "—" placeholders until the first /api/anon-usage
 *      response resolves.
 *   2. Mount triggers fetchAnonUsage(), populating the display from the
 *      server's truthful remaining counts.
 *   3. POST responses with search_remaining / dna_remaining fields update
 *      the display without any client-side guessing.
 *   4. A 429 with limit_type zeroes the matching counter immediately.
 *   5. Malformed /api/anon-usage responses don't crash the banner — it
 *      simply stays on "—".
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, act } from '@testing-library/react'

// Dynamic imports per test below — needed so vi.resetModules() in beforeEach
// gives us a fresh module-level _anonUsage state for each test (the api
// module caches it across tests otherwise, leaking the prior test's value).

// Use the real lib/api module so we can exercise fetchAnonUsage end-to-end
// against a stubbed global fetch. Tests that need to bypass the request
// pipeline (FindAShow, SearchFeedback) mock api.post directly — that
// pattern is incompatible with what we're testing here.

beforeEach(async () => {
  vi.clearAllMocks()
  // Reset module-level _anonUsage state between tests. Importing fresh
  // doesn't help because Vitest caches modules. The clean way is to fetch
  // a known state before each test; the simpler way is to call the
  // exported setter through a synthetic POST. We use a direct fetch stub
  // and re-invoke fetchAnonUsage from a clean slate per test.
  vi.resetModules()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('AnonBanner', () => {
  it('renders dashes before the first server response resolves', async () => {
    // Stub fetch with a promise that never resolves so the banner stays
    // in its loading state for the duration of this assertion.
    vi.stubGlobal('fetch', vi.fn().mockReturnValue(new Promise(() => {})))
    const { AnonBanner: FreshBanner } = await import('../components/AnonBanner').then(
      m => ({ AnonBanner: m.default }),
    )

    render(<FreshBanner />)

    // Both placeholders should be present (one for searches, one for DNA)
    const dashes = screen.getAllByText('—')
    expect(dashes.length).toBe(2)
  })

  it('populates from a successful /api/anon-usage response', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({
        search_remaining: 18,
        dna_remaining: 4,
        search_cap: 30,
        dna_cap: 10,
        reset_at: '2026-05-17T00:00:00+00:00',
      }),
    }))
    const { AnonBanner: FreshBanner } = await import('../components/AnonBanner').then(
      m => ({ AnonBanner: m.default }),
    )

    render(<FreshBanner />)

    await waitFor(() => {
      expect(screen.getByText('18')).toBeInTheDocument()
      expect(screen.getByText('4')).toBeInTheDocument()
    })
    expect(screen.getByText(/searches and/)).toBeInTheDocument()
    expect(screen.getByText(/Shows Like This remaining today/)).toBeInTheDocument()
  })

  it('falls back to dashes when the server returns a malformed payload', async () => {
    // Generic stub that mimics what other tests use for unrelated endpoints.
    // Without the type guard in fetchAnonUsage, this would render
    // "undefined searches" — defending against that here.
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ anime_count: 6236 }),
    }))
    const { AnonBanner: FreshBanner } = await import('../components/AnonBanner').then(
      m => ({ AnonBanner: m.default }),
    )

    render(<FreshBanner />)

    // Give the fetch promise a microtask to resolve; banner should remain
    // on "—" because the body lacks the expected fields.
    await act(async () => { await Promise.resolve() })
    expect(screen.queryByText('undefined')).not.toBeInTheDocument()
    expect(screen.getAllByText('—').length).toBeGreaterThanOrEqual(2)
  })

  it('reads search_remaining / dna_remaining off a POST response and updates the display', async () => {
    vi.stubGlobal('fetch', vi.fn()
      // First call: GET /api/anon-usage on mount
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({
          search_remaining: 20,
          dna_remaining: 5,
          search_cap: 30,
          dna_cap: 10,
          reset_at: '2026-05-17T00:00:00+00:00',
        }),
      })
      // Second call: POST /api/search returning fresh counts
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({
          success: true,
          pool: [],
          pool_size: 0,
          search_id: 'abc',
          error: null,
          search_remaining: 19,
          dna_remaining: 5,
        }),
      }),
    )
    const apiModule = await import('../lib/api')
    const { AnonBanner: FreshBanner } = await import('../components/AnonBanner').then(
      m => ({ AnonBanner: m.default }),
    )

    render(<FreshBanner />)

    // Wait for initial mount fetch to settle
    await waitFor(() => {
      expect(screen.getByText('20')).toBeInTheDocument()
    })

    // Simulate a search POST returning the new counters
    await act(async () => {
      await apiModule.api.post('/api/search', { query: 'test' })
    })

    await waitFor(() => {
      expect(screen.getByText('19')).toBeInTheDocument()
    })
    // DNA count unchanged
    expect(screen.getByText('5')).toBeInTheDocument()
  })

  it('zeroes the relevant counter on a 429 from the anon quota', async () => {
    vi.stubGlobal('fetch', vi.fn()
      // Mount fetch: starts with some quota remaining
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({
          search_remaining: 3,
          dna_remaining: 7,
          search_cap: 30,
          dna_cap: 10,
          reset_at: '2026-05-17T00:00:00+00:00',
        }),
      })
      // POST /api/dna gets a 429
      .mockResolvedValueOnce({
        ok: false,
        status: 429,
        statusText: 'Too Many Requests',
        json: () => Promise.resolve({
          detail: {
            detail: 'Anonymous daily limit reached',
            limit_type: 'dna',
            cap: 10,
            reset_at: '2026-05-17T00:00:00+00:00',
            upgrade_hint: 'Create a free account for higher limits.',
          },
        }),
      }),
    )
    const apiModule = await import('../lib/api')
    const { AnonBanner: FreshBanner } = await import('../components/AnonBanner').then(
      m => ({ AnonBanner: m.default }),
    )

    render(<FreshBanner />)

    await waitFor(() => {
      expect(screen.getByText('3')).toBeInTheDocument()
      expect(screen.getByText('7')).toBeInTheDocument()
    })

    // Simulate a DNA POST that 429s
    await act(async () => {
      try {
        await apiModule.api.post('/api/dna', { titles: ['Foo'] })
      } catch {
        // Expected — request() throws ApiError on non-2xx
      }
    })

    await waitFor(() => {
      // DNA counter zeroed
      expect(screen.getByText('0')).toBeInTheDocument()
    })
    // Search counter untouched
    expect(screen.getByText('3')).toBeInTheDocument()
  })
})
