import { useState, useEffect } from 'react'
import {
  fetchAnonUsage,
  getAnonUsage,
  onAnonUsageChange,
  type AnonUsage,
} from '../lib/api'

export default function AnonBanner() {
  const [usage, setUsage] = useState<AnonUsage | null>(getAnonUsage)

  useEffect(() => {
    const unsubscribe = onAnonUsageChange(setUsage)
    // Seed from the server on first banner mount. Subsequent renders
    // (after navigation) reuse the cached module-level state, so this
    // only fires once per page load. Failures are swallowed inside
    // fetchAnonUsage — the banner falls back to "—" and the next
    // successful /api/search or /api/dna POST will resolve it.
    if (getAnonUsage() === null) {
      void fetchAnonUsage()
    }
    return unsubscribe
  }, [])

  // Until the first /api/anon-usage response resolves, show a neutral
  // placeholder. Falsifying the count to "30" here would put us back in
  // the old "lies on freshly-opened tabs" regime that this whole change
  // was meant to fix.
  const searches = usage ? usage.searchesRemaining.toString() : '—'
  const dna = usage ? usage.dnaRemaining.toString() : '—'
  const searchesPlural = usage?.searchesRemaining === 1 ? '' : 'es'

  return (
    <div className="bg-vault-surface border border-vault-border rounded-lg px-4 py-3 text-sm">
      <p className="text-vault-text-muted">
        <span className="font-medium text-vault-text">{searches}</span> search{searchesPlural} and{' '}
        <span className="font-medium text-vault-text">{dna}</span> Shows Like This remaining today. Resets at midnight UTC.
      </p>
    </div>
  )
}
