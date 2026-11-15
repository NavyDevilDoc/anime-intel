import { describe, it, expect } from 'vitest'
import { detectObjectiveShape } from '../lib/objectiveDetect'

describe('detectObjectiveShape', () => {
  describe('Rule 1: explicit top-N always fires', () => {
    it.each([
      'top 5 sports anime',
      'top 10 anime',
      'top 20 mecha shows',
      'give me the top 100',
      'TOP 5 anime',
    ])('fires on %s', query => {
      const result = detectObjectiveShape(query)
      expect(result.matched).toBe(true)
      expect(result.signals.topN).not.toBeNull()
    })

    it('extracts the top-N number', () => {
      expect(detectObjectiveShape('top 10 anime').signals.topN).toBe(10)
      expect(detectObjectiveShape('top 5 mecha').signals.topN).toBe(5)
      expect(detectObjectiveShape('top 100 anime').signals.topN).toBe(100)
    })

    it('does not match runaway numbers ("top 12345")', () => {
      const result = detectObjectiveShape('top 12345 anime')
      expect(result.signals.topN).toBeNull()
    })
  })

  describe('Rule 2: superlative + qualifier fires', () => {
    it.each([
      'best mecha of the 90s',
      'greatest shounen',
      'highest rated seinen from 2015',
      'most popular fantasy',
      'best romance',
      'top isekai',
      'worst anime of 2020',
      'top-rated shows from 2019',
      'most acclaimed drama',
      'greatest sci-fi movies',
    ])('fires on %s', query => {
      const result = detectObjectiveShape(query)
      expect(result.matched).toBe(true)
      expect(result.signals.hasSuperlative).toBe(true)
    })

    it('flags the superlative-at-start signal for opening superlatives', () => {
      expect(detectObjectiveShape('best romance').signals.superlativeAtStart).toBe(true)
      expect(detectObjectiveShape('top isekai').signals.superlativeAtStart).toBe(true)
    })

    it('does not flag superlative-at-start when superlative appears mid-sentence', () => {
      expect(detectObjectiveShape('anime with the best animation').signals.superlativeAtStart).toBe(false)
    })
  })

  describe('Rule 3: year / decade alone fires', () => {
    it.each([
      'anime from 2015',
      'shows released in 2020',
      'the 90s',
      '2010s anime',
      'shows from the eighties',
      'anime after 2018',
      'shows before 2010',
      'anime since 2020',
      'between 2010 and 2019',
      '2015-2020',
      '2015 to 2020',
    ])('fires on %s', query => {
      const result = detectObjectiveShape(query)
      expect(result.matched).toBe(true)
    })
  })

  describe('Anti-patterns hard-block firing', () => {
    it.each([
      'greatest villain of all time',
      'best moments of all time',
      'top-tier animation',
      'top tier fight scenes',
      'best girl in romance',
      'best girl of 2020',        // anti-pattern beats year signal
      'top 10 best girl rankings', // anti-pattern beats top-N signal
    ])('does not fire on %s', query => {
      const result = detectObjectiveShape(query)
      expect(result.matched).toBe(false)
      expect(result.signals.antiPatternHit).toBe(true)
    })
  })

  describe('Non-firing cases (vibe queries)', () => {
    it.each([
      'sincere romcom without fanservice',
      'shows about competitors',
      'melancholy',
      'mecha',                              // genre alone doesn't fire
      'romance',                            // genre alone doesn't fire
      'spring anime',                       // season alone doesn't fire
      'summer',
      'shows with strong character arcs',
      'a mecha show with philosophical themes',
      'betrayed his mentor',
      '',                                   // empty string
      '   ',                                // whitespace
    ])('does not fire on %s', query => {
      expect(detectObjectiveShape(query).matched).toBe(false)
    })
  })

  describe('Signal breakdown surfaces what fired', () => {
    it('exposes granular signals for downstream use', () => {
      const result = detectObjectiveShape('top 10 mecha anime of 2020')
      expect(result.signals.topN).toBe(10)
      expect(result.signals.hasYear).toBe(true)
      expect(result.signals.hasGenre).toBe(true)
      expect(result.signals.hasSuperlative).toBe(true) // "top" is a superlative too
    })

    it('preserves signal detection even when anti-pattern blocks firing', () => {
      const result = detectObjectiveShape('greatest of all time')
      expect(result.matched).toBe(false)
      expect(result.signals.antiPatternHit).toBe(true)
      expect(result.signals.hasSuperlative).toBe(true)
    })
  })

  describe('Null / undefined inputs', () => {
    it('handles null-ish input without throwing', () => {
      // @ts-expect-error deliberate null-input probe
      expect(detectObjectiveShape(null).matched).toBe(false)
      // @ts-expect-error deliberate undefined-input probe
      expect(detectObjectiveShape(undefined).matched).toBe(false)
    })
  })
})
