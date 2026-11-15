import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { fetchProfiles, lookupOffline, detectTitleMatch, _resetCache } from '../lib/offlineEncyclopedia'

interface Fixture {
  id: number
  title: string
  romaji_title: string | null
  slug: string
  studio: string | null
  quality_score: number
  raw_mal_score: number
  scored_by: number
  avg_sentiment: number
  release_year: number | null
  season: string | null
  genres: string | null
  synopsis: string | null
  consensus_summary: string | null
  pros: string[]
  cons: string[]
  controversy_score: number | null
  thematic_vibe: string | null
  has_narrative: boolean
  mal_rating: string | null
  series_context: unknown
}

/** Small fixture set covering the interesting match cases: an exact-title
 *  franchise root, a same-cluster sibling, a romaji-only title, one that
 *  disambiguates with a substring hit, one with a series_context, and one
 *  with quirky punctuation for the normalize() logic. */
function makeFixtures(): Fixture[] {
  return [
    {
      id: 1,
      title: 'Cowboy Bebop',
      romaji_title: 'Cowboy Bebop',
      slug: '1-cowboy-bebop',
      studio: 'Sunrise',
      quality_score: 8.7,
      raw_mal_score: 8.75,
      scored_by: 1_054_562,
      avg_sentiment: 0.89,
      release_year: 1998,
      season: 'Spring_1998',
      genres: 'Action, Sci-Fi',
      synopsis: 'A space bounty-hunter crew...',
      consensus_summary: 'Cowboy Bebop is a classic.',
      pros: ['Excellent animation'],
      cons: ['Episodic format can be hit-or-miss'],
      controversy_score: 5,
      thematic_vibe: 'Neo-noir',
      has_narrative: true,
      mal_rating: 'R - 17+',
      series_context: {
        series_id: '1',
        series_size: 2,
        position: 1,
        score_min: 8.38,
        score_max: 8.75,
        score_range_filtered: false,
        sentiment_min: 0.46,
        sentiment_max: 0.89,
        trajectory: 'mixed',
        tv_position: 1,
        tv_count: 1,
      },
    },
    {
      id: 5,
      title: 'Cowboy Bebop: The Movie',
      romaji_title: 'Cowboy Bebop: Tengoku no Tobira',
      slug: '5-cowboy-bebop-the-movie',
      studio: 'Bones',
      quality_score: 8.4,
      raw_mal_score: 8.38,
      scored_by: 230_506,
      avg_sentiment: 0.46,
      release_year: 2001,
      season: 'Summer_2001',
      genres: 'Action, Sci-Fi',
      synopsis: 'Movie continuation...',
      consensus_summary: 'A fan-favorite movie.',
      pros: ['Great soundtrack'],
      cons: ['Feels like an extended episode'],
      controversy_score: 4,
      thematic_vibe: 'Neo-noir',
      has_narrative: false,
      mal_rating: 'R - 17+',
      series_context: null,
    },
    {
      id: 20,
      title: 'Naruto',
      romaji_title: 'Naruto',
      slug: '20-naruto',
      studio: 'Pierrot',
      quality_score: 8.0,
      raw_mal_score: 8.0,
      scored_by: 2_000_000,
      avg_sentiment: 0.7,
      release_year: 2002,
      season: 'Fall_2002',
      genres: 'Action, Shounen',
      synopsis: 'A young ninja...',
      consensus_summary: 'Beloved shounen.',
      pros: ['Iconic characters'],
      cons: ['Filler'],
      controversy_score: 6,
      thematic_vibe: 'Shounen',
      has_narrative: true,
      mal_rating: 'PG-13',
      series_context: null,
    },
    {
      id: 1735,
      title: 'Naruto: Shippuden',
      romaji_title: 'Naruto: Shippuuden',
      slug: '1735-naruto-shippuden',
      studio: 'Pierrot',
      quality_score: 8.3,
      raw_mal_score: 8.3,
      scored_by: 1_600_000,
      avg_sentiment: 0.75,
      release_year: 2007,
      season: 'Winter_2007',
      genres: 'Action, Shounen',
      synopsis: 'The sequel...',
      consensus_summary: 'The sequel arc.',
      pros: ['Better animation'],
      cons: ['Even more filler'],
      controversy_score: 5,
      thematic_vibe: 'Shounen',
      has_narrative: true,
      mal_rating: 'PG-13',
      series_context: null,
    },
    {
      id: 34566,
      title: 'Boruto: Naruto Next Generations',
      romaji_title: 'Boruto: Naruto Next Generations',
      slug: '34566-boruto-naruto-next-generations',
      studio: 'Pierrot',
      quality_score: 5.9,
      raw_mal_score: 5.9,
      scored_by: 400_000,
      avg_sentiment: 0.2,
      release_year: 2017,
      season: 'Spring_2017',
      genres: 'Action, Shounen',
      synopsis: 'The next generation...',
      consensus_summary: 'Divisive spinoff.',
      pros: ['New characters'],
      cons: ['Filler heavy'],
      controversy_score: 8,
      thematic_vibe: 'Shounen',
      has_narrative: true,
      mal_rating: 'PG-13',
      series_context: null,
    },
    {
      id: 55555,
      title: 'Road of Naruto',
      romaji_title: 'Road of Naruto',
      slug: '55555-road-of-naruto',
      studio: 'Pierrot',
      quality_score: 7.4,
      raw_mal_score: 7.4,
      scored_by: 20_000,
      avg_sentiment: 0.5,
      release_year: 2022,
      season: 'Fall_2022',
      genres: 'Action',
      synopsis: 'Anniversary special...',
      consensus_summary: 'A retrospective.',
      pros: ['Nostalgic'],
      cons: ['Short'],
      controversy_score: 3,
      thematic_vibe: 'Shounen',
      has_narrative: false,
      mal_rating: 'PG-13',
      series_context: null,
    },
    // Romaji-only match target — title differs from romaji significantly.
    {
      id: 999,
      title: 'Attack on Titan',
      romaji_title: 'Shingeki no Kyojin',
      slug: '999-attack-on-titan',
      studio: 'WIT',
      quality_score: 8.5,
      raw_mal_score: 8.5,
      scored_by: 3_000_000,
      avg_sentiment: 0.6,
      release_year: 2013,
      season: 'Spring_2013',
      genres: 'Action',
      synopsis: 'Titans...',
      consensus_summary: 'Genre-defining.',
      pros: ['Ambition'],
      cons: ['Pacing'],
      controversy_score: 7,
      thematic_vibe: 'Dark',
      has_narrative: true,
      mal_rating: 'R',
      series_context: null,
    },
  ]
}

function mockProfilesFetch(fixtures: Fixture[]) {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ anime: fixtures, generated_at: 'test', count: fixtures.length }),
    }),
  )
}

beforeEach(() => {
  _resetCache()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('offlineEncyclopedia — fetchProfiles', () => {
  it('fetches from /data/seo_profiles.json once and caches the result', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ anime: makeFixtures(), generated_at: 'test', count: 5 }),
    })
    vi.stubGlobal('fetch', fetchMock)

    const a = await fetchProfiles()
    const b = await fetchProfiles()

    expect(a).toBe(b)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock).toHaveBeenCalledWith('/data/seo_profiles.json')
  })

  it('propagates HTTP errors and clears the cache on failure', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValueOnce({ ok: false, status: 502 }),
    )

    await expect(fetchProfiles()).rejects.toThrow(/HTTP 502/)

    // Second call should retry (cache cleared) — succeeds now
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve({ anime: makeFixtures(), generated_at: 't', count: 5 }),
      }),
    )
    const profiles = await fetchProfiles()
    expect(profiles.length).toBe(7)
  })
})

describe('offlineEncyclopedia — lookupOffline exact match', () => {
  beforeEach(() => mockProfilesFetch(makeFixtures()))

  it('resolves an exact title match to a profile', async () => {
    const res = await lookupOffline('Cowboy Bebop')
    expect(res.success).toBe(true)
    expect(res.profile?.title).toBe('Cowboy Bebop')
    expect(res.matches).toEqual(['Cowboy Bebop'])
  })

  it('is case-insensitive on exact match', async () => {
    const res = await lookupOffline('cowboy bebop')
    expect(res.profile?.title).toBe('Cowboy Bebop')
  })

  it('normalizes punctuation for exact match', async () => {
    // "Naruto:  Shippuden" (extra colon whitespace) should still land
    const res = await lookupOffline('Naruto Shippuden')
    expect(res.profile?.title).toBe('Naruto: Shippuden')
  })

  it('resolves via romaji_title when different from title', async () => {
    const res = await lookupOffline('Shingeki no Kyojin')
    expect(res.profile?.title).toBe('Attack on Titan')
  })

  it('renames avg_sentiment → audience_sentiment on the returned profile', async () => {
    const res = await lookupOffline('Cowboy Bebop')
    expect(res.profile?.audience_sentiment).toBeCloseTo(0.89)
    // Ensure the JSON's field name doesn't leak into the returned profile
    expect((res.profile as unknown as Record<string, unknown>)?.avg_sentiment).toBeUndefined()
  })

  it('renames consensus_summary → audience_consensus on the returned profile', async () => {
    const res = await lookupOffline('Cowboy Bebop')
    expect(res.profile?.audience_consensus).toBe('Cowboy Bebop is a classic.')
    expect((res.profile as unknown as Record<string, unknown>)?.consensus_summary).toBeUndefined()
  })

  it('passes through series_context and stamps an empty dropped_siblings', async () => {
    const res = await lookupOffline('Cowboy Bebop')
    expect(res.series_context?.series_size).toBe(2)
    expect(res.series_context?.position).toBe(1)
    // dropped_siblings is required on the SeriesContext type but not
    // produced by the export (no rerank happens for encyclopedia). Adapter
    // stamps an empty array so the type contract holds.
    expect(res.series_context?.dropped_siblings).toEqual([])
  })

  it('returns null series_context when the raw entry has none', async () => {
    // Attack on Titan chosen deliberately — no siblings in the fixture,
    // so the franchise-root disambig heuristic doesn't fire and we get
    // a single-profile response. AoT's series_context in the fixture
    // is null, so the returned series_context should also be null.
    const res = await lookupOffline('Attack on Titan')
    expect(res.series_context).toBeNull()
  })
})

describe('offlineEncyclopedia — lookupOffline disambiguation', () => {
  beforeEach(() => mockProfilesFetch(makeFixtures()))

  it('offers franchise disambiguation when the exact hit has 2+ siblings', async () => {
    // "Naruto" is an exact title, but the vault also has Shippuden,
    // Boruto, and Road of Naruto — the franchise-root heuristic surfaces
    // the family so the user can pick which entry they meant. Deliberate
    // divergence from backend behavior (documented in the module).
    const res = await lookupOffline('Naruto')
    expect(res.success).toBe(true)
    expect(res.profile).toBeNull()
    expect(res.matches[0]).toBe('Naruto') // exact match pinned to top
    expect(res.matches).toContain('Naruto: Shippuden')
    expect(res.matches).toContain('Boruto: Naruto Next Generations')
    expect(res.matches).toContain('Road of Naruto')
  })

  it('skipFranchiseDisambig=true bypasses the heuristic and resolves directly', async () => {
    // Regression guard: the disambig-list click handler passes this
    // option so a click on the exact-match item (pinned at top) doesn't
    // re-trigger the heuristic and re-render the same list.
    const res = await lookupOffline('Naruto', { skipFranchiseDisambig: true })
    expect(res.success).toBe(true)
    expect(res.profile?.title).toBe('Naruto')
    expect(res.matches).toEqual(['Naruto'])
  })

  it('skipFranchiseDisambig=false (default) keeps the heuristic active', async () => {
    // Sanity: default behavior unchanged.
    const withoutOpts = await lookupOffline('Naruto')
    const explicitFalse = await lookupOffline('Naruto', { skipFranchiseDisambig: false })
    expect(withoutOpts.profile).toBeNull()
    expect(explicitFalse.profile).toBeNull()
    expect(withoutOpts.matches.length).toBeGreaterThan(1)
    expect(explicitFalse.matches.length).toBeGreaterThan(1)
  })

  it('does NOT trigger franchise-root disambig when exact hit has <2 siblings', async () => {
    // "Cowboy Bebop" is exact and has only ONE other substring hit
    // (the movie) — below the threshold, so we return the single
    // profile directly rather than a disambig list.
    const res = await lookupOffline('Cowboy Bebop')
    expect(res.success).toBe(true)
    expect(res.profile?.title).toBe('Cowboy Bebop')
    expect(res.matches).toEqual(['Cowboy Bebop'])
  })

  it('returns the matches list when substring matches are ambiguous', async () => {
    const res = await lookupOffline('Cowboy')
    // Two Cowboy Bebop entries share the "Cowboy" prefix
    expect(res.success).toBe(true)
    expect(res.profile).toBeNull()
    expect(res.matches).toContain('Cowboy Bebop')
    expect(res.matches).toContain('Cowboy Bebop: The Movie')
  })

  it('sorts substring disambiguation by title length (canonical first)', async () => {
    const res = await lookupOffline('Cowboy')
    expect(res.matches[0]).toBe('Cowboy Bebop') // shorter than "Cowboy Bebop: The Movie"
  })

  it('caps the matches list at 10 entries', async () => {
    // Build 15 pseudo-matches
    const base = makeFixtures()[0]
    const many: Fixture[] = Array.from({ length: 15 }, (_, i) => ({
      ...base,
      id: 100 + i,
      title: `Test Show ${i}`,
      romaji_title: `Test Show ${i}`,
      slug: `${100 + i}-test-show-${i}`,
    }))
    mockProfilesFetch(many)
    _resetCache()

    const res = await lookupOffline('Test Show')
    expect(res.matches.length).toBe(10)
  })
})

describe('offlineEncyclopedia — lookupOffline fuzzy match', () => {
  beforeEach(() => mockProfilesFetch(makeFixtures()))

  it('tolerates a single-character typo on a distinctive title', async () => {
    const res = await lookupOffline('Attak on Titan') // missing 'c'
    expect(res.success).toBe(true)
    expect(res.profile?.title).toBe('Attack on Titan')
  })

  it('returns a "did you mean" list when confidence is borderline', async () => {
    // Query that's close-ish to multiple candidates but not clearly one
    const res = await lookupOffline('Narto')
    expect(res.success).toBe(true)
    expect(res.matches).toContain('Naruto')
  })

  it('returns a not-found error when nothing crosses the fuzzy floor', async () => {
    const res = await lookupOffline('xzqvbmnpplkj')
    expect(res.success).toBe(false)
    expect(res.error).toMatch(/Could not identify/)
  })
})

describe('offlineEncyclopedia — lookupOffline input validation', () => {
  beforeEach(() => mockProfilesFetch(makeFixtures()))

  it('rejects empty input', async () => {
    const res = await lookupOffline('   ')
    expect(res.success).toBe(false)
    expect(res.error).toMatch(/required/)
  })

  it('rejects input over 100 characters', async () => {
    const res = await lookupOffline('a'.repeat(101))
    expect(res.success).toBe(false)
    expect(res.error).toMatch(/100 characters/)
  })
})

describe('offlineEncyclopedia — detectTitleMatch', () => {
  beforeEach(() => mockProfilesFetch(makeFixtures()))

  it('returns matched:true with canonical title on exact match', async () => {
    const res = await detectTitleMatch('Cowboy Bebop')
    expect(res.matched).toBe(true)
    expect(res.title).toBe('Cowboy Bebop')
  })

  it('is case-insensitive', async () => {
    const res = await detectTitleMatch('cowboy bebop')
    expect(res.matched).toBe(true)
    expect(res.title).toBe('Cowboy Bebop')
  })

  it('matches by romaji_title when different from title', async () => {
    const res = await detectTitleMatch('Shingeki no Kyojin')
    expect(res.matched).toBe(true)
    expect(res.title).toBe('Attack on Titan')
  })

  it('does NOT fire on substring match (higher precision than lookupOffline)', async () => {
    // "Cowboy" is a substring of "Cowboy Bebop" — lookupOffline would
    // treat this as a match. detectTitleMatch must NOT, because we're
    // gating a "did you mean this show" hint that erodes trust if it
    // fires on vibe queries that happen to contain a title word.
    const res = await detectTitleMatch('Cowboy')
    expect(res.matched).toBe(false)
    expect(res.title).toBeNull()
  })

  it('does NOT fire on fuzzy match (higher precision than lookupOffline)', async () => {
    // A single-char typo would trip the fuzzy branch in lookupOffline.
    // detectTitleMatch skips fuzzy entirely.
    const res = await detectTitleMatch('Attak on Titan')
    expect(res.matched).toBe(false)
    expect(res.title).toBeNull()
  })

  it('does NOT fire on a genuine vibe query', async () => {
    const res = await detectTitleMatch('gritty military drama')
    expect(res.matched).toBe(false)
    expect(res.title).toBeNull()
  })

  it('returns matched:false on empty input', async () => {
    const res = await detectTitleMatch('  ')
    expect(res.matched).toBe(false)
  })

  it('returns matched:false on input over 100 chars', async () => {
    const res = await detectTitleMatch('a'.repeat(101))
    expect(res.matched).toBe(false)
  })

  it('silently returns matched:false when fetchProfiles fails', async () => {
    // The hint is soft affordance — if the JSON can't load, we just
    // don't render the hint. Must NOT throw.
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 500 }))
    _resetCache()
    const res = await detectTitleMatch('Cowboy Bebop')
    expect(res.matched).toBe(false)
    expect(res.title).toBeNull()
  })
})

describe('offlineEncyclopedia — lookupOffline data-fetch failure', () => {
  it('surfaces a load error when /data/seo_profiles.json fails', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 500 }))
    const res = await lookupOffline('Cowboy Bebop')
    expect(res.success).toBe(false)
    expect(res.error).toMatch(/HTTP 500|Failed to load/)
  })
})
