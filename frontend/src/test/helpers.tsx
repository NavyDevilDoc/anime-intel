import { render, type RenderOptions } from '@testing-library/react'
import { BrowserRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import type { DNAResponse, ScoredResult, SearchResponse, ShowProfile } from '../lib/types'

/** Wraps components with Router + QueryClient providers (no auth — open-access app). */
export function renderWithProviders(ui: ReactNode, options?: RenderOptions) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>{ui}</BrowserRouter>
    </QueryClientProvider>,
    options,
  )
}

/** Wraps components with Router only */
export function renderWithRouter(ui: ReactNode, options?: RenderOptions) {
  return render(<BrowserRouter>{ui}</BrowserRouter>, options)
}

/** Factory for a realistic ShowProfile */
export function makeProfile(overrides?: Partial<ShowProfile>): ShowProfile {
  return {
    id: 1,
    title: 'Steins;Gate',
    studio: 'White Fox',
    quality_score: 9.1,
    scored_by: 1_200_000,
    audience_sentiment: 0.85,
    release_year: 2011,
    synopsis: 'A self-proclaimed mad scientist discovers time travel.',
    audience_consensus: 'A masterful sci-fi thriller with emotional depth.',
    pros: ['Brilliant plot twists', 'Deep character development'],
    cons: ['Slow start'],
    controversy_score: 2,
    semantic_similarity: 0.92,
    mal_rating: 'PG-13 - Teens 13 or older',
    thematic_vibe: 'Sci-Fi, Thriller, Time Travel',
    ...overrides,
  }
}

/** Factory for a realistic ScoredResult */
export function makeResult(overrides?: Partial<ScoredResult>): ScoredResult {
  return {
    ai_reasoning: 'Strong thematic match for time travel and psychological tension.',
    match_confidence: 87,
    controversy_warning: null,
    contains_spoiler: false,
    profile: makeProfile(),
    ...overrides,
  }
}

/** Factory for a /api/search response. Defaults to an empty-pool success;
 *  callers override just the fields their test cares about (typically
 *  `pool` + `pool_size`, or `success` + `error` for failure paths). */
export function makeSearchResponse(overrides?: Partial<SearchResponse>): SearchResponse {
  return {
    success: true,
    pool: [],
    pool_size: 0,
    search_id: null,
    error: null,
    search_remaining: null,
    dna_remaining: null,
    ...overrides,
  }
}

/** Factory for a /api/dna response. Same shape philosophy as
 *  makeSearchResponse — empty-data success by default, callers override
 *  the fields their test cares about (`data` + `data_size` + optional
 *  `intersection_summary`, or `success: false` + `error` for failure
 *  paths). Includes the required-nullable quota fields so the returned
 *  object always satisfies the strict DNAResponse interface. */
export function makeDNAResponse(overrides?: Partial<DNAResponse>): DNAResponse {
  return {
    success: true,
    data: [],
    data_size: 0,
    intersection_summary: null,
    error: null,
    negative_anchors_used: null,
    title_resolution_notes: null,
    search_remaining: null,
    dna_remaining: null,
    ...overrides,
  }
}
