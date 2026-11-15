import { useState, useCallback, useEffect } from 'react'
import { Helmet } from 'react-helmet-async'
import { useSearchParams, useNavigate } from 'react-router-dom'
import FindAShow from '../components/FindAShow'
import ShowsLikeThis from '../components/ShowsLikeThis'
import Encyclopedia from '../components/Encyclopedia'
import LandingHero from '../components/LandingHero'
import MarketingHero from '../components/MarketingHero'
import TodaysSpotlight from '../components/TodaysSpotlight'
import VaultStatsStrip from '../components/VaultStatsStrip'
import HowRecommendationsWork from '../components/HowRecommendationsWork'
import DataMinimizationStrip from '../components/DataMinimizationStrip'
import ControversyLandscapeLazy from '../components/ControversyLandscapeLazy'
import AnonBanner from '../components/AnonBanner'
import WatchlistExclusionPanel from '../components/WatchlistExclusionPanel'
import type { WatchlistCache } from '../components/WatchlistExclusionPanel'
import DiscoveryTabs, { type Tab } from '../components/DiscoveryTabs'

// Search.tsx only manages three of the four tabs as internal state — the
// fourth ("Top Lists") is handled by a route change to /top. See
// handleTabClick below for the branch.
type EngineTab = Exclude<Tab, 'Top Lists'>

const SAMPLE_QUERIES = [
  'gritty military drama',
  'cozy slice of life',
  'top sports anime from 2023',
  'anime where the MC is betrayed by a friend',
]

interface SearchProps {
  skipHero?: boolean
}

export default function Search({ skipHero = false }: SearchProps) {
  const [searchParams] = useSearchParams()
  const navigate = useNavigate()

  // URL params seed initial tab + query on mount. Post-mount, tab and
  // seed state are driven by user interactions (handleTabClick /
  // lookUpShow) — nothing here updates the URL, so the initializers
  // capture at first render and no effect is needed. ?tab= is the
  // return path from /top's DiscoveryTabs; ?mode= is the landing-page
  // hero / chip path.
  const qParam = searchParams.get('q')
  const modeParam = searchParams.get('mode')
  const tabParam = searchParams.get('tab')
  const [activeTab, setActiveTab] = useState<EngineTab>(() => {
    if (tabParam === 'find') return 'Find a Show'
    if (tabParam === 'dna') return 'Shows Like This'
    if (tabParam === 'encyclopedia') return 'Encyclopedia'
    if (modeParam === 'dna') return 'Shows Like This'
    if (modeParam === 'encyclopedia') return 'Encyclopedia'
    return 'Find a Show'
  })
  const [previousTab, setPreviousTab] = useState<EngineTab | null>(null)
  const [encyclopediaTitle, setEncyclopediaTitle] = useState(() =>
    modeParam === 'encyclopedia' && qParam ? qParam : '',
  )
  const [seedQuery, setSeedQuery] = useState(() =>
    !tabParam && !modeParam && qParam ? qParam : '',
  )
  const [seedDnaTitle, setSeedDnaTitle] = useState(() =>
    modeParam === 'dna' && qParam ? qParam : '',
  )

  // Sync activeTab + seed inputs when the URL's ?mode= param changes
  // WITHOUT a full route transition (e.g., clicking a `<Link
  // to="/try?mode=dna">` while already on /try, or ExploreVibeButton's
  // `<Link to="/try?mode=find&q=<vibe>">` from inside the Encyclopedia
  // widget). The useState initializers above only run on mount, so
  // same-route query-param changes wouldn't be reflected without this
  // effect. The App.tsx `key` prop handles route-level transitions
  // (/ → /try); this handles the intra-/try case.
  //
  // Each branch also seeds the corresponding input when `q` is present,
  // so navigating with `?mode=X&q=Y` pre-fills the right field — matches
  // the mount-time initializer semantics but during same-route navigation.
  useEffect(() => {
    const current = searchParams.get('mode')
    const q = searchParams.get('q')
    if (current === 'dna') {
      setActiveTab('Shows Like This')
      if (q) setSeedDnaTitle(q)
    } else if (current === 'encyclopedia') {
      setActiveTab('Encyclopedia')
      if (q) setEncyclopediaTitle(q)
    } else if (current === 'find') {
      setActiveTab('Find a Show')
      if (q) setSeedQuery(q)
    }
    // No modeParam = don't change — user may have switched tabs
    // manually and we shouldn't override their state.
  }, [searchParams])
  // PR C — watchlist exclusion. The panel owns the localStorage cache;
  // we just hold the resolved MAL ID list and thread it as a prop into
  // FindAShow and ShowsLikeThis, which fold it into the search/DNA
  // request body. null = no exclusion active.
  const [excludeMalIds, setExcludeMalIds] = useState<number[] | null>(null)

  const handleWatchlistChange = useCallback((cache: WatchlistCache | null) => {
    setExcludeMalIds(cache && cache.mal_ids.length > 0 ? cache.mal_ids : null)
  }, [])

  const lookUpShow = useCallback((title: string) => {
    setPreviousTab(activeTab)
    setEncyclopediaTitle(title)
    setActiveTab('Encyclopedia')
  }, [activeTab])

  const goBack = useCallback(() => {
    if (previousTab) {
      setActiveTab(previousTab)
      setPreviousTab(null)
    }
  }, [previousTab])

  const handleTabClick = (tab: Tab) => {
    // Top Lists is a URL-based tab — clicking it navigates to /top
    // where TopLists.tsx renders the browse landing. All other tabs
    // stay on /try and switch activeTab state.
    if (tab === 'Top Lists') {
      navigate('/top')
      return
    }
    setPreviousTab(null)
    setActiveTab(tab)
  }

  const seoHead = (
    <Helmet>
      <title>Anime Intel — AI-Powered Anime Discovery</title>
      <meta name="description" content="Find your next favorite anime with semantic search, AI reranking, and community intelligence. 6,200+ titles indexed from 1990 to today." />
      <link rel="canonical" href="https://animeintel.app/" />
      <script type="application/ld+json">{JSON.stringify({
        "@context": "https://schema.org",
        "@type": "WebApplication",
        "name": "Anime Intel",
        "url": "https://animeintel.app",
        "description": "AI-powered anime discovery engine with semantic search, community intelligence, and Gemini-powered reranking. 6,200+ titles indexed.",
        "applicationCategory": "EntertainmentApplication",
        "operatingSystem": "Web",
        "offers": { "@type": "Offer", "price": "0", "priceCurrency": "USD" }
      })}</script>
    </Helmet>
  )

  if (!skipHero) {
    // Landing routing (Landing Redesign PR #2):
    //   - Hiatus mode ALWAYS falls back to <LandingHero> because the new
    //     marketing hero has no graceful degradation for a disabled
    //     search path (its only interaction is Find a Show).
    //   - Otherwise, VITE_NEW_LANDING_HERO_ENABLED gates the redesign.
    //     Default (unset / anything != 'true') keeps the current LandingHero
    //     for backward compat until the flag permanently flips on.
    //   - <TodaysSpotlight /> is coupled to the new hero surface only; it
    //     honors its own VITE_DAILY_SPOTLIGHT_ENABLED flag internally.
    const hiatusMode = import.meta.env.VITE_ENGINE_HIATUS_MODE === 'true'
    const newHeroEnabled = import.meta.env.VITE_NEW_LANDING_HERO_ENABLED === 'true'
    if (!hiatusMode && newHeroEnabled) {
      // Below-fold marketing sections (PR #3) each honor their own flag
      // internally (VaultStatsStrip / HowRecommendationsWork /
      // DataMinimizationStrip return null when off), so we always render
      // them here — no branch fan-out for six flag combinations.
      return (
        <>
          {seoHead}
          <MarketingHero />
          {/* ControversyLandscape sits directly below the hero as the
              differentiating signature viz — moved up from the bottom
              of the stack after the initiative shipped so the big
              value prop isn't the last thing users see. Still below
              the fold, so lazy-chunk download still waits for scroll. */}
          <ControversyLandscapeLazy />
          <TodaysSpotlight />
          <VaultStatsStrip />
          <HowRecommendationsWork />
          <DataMinimizationStrip />
        </>
      )
    }
    return <>{seoHead}<LandingHero /></>
  }

  return (
    <div className="space-y-6">
      {seoHead}
      <AnonBanner />
      <div>
        <h1 className="text-3xl font-extrabold mb-1">Anime Discovery Engine</h1>
        <p className="text-sm text-vault-text-muted">
          Find your next favorite show using AI and community consensus.
        </p>
        <div className="flex flex-wrap gap-2 mt-2">
          <span className="text-xs text-vault-text-muted">Try:</span>
          {SAMPLE_QUERIES.map(q => (
            <button
              key={q}
              onClick={() => { setSeedQuery(q); setActiveTab('Find a Show') }}
              className="px-3 py-1 rounded-full bg-vault-surface border border-vault-border text-xs text-vault-text-muted hover:text-vault-text hover:border-vault-accent transition-colors cursor-pointer"
            >
              &ldquo;{q}&rdquo;
            </button>
          ))}
        </div>
      </div>

      {activeTab !== 'Encyclopedia' && (
        <WatchlistExclusionPanel onListChange={handleWatchlistChange} />
      )}

      <DiscoveryTabs activeTab={activeTab} onTabChange={handleTabClick} />

      <div className={activeTab === 'Find a Show' ? '' : 'hidden'}>
        <FindAShow
          onShowClick={lookUpShow}
          seedQuery={seedQuery}
          onSeedConsumed={() => setSeedQuery('')}
          excludeMalIds={excludeMalIds}
        />
      </div>
      <div className={activeTab === 'Shows Like This' ? '' : 'hidden'}>
        <ShowsLikeThis
          onShowClick={lookUpShow}
          seedTitle={seedDnaTitle}
          onSeedConsumed={() => setSeedDnaTitle('')}
          excludeMalIds={excludeMalIds}
        />
      </div>
      <div className={activeTab === 'Encyclopedia' ? '' : 'hidden'}>
        <Encyclopedia
          initialTitle={encyclopediaTitle}
          onTitleConsumed={() => setEncyclopediaTitle('')}
          onBack={previousTab ? goBack : undefined}
          backLabel={previousTab ?? undefined}
        />
      </div>
    </div>
  )
}
