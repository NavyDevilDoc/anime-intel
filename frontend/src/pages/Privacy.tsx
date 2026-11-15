import { Helmet } from 'react-helmet-async'

export default function Privacy() {
  return (
    <div className="max-w-3xl mx-auto space-y-6">
      <Helmet>
        <title>Privacy Policy — Anime Intel</title>
        <meta name="description" content="Anime Intel privacy policy: what minimal data we collect, how we use it, and your rights." />
        <link rel="canonical" href="https://animeintel.app/privacy" />
      </Helmet>
      <h1 className="text-2xl font-bold">Privacy Policy</h1>
      <p className="text-vault-text-muted text-sm">Last updated: September 23, 2026</p>

      <section className="space-y-2">
        <h2 className="text-lg font-semibold">What We Collect</h2>
        <p className="text-vault-text-muted">
          Anime Intel runs as an open-access service — there is no sign-up, no
          login, and no user accounts on the public surface. We collect five
          kinds of data:
        </p>
        <ul className="list-disc list-inside text-vault-text-muted space-y-1 ml-2">
          <li>
            <strong>Salted IP hash</strong> — a one-way SHA-256 hash of your IP
            address combined with a server-side secret salt. The original IP is
            never stored. Used solely to count requests per device per day.
          </li>
          <li>
            <strong>Daily request counters</strong> — how many searches and
            "Shows Like This" lookups have been made from your hashed identifier
            today. Resets at midnight UTC.
          </li>
          <li>
            <strong>Search-query telemetry</strong> — the query text you
            submitted, the mode and filters you selected (Intelligent Search vs.
            Objective Rankings vs. Staff/Studio, decade, MAL rating ceiling,
            etc.), the list of shows the engine returned, and timing metrics
            for the retrieval + reranking pipeline. Used to improve ranking
            quality and identify queries the engine handles poorly. Individual
            queries are never published in raw form and are not tied to any
            personal identifier.
          </li>
          <li>
            <strong>Optional feedback ratings</strong> — if you click the
            thumbs-up or thumbs-down widget under a result, we store a
            <code className="mx-1 text-xs bg-vault-bg/50 px-1 rounded">(search_id, up|down)</code>
            row. No identifier is attached to the rating.
          </li>
          <li>
            <strong>Contact-form submissions</strong> — when you use the
            contact form, we store the name, email address, and message you
            provide. This is the only place we collect personal contact
            information; it's opt-in on a per-message basis, and messages are
            read only by the site operator.
          </li>
        </ul>
      </section>

      <section className="space-y-2">
        <h2 className="text-lg font-semibold">What We Do NOT Collect</h2>
        <ul className="list-disc list-inside text-vault-text-muted space-y-1 ml-2">
          <li>Your raw IP address (only the salted hash — see above)</li>
          <li>Your browsing history outside of search interactions on this site</li>
          <li>Any data from third-party tracking, advertising, or analytics scripts</li>
          <li>Any tracking cookies or cross-site profiling identifiers</li>
        </ul>
      </section>

      <section className="space-y-2">
        <h2 className="text-lg font-semibold">How We Use Your Data</h2>
        <p className="text-vault-text-muted">
          The salted IP hash and daily counters exist for one reason: enforcing
          the public rate limits that keep the service free. Search-query
          telemetry helps us tune the AI ranker and identify queries the engine
          handles poorly. Feedback ratings inform which searches produced useful
          results. Contact-form messages are read by the site operator for
          support and product feedback. We do not sell or monetize any of this
          data. See "Third-Party Services" below for the subprocessors we route
          data through as part of normal service operation.
        </p>
      </section>

      <section className="space-y-2">
        <h2 className="text-lg font-semibold">Third-Party Services</h2>
        <p className="text-vault-text-muted">
          To deliver the search product, Anime Intel routes data through a
          small set of third-party services. Each is a data processor operating
          under its own privacy policy:
        </p>
        <ul className="list-disc list-inside text-vault-text-muted space-y-1 ml-2">
          <li>
            <strong>Google Gemini API</strong> — every search query is sent to
            Google's Gemini API for embedding (vector-space encoding of your
            query) and for the second-stage LLM reranking of results. Google
            processes query text under its own privacy terms.
          </li>
          <li>
            <strong>Supabase</strong> — our managed PostgreSQL database
            provider. All telemetry, feedback ratings, and contact messages
            are stored in Supabase-hosted PostgreSQL.
          </li>
          <li>
            <strong>Railway</strong> — hosts our backend API service and our
            self-hosted Jikan instance (see "Data Storage" below).
          </li>
          <li>
            <strong>Vercel</strong> — hosts the frontend you're reading this
            page on.
          </li>
        </ul>
        <p className="text-vault-text-muted">
          We do not use third-party advertising networks, third-party
          analytics scripts, or any other cross-site tracking service.
        </p>
      </section>

      <section className="space-y-2">
        <h2 className="text-lg font-semibold">Data Storage</h2>
        <p className="text-vault-text-muted">
          Rate-limit counters, search telemetry, feedback ratings, and
          contact messages are stored in our PostgreSQL database (managed by
          Supabase). Anime metadata itself comes from MyAnimeList, ingested via
          a self-hosted instance of the open-source Jikan project running on
          Railway. The optional Watchlist Exclusion feature (described below)
          also reads public list data from MyAnimeList or AniList when you
          opt in.
        </p>
      </section>

      <section className="space-y-2">
        <h2 className="text-lg font-semibold">Data Retention</h2>
        <ul className="list-disc list-inside text-vault-text-muted space-y-1 ml-2">
          <li>
            <strong>Salted IP hashes and daily counters</strong> — retained
            historically per day so the admin dashboard can compute rolling
            7-day and 30-day unique-visitor counts. The hash cannot be
            reversed to recover your IP.
          </li>
          <li>
            <strong>Search-query telemetry and feedback ratings</strong> —
            retained indefinitely for engine improvement and long-term
            analytics. Carries no identifier tying entries to you.
          </li>
          <li>
            <strong>Contact-form messages</strong> — retained indefinitely
            so we can follow up on requests or refer back to prior
            conversations. Purge on request (see "Your Rights").
          </li>
        </ul>
      </section>

      <section className="space-y-2">
        <h2 className="text-lg font-semibold">Cookies &amp; Local Storage</h2>
        <p className="text-vault-text-muted">
          Anime Intel does not use tracking cookies or third-party analytics
          scripts. The site uses browser local storage only for ephemeral UI
          state — no identifiers, no cross-site tracking. The optional
          Watchlist Exclusion feature (see below) is the one exception:
          if you opt in, the MAL IDs from your public watchlist live in
          your browser's local storage until you clear them.
        </p>
      </section>

      <section className="space-y-2">
        <h2 className="text-lg font-semibold">Watchlist Exclusion (Optional)</h2>
        <p className="text-vault-text-muted">
          The optional watchlist exclusion panel lets you supply a public
          MyAnimeList or AniList username. The backend reads your public
          list and sends back the MAL IDs of shows you've marked as
          completed, dropped, watching, etc. Those IDs are excluded from
          your search and "Shows Like This" results so the engine doesn't
          waste your time recommending shows you've already seen.
        </p>
        <ul className="list-disc list-inside text-vault-text-muted space-y-1 ml-2">
          <li>
            Your username is sent to our backend <em>once</em> at fetch time
            and is never persisted server-side. No telemetry row mentions it.
          </li>
          <li>
            The MAL ID list is cached in your browser's local storage and
            is controlled by you. Clearing it (via the Clear button) removes
            it immediately.
          </li>
          <li>
            We never see or store your MAL/AniList password. Both APIs read
            your public list using usernames only — no OAuth, no token from
            your end.
          </li>
          <li>
            The watchlist data is not linked to any telemetry log, search
            journal entry, or other data we hold. The IDs flow into your
            search request and stop there.
          </li>
          <li>
            If you change handles or want to use a different account, just
            clear the cached list and re-fetch with the new username.
          </li>
        </ul>
      </section>

      <section className="space-y-2">
        <h2 className="text-lg font-semibold">Your Rights</h2>
        <p className="text-vault-text-muted">
          Because we do not require sign-up and store no persistent identifier
          tying you to your search activity, there is no account to delete.
          The salted IP hash rolls over daily and cannot be reversed to your
          IP address.
        </p>
        <p className="text-vault-text-muted">
          If you would like us to purge search telemetry, feedback ratings,
          or a contact-form message you submitted, use the contact form below.
          For telemetry entries we may need context (approximate timestamps,
          distinctive query strings) to locate them, since they carry no
          identifier. For a contact-form message, tell us the email you used
          and we'll delete the record.
        </p>
      </section>

      <section className="space-y-2">
        <h2 className="text-lg font-semibold">Contact</h2>
        <p className="text-vault-text-muted">
          For privacy questions or data requests, please use
          our <a href="/contact" className="text-vault-accent hover:underline">contact form</a>.
        </p>
      </section>
    </div>
  )
}
