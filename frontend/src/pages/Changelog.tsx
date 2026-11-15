import { Helmet } from 'react-helmet-async'

export default function Changelog() {
  return (
    <div className="max-w-3xl mx-auto space-y-8">
      <Helmet>
        <title>Changelog — Anime Intel</title>
        <meta name="description" content="Recent updates to Anime Intel, the AI-powered anime discovery engine." />
        <link rel="canonical" href="https://animeintel.app/changelog" />
      </Helmet>
      <div>
        <h1 className="text-3xl font-extrabold mb-2">Changelog</h1>
        <p className="text-vault-text-muted">
          What's new in Anime Intel.
        </p>
      </div>

      <section className="space-y-3">
        <h2 className="text-lg font-bold">September 2026</h2>
        <ul className="list-disc list-inside text-sm text-vault-text-muted space-y-1.5">
          <li>
            <strong>Site reactivated</strong> after a brief August hiatus (see
            below). All discovery modes back online with no data loss.
          </li>
          <li>
            <strong>Privacy Policy refresh</strong> — added Data Retention and
            Third-Party Services sections; corrected the contact-form
            disclosure; named the specific subprocessors that touch your data.
          </li>
          <li>
            Behind the scenes: extensive code quality pass across the whole
            codebase. Nothing user-visible changed, but the polish is why the
            recent updates could ship as fast as they did.
          </li>
        </ul>
      </section>

      <section className="space-y-3">
        <h2 className="text-lg font-bold">August 2026</h2>
        <ul className="list-disc list-inside text-sm text-vault-text-muted space-y-1.5">
          <li>
            <strong>Brief hiatus</strong> — Find a Show and Shows Like This
            were temporarily paused while we shipped the recommendation-quality
            improvements listed below. Encyclopedia and Top Lists stayed
            available throughout via a client-side lookup path.
          </li>
          <li>
            <strong>Engine deep-dive</strong> — targeted retrieval-quality
            work driven by curated eval measurements. Hidden-gems precision
            improved 27% against the pre-initiative baseline; identical
            repeat queries now return the same results (previously varied
            run-to-run).
          </li>
          <li>
            <strong>Top Lists</strong> — 79 curated rankings by genre and
            decade. Each list shows the top 20 titles by community rating,
            with one entry per franchise so multi-season shows don't crowd
            out distinct works. Reachable from the "Top Lists" tab on the
            landing page.
          </li>
          <li>
            <strong>Similar Shows widget on Encyclopedia</strong> — every
            show profile now shows a "You might also like" section at the
            bottom. Recommendations include personnel-provenance context
            ("Also directed by Watanabe") where relevant.
          </li>
          <li>
            <strong>Staff/Studio search mode</strong> — new third mode on
            Find a Show. Filter by director, composer, original creator,
            series composer, character designer, or studio. Mix as many
            chips as you want. Deterministic — no AI reranker in this mode.
          </li>
          <li>
            <strong>Staff data</strong> — 6,300+ people and 28,000+ role
            attributions ingested and linked to shows. Powers the Staff/Studio
            search mode and the personnel-provenance strings in Similar Shows.
          </li>
        </ul>
      </section>

      <section className="space-y-3">
        <h2 className="text-lg font-bold">July 2026</h2>
        <ul className="list-disc list-inside text-sm text-vault-text-muted space-y-1.5">
          <li>
            <strong>Behind the scenes: infrastructure resilience</strong> —
            the free public API we use for anime metadata (Jikan) announced a
            sunset. Migrated to a self-hosted instance running on our own
            infrastructure. No user-visible impact; the site keeps working
            when the upstream retires.
          </li>
        </ul>
      </section>

      <section className="space-y-3">
        <h2 className="text-lg font-bold">June 2026</h2>
        <ul className="list-disc list-inside text-sm text-vault-text-muted space-y-1.5">
          <li>
            <strong>Watchlist Exclusion</strong> — supply your public MAL or
            AniList username and the engine will filter shows you've already
            watched or dropped out of recommendations. Franchise-aware: marking
            "Naruto" completed excludes all 19 cluster entries (Shippuden,
            Boruto, Road of Naruto, films, specials) in one move. No password,
            no login, no server-side persistence of your username.
          </li>
          <li>
            <strong>Franchise-aware results</strong> — Find a Show pools no
            longer waste slots on multiple seasons of the same franchise;
            Shows Like This "Naruto" now surfaces cross-franchise picks
            (Bleach, HxH, JJK, AoT) instead of Naruto sequels and side stories.
          </li>
          <li>
            <strong>Series context on result cards</strong> — every card shows
            franchise position ("Entry 3 of 5 in the franchise"), the score
            range across all entries in the franchise, and a reception
            trajectory ("stable" / "improving" / "declining"). Full pros/cons
            for dropped sibling entries available on a click-through drawer.
          </li>
          <li>
            <strong>Real cost tracking</strong> — every AI call is now logged
            with real token counts. Admin dashboard shows month-to-date spend
            against the operating budget. Accountability, not vanity.
          </li>
        </ul>
      </section>

      <section className="space-y-3">
        <h2 className="text-lg font-bold">May 2026</h2>
        <ul className="list-disc list-inside text-sm text-vault-text-muted space-y-1.5">
          <li>
            <strong>Query-relevant cons on result cards</strong> — each result
            now shows one weakness that specifically speaks to your query, not
            the show's generic top con. Same show surfaces different friction
            depending on what you asked for.
          </li>
          <li>
            <strong>"I Don't Know" mode</strong> — yellow advisory banner fires
            when the engine's top result isn't strongly confident (below 75%
            match) or the pool comes back empty. Honest signal for when to
            refine your query.
          </li>
          <li>
            <strong>MAL content rating filter</strong> — opt-in dropdown on
            Find a Show. "PG-13 and below" / "PG and below" / "G only." Skews
            to what MAL surfaces for a given ceiling (PG naturally leans
            heavily toward G because the PG bucket is small in absolute
            terms).
          </li>
          <li>
            <strong>Removed streaming-platform display and filter</strong> —
            the AniList-sourced data wasn't region-aware, so a show listed as
            "on Netflix" might only stream on Netflix in another country.
            Better to ship nothing than misleading info. Reversible if a
            higher-fidelity data source becomes viable.
          </li>
          <li>Loading spinners + cancellable searches across all three discovery modes</li>
          <li>
            <strong>Open-access pivot</strong> — sign-up and sign-in removed
            from the public site. The product is now fully anonymous, with
            generous per-IP daily limits (30 searches / 10 "Shows Like This"
            lookups). No accounts, no email collection, no watchlists, no
            saved searches.
          </li>
          <li>Updated privacy policy reflecting the simplified data collection</li>
          <li>
            <strong>Search-first landing page</strong> — Find a Show / Shows
            Like This / Encyclopedia tabs above the search bar (Top Lists
            joined the tab row in August), each with mode-specific
            placeholders and example chips
          </li>
          <li>Build-time SEO content pages (6,300+ anime profiles + 79 curated lists)</li>
        </ul>
      </section>

      <section className="space-y-3">
        <h2 className="text-lg font-bold">Earlier</h2>
        <p className="text-sm text-vault-text-muted">
          Previous milestones included the React frontend rebuild, FAISS
          semantic search with Gemini embeddings, the four specialized AI
          rerankers (Fusion / Constraint / DNA / Narrative), decade filters,
          structured constraint parsing, narrative-arc search across the
          plot-heavy portion of the catalog, LLM-flagged spoiler alerts,
          mobile-responsive layout, and the move to a custom domain at
          animeintel.app.
        </p>
      </section>
    </div>
  )
}
