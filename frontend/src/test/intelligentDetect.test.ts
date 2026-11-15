import { describe, it, expect } from 'vitest'
import { detectIntelligentShape } from '../lib/intelligentDetect'

describe('detectIntelligentShape', () => {
  describe('Vibe adjectives fire the hint', () => {
    it.each([
      'cozy fantasy',
      'cozy anime',
      'gritty war drama',
      'wholesome slice of life',
      'philosophical mecha with soul',
      'melancholic coming-of-age story',
      'dark psychological thriller',
      'uplifting show about friendship',
      'atmospheric sci-fi',
      'dystopian romance',
      'surreal comedy',
      'nostalgic feel-good show',
      'slow-burn mystery',
      'cerebral thriller',
    ])('fires on %s', query => {
      const result = detectIntelligentShape(query)
      expect(result.matched).toBe(true)
      expect(result.signals.hasVibeAdjective).toBe(true)
    })

    it('is case-insensitive on vibe adjectives', () => {
      expect(detectIntelligentShape('COZY FANTASY').matched).toBe(true)
      expect(detectIntelligentShape('Dark Psychological').matched).toBe(true)
    })

    it('handles both hyphenated and space-separated vibes', () => {
      expect(detectIntelligentShape('slow-burn mystery').signals.hasVibeAdjective).toBe(true)
      expect(detectIntelligentShape('slow burn mystery').signals.hasVibeAdjective).toBe(true)
    })
  })

  describe('Compound-requirement patterns fire the hint', () => {
    it.each([
      'anime with complex worldbuilding',
      'show with deep character development',
      'series where the villain has a point',
      'anime where the protagonist dies',
      'show that features time travel',
      'series without romance',
    ])('fires on %s', query => {
      const result = detectIntelligentShape(query)
      expect(result.matched).toBe(true)
      expect(result.signals.hasCompoundPattern).toBe(true)
    })
  })

  describe('Similarity patterns fire the hint', () => {
    it.each([
      'something similar to Steins;Gate',
      'anime that reminds me of Evangelion',
      'show in the style of Studio Ghibli',
      'a series in the vein of Serial Experiments Lain',
    ])('fires on %s', query => {
      const result = detectIntelligentShape(query)
      expect(result.matched).toBe(true)
      expect(result.signals.hasSimilarityPattern).toBe(true)
    })
  })

  describe('Hybrid rule: vibe signal + objective signal does NOT fire', () => {
    // These queries have a vibe adjective AND an objective-shape signal
    // (year, decade, top-N, or superlative + qualifier). The hint should
    // stay quiet — the user is deliberately combining vibe + filter and
    // Objective Rankings can honor the filter.
    it.each([
      'cozy fantasy from 2010',           // vibe + year
      'dark anime from the 90s',          // vibe + decade
      'top 10 philosophical mecha',       // vibe + top-N
      'best gritty seinen of the 2000s',  // vibe + superlative + decade
      'nostalgic 80s-feel show',          // vibe + decade (dash-prefix form)
    ])('does NOT fire on %s (vibe + objective)', query => {
      const result = detectIntelligentShape(query)
      expect(result.matched).toBe(false)
      expect(result.signals.objectiveShapeMatched).toBe(true)
    })
  })

  describe('No signals → does not fire', () => {
    it.each([
      'action',
      'romance',
      'mecha',
      'Cowboy Bebop',
      'anime',
      '',
    ])('does NOT fire on %s (no vibe signals)', query => {
      const result = detectIntelligentShape(query)
      expect(result.matched).toBe(false)
    })
  })

  describe('Null-safe / defensive', () => {
    it('handles null-ish input via `?? \"\"` coercion', () => {
      // The ?? default protects against callers passing undefined
      expect(() => detectIntelligentShape(undefined as unknown as string)).not.toThrow()
    })
  })
})
