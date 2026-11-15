import { useEffect, useState } from 'react'
import { Helmet } from 'react-helmet-async'
import { api, ApiError } from '../lib/api'

interface FeedbackSummary {
  thumbs_up: number
  thumbs_down: number
}

interface AnonTraffic {
  anon_searches_today: number
  anon_dna_today: number
  anon_unique_ips_today: number
  anon_cap_hits_today: number
  anon_unique_ips_7d: number
  anon_unique_ips_30d: number
}

interface ApiCosts {
  total_searches_today: number
  total_dna_today: number
  gemini_cost_today_usd: number
  gemini_mtd_usd: number
  gemini_ceiling_usd: number
  production_projection_monthly_usd: number
  railway_monthly_usd: number
  total_projected_monthly_usd: number
  budget_ceiling_usd: number
  last_7d_total_usd: number
  last_7d_by_origin: Record<string, number>
}

const ORIGIN_DISPLAY_ORDER = [
  'production',
  'ingestion',
  'dev',
  'test',
  'probe',
  'regression',
] as const

interface StagePercentiles {
  p50_ms: number | null
  p95_ms: number | null
  p99_ms: number | null
}

interface TimingProfile {
  rerank_type: string
  sample_count: number
  retrieval: StagePercentiles
  hydration: StagePercentiles
  rerank: StagePercentiles
  total: StagePercentiles
}

interface PipelineTimings {
  window_hours: number
  profiles: TimingProfile[]
  has_data: boolean
  error: string | null
}

interface DashboardData {
  feedback_summary: FeedbackSummary
  contact_messages: number
  anon_traffic: AnonTraffic
  lockdown_active: boolean
  api_costs: ApiCosts
  pipeline_timings: PipelineTimings
}

const RERANK_TYPE_LABELS: Record<string, string> = {
  fusion: 'Find a Show — Fusion (vibe)',
  narrative: 'Find a Show — Narrative',
  constrained: 'Find a Show — Constraint',
  objective: 'Find a Show — Objective Rankings',
  dna: 'Shows Like This — DNA',
  unknown: 'Unknown',
}

function fmtMs(v: number | null): string {
  if (v === null || v === undefined) return '—'
  return `${v.toFixed(1)} ms`
}

function MetricCard({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="bg-vault-surface border border-vault-border rounded-lg p-4">
      <div className="text-sm text-vault-text-muted">{label}</div>
      <div className="text-2xl font-bold mt-1">{value}</div>
    </div>
  )
}

function costColor(projected: number, ceiling: number): string {
  const ratio = projected / ceiling
  if (ratio >= 0.9) return 'text-red-400'
  if (ratio >= 0.6) return 'text-yellow-400'
  return 'text-green-400'
}

export default function Admin() {
  const [data, setData] = useState<DashboardData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [lockdownActive, setLockdownActive] = useState(false)
  const [lockdownToggling, setLockdownToggling] = useState(false)
  const [showLockdownConfirm, setShowLockdownConfirm] = useState(false)

  useEffect(() => {
    // Browser handles auth: a 401 response triggers the native Basic Auth
    // dialog, the user enters credentials once, the browser caches them and
    // retries automatically. This handler only sees the post-auth result.
    api.get<DashboardData>('/api/admin/dashboard')
      .then((d) => {
        setData(d)
        setLockdownActive(d.lockdown_active)
      })
      .catch((err) => {
        if (err instanceof ApiError && err.status === 401) {
          setError('unauthorized')
        } else {
          setError('Failed to load dashboard data.')
        }
      })
      .finally(() => setLoading(false))
  }, [])

  const handleLockdownToggle = async () => {
    const newState = !lockdownActive
    if (newState) {
      setShowLockdownConfirm(true)
      return
    }
    await executeLockdownToggle(false)
  }

  const executeLockdownToggle = async (enabled: boolean) => {
    setLockdownToggling(true)
    setShowLockdownConfirm(false)
    try {
      const resp = await api.post<{ lockdown_active: boolean }>('/api/admin/lockdown', { enabled })
      setLockdownActive(resp.lockdown_active)
    } catch {
      // Revert on failure — don't leave UI in inconsistent state
    } finally {
      setLockdownToggling(false)
    }
  }

  if (loading) {
    return <div className="text-vault-text-muted text-center py-12">Loading...</div>
  }

  if (error === 'unauthorized') {
    // Browser dismissed the credential dialog without valid creds — give a
    // hint instead of a confusing blank page.
    return <div className="text-vault-text-muted text-center py-12">Authentication required. Refresh to re-prompt.</div>
  }

  if (error) {
    return <div className="text-red-400 text-center py-12">{error}</div>
  }

  if (!data) return null

  return (
    <div className="max-w-4xl mx-auto space-y-8">
      <Helmet>
        <title>Admin Dashboard — Anime Intel</title>
        <meta name="robots" content="noindex" />
      </Helmet>

      <div>
        <h1 className="text-3xl font-extrabold mb-2">Admin Dashboard</h1>
        <p className="text-vault-text-muted text-sm">
          Operational metrics and controls. Data refreshed on page load.
        </p>
      </div>

      {/* Global Lockdown */}
      <section className={`rounded-lg border p-5 space-y-3 ${
        lockdownActive
          ? 'bg-red-950 border-red-800'
          : 'bg-vault-surface border-vault-border'
      }`}>
        <div className="flex items-center justify-between">
          <div>
            <h2 className="text-lg font-bold">Global Lockdown</h2>
            <p className="text-sm text-vault-text-muted mt-1">
              {lockdownActive
                ? 'ACTIVE — All non-admin requests are returning 503.'
                : 'Inactive — Site operating normally.'}
            </p>
          </div>
          <button
            onClick={handleLockdownToggle}
            disabled={lockdownToggling}
            className={`px-4 py-2 rounded font-medium text-sm cursor-pointer transition-colors ${
              lockdownActive
                ? 'bg-green-700 hover:bg-green-600 text-white'
                : 'bg-red-700 hover:bg-red-600 text-white'
            } disabled:opacity-50 disabled:cursor-not-allowed`}
          >
            {lockdownToggling
              ? 'Updating...'
              : lockdownActive
              ? 'Disable Lockdown'
              : 'Enable Lockdown'}
          </button>
        </div>
      </section>

      {/* Lockdown Confirmation Modal */}
      {showLockdownConfirm && (
        <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50 p-4">
          <div className="bg-vault-surface border border-red-800 rounded-lg p-6 max-w-md w-full space-y-4">
            <h3 className="text-lg font-bold text-red-400">Confirm Global Lockdown</h3>
            <p className="text-sm text-vault-text-muted">
              This will block ALL users — including logged-in users — from accessing the site.
              Only admin endpoints will remain accessible.
            </p>
            <p className="text-sm text-vault-text-muted">
              Are you sure you want to enable lockdown?
            </p>
            <div className="flex gap-3 justify-end">
              <button
                onClick={() => setShowLockdownConfirm(false)}
                className="px-4 py-2 rounded text-sm bg-vault-bg border border-vault-border text-vault-text-muted hover:text-vault-text cursor-pointer transition-colors"
              >
                Cancel
              </button>
              <button
                onClick={() => executeLockdownToggle(true)}
                className="px-4 py-2 rounded text-sm bg-red-700 hover:bg-red-600 text-white font-medium cursor-pointer transition-colors"
              >
                Enable Lockdown
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Activity */}
      <section className="space-y-3">
        <h2 className="text-lg font-bold">Activity</h2>
        <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
          <MetricCard label="Contact Messages" value={data.contact_messages} />
          <MetricCard label="Thumbs Up" value={data.feedback_summary.thumbs_up} />
          <MetricCard label="Thumbs Down" value={data.feedback_summary.thumbs_down} />
        </div>
      </section>

      {/* Anonymous Traffic */}
      <section className="space-y-3">
        <h2 className="text-lg font-bold">Anonymous Traffic</h2>
        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
          <MetricCard label="Anon Searches Today" value={data.anon_traffic.anon_searches_today} />
          <MetricCard label="Anon DNA Today" value={data.anon_traffic.anon_dna_today} />
          <MetricCard label="Unique Anon IPs (Today)" value={data.anon_traffic.anon_unique_ips_today} />
          <MetricCard label="Cap Hits Today" value={data.anon_traffic.anon_cap_hits_today} />
          <MetricCard label="Unique Anon IPs (7d)" value={data.anon_traffic.anon_unique_ips_7d} />
          <MetricCard label="Unique Anon IPs (30d)" value={data.anon_traffic.anon_unique_ips_30d} />
        </div>
      </section>

      {/* API Costs */}
      <section className="space-y-3">
        <h2 className="text-lg font-bold">API Costs</h2>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <MetricCard label="Searches Today (All)" value={data.api_costs.total_searches_today} />
          <MetricCard label="DNA Today (All)" value={data.api_costs.total_dna_today} />
          <div className="bg-vault-surface border border-vault-border rounded-lg p-4">
            <div className="text-sm text-vault-text-muted">Gemini Cost Today</div>
            <div className="text-2xl font-bold mt-1">
              ${data.api_costs.gemini_cost_today_usd.toFixed(4)}
            </div>
          </div>
          <div className="bg-vault-surface border border-vault-border rounded-lg p-4">
            <div className="text-sm text-vault-text-muted">Gemini Month-to-Date</div>
            <div className={`text-2xl font-bold mt-1 ${costColor(data.api_costs.gemini_mtd_usd, data.api_costs.gemini_ceiling_usd)}`}>
              ${data.api_costs.gemini_mtd_usd.toFixed(2)}
            </div>
            <div className="text-xs text-vault-text-muted mt-1">
              of ${data.api_costs.gemini_ceiling_usd} Gemini ceiling
            </div>
          </div>
        </div>
        <div className="text-xs text-vault-text-muted">
          Month-to-date Gemini spend from the gemini_calls ledger (resets at UTC month boundary).
          For context: heuristic anon-traffic projection is ${data.api_costs.production_projection_monthly_usd.toFixed(2)}/mo; Railway is ${data.api_costs.railway_monthly_usd}/mo fixed.
        </div>
      </section>

      {/* Cost Attribution (Last 7d) — reads real spend from gemini_calls */}
      <section className="space-y-3">
        <h2 className="text-lg font-bold">Cost Attribution (Last 7d)</h2>
        <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-7 gap-3">
          {ORIGIN_DISPLAY_ORDER.map((origin) => (
            <MetricCard
              key={origin}
              label={origin.charAt(0).toUpperCase() + origin.slice(1)}
              value={`$${(data.api_costs.last_7d_by_origin[origin] ?? 0).toFixed(4)}`}
            />
          ))}
          <div className="bg-vault-surface border border-vault-border rounded-lg p-4">
            <div className="text-sm text-vault-text-muted">Total (7d)</div>
            <div className="text-2xl font-bold mt-1">
              ${data.api_costs.last_7d_total_usd.toFixed(4)}
            </div>
          </div>
        </div>
        <div className="text-xs text-vault-text-muted">
          Real per-call cost from the gemini_calls ledger, grouped by origin. Includes thoughts_token_count
          (billed at output rate) — the hidden cost line the production heuristic above does NOT capture.
        </div>
      </section>

      {/* Pipeline Timings */}
      <section className="space-y-3">
        <h2 className="text-lg font-bold">
          Pipeline Timings (Last {data.pipeline_timings.window_hours}h)
        </h2>

        {data.pipeline_timings.error && (
          <div className="text-sm text-yellow-400 bg-vault-surface border border-vault-border rounded-lg p-4">
            {data.pipeline_timings.error}
          </div>
        )}

        {!data.pipeline_timings.error && !data.pipeline_timings.has_data && (
          <div className="text-sm text-vault-text-muted bg-vault-surface border border-vault-border rounded-lg p-4">
            No timing samples yet in the last {data.pipeline_timings.window_hours} hours.
            Data accumulates from production search traffic.
          </div>
        )}

        {data.pipeline_timings.has_data && data.pipeline_timings.profiles.map((profile) => (
          <div
            key={profile.rerank_type}
            className="bg-vault-surface border border-vault-border rounded-lg p-4 space-y-3"
          >
            <div className="flex items-center justify-between">
              <h3 className="font-semibold">
                {RERANK_TYPE_LABELS[profile.rerank_type] ?? profile.rerank_type}
              </h3>
              <span className="text-xs text-vault-text-muted">
                {profile.sample_count} sample{profile.sample_count === 1 ? '' : 's'}
              </span>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-left text-vault-text-muted border-b border-vault-border">
                    <th className="font-medium pb-2">Stage</th>
                    <th className="font-medium pb-2 text-right">P50</th>
                    <th className="font-medium pb-2 text-right">P95</th>
                    <th className="font-medium pb-2 text-right">P99</th>
                  </tr>
                </thead>
                <tbody>
                  <tr className="border-b border-vault-border/40">
                    <td className="py-2">Retrieval</td>
                    <td className="py-2 text-right tabular-nums">{fmtMs(profile.retrieval.p50_ms)}</td>
                    <td className="py-2 text-right tabular-nums">{fmtMs(profile.retrieval.p95_ms)}</td>
                    <td className="py-2 text-right tabular-nums">{fmtMs(profile.retrieval.p99_ms)}</td>
                  </tr>
                  <tr className="border-b border-vault-border/40">
                    <td className="py-2">Hydration</td>
                    <td className="py-2 text-right tabular-nums">{fmtMs(profile.hydration.p50_ms)}</td>
                    <td className="py-2 text-right tabular-nums">{fmtMs(profile.hydration.p95_ms)}</td>
                    <td className="py-2 text-right tabular-nums">{fmtMs(profile.hydration.p99_ms)}</td>
                  </tr>
                  <tr className="border-b border-vault-border/40">
                    <td className="py-2">Rerank (Gemini)</td>
                    <td className="py-2 text-right tabular-nums">{fmtMs(profile.rerank.p50_ms)}</td>
                    <td className="py-2 text-right tabular-nums">{fmtMs(profile.rerank.p95_ms)}</td>
                    <td className="py-2 text-right tabular-nums">{fmtMs(profile.rerank.p99_ms)}</td>
                  </tr>
                  <tr className="font-semibold">
                    <td className="py-2">Total</td>
                    <td className="py-2 text-right tabular-nums">{fmtMs(profile.total.p50_ms)}</td>
                    <td className="py-2 text-right tabular-nums">{fmtMs(profile.total.p95_ms)}</td>
                    <td className="py-2 text-right tabular-nums">{fmtMs(profile.total.p99_ms)}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        ))}

        <div className="text-xs text-vault-text-muted">
          Sourced from Supabase telemetry_logs. Stages: Retrieval (FAISS / objective query) → Hydration (SQLite fusion profile fetch) → Rerank (Gemini Phase 2) → Total (end-to-end).
        </div>
      </section>
    </div>
  )
}
