
# Anime Intel

**AI-powered anime discovery engine.** Natural language search across 6,600+ titles spanning 1979–2026, with a live prediction pipeline that tracks whether currently-airing shows are outperforming their structural expectations.

**Live at [animeintel.app](https://animeintel.app)**

---

## What It Does

Anime Intel ingests anime metadata and community reviews from MyAnimeList, distills them into structured intelligence via Gemini LLM, and serves natural language discovery queries through a two-phase retrieval pipeline: dense vector search (FAISS) followed by Gemini reranking with Pydantic structured output.

Every Monday, an autonomous cron pipeline refreshes live MAL scores and community sentiment for all currently-airing shows, runs the hype predictor, and opens a pull request with the week's data snapshot — no operator intervention required.

### Four Discovery Surfaces

**Find a Show** — Three search modes under one surface:

| Mode | What You Search By | Example |
|------|--------------------|---------|
| **Intelligent Search** | Natural language vibe, narrative, theme | "cynical loner protagonist rebuilding trust" |
| **Objective Rankings** | Quality + metadata constraints, no AI | "top 15 mecha anime between 2009 and 2016" |
| **Staff / Studio** | Director, composer, studio, original creator | Shows directed by Shinichiro Watanabe |

**Shows Like This** — Enter 1–3 reference shows (and up to 2 avoid-shows). The engine computes a vector centroid from their embeddings, excludes same-franchise candidates, and finds thematically similar titles via nearest-neighbor search.

**Encyclopedia** — Fuzzy title lookup returning the full intelligence profile: Bayesian quality score, audience sentiment %, controversy rating, distilled consensus, pros/cons, and a "You might also like" similar-shows widget.

**Top Lists** — Curated and database-ordered rankings with compound query support (genre + subtype + year range).

---

## Prediction Pipeline

A parallel data layer tracks in-season show performance against structural expectations.

**Weekly autonomous cron** (`weekly-airing-predictions.yml`, every Monday 06:00 UTC):

1. **Wake Railway** — Deploys the self-hosted Jikan REST + MongoDB instances via the Railway GraphQL API and waits for both to reach `SUCCESS` before proceeding.
2. **Score refresh** (`refresh_airing_scores.py`) — Pulls live `mal_score` + `scored_by` from Jikan for the current and previous season cohort and writes them back to the vault. Fixes the stale-score problem inherent to the ingestor's skip-existing-shows design.
3. **Sentiment refresh** (`refresh_airing_sentiment.py`) — Fetches episode reviews, computes fresh VADER sentiment, and distills a trajectory-aware consensus via Gemini. Writes `avg_sentiment` to `anime_info` (Encyclopedia freshness) and a weekly snapshot to `airing_sentiment_log` (Season Tracker source of truth). `consensus_json` in `anime_info` is intentionally frozen at ingest time so the Encyclopedia text stays stable.
4. **Hype prediction** (`predict_airing_shows.py`) — Loads the trained model and writes one `(predicted_score, actual_score_at_time)` row per show to `score_prediction_log`. Auto-opens a PR with the DB diff.
5. **Sleep Railway** (`if: always()`) — Removes both deployments regardless of workflow outcome, keeping Railway costs near $0 between Monday runs.

**Target deliverable (Phase 3 — Season Tracker):** *"This show was predicted at 7.5 based on staff/source signals. After 4 episodes, community sentiment is tracking above its structural baseline — it's outperforming its pedigree."* No anime discovery site currently surfaces this delta.

---

## Architecture

```
[Vercel — Frontend]  ──HTTPS──>  [Railway — Backend]  ──writes──>  [Supabase — Telemetry]
  React SPA (TypeScript)          FastAPI + FAISS                   gemini_calls cost ledger
  Vite + Tailwind v4              Python 3.12                       anon usage log
                                  SQLite (committed to git)
```

### Retrieval Pipeline

```
User query
  |
  ├─ Intelligent Search
  |    -> Intent classification (vibe / genre_combo / hidden_gems / crowd_pleasers / polarizing / narrative)
  |    -> Adaptive composite weights per intent
  |    -> Filter extraction (temporal, studio, quality, sentiment, popularity)
  |    -> FAISS search -> composite rerank (similarity + Bayesian quality + log popularity + input rank)
  |    -> Phase 2: Gemini reranker (FUSION / CONSTRAINT / NARRATIVE prompt, selected by intent)
  |
  ├─ Shows Like This
  |    -> Vector centroid from reference embeddings (+ anti-centroid for avoid-shows)
  |    -> FAISS nearest-neighbor -> franchise exclusion -> composite rerank
  |    -> Phase 2: Gemini reranker (DNA prompt)
  |
  ├─ Staff / Studio Search
  |    -> Chip-based entity resolution against people + anime_staff tables
  |    -> Deterministic SQL join — no AI reranking
  |
  ├─ Objective Rankings
  |    -> Compound query parsing (genre + year + season + studio + top-N)
  |    -> SQLite query with two-gate genre validation — no AI reranking
  |
  └─ Encyclopedia
       -> 3-tier fuzzy title matching -> full profile hydration from SQLite
       -> Similar-shows widget from pre-computed cosine similarity files
```

### Data Pipeline

```
vault_manager.py (ingest)  ->  vault_enrichment.py (enrich)  ->  git push (deploy)

weekly-airing-predictions.yml (autonomous, every Monday):
  Wake Railway -> score refresh -> sentiment refresh -> hype predict -> Sleep Railway -> auto-PR
```

---

## Key Technical Details

- **Embeddings:** Google `gemini-embedding-001`, 3,072 dimensions, asymmetric task types (`RETRIEVAL_DOCUMENT` for indexing, `RETRIEVAL_QUERY` for search)
- **Vector index:** FAISS `IndexFlatIP`, ~6,600 vectors, committed to git alongside the SQLite database
- **LLM reranking:** Gemini 2.5 Flash with 4 specialized prompts (Fusion, Constraint, DNA, Narrative) and anti-stretch scoring rules; AoT global scoring (all candidates scored in one call, paginated client-side)
- **Composite reranking:** 5-signal weighted blend (semantic similarity, Bayesian quality, log popularity, input rank, recency) with per-intent weight profiles tuned against a 47-entry curated eval set
- **Narrative data:** 1,476+ shows enriched with plot arcs, character dynamics, archetypes, emotional trajectory, and tone shifts — distilled from episode synopses via two-tier Gemini prompts
- **Staff data:** `people` table (6,400+ rows, keyed on Jikan `person_mal_id`) + `anime_staff` junction (28,000+ attributions, 5-role CHECK constraint: Director, Music, Original Creator, Series Composition, Character Design). 94.8% vault coverage.
- **Hype prediction:** `LinearRegression` on staff hit-rates + source material + subtype + genre + release_year. Test MAE ~0.44 (+25% lift over predict-mean baseline). `original_creator_hit_rate` carries ~58% of model weight.
- **Cost tracking:** `gemini_client.py` wraps every Gemini SDK call and writes per-call rows to Supabase `gemini_calls` with real `usage_metadata` token counts, including `thoughts_token_count` (billed at output rate)
- **Security:** SQLite read-only URI mode (`?mode=ro`), per-IP anonymous quotas with salted hashing, CORS origin restriction, LLM output HTML escaping, HTTP Basic Auth on `/admin`

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Frontend | React 19, TypeScript 5.9, Vite, Tailwind CSS v4 |
| Backend | FastAPI, Python 3.12, FAISS (faiss-cpu) |
| Telemetry | Supabase (PostgreSQL — `gemini_calls` cost ledger + anon usage log) |
| AI / NLP | Google Gemini 2.5 Flash (reranking, distillation), `gemini-embedding-001` (embeddings), VADER (sentiment) |
| Prediction | scikit-learn `LinearRegression`, weekly GitHub Actions cron |
| External APIs | MyAnimeList API v2, Jikan REST API (self-hosted on Railway + MongoDB) |
| Hosting | Vercel (frontend), Railway (backend + Jikan + MongoDB), Supabase (telemetry) |

---

## Repository Structure

```
api/                    FastAPI application
  routes/               Endpoints: search, dna, encyclopedia, top-lists, spotlight, sharing, admin
  middleware/           Rate limiting, anonymous quotas, lockdown switch
  schemas/              Pydantic request/response models
analysis/               Core AI + prediction layer
  vector_store.py       FAISS index, search, composite reranking, intent classification
  recommendation_engine.py  Orchestrator: routes queries through retrieval -> reranking
  prompts.py            LLM prompt templates (Fusion, Constraint, DNA, Narrative rerankers)
  queries.py            SQLite read-only interface, Bayesian scoring, title resolution
  hype_predictor.py     HypePredictor: train / predict / save / load (LinearRegression)
  prediction_log.py     score_prediction_log + airing_sentiment_log schema + upsert helpers
  sentiment_distiller.py  Review corpus -> ConsensusData (Gemini); rolling trajectory variant
  narrative_distiller.py  Episode synopsis -> structured narrative data (Gemini)
  gemini_client.py      Single chokepoint: wraps every Gemini SDK call with cost tracking
frontend/               React SPA (Vite + TypeScript + Tailwind v4)
  src/components/       ResultCard, Encyclopedia, SimilarShows, ControversyLandscape, etc.
  src/pages/            FindAShow, ShowsLikeThis, TopLists, Landing, Admin
src/                    Ingestion pipeline (MAL client, Jikan client, seasonal ingestor v2)
tools/                  CLI utilities
  refresh_airing_scores.py     Weekly Step 1 — live MAL score refresh for airing cohort
  refresh_airing_sentiment.py  Weekly Step 2 — VADER + Gemini trajectory refresh
  predict_airing_shows.py      Weekly Step 3 — hype prediction logging
  backfill_staff.py            Staff data backfill (Jikan person API)
  build_similar_shows.py       Pre-compute cosine-similarity similar-shows JSON
  report_prediction_health.py  Monthly MAE + drift diagnostics + PNG plots
  probe_hype_prediction.py     Model comparison probe (9 sklearn configurations)
tests/                  1,207 pytest tests + 580 Vitest tests
data/                   SQLite database, FAISS index, vector metadata, trained model (all committed to git)
.github/workflows/      5 workflows: weekly predictions, monthly retrain, seasonal ingest, drift refresh, regression CI
vault_manager.py        Ingestion orchestrator (seasonal sweep, targeted strike, rolling window)
vault_enrichment.py     Enrichment orchestrator (genres, narratives, FAISS sync)
```

---

## Local Development

> **Private artifacts:** The intelligence vault (`data/anime_intelligence_v2.db`), FAISS embeddings index, trained hype predictor, and reranker prompts are private deployment artifacts not included in this repository. The prompts loader (`analysis/prompts.py`) shows the expected environment variable names. The ingestion pipeline (`vault_manager.py` → `vault_enrichment.py`) rebuilds the vault from MAL/Jikan data given a Google API key and MAL OAuth token.

```bash
# Clone and install
git clone https://github.com/NavyDevilDoc/anime-intel.git
cd anime-intel
pip install -r requirements.txt

# Environment variables
# Create env_variables.env with: GOOGLE_API_KEY="your_key_here"
# JIKAN_BASE_URL is required for ingest and weekly refresh tools

# Run the backend API
uvicorn api.main:app --reload

# Run the frontend (separate terminal)
cd frontend && npm install && npm run dev

# Run tests
pytest                              # 1,207 backend tests
cd frontend && npm test             # 580 frontend tests

# Ingestion pipeline (requires MAL OAuth token)
python vault_manager.py             # Ingest seasonal data
python vault_enrichment.py          # Enrich: genres, narratives, FAISS sync
python vault_enrichment.py --stats  # Show coverage gaps without processing
```

---

## Tests

1,787 tests across the full stack:

| Suite | Count | Coverage |
|-------|-------|----------|
| Frontend (Vitest) | 580 | Components, pages, search flows, API mocking, accessibility |
| Backend (pytest) | 1,207 | Security (SQL injection, prompt injection, BOLA), retrieval quality, API validation, rate limiting, anonymous quotas, hype prediction, score + sentiment refresh, ingestor, regression baseline |

---

## Data Attribution

All anime metadata sourced from [MyAnimeList](https://myanimelist.net/). This project is not affiliated with MyAnimeList.
