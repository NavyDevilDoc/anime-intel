import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor, fireEvent } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import Admin from '../pages/Admin'

// Mock the api module
vi.mock('../lib/api', async () => {
  const actual = await vi.importActual<typeof import('../lib/api')>('../lib/api')
  return {
    ...actual,
    api: {
      get: vi.fn(),
      post: vi.fn(),
      patch: vi.fn(),
      delete: vi.fn(),
    },
  }
})

import { api, ApiError } from '../lib/api'
const mockGet = vi.mocked(api.get)
const mockPost = vi.mocked(api.post)

const MOCK_DASHBOARD = {
  feedback_summary: { thumbs_up: 30, thumbs_down: 5 },
  contact_messages: 3,
  anon_traffic: { anon_searches_today: 27, anon_dna_today: 8, anon_unique_ips_today: 12, anon_cap_hits_today: 3, anon_unique_ips_7d: 38, anon_unique_ips_30d: 96 },
  lockdown_active: false,
  api_costs: {
    total_searches_today: 183,
    total_dna_today: 12,
    gemini_cost_today_usd: 0.0999,
    gemini_mtd_usd: 6.50,
    gemini_ceiling_usd: 35,
    production_projection_monthly_usd: 3.0,
    railway_monthly_usd: 15,
    total_projected_monthly_usd: 18.0,
    budget_ceiling_usd: 50,
    last_7d_total_usd: 1.2345,
    last_7d_by_origin: {
      production: 0.5,
      regression: 0.7,
      probe: 0.0245,
    },
  },
  pipeline_timings: {
    window_hours: 24,
    profiles: [],
    has_data: false,
    error: null,
  },
}

const MOCK_TIMING_PROFILE = {
  rerank_type: 'fusion',
  sample_count: 42,
  retrieval: { p50_ms: 38.4, p95_ms: 72.1, p99_ms: 95.2 },
  hydration: { p50_ms: 12.0, p95_ms: 18.5, p99_ms: 24.0 },
  rerank: { p50_ms: 1850.0, p95_ms: 2840.0, p99_ms: 3600.0 },
  total: { p50_ms: 1900.0, p95_ms: 2950.0, p99_ms: 3700.0 },
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('Admin Dashboard', () => {
  it('shows loading state initially', () => {
    mockGet.mockReturnValue(new Promise(() => {})) // never resolves
    renderWithProviders(<Admin />)
    expect(screen.getByText('Loading...')).toBeInTheDocument()
  })

  it('renders dashboard with metrics on success', async () => {
    mockGet.mockResolvedValue(MOCK_DASHBOARD)
    renderWithProviders(<Admin />)

    await waitFor(() => {
      expect(screen.getByText('Admin Dashboard')).toBeInTheDocument()
    })

    expect(screen.getByText('30')).toBeInTheDocument()    // thumbs up
    expect(screen.getByText('5')).toBeInTheDocument()     // thumbs down
    // "3" appears twice (contact_messages + anon_cap_hits_today)
    expect(screen.getAllByText('3')).toHaveLength(2)
  })

  it('shows "Authentication required" hint on 401 (browser dismissed Basic Auth)', async () => {
    mockGet.mockRejectedValue(new ApiError(401, 'Invalid credentials.'))
    renderWithProviders(<Admin />)

    await waitFor(() => {
      expect(screen.getByText(/Authentication required\. Refresh to re-prompt\./)).toBeInTheDocument()
    })
  })

  it('shows error message on API failure', async () => {
    mockGet.mockRejectedValue(new Error('Network error'))
    renderWithProviders(<Admin />)

    await waitFor(() => {
      expect(screen.getByText('Failed to load dashboard data.')).toBeInTheDocument()
    })
  })

  it('displays section headings', async () => {
    mockGet.mockResolvedValue(MOCK_DASHBOARD)
    renderWithProviders(<Admin />)

    await waitFor(() => {
      expect(screen.getByText('Activity')).toBeInTheDocument()
    })
    expect(screen.getByText('Anonymous Traffic')).toBeInTheDocument()
    expect(screen.queryByText('Users')).not.toBeInTheDocument()
  })

  it('has noindex meta tag', async () => {
    mockGet.mockResolvedValue(MOCK_DASHBOARD)
    renderWithProviders(<Admin />)

    await waitFor(() => {
      expect(screen.getByText('Admin Dashboard')).toBeInTheDocument()
    })
    // Helmet sets meta tags — verify the title was set
    expect(document.title).toContain('Admin Dashboard')
  })
})

describe('Global Lockdown Toggle', () => {
  it('shows lockdown section with inactive state', async () => {
    mockGet.mockResolvedValue(MOCK_DASHBOARD)
    renderWithProviders(<Admin />)

    await waitFor(() => {
      expect(screen.getByText('Global Lockdown')).toBeInTheDocument()
    })
    expect(screen.getByText('Inactive — Site operating normally.')).toBeInTheDocument()
    expect(screen.getByText('Enable Lockdown')).toBeInTheDocument()
  })

  it('shows lockdown section with active state', async () => {
    mockGet.mockResolvedValue({ ...MOCK_DASHBOARD, lockdown_active: true })
    renderWithProviders(<Admin />)

    await waitFor(() => {
      expect(screen.getByText('Global Lockdown')).toBeInTheDocument()
    })
    expect(screen.getByText('ACTIVE — All non-admin requests are returning 503.')).toBeInTheDocument()
    expect(screen.getByText('Disable Lockdown')).toBeInTheDocument()
  })

  it('shows confirmation modal before enabling lockdown', async () => {
    mockGet.mockResolvedValue(MOCK_DASHBOARD)
    renderWithProviders(<Admin />)

    await waitFor(() => {
      expect(screen.getByText('Enable Lockdown')).toBeInTheDocument()
    })

    fireEvent.click(screen.getByText('Enable Lockdown'))

    expect(screen.getByText('Confirm Global Lockdown')).toBeInTheDocument()
    expect(screen.getByText(/This will block ALL users/)).toBeInTheDocument()
  })

  it('cancels lockdown confirmation', async () => {
    mockGet.mockResolvedValue(MOCK_DASHBOARD)
    renderWithProviders(<Admin />)

    await waitFor(() => {
      expect(screen.getByText('Enable Lockdown')).toBeInTheDocument()
    })

    fireEvent.click(screen.getByText('Enable Lockdown'))
    expect(screen.getByText('Confirm Global Lockdown')).toBeInTheDocument()

    fireEvent.click(screen.getByText('Cancel'))
    expect(screen.queryByText('Confirm Global Lockdown')).not.toBeInTheDocument()
  })

  it('confirms lockdown and calls API', async () => {
    mockGet.mockResolvedValue(MOCK_DASHBOARD)
    mockPost.mockResolvedValue({ lockdown_active: true })
    renderWithProviders(<Admin />)

    await waitFor(() => {
      expect(screen.getByText('Enable Lockdown')).toBeInTheDocument()
    })

    // Click Enable -> Confirmation modal -> Confirm
    fireEvent.click(screen.getByText('Enable Lockdown'))
    // Inside the modal, there's also an "Enable Lockdown" button
    const confirmButtons = screen.getAllByText('Enable Lockdown')
    fireEvent.click(confirmButtons[confirmButtons.length - 1])

    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledWith('/api/admin/lockdown', { enabled: true })
    })
  })

  it('disables lockdown without confirmation', async () => {
    mockGet.mockResolvedValue({ ...MOCK_DASHBOARD, lockdown_active: true })
    mockPost.mockResolvedValue({ lockdown_active: false })
    renderWithProviders(<Admin />)

    await waitFor(() => {
      expect(screen.getByText('Disable Lockdown')).toBeInTheDocument()
    })

    fireEvent.click(screen.getByText('Disable Lockdown'))

    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledWith('/api/admin/lockdown', { enabled: false })
    })
    // No confirmation modal shown for disabling
    expect(screen.queryByText('Confirm Global Lockdown')).not.toBeInTheDocument()
  })
})

describe('API Costs Section', () => {
  it('displays API Costs section heading', async () => {
    mockGet.mockResolvedValue(MOCK_DASHBOARD)
    renderWithProviders(<Admin />)

    await waitFor(() => {
      expect(screen.getByText('API Costs')).toBeInTheDocument()
    })
  })

  it('displays combined search and DNA counts', async () => {
    mockGet.mockResolvedValue(MOCK_DASHBOARD)
    renderWithProviders(<Admin />)

    await waitFor(() => {
      expect(screen.getByText('API Costs')).toBeInTheDocument()
    })
    expect(screen.getByText('183')).toBeInTheDocument()  // total searches
    // "12" appears twice: anon_unique_ips_today and total_dna_today
    expect(screen.getAllByText('12')).toHaveLength(2)
  })

  it('displays Gemini cost today', async () => {
    mockGet.mockResolvedValue(MOCK_DASHBOARD)
    renderWithProviders(<Admin />)

    await waitFor(() => {
      expect(screen.getByText('Gemini Cost Today')).toBeInTheDocument()
    })
    expect(screen.getByText('$0.0999')).toBeInTheDocument()
  })

  it('displays Gemini month-to-date with ceiling', async () => {
    mockGet.mockResolvedValue(MOCK_DASHBOARD)
    renderWithProviders(<Admin />)

    await waitFor(() => {
      expect(screen.getByText('Gemini Month-to-Date')).toBeInTheDocument()
    })
    expect(screen.getByText('$6.50')).toBeInTheDocument()
    expect(screen.getByText('of $35 Gemini ceiling')).toBeInTheDocument()
  })

  it('shows green color when MTD is low', async () => {
    const lowCost = {
      ...MOCK_DASHBOARD,
      api_costs: { ...MOCK_DASHBOARD.api_costs, gemini_mtd_usd: 8 },
    }
    mockGet.mockResolvedValue(lowCost)  // $8 of $35 = 23% = green
    renderWithProviders(<Admin />)

    await waitFor(() => {
      expect(screen.getByText('$8.00')).toBeInTheDocument()
    })
    expect(screen.getByText('$8.00').className).toContain('text-green-400')
  })

  it('shows yellow color when MTD is moderate', async () => {
    const moderateCost = {
      ...MOCK_DASHBOARD,
      api_costs: { ...MOCK_DASHBOARD.api_costs, gemini_mtd_usd: 25 },
    }
    mockGet.mockResolvedValue(moderateCost)  // $25 of $35 = 71% = yellow
    renderWithProviders(<Admin />)

    await waitFor(() => {
      expect(screen.getByText('$25.00')).toBeInTheDocument()
    })
    expect(screen.getByText('$25.00').className).toContain('text-yellow-400')
  })

  it('shows red color when MTD nears the ceiling', async () => {
    const dangerCost = {
      ...MOCK_DASHBOARD,
      api_costs: { ...MOCK_DASHBOARD.api_costs, gemini_mtd_usd: 33 },
    }
    mockGet.mockResolvedValue(dangerCost)  // $33 of $35 = 94% = red
    renderWithProviders(<Admin />)

    await waitFor(() => {
      expect(screen.getByText('$33.00')).toBeInTheDocument()
    })
    expect(screen.getByText('$33.00').className).toContain('text-red-400')
  })
})

describe('Pipeline Timings Section', () => {
  it('displays the Pipeline Timings section heading', async () => {
    mockGet.mockResolvedValue(MOCK_DASHBOARD)
    renderWithProviders(<Admin />)

    await waitFor(() => {
      expect(screen.getByText(/Pipeline Timings/)).toBeInTheDocument()
    })
    expect(screen.getByText(/Last 24h/)).toBeInTheDocument()
  })

  it('shows the no-data message when has_data is false and no error', async () => {
    mockGet.mockResolvedValue(MOCK_DASHBOARD)
    renderWithProviders(<Admin />)

    await waitFor(() => {
      expect(screen.getByText(/No timing samples yet/)).toBeInTheDocument()
    })
  })

  it('shows the error message when error is set', async () => {
    const withError = {
      ...MOCK_DASHBOARD,
      pipeline_timings: {
        window_hours: 24,
        profiles: [],
        has_data: false,
        error: 'Telemetry query failed (HTTPStatusError)',
      },
    }
    mockGet.mockResolvedValue(withError)
    renderWithProviders(<Admin />)

    await waitFor(() => {
      expect(screen.getByText(/Telemetry query failed/)).toBeInTheDocument()
    })
    expect(screen.queryByText(/No timing samples yet/)).not.toBeInTheDocument()
  })

  it('renders a timing table when has_data is true', async () => {
    const withData = {
      ...MOCK_DASHBOARD,
      pipeline_timings: {
        window_hours: 24,
        profiles: [MOCK_TIMING_PROFILE],
        has_data: true,
        error: null,
      },
    }
    mockGet.mockResolvedValue(withData)
    renderWithProviders(<Admin />)

    await waitFor(() => {
      expect(screen.getByText(/Find a Show — Fusion/)).toBeInTheDocument()
    })

    expect(screen.getByText('Stage')).toBeInTheDocument()
    expect(screen.getByText('Retrieval')).toBeInTheDocument()
    expect(screen.getByText('Hydration')).toBeInTheDocument()
    expect(screen.getByText('Rerank (Gemini)')).toBeInTheDocument()
    expect(screen.getByText('Total')).toBeInTheDocument()

    expect(screen.getByText('42 samples')).toBeInTheDocument()

    expect(screen.getByText('38.4 ms')).toBeInTheDocument()      // retrieval P50
    expect(screen.getByText('2840.0 ms')).toBeInTheDocument()    // rerank P95
    expect(screen.getByText('3700.0 ms')).toBeInTheDocument()    // total P99
  })

  it('renders multiple rerank-type cards when data covers multiple paths', async () => {
    const dnaProfile = {
      ...MOCK_TIMING_PROFILE,
      rerank_type: 'dna',
      sample_count: 1,
    }
    const withTwoProfiles = {
      ...MOCK_DASHBOARD,
      pipeline_timings: {
        window_hours: 24,
        profiles: [MOCK_TIMING_PROFILE, dnaProfile],
        has_data: true,
        error: null,
      },
    }
    mockGet.mockResolvedValue(withTwoProfiles)
    renderWithProviders(<Admin />)

    await waitFor(() => {
      expect(screen.getByText(/Find a Show — Fusion/)).toBeInTheDocument()
    })
    expect(screen.getByText(/Shows Like This — DNA/)).toBeInTheDocument()
    expect(screen.getByText('1 sample')).toBeInTheDocument()
  })
})
