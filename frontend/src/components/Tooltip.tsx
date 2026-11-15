import { useState } from 'react'
import type { ReactNode } from 'react'

interface Props {
  text: string
  children: ReactNode
}

export default function Tooltip({ text, children }: Props) {
  const [visible, setVisible] = useState(false)

  return (
    <span
      className="relative inline-flex items-center"
      onMouseEnter={() => setVisible(true)}
      onMouseLeave={() => setVisible(false)}
    >
      {children}
      <span
        className="ml-1 text-vault-text-muted cursor-help select-none"
        aria-label={text}
      >
        &#9432;
      </span>
      {visible && (
        <span
          role="tooltip"
          className="absolute bottom-full left-1/2 -translate-x-1/2 mb-2 px-3 py-2 text-xs text-vault-text bg-vault-surface border border-vault-border rounded shadow-lg whitespace-normal w-56 z-50 pointer-events-none"
        >
          {text}
        </span>
      )}
    </span>
  )
}
