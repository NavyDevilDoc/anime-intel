import { useState, useEffect, useRef } from 'react'
import { api } from '../lib/api'
import type {
  StaffAutocompleteResponse,
  StaffAutocompletePerson,
  StaffAutocompleteStudio,
  StaffChip,
  StaffRole,
} from '../lib/types'

/**
 * Item 4a — chip-based input for Staff/Studio search mode.
 *
 * User types a name; 150ms-debounced fetch to `/api/autocomplete/staff`
 * populates a grouped dropdown (Persons + Studios). Clicking a suggestion
 * adds a persistent chip and clears the input for the next one. Each
 * person surfaces "Any role" pinned at the top of their group plus one
 * option per role they hold, so users can filter to a specific role or
 * pick everything the person touched.
 *
 * Chip semantics (enforced server-side by `staff_studio_search`):
 *   - person chips OR each other,
 *   - studio chips OR each other,
 *   - the persons set ANDs with the studios set when both present.
 *
 * State is owned by the parent (FindAShow) via `value` + `onChange` so
 * mode-switch behavior and submit gating live where the rest of the
 * search form lives. This component is purely a chip editor.
 */

interface Props {
  value: StaffChip[]
  onChange: (chips: StaffChip[]) => void
  disabled?: boolean
}

const DEBOUNCE_MS = 150

// Compact role label used inside chip and dropdown. Kept in sync with
// the StaffRole literal in lib/types.ts.
function formatRoleLabel(role: StaffRole | null): string {
  return role ?? 'Any role'
}

function chipsEqual(a: StaffChip, b: StaffChip): boolean {
  if (a.kind !== b.kind) return false
  if (a.kind === 'person' && b.kind === 'person') {
    return a.personMalId === b.personMalId && a.role === b.role
  }
  if (a.kind === 'studio' && b.kind === 'studio') {
    return a.name === b.name
  }
  return false
}

export default function StaffStudioInput({ value, onChange, disabled }: Props) {
  const [query, setQuery] = useState('')
  const [suggestions, setSuggestions] = useState<StaffAutocompleteResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [dropdownOpen, setDropdownOpen] = useState(false)
  const abortRef = useRef<AbortController | null>(null)
  const containerRef = useRef<HTMLDivElement | null>(null)

  // Close dropdown on click outside the component.
  useEffect(() => {
    function handler(e: MouseEvent) {
      if (!containerRef.current) return
      if (!containerRef.current.contains(e.target as Node)) {
        setDropdownOpen(false)
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [])

  // Debounced autocomplete fetch. Cancels in-flight requests on
  // supersede so the visible dropdown always reflects the latest query.
  useEffect(() => {
    const trimmed = query.trim()
    if (!trimmed) {
      abortRef.current?.abort()
      setSuggestions(null)
      setLoading(false)
      return
    }
    const controller = new AbortController()
    abortRef.current?.abort()
    abortRef.current = controller
    const timer = window.setTimeout(async () => {
      setLoading(true)
      try {
        const res = await api.post<StaffAutocompleteResponse>(
          '/api/autocomplete/staff',
          { query: trimmed, limit: 5 },
          controller.signal,
        )
        if (!controller.signal.aborted) {
          setSuggestions(res)
          setDropdownOpen(true)
        }
      } catch (err) {
        if (err instanceof DOMException && err.name === 'AbortError') return
        // Silent on network errors — dropdown stays as-is; user can retry.
      } finally {
        if (!controller.signal.aborted) setLoading(false)
      }
    }, DEBOUNCE_MS)
    return () => {
      window.clearTimeout(timer)
      controller.abort()
    }
  }, [query])

  const addChip = (chip: StaffChip) => {
    if (value.some(c => chipsEqual(c, chip))) {
      // Already present — silently no-op, keeps focus for next chip.
      setQuery('')
      return
    }
    onChange([...value, chip])
    setQuery('')
    setSuggestions(null)
    setDropdownOpen(false)
  }

  const removeChip = (idx: number) => {
    onChange(value.filter((_, i) => i !== idx))
  }

  const personSuggestions = suggestions?.persons ?? []
  const studioSuggestions = suggestions?.studios ?? []
  const hasSuggestions = personSuggestions.length > 0 || studioSuggestions.length > 0
  const showEmptyState = dropdownOpen && !loading && query.trim() && !hasSuggestions

  return (
    <div ref={containerRef} className="relative">
      <div
        className={`flex flex-wrap items-center gap-2 min-h-[42px] px-2 py-1.5 rounded bg-vault-surface border border-vault-border ${
          disabled ? 'opacity-60' : 'focus-within:border-vault-accent'
        }`}
      >
        {value.map((chip, idx) => (
          <span
            key={`${chip.kind}-${idx}`}
            className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full bg-vault-border text-xs text-vault-text"
            data-testid={`staff-chip-${chip.kind}-${idx}`}
          >
            <span className="text-vault-text-muted">
              {chip.kind === 'person' ? formatRoleLabel(chip.role) : 'Studio'} ·
            </span>
            <span>
              {chip.kind === 'person' ? chip.nameDisplay : chip.name}
            </span>
            <button
              type="button"
              onClick={() => removeChip(idx)}
              aria-label={`Remove ${chip.kind === 'person' ? chip.nameDisplay : chip.name}`}
              disabled={disabled}
              className="ml-1 text-vault-text-muted hover:text-vault-text bg-transparent border-none cursor-pointer text-sm leading-none"
            >
              &times;
            </button>
          </span>
        ))}
        <input
          type="text"
          value={query}
          onChange={e => setQuery(e.target.value)}
          onFocus={() => query.trim() && setDropdownOpen(true)}
          onKeyDown={e => {
            // Backspace on empty input removes the last chip — standard
            // chip-input UX. Guarded on empty query so mid-typing
            // backspaces edit the query normally.
            if (e.key === 'Backspace' && !query && value.length > 0) {
              e.preventDefault()
              removeChip(value.length - 1)
            } else if (e.key === 'Escape') {
              setDropdownOpen(false)
            }
          }}
          disabled={disabled}
          placeholder={
            value.length === 0
              ? 'Add a director, composer, creator, or studio...'
              : 'Add another...'
          }
          className="flex-1 min-w-[180px] bg-transparent border-none text-vault-text focus:outline-none py-1 text-sm"
          aria-label="Search directors, composers, creators, or studios"
          aria-autocomplete="list"
          aria-expanded={dropdownOpen}
        />
      </div>

      {(dropdownOpen && (hasSuggestions || loading || showEmptyState)) && (
        <div
          role="listbox"
          className="absolute z-20 mt-1 w-full max-h-96 overflow-y-auto rounded border border-vault-border bg-vault-surface shadow-lg"
        >
          {loading && (
            <div className="px-3 py-2 text-xs text-vault-text-muted italic">
              Searching...
            </div>
          )}

          {!loading && personSuggestions.length > 0 && (
            <div className="border-b border-vault-border">
              <div className="px-3 pt-2 pb-1 text-xs uppercase tracking-wide text-vault-text-muted">
                People
              </div>
              {personSuggestions.map(person => (
                <PersonSuggestionRows
                  key={person.person_mal_id}
                  person={person}
                  onPick={addChip}
                />
              ))}
            </div>
          )}

          {!loading && studioSuggestions.length > 0 && (
            <div>
              <div className="px-3 pt-2 pb-1 text-xs uppercase tracking-wide text-vault-text-muted">
                Studios
              </div>
              {studioSuggestions.map(studio => (
                <StudioSuggestionRow
                  key={studio.name}
                  studio={studio}
                  onPick={addChip}
                />
              ))}
            </div>
          )}

          {showEmptyState && (
            <div className="px-3 py-2 text-xs text-vault-text-muted italic">
              No matches. Check spelling, or try a studio name.
            </div>
          )}
        </div>
      )}
    </div>
  )
}


// -------- Suggestion sub-components --------

interface PersonSuggestionRowsProps {
  person: StaffAutocompletePerson
  onPick: (chip: StaffChip) => void
}

function PersonSuggestionRows({ person, onPick }: PersonSuggestionRowsProps) {
  // Pinned "Any role" option first, then one row per role held. Role
  // order preserves what the backend returned (already sorted by count).
  return (
    <>
      <SuggestionButton
        onClick={() =>
          onPick({
            kind: 'person',
            personMalId: person.person_mal_id,
            nameDisplay: person.name_display,
            role: null,
          })
        }
        prefix="Any role"
        label={person.name_display}
        count={person.total_count}
        emphasized
      />
      {person.roles.map(r => (
        <SuggestionButton
          key={r.role}
          onClick={() =>
            onPick({
              kind: 'person',
              personMalId: person.person_mal_id,
              nameDisplay: person.name_display,
              role: r.role,
            })
          }
          prefix={r.role}
          label={person.name_display}
          count={r.count}
        />
      ))}
    </>
  )
}

interface StudioSuggestionRowProps {
  studio: StaffAutocompleteStudio
  onPick: (chip: StaffChip) => void
}

function StudioSuggestionRow({ studio, onPick }: StudioSuggestionRowProps) {
  return (
    <SuggestionButton
      onClick={() => onPick({ kind: 'studio', name: studio.name })}
      prefix="Studio"
      label={studio.name}
      count={studio.show_count}
    />
  )
}

interface SuggestionButtonProps {
  onClick: () => void
  prefix: string
  label: string
  count: number
  emphasized?: boolean
}

function SuggestionButton({ onClick, prefix, label, count, emphasized }: SuggestionButtonProps) {
  return (
    <button
      type="button"
      role="option"
      aria-selected="false"
      onClick={onClick}
      className={`w-full text-left px-3 py-1.5 text-sm cursor-pointer bg-transparent border-none hover:bg-vault-border transition-colors ${
        emphasized ? 'font-medium' : ''
      }`}
    >
      <span className="text-vault-text-muted">{prefix} · </span>
      <span className="text-vault-text">{label}</span>
      <span className="ml-2 text-xs text-vault-text-muted">
        ({count} show{count === 1 ? '' : 's'})
      </span>
    </button>
  )
}
