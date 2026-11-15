import { useState } from 'react'

interface Props {
  avgRating: number
  ratingCount: number
  userRating: number | null
  onRate?: (rating: number) => void
  disabled?: boolean
}

export default function StarRating({ avgRating, ratingCount, userRating, onRate, disabled }: Props) {
  const [hoverRating, setHoverRating] = useState(0)
  const displayRating = hoverRating || userRating || 0

  return (
    <div className="flex items-center gap-2">
      <div className="flex items-center gap-0.5">
        {[1, 2, 3, 4, 5].map(star => (
          <button
            key={star}
            onClick={() => onRate?.(star)}
            onMouseEnter={() => !disabled && setHoverRating(star)}
            onMouseLeave={() => setHoverRating(0)}
            disabled={disabled || !onRate}
            className={`text-lg bg-transparent border-none p-0 transition-colors ${
              onRate && !disabled ? 'cursor-pointer' : 'cursor-default'
            } ${
              star <= displayRating
                ? 'text-yellow-400'
                : 'text-vault-border'
            }`}
            title={onRate ? `Rate ${star} star${star > 1 ? 's' : ''}` : undefined}
          >
            ★
          </button>
        ))}
      </div>
      <span className="text-sm text-vault-text-muted">
        {avgRating > 0 ? `${avgRating.toFixed(1)}` : 'No ratings'}
        {ratingCount > 0 && ` (${ratingCount})`}
      </span>
    </div>
  )
}
