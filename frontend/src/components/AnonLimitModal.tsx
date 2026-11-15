import type { AnonLimitDetail } from '../lib/api'

interface Props {
  detail: AnonLimitDetail
  onClose: () => void
}

export default function AnonLimitModal({ detail, onClose }: Props) {
  const label = detail.limit_type === 'dna' ? 'Shows Like This' : 'search'

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
      <div className="bg-vault-surface border border-vault-border rounded-lg p-6 max-w-md w-full mx-4 space-y-4 text-center">
        <h2 className="text-xl font-bold text-vault-text">
          Daily limit reached
        </h2>
        <p className="text-vault-text-muted">
          You've used all {detail.cap} free {label} queries for today.
          Your limit resets at midnight UTC.
        </p>
        <button
          onClick={onClose}
          className="px-6 py-2 rounded border border-vault-border text-vault-text-muted hover:text-vault-text transition-colors bg-transparent cursor-pointer"
        >
          Close
        </button>
      </div>
    </div>
  )
}
