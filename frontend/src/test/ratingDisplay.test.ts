import { describe, it, expect } from 'vitest'
import { parseRating, ratingPillClasses } from '../lib/ratingDisplay'

describe('parseRating', () => {
  it.each([
    ['G - All Ages',                          'G',     'bg-green-700'],
    ['PG - Children',                         'PG',    'bg-green-500'],
    ['PG-13 - Teens 13 or older',             'PG-13', 'bg-yellow-600'],
    ['R - 17+ (violence & profanity)',        'R',     'bg-orange-600'],
    ['R+ - Mild Nudity',                      'R+',    'bg-red-700'],
    ['Rx - Hentai',                           'Rx',    'bg-red-900'],
  ])('parses %s and maps to bucket %s with %s palette', (raw, expectedShort, expectedBg) => {
    const parsed = parseRating(raw)
    expect(parsed).not.toBeNull()
    expect(parsed!.short).toBe(expectedShort)
    expect(parsed!.full).toBe(raw)
    expect(parsed!.bucket).toBe(expectedShort)
    expect(ratingPillClasses(parsed!.bucket)).toContain(expectedBg)
  })

  it('returns null for empty string', () => {
    expect(parseRating('')).toBeNull()
  })

  it('returns null for null input', () => {
    expect(parseRating(null)).toBeNull()
  })

  it('returns null for undefined input', () => {
    expect(parseRating(undefined)).toBeNull()
  })

  it('returns null for malformed strings missing the " - " delimiter', () => {
    expect(parseRating('PG13')).toBeNull()
    expect(parseRating('R+')).toBeNull()
  })

  it('returns null for unknown rating prefixes', () => {
    expect(parseRating('X - Adult Only')).toBeNull()
    expect(parseRating('NC-17 - No One 17 and Under Admitted')).toBeNull()
  })
})
