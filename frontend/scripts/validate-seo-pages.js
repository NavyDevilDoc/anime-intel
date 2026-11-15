/**
 * Automated validation for generated SEO pages.
 * Run after `npm run build` to verify page structure and content integrity.
 *
 * Usage: node scripts/validate-seo-pages.js
 */

import { readFileSync, existsSync } from 'fs'
import { join, dirname } from 'path'
import { fileURLToPath } from 'url'
import { globSync } from 'fs'

const __dirname = dirname(fileURLToPath(import.meta.url))
const distDir = join(__dirname, '..', 'dist')
const dataPath = join(distDir, 'data', 'seo_profiles.json')

let passed = 0
let failed = 0

function assert(condition, testName) {
  if (condition) {
    passed++
  } else {
    failed++
    console.error(`  FAIL: ${testName}`)
  }
}

// Load profile data for comparison
const data = JSON.parse(readFileSync(dataPath, 'utf-8'))
const profiles = data.anime

console.log(`\nValidating SEO pages (${profiles.length} profiles)...\n`)

// --- Test 1: Page count matches profile count ---
let animePageCount = 0
for (const p of profiles) {
  if (existsSync(join(distDir, 'anime', p.slug, 'index.html'))) animePageCount++
}
assert(animePageCount === profiles.length, `Page count: expected ${profiles.length}, got ${animePageCount}`)
console.log(`  Page count: ${animePageCount} / ${profiles.length}`)

// --- Tests 2-9: Sample 20 pages for structural validation ---
const sampleSize = Math.min(20, profiles.length)
const step = Math.floor(profiles.length / sampleSize)
const samples = []
for (let i = 0; i < sampleSize; i++) {
  samples.push(profiles[i * step])
}
// Always include Attack on Titan if present
const aot = profiles.find(p => p.id === 16498)
if (aot && !samples.find(p => p.id === 16498)) samples.push(aot)

let missingTitle = 0, missingDesc = 0, missingCanonical = 0
let missingLd = 0, missingMal = 0, missingCta = 0, emptyContent = 0
let missingExploreVibe = 0

for (const p of samples) {
  const filePath = join(distDir, 'anime', p.slug, 'index.html')
  if (!existsSync(filePath)) continue
  const html = readFileSync(filePath, 'utf-8')

  // Test 2: Has <title>
  if (!html.includes(`<title>${p.title.replace(/&/g, '&amp;')} — Anime Intel</title>`) &&
      !html.includes(`<title>${p.title} — Anime Intel</title>`)) {
    // Try HTML-escaped version
    if (!/<title>[^<]+<\/title>/.test(html)) missingTitle++
  }

  // Test 3: Has <meta description>
  if (!html.includes('<meta name="description"')) missingDesc++

  // Test 4: Has canonical URL
  const expectedCanonical = `https://animeintel.app/anime/${p.slug}`
  if (!html.includes(`<link rel="canonical" href="${expectedCanonical}"`)) missingCanonical++

  // Test 5: Has JSON-LD
  if (!html.includes('<script type="application/ld+json">')) missingLd++

  // Test 6: Has content (synopsis or consensus)
  if (!html.includes('section-title">Synopsis') && !html.includes('section-title">Audience Consensus')) {
    if (!p.synopsis && !p.consensus_summary) {
      // Acceptable — no data available
    } else {
      emptyContent++
    }
  }

  // Test 7: Has MAL link
  if (!html.includes(`https://myanimelist.net/anime/${p.id}`)) missingMal++

  // Test 8: Has CTA to /try (SLT mode — "Find Similar Shows" routes to Shows Like This)
  if (!html.includes('/try?mode=dna&amp;q=')) missingCta++

  // Test 9: Has "Explore this vibe" secondary CTA when thematic_vibe is populated.
  // Suppressed on shows with empty/null thematic_vibe (currently 0 — distiller
  // guarantees the field — but guarded here against future schema drift).
  if (p.thematic_vibe && !html.includes('class="cta-button-secondary"')) missingExploreVibe++
}

assert(missingTitle === 0, `All samples have <title> (${missingTitle} missing)`)
assert(missingDesc === 0, `All samples have <meta description> (${missingDesc} missing)`)
assert(missingCanonical === 0, `All samples have correct canonical URL (${missingCanonical} missing)`)
assert(missingLd === 0, `All samples have JSON-LD (${missingLd} missing)`)
assert(emptyContent === 0, `All samples with data have visible content (${emptyContent} empty)`)
assert(missingMal === 0, `All samples have MAL link (${missingMal} missing)`)
assert(missingCta === 0, `All samples have CTA link to /try (${missingCta} missing)`)
assert(missingExploreVibe === 0, `All samples with thematic_vibe have Explore-this-vibe secondary CTA (${missingExploreVibe} missing)`)

// --- Tests 9-15: Top list page validation ---
import { readdirSync } from 'fs'

const topDir = join(distDir, 'top')
let topPageCount = 0
if (existsSync(topDir)) {
  const topDirs = readdirSync(topDir, { withFileTypes: true }).filter(d => d.isDirectory())
  topPageCount = topDirs.length
}
assert(topPageCount >= 60, `Top list page count >= 60 (got ${topPageCount})`)
assert(topPageCount <= 80, `Top list page count <= 80 (got ${topPageCount})`)
console.log(`  Top list pages: ${topPageCount}`)

// Sample 5 top list pages for structural checks
const topSamples = ['action-anime', 'romance-anime-2020s', 'sports-anime', 'drama-anime-2010s', 'sci-fi-anime-1990s']
let topMissingTitle = 0, topMissingLd = 0, topThinPage = 0, topBadSort = 0, topMissingCta = 0
let topMissingAnimeLinks = 0, topMissingRelated = 0

for (const slug of topSamples) {
  const filePath = join(distDir, 'top', slug, 'index.html')
  if (!existsSync(filePath)) continue
  const html = readFileSync(filePath, 'utf-8')

  // Test 10: Has title with "Best ... Anime"
  if (!/<title>Best .+ Anime/.test(html)) topMissingTitle++

  // Test 11: Has JSON-LD ItemList
  if (!html.includes('"@type":"ItemList"')) topMissingLd++

  // Test 12: Has at least 5 ranked items (no thin pages)
  const rankMatches = html.match(/class="rank-number"/g)
  if (!rankMatches || rankMatches.length < 5) topThinPage++

  // Test 13: Scores are sorted descending
  const scoreMatches = [...html.matchAll(/class="ranked-item-score">([0-9.]+)</g)]
  const scores = scoreMatches.map(m => parseFloat(m[1]))
  let sorted = true
  for (let i = 1; i < scores.length; i++) {
    if (scores[i] > scores[i - 1] + 0.01) { sorted = false; break }
  }
  if (!sorted) topBadSort++

  // Test 14: Ranked show titles link to /anime/ pages
  const animeLinks = html.match(/href="https:\/\/animeintel\.app\/anime\//g)
  if (!animeLinks || animeLinks.length < 5) topMissingAnimeLinks++

  // Test 15: Has CTA to /try
  if (!html.includes('/try"')) topMissingCta++

  // Test 16: Has related category links
  if (!html.includes('related-section')) topMissingRelated++
}

assert(topMissingTitle === 0, `Top pages have "Best ... Anime" title (${topMissingTitle} missing)`)
assert(topMissingLd === 0, `Top pages have ItemList JSON-LD (${topMissingLd} missing)`)
assert(topThinPage === 0, `Top pages have >= 5 ranked items (${topThinPage} thin)`)
assert(topBadSort === 0, `Top pages sorted by score descending (${topBadSort} unsorted)`)
assert(topMissingAnimeLinks === 0, `Top pages link to anime profile pages (${topMissingAnimeLinks} missing)`)
assert(topMissingCta === 0, `Top pages have CTA to /try (${topMissingCta} missing)`)
assert(topMissingRelated === 0, `Top pages have related category links (${topMissingRelated} missing)`)

// --- Test 17: Franchise dedup on top-list pages (Item 1) ---
// Each ranked show on a top-list page should be from a distinct series_id
// cluster. Pre-Item-1 the page allowed 8 Gintama entries + 3 AoT entries
// on Top Action; post-fix, each franchise gets exactly one representative
// (the peak-scored entry). Test resolves the /anime/{slug} links back to
// profiles and asserts distinct series_ids across each page.
const profileBySlug = new Map(profiles.map(p => [p.slug, p]))
let topDupFranchises = 0
const dupDetails = []
for (const slug of topSamples) {
  const filePath = join(distDir, 'top', slug, 'index.html')
  if (!existsSync(filePath)) continue
  const html = readFileSync(filePath, 'utf-8')
  const slugMatches = [...html.matchAll(/href="https:\/\/animeintel\.app\/anime\/([^"]+)"/g)]
  const rankedSlugs = slugMatches.map(m => m[1])
  const clusterKeys = rankedSlugs
    .map(s => profileBySlug.get(s))
    .filter(Boolean)
    .map(p => p.series_context?.series_id ?? `singleton-${p.id}`)
  const uniqueClusters = new Set(clusterKeys)
  if (uniqueClusters.size !== clusterKeys.length) {
    topDupFranchises++
    dupDetails.push(`${slug}: ${clusterKeys.length} ranked slots, ${uniqueClusters.size} distinct franchises`)
  }
}
assert(topDupFranchises === 0, `Top pages have one entry per franchise (${topDupFranchises} pages with duplicates${dupDetails.length ? ': ' + dupDetails.join('; ') : ''})`)

// --- Test 17+: Sitemap validation ---
const sitemapPath = join(distDir, 'sitemap.xml')
assert(existsSync(sitemapPath), 'sitemap.xml exists')
if (existsSync(sitemapPath)) {
  const sitemap = readFileSync(sitemapPath, 'utf-8')
  const urlMatches = sitemap.match(/<loc>/g)
  const urlCount = urlMatches ? urlMatches.length : 0
  // Should have: 1 homepage + 5 static + N anime pages + M top list pages
  const expectedMin = profiles.length + 5 + topPageCount
  assert(urlCount >= expectedMin, `Sitemap has enough URLs: ${urlCount} >= ${expectedMin}`)
  console.log(`  Sitemap URLs: ${urlCount}`)

  // Check no duplicate URLs
  const locRegex = /<loc>([^<]+)<\/loc>/g
  const allUrls = []
  let match
  while ((match = locRegex.exec(sitemap)) !== null) {
    allUrls.push(match[1])
  }
  const uniqueUrls = new Set(allUrls)
  assert(allUrls.length === uniqueUrls.size, `No duplicate sitemap URLs (${allUrls.length} total, ${uniqueUrls.size} unique)`)
}

// --- Summary ---
console.log(`\n${'='.repeat(50)}`)
console.log(`  Results: ${passed} passed, ${failed} failed`)
console.log(`${'='.repeat(50)}\n`)

process.exit(failed > 0 ? 1 : 0)
