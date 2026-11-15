import { describe, it, expect } from 'vitest'
import { formatVoteCount, formatVoteCountExact } from '../lib/voteCountDisplay'

describe('formatVoteCount', () => {
  it('returns null for 0', () => {
    expect(formatVoteCount(0)).toBeNull()
  })

  it('returns null for null', () => {
    expect(formatVoteCount(null)).toBeNull()
  })

  it('returns null for undefined', () => {
    expect(formatVoteCount(undefined)).toBeNull()
  })

  it('returns null for negative counts (defensive)', () => {
    expect(formatVoteCount(-5)).toBeNull()
  })

  it('formats counts under 1000 as literal', () => {
    expect(formatVoteCount(1)).toBe('1 ratings')
    expect(formatVoteCount(384)).toBe('384 ratings')
    expect(formatVoteCount(999)).toBe('999 ratings')
  })

  it('formats counts 1000-9999 with one decimal', () => {
    expect(formatVoteCount(1000)).toBe('1k ratings')
    expect(formatVoteCount(1234)).toBe('1.2k ratings')
    expect(formatVoteCount(5678)).toBe('5.7k ratings')
    expect(formatVoteCount(9999)).toBe('10k ratings')
  })

  it('formats counts 10k-999k as whole k', () => {
    expect(formatVoteCount(10_000)).toBe('10k ratings')
    expect(formatVoteCount(48_524)).toBe('49k ratings')
    expect(formatVoteCount(296_384)).toBe('296k ratings')
    expect(formatVoteCount(999_499)).toBe('999k ratings')
  })

  it('formats 1M+ with one decimal', () => {
    expect(formatVoteCount(1_000_000)).toBe('1M ratings')
    expect(formatVoteCount(1_234_567)).toBe('1.2M ratings')
    expect(formatVoteCount(3_035_579)).toBe('3M ratings')
  })
})

describe('formatVoteCountExact', () => {
  it('returns null for 0 / null / undefined', () => {
    expect(formatVoteCountExact(0)).toBeNull()
    expect(formatVoteCountExact(null)).toBeNull()
    expect(formatVoteCountExact(undefined)).toBeNull()
  })

  it('formats with locale thousands separators', () => {
    expect(formatVoteCountExact(384)).toBe('384 ratings')
    expect(formatVoteCountExact(3_035_579)).toBe('3,035,579 ratings')
  })
})
