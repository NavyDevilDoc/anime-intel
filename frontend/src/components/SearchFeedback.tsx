import { useState } from 'react'
import { api } from '../lib/api'

interface Props {
  searchId: string
}

export default function SearchFeedback({ searchId }: Props) {
  const [status, setStatus] = useState<'idle' | 'submitting' | 'submitted' | 'error'>('idle')
  const [submitted, setSubmitted] = useState<'up' | 'down' | null>(null)

  const submit = async (rating: 'up' | 'down') => {
    setStatus('submitting')
    try {
      await api.post('/api/search-feedback', { search_id: searchId, rating })
      setSubmitted(rating)
      setStatus('submitted')
    } catch {
      setStatus('error')
    }
  }

  if (status === 'submitted') {
    return (
      <span className="inline-flex items-center gap-1.5 text-sm text-green-400">
        <span aria-hidden>✓</span>
        <span>{submitted === 'up' ? 'Thanks!' : 'Noted — we\'ll improve.'}</span>
      </span>
    )
  }

  if (status === 'error') {
    return (
      <span className="inline-flex items-center gap-2 text-sm">
        <span className="text-red-400">Couldn't save your rating.</span>
        <button
          onClick={() => setStatus('idle')}
          className="text-vault-accent hover:underline cursor-pointer bg-transparent border-none p-0"
        >
          Try again
        </button>
      </span>
    )
  }

  return (
    <span className="flex items-center gap-1.5">
      <span className="text-xs text-vault-text-muted">Helpful?</span>
      <button
        onClick={() => submit('up')}
        disabled={status === 'submitting'}
        className="text-sm px-1.5 py-0.5 rounded bg-transparent border border-vault-border text-vault-text-muted hover:text-green-400 hover:border-green-700 cursor-pointer transition-colors disabled:opacity-50"
        aria-label="Thumbs up"
        title="Good results"
      >
        &#x1F44D;
      </button>
      <button
        onClick={() => submit('down')}
        disabled={status === 'submitting'}
        className="text-sm px-1.5 py-0.5 rounded bg-transparent border border-vault-border text-vault-text-muted hover:text-red-400 hover:border-red-700 cursor-pointer transition-colors disabled:opacity-50"
        aria-label="Thumbs down"
        title="Poor results"
      >
        &#x1F44E;
      </button>
    </span>
  )
}
