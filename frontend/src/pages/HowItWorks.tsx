import { useEffect, useState } from 'react'
import { Helmet } from 'react-helmet-async'
import { Link } from 'react-router-dom'
import ControversyLandscapeLazy from '../components/ControversyLandscapeLazy'

const API_BASE = import.meta.env.VITE_API_BASE_URL as string ?? ''

export default function HowItWorks() {
  const [animeCount, setAnimeCount] = useState<number | null>(null)

  useEffect(() => {
    fetch(`${API_BASE}/api/health`)
      .then(res => res.json())
      .then(data => setAnimeCount(data.anime_count))
      .catch(() => {}) // Silently fail — fallback text handles it (hiatus/backend-down)
  }, [])

  const vaultCountText = animeCount
    ? `${animeCount.toLocaleString()} anime titles`
    : 'thousands of anime titles'

  return (
    <div className="max-w-3xl mx-auto space-y-8">
      <Helmet>
        <title>How It Works — Anime Intel</title>
        <meta name="description" content="How Anime Intel works: AI-powered semantic search, smoothed quality scores, distilled community consensus, and what's deliberately excluded." />
        <link rel="canonical" href="https://animeintel.app/how-it-works" />
      </Helmet>

      <div>
        <h1 className="text-3xl font-extrabold mb-2">How It Works</h1>
        <p className="text-vault-text-muted">
          Anime Intel is an AI-powered discovery engine that helps you find anime based on
          vibes, themes, and community consensus — not just genre tags. MAL and AniList are
          great databases; this is a discovery layer that helps you find shows you didn't
          know you were looking for.
        </p>
      </div>

      <section className="space-y-4">
        <h2 className="text-xl font-bold">Five Ways to Discover</h2>

        <div className="space-y-4">
          <div className="p-4 rounded bg-vault-surface border border-vault-border">
            <h3 className="font-semibold mb-1">Intelligent Search</h3>
            <p className="text-sm text-vault-text-muted">
              Describe what you're in the mood for in plain language — "gritty war drama
              with moral ambiguity" or "wholesome found family with comedy." The engine
              understands vibes, not just keywords, and reranks results using community
              consensus to surface the best matches.
            </p>
          </div>

          <div className="p-4 rounded bg-vault-surface border border-vault-border">
            <h3 className="font-semibold mb-1">Shows Like This</h3>
            <p className="text-sm text-vault-text-muted">
              Enter 1-3 shows you love. The engine extracts their shared thematic DNA and
              finds titles with similar tonal qualities, narrative structures, and emotional
              registers — going deeper than surface-level genre matching.
            </p>
          </div>

          <div className="p-4 rounded bg-vault-surface border border-vault-border">
            <h3 className="font-semibold mb-1">Objective Rankings</h3>
            <p className="text-sm text-vault-text-muted">
              Skip the AI and get hard database rankings. Query by genre, decade, or season —
              "top romance anime from 2023" or "best seinen from the 2000s." Results are
              ordered by a smoothed quality score that adjusts for how many people voted,
              so popular shows with many ratings are weighted more accurately than niche
              shows with a handful.
            </p>
          </div>

          <div className="p-4 rounded bg-vault-surface border border-vault-border">
            <h3 className="font-semibold mb-1">Staff/Studio</h3>
            <p className="text-sm text-vault-text-muted">
              Filter by the people behind the show, not the show itself. Add chips for a
              director, composer, original creator, series composer, character designer, or
              studio — mix as many as you want. Cowboy Bebop was Watanabe-directed and
              Kanno-scored; Studio Ghibli has a distinct filmography. This mode surfaces
              those catalogs directly, drawn from 6,300+ people and 28,000+ role
              attributions.
            </p>
          </div>

          <div className="p-4 rounded bg-vault-surface border border-vault-border">
            <h3 className="font-semibold mb-1">Top Lists</h3>
            <p className="text-sm text-vault-text-muted">
              Browse curated rankings by genre and decade — 79 pre-built lists across
              16 genres and 5 decades. Each list shows the top 20 titles ranked by
              community ratings, with one entry per franchise so multi-season shows
              don't crowd out distinct works. Every entry links back to its Encyclopedia
              profile. Reachable from the "Top Lists" tab on the landing page or from
              the genre tags at the bottom of any show profile.
            </p>
          </div>
        </div>
      </section>

      <section className="space-y-4">
        <h2 className="text-xl font-bold">About the Data</h2>
        <p className="text-sm text-vault-text-muted">
          The recommender is only as honest as the data it's built on. Here's what's in the
          vault, how it's scored, and what's deliberately discarded.
        </p>

        {/* --- The Vault --- */}
        <div id="vault" className="p-4 rounded bg-vault-surface border border-vault-border space-y-3 scroll-mt-16">
          <h3 className="font-semibold">The Vault</h3>
          <p className="text-sm text-vault-text-muted">
            The vault holds <strong className="text-vault-text">{vaultCountText}</strong>{' '}
            from MyAnimeList — TV series, movies, OVAs, and specials. Every show carries
            its original MAL ID and links back to its MyAnimeList page. MAL is the source
            of truth for the underlying community data, and credit belongs there.
          </p>
          <p className="text-sm text-vault-text-muted">
            By content rating, the catalog skews toward general and teen audiences —
            PG-13 is by far the largest bucket, with G, PG, R, and R+ filling out the
            tails. Rx (explicit adult content) is not part of the vault; MAL's TV catalog
            filters it out at the source.
          </p>
        </div>

        {/* --- Scoring --- */}
        <div id="scoring" className="p-4 rounded bg-vault-surface border border-vault-border space-y-3 scroll-mt-16">
          <h3 className="font-semibold">Scoring</h3>
          <p className="text-sm text-vault-text-muted">
            MAL's raw user score is a starting point, not the ending point. A show with 50
            ratings claiming a 9.5 means something very different from a show with 50,000
            ratings at 8.5. The score you see on Anime Intel is{' '}
            <strong className="text-vault-text">smoothed</strong> — pulled toward the
            global mean (~7.05) until a show earns enough votes to claim its own number
            with confidence. A small show with a 9.5 from 50 ratings ends up around 7.5 in
            our ranking; a popular show with an 8.5 from 1.2M ratings barely shifts.
          </p>
          <p className="text-sm text-vault-text-muted">
            Here's what the full vault looks like when you plot quality against popularity:
          </p>
          <a
            href="/quadrant_analysis.png"
            target="_blank"
            rel="noopener noreferrer"
            title="Open full-size chart in new tab"
            className="block cursor-zoom-in hover:opacity-90 transition-opacity"
          >
            <img
              src="/quadrant_analysis.png"
              alt="Scatter plot of 6,279 anime plotted by smoothed MAL score (Y-axis) against popularity measured as log of rating count (X-axis). Crowd Pleasers (high quality, mainstream) cluster in the top right at 2,257 shows. Hidden Gems (high quality, niche) at 883. Overhyped (lower quality, mainstream) at 883. Deep Cuts (lower quality, niche) at 2,256. Median lines split the chart at quality 7.09 and 16,715 ratings."
              className="w-full h-auto rounded border border-vault-border"
              loading="lazy"
            />
          </a>
          <p className="text-xs text-vault-text-muted italic">
            Each dot is one show. X-axis: popularity. Y-axis: quality. Quadrant lines split
            at the medians.
          </p>
          <ul className="text-xs text-vault-text-muted space-y-1 pl-4 list-disc">
            <li><strong className="text-vault-text">Crowd Pleasers</strong> — high quality, mainstream (2,257)</li>
            <li><strong className="text-vault-text">Hidden Gems</strong> — high quality, niche audience (883)</li>
            <li><strong className="text-vault-text">Overhyped</strong> — lower quality, mainstream (883)</li>
            <li><strong className="text-vault-text">Deep Cuts</strong> — lower quality, niche (2,256)</li>
          </ul>
          <blockquote className="border-l-4 border-vault-accent pl-4 py-1 text-sm text-vault-text italic">
            72% of titles fall on the quality-popularity diagonal — popular shows tend to
            be better, obscure shows tend to be worse. The interesting question is what's
            in the other 28%.
          </blockquote>
          {/* Interactive complement to the static quadrant chart above —
              different axes (quality × controversy, popularity as color)
              but same storytelling intent. Lazy-loaded so the Plot bundle
              only hits this page's bundle if the user actually visits. */}
          <div className="pt-2">
            <p className="text-sm text-vault-text-muted">
              Another angle: plot every show by quality against how
              divisive it is in community reviews, colored by popularity.
              The interactive landscape below filters by genre, decade,
              and subtype — click any dot to look up that show.
            </p>
          </div>
          <ControversyLandscapeLazy />
        </div>

        {/* --- Consensus Distillation --- */}
        <div id="consensus" className="p-4 rounded bg-vault-surface border border-vault-border space-y-3 scroll-mt-16">
          <h3 className="font-semibold">Consensus Distillation</h3>
          <p className="text-sm text-vault-text-muted">
            Star ratings are blunt. What viewers actually say about a show — in thousands
            of long-form reviews — carries the signal you'd never get from a number.
          </p>
          <p className="text-sm text-vault-text-muted">
            For every show in the vault, Anime Intel reads community reviews and uses
            Google's Gemini model to distill them into a structured profile: thematic vibe,
            strengths, weaknesses, an audience consensus summary, and a controversy score
            that captures how divisive the show is among viewers. The reranker reads this
            profile — not the raw star score — when deciding whether a show matches your
            search.
          </p>
          <p className="text-sm text-vault-text-muted">
            When the reranker picks a result for you, it also picks one con that
            specifically speaks to what you asked for. So a result card shows you why the
            show matches AND what might not work — in the same glance. The same show
            recommended to someone asking "space western with jazz vibes" surfaces a
            different friction than the same show recommended to someone asking "tight
            serialized storytelling." Encyclopedia entries still show the full pros/cons
            list when you want the whole picture.
          </p>
        </div>

        {/* --- Ranking --- */}
        <div id="ranking" className="p-4 rounded bg-vault-surface border border-vault-border space-y-3 scroll-mt-16">
          <h3 className="font-semibold">Ranking</h3>
          <p className="text-sm text-vault-text-muted">
            Every search runs through two AI layers. First, the semantic retrieval layer
            finds candidates whose meaning matches your query — search for "existential
            mecha" and the engine surfaces shows that <em>feel</em> existential and involve
            mecha, even if those exact words never appear in any description. Second, a
            specialized reranker reads each candidate's full community profile and scores
            it against your specific query. Different kinds of queries get different
            reranker treatments — a plot-and-character query gets scored on plot and
            character, a vibe query gets scored on vibe — so no single scoring rubric has
            to compromise across every mode.
          </p>
          <p className="text-sm text-vault-text-muted">
            Sometimes Gemini's content policy blocks the reranker on certain query
            categories — a limitation we can't tune around. When that happens, the engine
            falls back to the retrieval-layer ordering, marks each result with a "Semantic
            match only" pill, and shows a banner explaining that detailed analysis wasn't
            available. You get the closest matches by content similarity, honestly labeled
            as a reduced-quality mode.
          </p>
        </div>

        {/* --- Watchlist Exclusion --- */}
        <div className="p-4 rounded bg-vault-surface border border-vault-border space-y-3">
          <h3 className="font-semibold">Watchlist Exclusion (Optional)</h3>
          <p className="text-sm text-vault-text-muted">
            Tell Anime Intel a public MyAnimeList or AniList username, and the engine will
            filter your watched and dropped shows out of future recommendations. Both
            platforms expose public watchlists by username, so we never see your password
            and you never log in to Anime Intel.
          </p>
          <p className="text-sm text-vault-text-muted">
            Once a show is on your exclusion list, the engine also filters out its whole
            franchise — every season, movie, OVA, and spin-off in the same series cluster.
            Marking "Naruto" as completed excludes Naruto Shippuden, Boruto, Road of
            Naruto, and the full set of films and specials in one move.
          </p>
          <p className="text-sm text-vault-text-muted">
            The MAL IDs live in your browser's local storage until you clear them.
            Nothing is persisted server-side. See the{' '}
            <Link to="/privacy" className="text-vault-accent hover:underline">Privacy</Link>{' '}
            page for the full data-handling contract.
          </p>
        </div>

        {/* --- Data Minimization --- */}
        <div id="data-minimization" className="p-4 rounded bg-vault-surface border border-vault-border space-y-3 scroll-mt-16">
          <h3 className="font-semibold">Data Minimization</h3>
          <p className="text-sm text-vault-text-muted">
            When the distillation finishes, the raw reviews and episode synopses are
            deleted from the database. Anime Intel keeps the structured intelligence (the
            consensus profiles, the narrative arcs) — not the original text.
          </p>
          <p className="text-sm text-vault-text-muted">
            Two reasons.{' '}
            <strong className="text-vault-text">Data minimization is its own commitment.</strong>{' '}
            The raw text belongs to the viewers who wrote it on MAL, not to Anime Intel.
            It's used just long enough to extract structured signal, then discarded.{' '}
            <strong className="text-vault-text">And DB hygiene.</strong> Thousands of shows
            times dozens of reviews times a couple KB each adds up. Discarding the source
            after distillation keeps the vault lean and the data-handling surface small.
          </p>
          <p className="text-sm text-vault-text-muted">
            User-side privacy (what's collected when you visit) is covered separately on
            the{' '}
            <Link to="/privacy" className="text-vault-accent hover:underline">Privacy</Link>{' '}
            page.
          </p>
        </div>
      </section>

      <section className="space-y-4">
        <h2 className="text-xl font-bold">Open Access</h2>
        <p className="text-sm text-vault-text-muted">
          Anime Intel runs as a free, open-access service — no sign-up, no
          accounts, no ads. User data is never sold. See the{' '}
          <Link to="/privacy" className="text-vault-accent hover:underline">privacy policy</Link>{' '}
          for details.
        </p>
      </section>

      <div className="pt-4 border-t border-vault-border text-center">
        <Link
          to="/try"
          className="inline-block px-6 py-3 rounded bg-vault-accent text-white font-medium hover:bg-vault-accent-hover transition-colors no-underline"
        >
          Start Searching
        </Link>
      </div>
    </div>
  )
}
