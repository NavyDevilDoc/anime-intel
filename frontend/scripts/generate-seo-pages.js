/**
 * Post-build SEO page generator.
 *
 * Problem: This is a Vite SPA — every route serves the same index.html.
 * Google crawls the raw HTML before JavaScript runs, so every page shows
 * the same base <title> and <meta description> in search results.
 *
 * Solution: After `vite build`, this script generates per-route HTML files
 * (e.g., dist/how-it-works/index.html) with the correct meta tags baked in.
 * Vercel serves static files before rewrites, so crawlers get the right
 * content without waiting for JS rendering. The SPA still hydrates normally.
 *
 * Additionally generates standalone anime profile pages and curated "top"
 * list pages from seo_profiles.json — full HTML content visible to crawlers
 * without any JavaScript execution.
 */

import { readFileSync, writeFileSync, mkdirSync, existsSync } from 'fs'
import { join, dirname } from 'path'
import { fileURLToPath } from 'url'

const __dirname = dirname(fileURLToPath(import.meta.url))
const distDir = join(__dirname, '..', 'dist')
const BASE_URL = 'https://animeintel.app'

// =====================================================================
// PART 1: SPA meta-tag pages (existing behavior)
// =====================================================================

const PAGES = [
  {
    path: 'how-it-works',
    title: 'How It Works — Anime Intel',
    description: 'Three AI-powered discovery modes: semantic vibe search, DNA triangulation for similar shows, and objective rankings. See how Anime Intel finds your next favorite anime.',
    canonical: `${BASE_URL}/how-it-works`,
  },
  {
    path: 'changelog',
    title: 'Changelog — Anime Intel',
    description: 'Latest features and updates to Anime Intel, the AI-powered anime discovery engine.',
    canonical: `${BASE_URL}/changelog`,
  },
  {
    path: 'privacy',
    title: 'Privacy Policy — Anime Intel',
    description: 'Anime Intel privacy policy: what we collect, how we use your data, and your rights.',
    canonical: `${BASE_URL}/privacy`,
  },
  {
    path: 'contact',
    title: 'Contact — Anime Intel',
    description: 'Questions, feedback, or feature requests for Anime Intel? Get in touch.',
    canonical: `${BASE_URL}/contact`,
  },
]

function generateSpaPages() {
  const baseHtml = readFileSync(join(distDir, 'index.html'), 'utf-8')
  let count = 0

  for (const page of PAGES) {
    let html = baseHtml
    html = html.replace(/<title>[^<]*<\/title>/, `<title>${page.title}</title>`)
    html = html.replace(/<meta name="description" content="[^"]*"/, `<meta name="description" content="${page.description}"`)
    html = html.replace(/<meta property="og:title" content="[^"]*"/, `<meta property="og:title" content="${page.title}"`)
    html = html.replace(/<meta property="og:description" content="[^"]*"/, `<meta property="og:description" content="${page.description}"`)
    html = html.replace(/<meta property="og:url" content="[^"]*"/, `<meta property="og:url" content="${page.canonical}"`)
    html = html.replace(/<meta name="twitter:title" content="[^"]*"/, `<meta name="twitter:title" content="${page.title}"`)
    html = html.replace(/<meta name="twitter:description" content="[^"]*"/, `<meta name="twitter:description" content="${page.description}"`)
    if (!html.includes('<link rel="canonical"')) {
      html = html.replace('</head>', `    <link rel="canonical" href="${page.canonical}" />\n  </head>`)
    }
    const outDir = join(distDir, page.path)
    mkdirSync(outDir, { recursive: true })
    writeFileSync(join(outDir, 'index.html'), html)
    count++
  }
  return count
}


// =====================================================================
// PART 2: Anime profile pages (standalone HTML, no React)
// =====================================================================

/** Escape HTML special characters to prevent XSS from DB content. */
function esc(str) {
  if (!str) return ''
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#x27;')
}

/** Truncate text to a max length, ending at a word boundary. */
function truncate(str, max) {
  if (!str || str.length <= max) return str || ''
  const cut = str.slice(0, max)
  const last = cut.lastIndexOf(' ')
  return (last > 0 ? cut.slice(0, last) : cut) + '...'
}

/** Map a genre string to a slug usable in /top/ URLs. */
function genreToSlug(genre) {
  return genre.toLowerCase().replace(/[\s]+/g, '-').replace(/[^a-z0-9-]/g, '')
}

/** Determine sentiment class for styling. */
function sentimentClass(val) {
  if (val == null) return 'stat-neutral'
  if (val >= 0.2) return 'stat-positive'
  if (val <= -0.1) return 'stat-negative'
  return 'stat-neutral'
}

/** Format sentiment as readable text. */
function formatSentiment(val) {
  if (val == null) return 'N/A'
  if (val >= 0.3) return `${val.toFixed(2)} (Positive)`
  if (val >= 0.1) return `${val.toFixed(2)} (Mild Positive)`
  if (val >= -0.1) return `${val.toFixed(2)} (Mixed)`
  if (val >= -0.3) return `${val.toFixed(2)} (Mild Negative)`
  return `${val.toFixed(2)} (Negative)`
}

/** Genres we generate /top/ pages for — used for internal linking. */
const LINKABLE_GENRES = new Set([
  'action', 'romance', 'comedy', 'horror', 'mecha', 'sports', 'fantasy',
  'sci-fi', 'thriller', 'psychological', 'slice of life', 'drama',
  'mystery', 'isekai', 'adventure', 'supernatural',
])

/**
 * Build the nav bar HTML shared across all static pages.
 */
function navHtml() {
  return `<nav class="nav">
  <a href="${BASE_URL}" class="nav-brand">Anime Intel</a>
  <div class="nav-links">
    <a href="${BASE_URL}/how-it-works">How It Works</a>
    <a href="${BASE_URL}/changelog">Changelog</a>
  </div>
</nav>`
}

/**
 * Build the footer HTML shared across all static pages.
 */
function footerHtml() {
  return `<footer class="footer">
  <div>Data sourced from <a href="https://myanimelist.net/" rel="noopener" target="_blank">MyAnimeList</a>. Streaming data from <a href="https://anilist.co/" rel="noopener" target="_blank">AniList</a>.</div>
  <div class="footer-links">
    <a href="${BASE_URL}/privacy">Privacy</a>
    <a href="${BASE_URL}/contact">Contact</a>
    <a href="${BASE_URL}/how-it-works">How It Works</a>
  </div>
</footer>`
}

/**
 * Parse a MAL rating string into its short form + CSS modifier class.
 * Mirrors the TS parseRating in src/lib/ratingDisplay.ts — closed enum,
 * no regex needed (Phase A confirmed format is bulletproof).
 * Returns null for null/empty/unknown so the caller can skip rendering.
 */
const RATING_BUCKET_TO_CLASS = {
  'G':     'rating-pill-g',
  'PG':    'rating-pill-pg',
  'PG-13': 'rating-pill-pg13',
  'R':     'rating-pill-r',
  'R+':    'rating-pill-rplus',
  'Rx':    'rating-pill-rx',
}

function parseRating(raw) {
  if (!raw) return null
  const idx = raw.indexOf(' - ')
  if (idx === -1) return null
  const short = raw.slice(0, idx)
  if (!(short in RATING_BUCKET_TO_CLASS)) return null
  return { short, full: raw, cssClass: RATING_BUCKET_TO_CLASS[short] }
}

/**
 * Inline pill HTML for a profile's mal_rating. Returns '' (no node)
 * when the rating is null, empty, or unparseable.
 */
function ratingPillHtml(profile) {
  const parsed = parseRating(profile.mal_rating)
  if (!parsed) return ''
  return `<span class="rating-pill ${parsed.cssClass}" title="${esc(parsed.full)}" aria-label="${esc(parsed.full)}">${esc(parsed.short)}</span>`
}

/**
 * Build a JSON-LD script tag for TVSeries structured data.
 */
function animeLd(profile) {
  const ld = {
    '@context': 'https://schema.org',
    '@type': 'TVSeries',
    name: profile.title,
    description: truncate(profile.synopsis, 500),
    genre: profile.genres ? profile.genres.split(', ') : [],
  }
  if (profile.release_year) ld.datePublished = String(profile.release_year)
  if (profile.studio) {
    ld.productionCompany = { '@type': 'Organization', name: profile.studio }
  }
  if (profile.quality_score > 0 && profile.scored_by > 0) {
    ld.aggregateRating = {
      '@type': 'AggregateRating',
      ratingValue: profile.quality_score,
      bestRating: 10,
      worstRating: 1,
      ratingCount: profile.scored_by,
    }
  }
  // Schema.org contentRating — surfaces the MAL rating in Google's SERP
  // chips when the verbatim string parses to a known bucket. Skips
  // unknown/empty/null to avoid polluting structured data with garbage.
  const parsedRating = parseRating(profile.mal_rating)
  if (parsedRating) {
    ld.contentRating = parsedRating.full
  }
  return `<script type="application/ld+json">${JSON.stringify(ld)}</script>`
}

/**
 * Generate the full standalone HTML for one anime profile page.
 */
function animePageHtml(profile) {
  const title = esc(profile.title)
  const metaDesc = truncate(profile.consensus_summary || profile.synopsis, 155)
  const canonical = `${BASE_URL}/anime/${profile.slug}`

  // Genre tag pills
  const genreTags = profile.genres
    ? profile.genres.split(', ').map(g => `<span class="genre-tag">${esc(g)}</span>`).join('')
    : ''

  // Stats row
  const moodVal = profile.avg_sentiment
  const moodClass = sentimentClass(moodVal)
  const moodText = formatSentiment(moodVal)
  const controversyVal = profile.controversy_score
  const controversyText = controversyVal != null ? `${controversyVal} / 10` : 'N/A'

  // Pros / Cons
  const prosHtml = (profile.pros && profile.pros.length > 0)
    ? profile.pros.map(p => `<li>${esc(p)}</li>`).join('')
    : '<li>No data available</li>'
  const consHtml = (profile.cons && profile.cons.length > 0)
    ? profile.cons.map(c => `<li>${esc(c)}</li>`).join('')
    : '<li>No data available</li>'

  // Internal links to genre pages
  const genreLinks = profile.genres
    ? profile.genres.split(', ')
        .map(g => g.trim().toLowerCase())
        .filter(g => LINKABLE_GENRES.has(g))
        .map(g => {
          const slug = genreToSlug(g)
          const label = g.charAt(0).toUpperCase() + g.slice(1)
          return `<a href="${BASE_URL}/top/${slug}-anime" class="internal-link">Top ${esc(label)} Anime</a>`
        })
        .join('')
    : ''

  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>${title} — Anime Intel</title>
  <meta name="description" content="${esc(metaDesc)}">
  <meta property="og:title" content="${title} — Anime Intel">
  <meta property="og:description" content="${esc(metaDesc)}">
  <meta property="og:type" content="website">
  <meta property="og:url" content="${canonical}">
  <meta name="twitter:card" content="summary">
  <meta name="twitter:title" content="${title} — Anime Intel">
  <meta name="twitter:description" content="${esc(metaDesc)}">
  <link rel="canonical" href="${canonical}">
  <link rel="icon" type="image/svg+xml" href="/favicon.svg">
  <link rel="stylesheet" href="/seo-page.css">
  ${animeLd(profile)}
</head>
<body>
${navHtml()}
<main class="container">
  <div class="show-header">
    <h1 class="show-title">${title}${ratingPillHtml(profile)}</h1>
    <div class="show-meta">
      ${profile.studio ? `<span>${esc(profile.studio)}</span>` : ''}
      ${profile.season ? `<span>${esc(profile.season.replace('_', ' '))}</span>` : (profile.release_year ? `<span>${profile.release_year}</span>` : '')}
    </div>
  </div>

  ${genreTags ? `<div class="genre-tags">${genreTags}</div>` : ''}

  <div class="hero-row${profile.poster_url_large ? '' : ' hero-row-no-poster'}">
    ${profile.poster_url_large ? `<div class="hero-poster-col">
      <a href="https://myanimelist.net/anime/${profile.id}" target="_blank" rel="noopener noreferrer" class="hero-poster-link" aria-label="View ${esc(title)} on MyAnimeList">
        <img src="${esc(profile.poster_url_large)}" alt="${esc(title)} cover art" loading="lazy" onerror="var r=this.closest('.hero-row');var c=this.closest('.hero-poster-col');if(r)r.classList.add('hero-row-no-poster');if(c)c.remove();">
        <span class="hero-poster-attribution">&copy; MAL</span>
      </a>
    </div>` : ''}
    <div class="stats-row">
      <div class="stat-card">
        <div class="stat-value stat-accent">${profile.quality_score > 0 ? profile.quality_score : 'N/A'}</div>
        <div class="stat-label">Quality Score</div>
      </div>
      <div class="stat-card">
        <div class="stat-value ${moodClass}">${moodText}</div>
        <div class="stat-label">Audience Mood</div>
      </div>
      <div class="stat-card">
        <div class="stat-value">${controversyText}</div>
        <div class="stat-label">Controversy</div>
      </div>
      <div class="stat-card">
        <div class="stat-value">${profile.scored_by ? profile.scored_by.toLocaleString() : 'N/A'}</div>
        <div class="stat-label">MAL Ratings</div>
      </div>
    </div>
  </div>

  ${profile.synopsis ? `<div class="section">
    <h2 class="section-title">Synopsis <span class="section-attribution">&mdash; from MyAnimeList</span></h2>
    <p>${esc(profile.synopsis)}</p>
  </div>` : ''}

  ${profile.consensus_summary ? `<div class="section">
    <h2 class="section-title">Audience Consensus</h2>
    <p>${esc(profile.consensus_summary)}</p>
  </div>` : ''}

  <div class="pros-cons">
    <div class="section">
      <h2 class="section-title pros-title">Strengths</h2>
      <ul>${prosHtml}</ul>
    </div>
    <div class="section">
      <h2 class="section-title cons-title">Weaknesses</h2>
      <ul>${consHtml}</ul>
    </div>
  </div>

  <div class="links-row">
    <a href="${BASE_URL}/try?mode=dna&amp;q=${encodeURIComponent(profile.title)}" class="cta-button">Find Similar Shows</a>
    ${profile.thematic_vibe ? `<a href="${BASE_URL}/try?q=${encodeURIComponent(profile.thematic_vibe)}" class="cta-button-secondary">Explore this vibe</a>` : ''}
    <a href="https://myanimelist.net/anime/${profile.id}" class="mal-link" rel="noopener" target="_blank">View on MyAnimeList &#8599;</a>
  </div>

  ${genreLinks ? `<div class="internal-links">${genreLinks}</div>` : ''}
</main>
${footerHtml()}
</body>
</html>`
}


function generateAnimePages(profiles) {
  let count = 0
  for (const profile of profiles) {
    const html = animePageHtml(profile)
    const outDir = join(distDir, 'anime', profile.slug)
    mkdirSync(outDir, { recursive: true })
    writeFileSync(join(outDir, 'index.html'), html)
    count++
  }
  return count
}


// =====================================================================
// PART 3: Curated "Top" list pages
// =====================================================================

/**
 * Genre definitions for curated list pages.
 * label: display name, slug: URL segment, malTags: tags to match in the genres field.
 */
const GENRE_DEFS = [
  { label: 'Action', slug: 'action', malTags: ['Action'] },
  { label: 'Romance', slug: 'romance', malTags: ['Romance'] },
  { label: 'Comedy', slug: 'comedy', malTags: ['Comedy'] },
  { label: 'Horror', slug: 'horror', malTags: ['Horror'] },
  { label: 'Mecha', slug: 'mecha', malTags: ['Mecha'] },
  { label: 'Sports', slug: 'sports', malTags: ['Sports'] },
  { label: 'Fantasy', slug: 'fantasy', malTags: ['Fantasy'] },
  { label: 'Sci-Fi', slug: 'sci-fi', malTags: ['Sci-Fi'] },
  { label: 'Thriller', slug: 'thriller', malTags: ['Suspense', 'Thriller'] },
  { label: 'Psychological', slug: 'psychological', malTags: ['Psychological'] },
  { label: 'Slice of Life', slug: 'slice-of-life', malTags: ['Slice of Life'] },
  { label: 'Drama', slug: 'drama', malTags: ['Drama'] },
  { label: 'Mystery', slug: 'mystery', malTags: ['Mystery'] },
  { label: 'Isekai', slug: 'isekai', malTags: ['Isekai'] },
  { label: 'Adventure', slug: 'adventure', malTags: ['Adventure'] },
  { label: 'Supernatural', slug: 'supernatural', malTags: ['Supernatural'] },
]

const DECADES = [
  { label: 'of All Time', slug: '', yearMin: null, yearMax: null },
  { label: 'of the 1990s', slug: '-1990s', yearMin: 1990, yearMax: 1999 },
  { label: 'of the 2000s', slug: '-2000s', yearMin: 2000, yearMax: 2009 },
  { label: 'of the 2010s', slug: '-2010s', yearMin: 2010, yearMax: 2019 },
  { label: 'of the 2020s', slug: '-2020s', yearMin: 2020, yearMax: 2029 },
]

const MIN_SHOWS_FOR_PAGE = 5
const TOP_N = 20

/**
 * Check if a profile's genres field contains any of the target MAL tags.
 */
function matchesGenre(profile, malTags) {
  if (!profile.genres) return false
  const genresLower = profile.genres.toLowerCase()
  return malTags.some(tag => genresLower.includes(tag.toLowerCase()))
}

/**
 * Filter and sort profiles for a genre + decade combination.
 */
function getTopShows(profiles, malTags, yearMin, yearMax) {
  return profiles
    .filter(p => {
      if (!matchesGenre(p, malTags)) return false
      if (p.quality_score <= 0) return false
      if (yearMin != null && (p.release_year == null || p.release_year < yearMin)) return false
      if (yearMax != null && (p.release_year == null || p.release_year > yearMax)) return false
      return true
    })
    .sort((a, b) => b.quality_score - a.quality_score)
    .slice(0, TOP_N)
}

/**
 * Build JSON-LD ItemList for a curated list page.
 */
function listLd(pageTitle, shows) {
  const ld = {
    '@context': 'https://schema.org',
    '@type': 'ItemList',
    name: pageTitle,
    numberOfItems: shows.length,
    itemListElement: shows.map((s, i) => ({
      '@type': 'ListItem',
      position: i + 1,
      url: `${BASE_URL}/anime/${s.slug}`,
      name: s.title,
    })),
  }
  return `<script type="application/ld+json">${JSON.stringify(ld)}</script>`
}

/**
 * Generate the full HTML for one curated "Top" list page.
 */
function topListPageHtml(genre, decade, shows, totalMatching) {
  const pageTitle = `Best ${genre.label} Anime ${decade.label}`
  const pageSlug = `${genre.slug}-anime${decade.slug}`
  const canonical = `${BASE_URL}/top/${pageSlug}`
  const yearRange = decade.yearMin
    ? `${decade.yearMin}–${decade.yearMax}`
    : '1990–present'
  const metaDesc = `Top ${shows.length} ${genre.label.toLowerCase()} anime ranked by community ratings from MyAnimeList. Curated from ${totalMatching} titles spanning ${yearRange}.`

  // Ranked items
  const itemsHtml = shows.map((s, i) => {
    const summary = truncate(s.consensus_summary || s.synopsis, 120)
    return `<div class="ranked-item">
      <div class="rank-number">${i + 1}</div>
      <div class="ranked-item-content">
        <div class="ranked-item-title"><a href="${BASE_URL}/anime/${s.slug}">${esc(s.title)}</a></div>
        <div class="ranked-item-meta">${s.studio ? esc(s.studio) + ' · ' : ''}${s.release_year || 'Unknown'} · ${s.scored_by ? s.scored_by.toLocaleString() + ' ratings' : ''}</div>
        ${summary ? `<div class="ranked-item-summary">${esc(summary)}</div>` : ''}
      </div>
      <div class="ranked-item-score">${s.quality_score}</div>
    </div>`
  }).join('\n  ')

  // Related: same genre, different decades
  const relatedDecades = DECADES
    .filter(d => d.slug !== decade.slug)
    .map(d => {
      const slug = `${genre.slug}-anime${d.slug}`
      return `<a href="${BASE_URL}/top/${slug}" class="internal-link">Best ${genre.label} Anime ${d.label}</a>`
    })
    .join('')

  // Related: different genres, same decade
  const relatedGenres = GENRE_DEFS
    .filter(g => g.slug !== genre.slug)
    .slice(0, 6)
    .map(g => {
      const slug = `${g.slug}-anime${decade.slug}`
      return `<a href="${BASE_URL}/top/${slug}" class="internal-link">Best ${g.label} Anime${decade.slug ? ' ' + decade.label : ''}</a>`
    })
    .join('')

  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>${esc(pageTitle)} — Anime Intel</title>
  <meta name="description" content="${esc(metaDesc)}">
  <meta property="og:title" content="${esc(pageTitle)} — Anime Intel">
  <meta property="og:description" content="${esc(metaDesc)}">
  <meta property="og:type" content="website">
  <meta property="og:url" content="${canonical}">
  <meta name="twitter:card" content="summary">
  <meta name="twitter:title" content="${esc(pageTitle)} — Anime Intel">
  <meta name="twitter:description" content="${esc(metaDesc)}">
  <link rel="canonical" href="${canonical}">
  <link rel="icon" type="image/svg+xml" href="/favicon.svg">
  <link rel="stylesheet" href="/seo-page.css">
  ${listLd(pageTitle, shows)}
</head>
<body>
${navHtml()}
<main class="container">
  <div class="list-header">
    <h1 class="list-title">${esc(pageTitle)}</h1>
    <p class="list-intro">Ranked by community ratings from MyAnimeList across ${totalMatching} ${genre.label.toLowerCase()} titles spanning ${yearRange}.</p>
  </div>

  <div class="ranked-list">
  ${itemsHtml}
  </div>

  <div class="links-row" style="margin-top:1.5rem">
    <a href="${BASE_URL}/try" class="cta-button">Discover More with AI Search</a>
  </div>

  <div class="related-section">
    <h2 class="related-title">More ${esc(genre.label)} Anime</h2>
    <div class="internal-links">${relatedDecades}</div>
  </div>

  <div class="related-section">
    <h2 class="related-title">Other Genres${decade.slug ? ' ' + decade.label : ''}</h2>
    <div class="internal-links">${relatedGenres}</div>
  </div>
</main>
${footerHtml()}
</body>
</html>`
}


/**
 * Generate the /top/index.html landing page — Top Lists browse surface.
 *
 * Static HTML with a decade selector (as separate anchor links) and the
 * 16-genre chip grid, so search crawlers can index the landing itself
 * (previously only /top/{slug} pages were emitted, and /top was a bare
 * SPA fallback). Complements TopLists.tsx which handles the interactive
 * decade filtering client-side; the static version emits one full
 * "All Time" chip grid plus per-decade lists so bots see every path
 * to every downstream page without needing JS.
 */
function topListsLandingHtml() {
  const pageTitle = 'Top Anime Lists — Browse by Genre and Decade'
  const metaDesc =
    'Browse 79 curated top-anime lists across 16 genres and 5 decades. ' +
    'Ranked by community ratings from MyAnimeList, one entry per franchise.'
  const canonical = `${BASE_URL}/top`

  // Render one section per decade so every /top/{slug} target is a
  // static anchor a crawler can follow without executing JS.
  const decadeSections = DECADES.map(decade => {
    const decadeTitle = decade.slug
      ? `Best of the ${decade.label.replace('of the ', '')}`
      : 'Best of All Time'
    const chips = GENRE_DEFS.map(genre => {
      const slug = `${genre.slug}-anime${decade.slug}`
      return `<a href="${BASE_URL}/top/${slug}" class="internal-link">Best ${esc(genre.label)} Anime</a>`
    }).join('')
    return `<div class="related-section">
    <h2 class="related-title">${esc(decadeTitle)}</h2>
    <div class="internal-links">${chips}</div>
  </div>`
  }).join('\n  ')

  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>${esc(pageTitle)} | Anime Intel</title>
  <meta name="description" content="${esc(metaDesc)}">
  <meta property="og:title" content="${esc(pageTitle)} | Anime Intel">
  <meta property="og:description" content="${esc(metaDesc)}">
  <meta property="og:type" content="website">
  <meta property="og:url" content="${canonical}">
  <meta name="twitter:card" content="summary">
  <meta name="twitter:title" content="${esc(pageTitle)} | Anime Intel">
  <meta name="twitter:description" content="${esc(metaDesc)}">
  <link rel="canonical" href="${canonical}">
  <link rel="icon" type="image/svg+xml" href="/favicon.svg">
  <link rel="stylesheet" href="/seo-page.css">
</head>
<body>
${navHtml()}
<main class="container">
  <div class="list-header">
    <h1 class="list-title">Top Anime Lists</h1>
    <p class="list-intro">Browse curated top-anime rankings by genre and decade. Each list is ranked by community ratings with one entry per franchise — no repeat seasons crowding the top 20.</p>
  </div>

  <div class="links-row" style="margin-top:1rem">
    <a href="${BASE_URL}/try" class="cta-button">Discover More with AI Search</a>
  </div>

  ${decadeSections}
</main>
${footerHtml()}
</body>
</html>`
}

/**
 * Emit the /top/index.html landing to dist/top/index.html.
 * Returns 1 on success (used by main for reporting).
 */
function generateTopListsLanding() {
  const outDir = join(distDir, 'top')
  mkdirSync(outDir, { recursive: true })
  writeFileSync(join(outDir, 'index.html'), topListsLandingHtml())
  return 1
}


/**
 * Generate all curated "Top" list pages.
 * Returns { count, slugs } for sitemap integration.
 */
function generateTopListPages(profiles) {
  let count = 0
  const slugs = []

  for (const genre of GENRE_DEFS) {
    for (const decade of DECADES) {
      const allMatching = profiles.filter(p => {
        if (!matchesGenre(p, genre.malTags)) return false
        if (p.quality_score <= 0) return false
        if (decade.yearMin != null && (p.release_year == null || p.release_year < decade.yearMin)) return false
        if (decade.yearMax != null && (p.release_year == null || p.release_year > decade.yearMax)) return false
        return true
      })

      if (allMatching.length < MIN_SHOWS_FOR_PAGE) continue

      // Item 1 — franchise dedup. Sort by quality DESC, then first-seen-wins
      // on the series_id cluster key = peak entry per franchise. Matches the
      // engine's `_collapse_same_series` convention on Find-a-Show + SLT so
      // the same franchise doesn't appear as "Gintama°" on one surface and
      // "Gintama Season 4" on another. Falls back to profile.id for the
      // 54/6347 profiles without series_context (treated as singletons —
      // no false dedup).
      // totalMatching intentionally still uses allMatching.length (raw
      // pre-dedup count) so the meta copy "Curated from N titles" stays
      // honest — we did look at N titles to pick these franchises.
      const sortedByScore = [...allMatching].sort(
        (a, b) => b.quality_score - a.quality_score,
      )
      const seenClusters = new Set()
      const shows = []
      for (const p of sortedByScore) {
        const clusterKey = p.series_context?.series_id ?? `singleton-${p.id}`
        if (seenClusters.has(clusterKey)) continue
        seenClusters.add(clusterKey)
        shows.push(p)
        if (shows.length >= TOP_N) break
      }

      const pageSlug = `${genre.slug}-anime${decade.slug}`
      const html = topListPageHtml(genre, decade, shows, allMatching.length)
      const outDir = join(distDir, 'top', pageSlug)
      mkdirSync(outDir, { recursive: true })
      writeFileSync(join(outDir, 'index.html'), html)
      slugs.push(pageSlug)
      count++
    }
  }

  return { count, slugs }
}


// =====================================================================
// PART 4: Sitemap generation
// =====================================================================

function generateSitemap(profiles, topListSlugs) {
  const urls = []

  // Static pages
  urls.push({ loc: BASE_URL + '/', priority: '1.0' })
  for (const page of PAGES) {
    urls.push({ loc: page.canonical, priority: '0.5' })
  }

  // Top Lists landing (browse surface)
  urls.push({ loc: `${BASE_URL}/top`, priority: '0.7' })

  // Curated list pages
  for (const slug of topListSlugs) {
    urls.push({ loc: `${BASE_URL}/top/${slug}`, priority: '0.8' })
  }

  // Anime profile pages
  for (const profile of profiles) {
    urls.push({
      loc: `${BASE_URL}/anime/${profile.slug}`,
      priority: '0.6',
    })
  }

  const xml = `<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
${urls.map(u => `  <url>
    <loc>${u.loc}</loc>
    <priority>${u.priority}</priority>
  </url>`).join('\n')}
</urlset>`

  writeFileSync(join(distDir, 'sitemap.xml'), xml)
  return urls.length
}


// =====================================================================
// MAIN
// =====================================================================

// Load profile data
const dataPath = join(distDir, 'data', 'seo_profiles.json')
let profiles = []

if (existsSync(dataPath)) {
  const raw = readFileSync(dataPath, 'utf-8')
  const data = JSON.parse(raw)
  profiles = data.anime || []
  console.log(`SEO: Loaded ${profiles.length} anime profiles from seo_profiles.json`)
} else {
  console.warn('SEO: seo_profiles.json not found — skipping anime pages and sitemap.')
}

// Generate SPA meta pages (existing)
const spaCount = generateSpaPages()
console.log(`SEO: Generated ${spaCount} SPA meta page(s).`)

// Generate anime profile pages + top list pages + sitemap
if (profiles.length > 0) {
  const animeCount = generateAnimePages(profiles)
  console.log(`SEO: Generated ${animeCount} anime profile page(s).`)

  // Generate curated "Top" list pages
  const { count: topCount, slugs: topSlugs } = generateTopListPages(profiles)
  console.log(`SEO: Generated ${topCount} curated "Top" list page(s).`)

  // Generate the /top/index.html browse landing (Top Lists discoverability)
  generateTopListsLanding()
  console.log(`SEO: Generated /top/index.html landing.`)

  // Generate sitemap (replaces static sitemap.xml)
  const sitemapCount = generateSitemap(profiles, topSlugs)
  console.log(`SEO: Generated sitemap.xml with ${sitemapCount} URL(s).`)
}
