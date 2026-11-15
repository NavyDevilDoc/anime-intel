"""Seasonal bulk-ingestion orchestrator.

Pulls a MAL season's anime (TV + Movie + OVA + ONA + TV Special +
Special per the SEASONAL_ALLOWED_MEDIA_TYPES whitelist, pre-Option-C
was TV-only), fetches reviews via Jikan, computes weighted VADER
sentiment, distills the audience consensus via Gemini, and upserts
the result into `anime_info`. Failures at any step land in the
`intelligence_quarantine` table with a structured `failure_node`
label so vault_manager's self-healing pass can retry just the shows
that broke.

Callers:
  - `vault_manager.py` — interactive top-level driver for the operator
  - `.github/workflows/seasonal-ingest.yml` — on-demand workflow_dispatch
"""
import asyncio
import json
import math
import os
import sqlite3
import sys

import requests
from dotenv import load_dotenv
from tqdm import tqdm
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

# --- PATH HANDLER ---
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

load_dotenv(os.path.join(ROOT_DIR, "env_variables.env"))

from analysis.sentiment_distiller import ReviewDistiller
from src.jikan_client import JikanClient
from src.mal_api_client import MALClient
from src.seasonal_ingest_manifest import (
    SeasonalIngestManifest,
    _iso_now,
    compute_outcomes,
    snapshot_vault_and_quarantine,
    write_manifest,
)

# Default path used if none is injected
DEFAULT_DB_PATH = os.path.join(ROOT_DIR, "data", "anime_intelligence_v2.db")

# Seasonal ingest media-type whitelist (Option C, PR #171). Pre-Option-C
# this was hard-coded to {'tv'} only — new seasonal shows that were
# Movies, OVAs, ONAs, etc. got filtered out and had to be caught via
# manual popularity-backfill passes (Phase E for TV catchup, PR #170 for
# non-TV catchup). Widening the default to the full non-noise whitelist
# means future seasonal runs catch all legitimate anime content natively.
#
# Deliberately excluded from the default:
#   Music  — standalone music videos (noise; see PR #168 noise prune)
#   CM     — commercials
#   PV     — promotional trailers
#
# Narrowing is still possible via the `allowed_media_types` parameter on
# ingest_season — operators debugging a specific season can scope back
# to TV-only without editing this constant.
SEASONAL_ALLOWED_MEDIA_TYPES = frozenset({
    'tv', 'movie', 'ova', 'ona', 'tv_special', 'special',
})


def _filter_by_media_type(
    page_data: list,
    allowed_media_types: frozenset,
) -> tuple[list, dict]:
    """Pure filter for a MAL /anime/season/ page response.

    Extracted so the Option-C whitelist semantics can be unit-tested
    without spinning up the full async ingest pipeline (which hits
    MAL, Jikan, and Gemini and has no existing test coverage).

    Returns:
        (passed, skipped) where:
          - passed is entries whose node.media_type is in the whitelist
          - skipped is {mal_id: f"media_type={type}"} for the rest
    """
    passed: list = []
    skipped: dict = {}
    for entry in page_data:
        node = entry['node']
        media_type = node.get('media_type', 'unknown')
        if media_type in allowed_media_types:
            passed.append(entry)
        else:
            skipped[node['id']] = f"media_type={media_type}"
    return passed, skipped

class SentimentEngine:
    """Weighted VADER sentiment for a show's review corpus.

    Two-pass: reviews are split intro/conclusion (75/25) with a
    conclusion bias (0.7 vs 0.3) since reviewers tend to soften
    critiques early and land the honest verdict late. Per-review
    scores are then weighted by log10(reactions + 1) so heavily
    upvoted reviews carry more weight than one-off drive-bys.
    """

    def __init__(self):
        self.analyzer = SentimentIntensityAnalyzer()

    def calculate_jit_sentiment(self, reviews):
        if not reviews: return 0.0
        
        sum_weighted_sentiments = 0.0
        sum_weights = 0.0
        
        for r in reviews:
            content = r.get('content', '')
            interactions = r.get('reactions', {}).get('overall', 0)
            user_score = r.get('score', 0)
            if not content or len(content.strip()) < 10: continue

            # Clean the sentences to avoid empty strings
            sentences = [s.strip() for s in content.split('.') if s.strip()]
            
            # EDGE CASE FIX: If review is 2 sentences or less, do a flat sentiment analysis
            if len(sentences) <= 2:
                raw_sentiment = self.analyzer.polarity_scores(content)['compound']
            else:
                split_point = int(len(sentences) * 0.75)
                intro = ". ".join(sentences[:split_point])
                concl = ". ".join(sentences[split_point:])
                
                intro_score = self.analyzer.polarity_scores(intro)['compound']
                concl_score = self.analyzer.polarity_scores(concl)['compound']
                raw_sentiment = (intro_score * 0.3) + (concl_score * 0.7)

            final_sentiment = raw_sentiment
            if user_score >= 9: final_sentiment = max(raw_sentiment, 0.5)
            elif user_score <= 3: final_sentiment = min(raw_sentiment, -0.2)

            # True Weighted Average Calculation
            hype_weight = math.log10(interactions + 1) + 1
            sum_weighted_sentiments += (final_sentiment * hype_weight)
            sum_weights += hype_weight

        # Divide the total weighted sentiment by the total weight
        return sum_weighted_sentiments / sum_weights if sum_weights > 0 else 0.0

class SeasonalIngestor:
    """Orchestrates the four-stage ingestion pipeline for a season.

    Per-show pipeline: MAL metadata → Jikan reviews → weighted VADER
    sentiment → Gemini consensus distillation → SQLite upsert. Any
    step's failure is written to `intelligence_quarantine` with a
    labeled `failure_node`, and a subsequent successful re-run of the
    same show clears the row automatically via `_clear_casualty`.

    Constructor accepts an optional `db_path` override so tests /
    dev tools can target a scratch DB without touching the production
    vault.
    """

    def __init__(self, db_path=None):
        self.db_path = db_path if db_path else DEFAULT_DB_PATH
        self.mal = MALClient(token_path=os.path.join(ROOT_DIR, "token_data.json"))
        self.jikan = JikanClient()
        self.distiller = ReviewDistiller(api_key=os.getenv("GOOGLE_API_KEY"))
        self.sentiment_engine = SentimentEngine()
        self.jikan.rate_limit_delay = 3.0
        self._init_quarantine()
        
        print(f"📡 Ingestor Initialized. Target Database: {self.db_path}")


    async def _process_show(self, node, year, season, conn=None):
        """
        Core ingestion logic shared by both ingest_single_anime and ingest_season.
        Handles: existence check, review fetch, sentiment calc, AI distillation, DB save.
        Returns True if the show was successfully vaulted or already complete, False otherwise.
        """
        mal_id = node['id']
        title = node.get('title', f"Unknown ID {mal_id}")
        season_tag = f"{season.capitalize()}_{year}"

        if self._show_exists(mal_id, conn=conn):
            self._clear_casualty(mal_id, conn=conn)
            print(f" ⚠️ Show '{title}' already exists with full intelligence. Skipping.")
            return True

        # --- STEP 1: JIKAN REVIEWS ---
        # ReviewFetchResult carries structured error info so the
        # quarantine table's error_message field can distinguish
        # legitimate empty reviews from Jikan HTTP failures.
        # Pre-change (July 2026): both cases logged the same
        # "Returned 0 reviews" string, so the July 2026 Jikan /reviews
        # 504 outage silently quarantined 65 Spring 2026 shows.
        result = self.jikan.get_anime_reviews(mal_id)
        if result.is_error:
            self._log_casualty(mal_id, title, season_tag, "JIKAN_API", f"Jikan {result.summary}", conn=conn)
            return False
        if not result.reviews:
            self._log_casualty(mal_id, title, season_tag, "JIKAN_API", "Returned 0 reviews. Cannot distill.", conn=conn)
            return False
        reviews = result.reviews

        # --- STEP 2: MATH ---
        avg_sentiment = self.sentiment_engine.calculate_jit_sentiment(reviews)

        # --- STEP 3: AI DISTILLATION ---
        context = {
            "title": title,
            "synopsis": node.get('synopsis', ''),
            "reviews": [r.get('content', '') for r in reviews[:15]]
        }

        try:
            await asyncio.sleep(2.0)
            consensus_json = await self.distiller.distill_sentiment(context)

            if not consensus_json:
                self._log_casualty(mal_id, title, season_tag, "AI_DISTILLATION", "Gemini returned None. Possible safety filter or parsing failure.", conn=conn)
                return False

            # --- STEP 4: DATABASE ---
            self._save_to_db(node, consensus_json, avg_sentiment, year, season, conn=conn)
            self._clear_casualty(mal_id, conn=conn)
            return True

        except Exception as e:  # noqa: BLE001 — every failure mode routes to quarantine; per-show blowups must never take down the batch
            if "429" in str(e):
                print(f"   ⏳ Quota hit for {title}. Cooling down...")
                await asyncio.sleep(30)
                self._log_casualty(mal_id, title, season_tag, "RATE_LIMIT", "Gemini 429 quota exceeded.", conn=conn)
            else:
                print(f"   ❌ DISTILLATION ERROR for {title}: {e}")
                self._log_casualty(mal_id, title, season_tag, "UNHANDLED_EXCEPTION", str(e), conn=conn)
            return False

    async def ingest_single_anime(self, mal_id):
        """
        Capability: The Sniper Mode.
        Bypasses the season sweep and ingests/updates a single show directly by ID.
        """
        print(f"\n🎯 DIRECT INGESTION MISSION: ID {mal_id}")

        # 1. Fetch exact metadata from MAL
        url = f"https://api.myanimelist.net/v2/anime/{mal_id}"
        params = {'fields': 'synopsis,genres,studios,mean,num_scoring_users,alternative_titles,media_type,start_season'}

        try:
            # ASYNC210 is acceptable here — this async method has no concurrent
            # sibling coros (single-show sniper mode), so blocking the event
            # loop on this fetch has no observable cost. A migration to
            # httpx.AsyncClient would parallelize nothing and add a dep.
            # get_with_auto_refresh handles 401→refresh→retry-once so a stale
            # MAL OAuth token doesn't abort the mission (2026-09-25 fix).
            response = self.mal.get_with_auto_refresh(url, params=params)  # noqa: ASYNC210
            response.raise_for_status()
            node = response.json()
        except Exception as e:  # noqa: BLE001 — MAL fetch failure aborts this single-show mission; no downstream to blow up
            print(f" ❌ MAL API FATAL ERROR FOR ID {mal_id}: {e}")
            return

        title = node.get('title', f"Unknown ID {mal_id}")

        # 2. Extract season/year dynamically
        start_season = node.get('start_season', {})
        year = start_season.get('year', 9999)
        season = start_season.get('season', 'unknown')

        # 3. Process through shared pipeline
        print(f" ⏳ Processing {title}...")
        success = await self._process_show(node, year, season)
        if success:
            print(f" ✅ MISSION ACCOMPLISHED: '{title}' vaulted.")


    async def ingest_season(
        self,
        year,
        season,
        target_ids=None,
        allowed_media_types: frozenset = SEASONAL_ALLOWED_MEDIA_TYPES,
    ):
        print(f"\n🚀 MISSION START: {season.upper()} {year}")
        print(f"   Media-type whitelist: {sorted(allowed_media_types)}")
        if target_ids:
            print(f"🎯 TARGETED STRIKE: Focusing strictly on {len(target_ids)} missing/incomplete shows.")

        # Phase C manifest tracking (2026-09-25): snapshot vault + quarantine
        # sets at run start so `newly_vaulted` can be computed at the end.
        # Manifest is finalized in a finally block below so partial / failed
        # runs still produce diagnostic output.
        prior_vaulted, _prior_quarantined = snapshot_vault_and_quarantine(self.db_path)
        manifest = SeasonalIngestManifest(
            run_id=_iso_now(),
            year=year,
            season=season,
            season_tag=f"{season.capitalize()}_{year}",
            target_ids_mode=(target_ids is not None),
        )

        try:
            url = f"https://api.myanimelist.net/v2/anime/season/{year}/{season}"
            params = {'limit': 100, 'fields': 'synopsis,genres,studios,mean,num_scoring_users,alternative_titles,media_type'}

            anime_list = []

            try:
                while url:
                    # See ingest_single_anime for why blocking `requests` inside
                    # an async method is intentional here. get_with_auto_refresh
                    # handles 401 across the full pagination loop.
                    response = self.mal.get_with_auto_refresh(url, params=params)  # noqa: ASYNC210
                    response.raise_for_status()
                    payload = response.json()

                    page_data = payload.get('data', [])

                    # Record source_ids into the manifest BEFORE the filter —
                    # the manifest's whole point is a complete record of what
                    # MAL returned vs what we attempted.
                    for entry in page_data:
                        manifest.source_ids.append(entry['node']['id'])

                    # THE MEDIA FILTER (Option C, PR #171): keep entries
                    # whose media_type is in the whitelist. Pre-Option-C
                    # this was hard-coded to tv-only; it now accepts the
                    # full non-noise whitelist by default so new seasonal
                    # Movies / OVAs / ONAs / Specials vault natively.
                    # `skipped_non_tv` field name kept for backward compat
                    # with existing manifest consumers (semantics are now
                    # "skipped by media-type whitelist" rather than
                    # literally "non-TV").
                    passed, skipped = _filter_by_media_type(page_data, allowed_media_types)
                    manifest.skipped_non_tv.update(skipped)
                    anime_list.extend(passed)

                    paging = payload.get('paging', {})
                    url = paging.get('next')
                    params = None

                if target_ids is not None:
                    anime_list = [entry for entry in anime_list if entry['node']['id'] in target_ids]

                # attempted_ids reflects the final subset _process_show sees —
                # source minus whitelist filter minus (in re-run mode) the target_ids filter.
                manifest.attempted_ids = [entry['node']['id'] for entry in anime_list]

                print(f"✅ MAL API: Successfully extracted {len(anime_list)} titles for this season "
                      f"(from {len(manifest.source_ids)} returned, {len(manifest.skipped_non_tv)} skipped by whitelist).")

            except Exception as e:  # noqa: BLE001 — MAL pagination failure aborts this season's ingest; caller (vault_manager) re-queues via quarantine
                print(f"❌ MAL API FATAL ERROR: {e}")
                return

            with sqlite3.connect(self.db_path, timeout=20) as conn:
                for entry in tqdm(anime_list, desc=f"Vaulting {season}", unit="show"):
                    node = entry['node']
                    await self._process_show(node, year, season, conn=conn)
        finally:
            self._finalize_manifest(manifest, prior_vaulted)

    def _finalize_manifest(self, manifest: SeasonalIngestManifest, prior_vaulted: set[int]):
        """Compute outcome buckets, persist manifest to disk, print summary.

        Runs in the `ingest_season` finally block so partial or failed
        runs still get a manifest — the runs that broke are exactly the
        ones where diagnostic output has the highest value. Manifest
        write errors are logged but don't propagate; the actual ingest
        outcome takes precedence.
        """
        current_vaulted, current_quarantined = snapshot_vault_and_quarantine(self.db_path)
        success, quarantined, newly_vaulted, missed = compute_outcomes(
            manifest.attempted_ids,
            current_vaulted,
            current_quarantined,
            prior_vaulted,
        )
        manifest.processed_success = success
        manifest.processed_quarantine = quarantined
        manifest.newly_vaulted = newly_vaulted
        manifest.missed = missed

        try:
            fpath = write_manifest(manifest, ROOT_DIR)
            print(f"\n📋 {manifest.summary_line}")
            print(f"   Manifest: {fpath}")
        except Exception as e:  # noqa: BLE001 — manifest write is diagnostic; ingest outcome shouldn't be lost to a filesystem hiccup
            print(f"⚠️  Failed to write manifest: {type(e).__name__}: {e}")

    def _show_exists(self, mal_id, conn=None):
        """Checks if a show is not just present, but has 'Complete Intelligence'."""
        def _query(c):
            query = "SELECT 1 FROM anime_info WHERE id = ? AND consensus_json IS NOT NULL AND mal_synopsis IS NOT NULL"
            return c.execute(query, (mal_id,)).fetchone() is not None

        if conn:
            return _query(conn)
        with sqlite3.connect(self.db_path, timeout=20) as c:
            return _query(c)

    def _save_to_db(self, node, consensus_json, avg_sentiment, year, season, conn=None):
        try:
            def _execute(c):
                studio = node.get('studios', [{}])[0].get('name', 'Unknown') if node.get('studios') else "Unknown"
                eng_title = node.get('alternative_titles', {}).get('en') or node['title']

                cursor = c.cursor()
                # UPSERT LOGIC: Insert new, or update ALL mutable columns if the ID exists
                cursor.execute("""
                    INSERT INTO anime_info (
                        id, romaji_title, english_title, mal_score,
                        season, scored_by, studio, avg_sentiment,
                        consensus_json, mal_synopsis, release_year
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        romaji_title=excluded.romaji_title,
                        english_title=excluded.english_title,
                        mal_score=excluded.mal_score,
                        scored_by=excluded.scored_by,
                        studio=excluded.studio,
                        avg_sentiment=excluded.avg_sentiment,
                        consensus_json=excluded.consensus_json,
                        mal_synopsis=excluded.mal_synopsis;
                """, (
                    node['id'], node['title'], eng_title, node.get('mean'),
                    f"{season.capitalize()}_{year}", node.get('num_scoring_users'),
                    studio, avg_sentiment, json.dumps(consensus_json),
                    node.get('synopsis', ''), year
                ))
                c.commit()
                print(f"  💾 UPSERT SUCCESS: '{eng_title}' secured in Vault.")

            if conn:
                _execute(conn)
            else:
                with sqlite3.connect(self.db_path, timeout=20) as c:
                    _execute(c)
        except Exception as e:  # noqa: BLE001 — DB write failure logs and returns; caller sees the show didn't land and re-queues via quarantine
            print(f"  ❌ DATABASE WRITE ERROR for {node['title']}: {e}")

    def _init_quarantine(self):
        """Creates the CASREP tracking table if it doesn't exist."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS intelligence_quarantine (
                    mal_id INTEGER PRIMARY KEY,
                    title TEXT,
                    season TEXT,
                    failure_node TEXT,
                    error_message TEXT,
                    last_attempt TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

    def _log_casualty(self, mal_id, title, season, node, error_msg, conn=None):
        """Logs a failure to the CASREP table."""
        # Echo to console so the operator sees the failure inline. Pre-this-line,
        # JIKAN_API and AI_DISTILLATION (returned-None) branches logged to DB
        # silently — vault_manager's "see per-ID diagnostics above" line pointed
        # at nothing when those branches fired.
        print(f"   ⚠️ CASUALTY [{node}] MAL {mal_id} '{title}': {error_msg}")

        def _execute(c):
            c.execute("""
                INSERT INTO intelligence_quarantine (mal_id, title, season, failure_node, error_message, last_attempt)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(mal_id) DO UPDATE SET
                    failure_node=excluded.failure_node,
                    error_message=excluded.error_message,
                    last_attempt=CURRENT_TIMESTAMP;
            """, (mal_id, title, season, node, str(error_msg)))

        try:
            if conn:
                _execute(conn)
            else:
                with sqlite3.connect(self.db_path, timeout=20) as c:
                    _execute(c)
        except Exception as e:  # noqa: BLE001 — quarantine-write failure would compound the original failure; log and move on so the batch keeps flowing
            print(f"  ❌ CASREP LOGGING FAILED for {title}: {e}")

    def _clear_casualty(self, mal_id, conn=None):
        """Removes a show from quarantine if it successfully heals."""
        def _execute(c):
            c.execute("DELETE FROM intelligence_quarantine WHERE mal_id = ?", (mal_id,))

        try:
            if conn:
                _execute(conn)
            else:
                with sqlite3.connect(self.db_path, timeout=20) as c:
                    _execute(c)
        except Exception:  # noqa: BLE001, S110 — best-effort quarantine cleanup; a failed delete is silently OK because the row's now-stale error message won't hurt anything (the show is present in anime_info, so future runs skip it before reading quarantine)
            pass