import type { StaffChip } from './types'

/**
 * Convert internal chip state to the wire payload sent on /api/search.
 * Persons collapse to `{person_mal_id, role}`; studios to raw name
 * strings. Returns null when no chips are present so callers can gate
 * the submit button on it.
 */
export function toStaffFilterPayload(chips: StaffChip[]) {
  if (chips.length === 0) return null
  const persons = chips
    .filter((c): c is Extract<StaffChip, { kind: 'person' }> => c.kind === 'person')
    .map(c => ({ person_mal_id: c.personMalId, role: c.role }))
  const studios = chips
    .filter((c): c is Extract<StaffChip, { kind: 'studio' }> => c.kind === 'studio')
    .map(c => c.name)
  return { persons, studios }
}
