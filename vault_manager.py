"""
MODULE: vault_manager.py
FUNCTION: Unified Self-Healing Commander. Orchestrates ingestion with 
          real-time quality auditing and autonomous retries.
"""
import os
import sys
import sqlite3
import asyncio
import json
import time
import requests
from tqdm import tqdm
from datetime import datetime # [ADDED] For rolling window calculations

# --- DYNAMIC PATH ANCHOR ---
current_dir = os.path.abspath(os.path.dirname(__file__))

# Auto-detect if we are in the root or inside the 'src' folder
if os.path.exists(os.path.join(current_dir, 'data')):
    ROOT_DIR = current_dir
else:
    ROOT_DIR = os.path.abspath(os.path.join(current_dir, '..'))

if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from src.seasonal_ingestor_v2 import SeasonalIngestor
from analysis.vector_store import update_index

# Jikan base URL for Targeted Strike's title→ID lookup.
#
# Public Jikan (api.jikan.moe) is DEPRECATED for our use as of 2026-08:
# rate-limited, has recurring upstream MAL connectivity problems, and its
# pull deadline makes it unreliable long-term. That's exactly why we built
# a self-hosted jikan-rest on Railway (PR #95). Default here points at the
# Railway instance; override via JIKAN_BASE_URL env var only if
# intentionally probing a different endpoint.
#
# Note: our jikan-rest is paused during the offline transition — un-pause
# via Railway UI before running any Jikan-dependent ingest path.
JIKAN_BASE_URL = os.environ.get("JIKAN_BASE_URL") or "https://jikan-rest-production-d553.up.railway.app/v4"


def _jikan_preflight_check(timeout_seconds=10):
    """Ping Jikan (public or configured self-hosted) before starting a
    Targeted Strike batch. Returns True if reachable, False if not.

    Fails fast on Jikan downtime rather than partway through the batch
    — the loss of a mid-batch Jikan connection would leave the vault in
    an inconsistent state (some shows ingested, some not) and waste MAL
    API calls on lookups whose IDs never resolved. Called before any
    per-title work so the operator can un-pause the self-hosted instance
    and rerun cleanly.
    """
    probe_url = f"{JIKAN_BASE_URL}/anime?q=Berserk&type=tv&limit=1"
    print(f"\n🔍 Preflight: probing Jikan at {JIKAN_BASE_URL}...")
    try:
        response = requests.get(probe_url, timeout=timeout_seconds)
        if response.status_code == 200 and response.json().get("data"):
            print(" ✅ Jikan reachable.")
            return True
        print(f" ❌ Jikan responded {response.status_code} — not usable.")
        return False
    except Exception as e:
        print(f" ❌ Jikan unreachable ({type(e).__name__}: {e})")
        return False

# --- CONFIG ---
DB_PATH = os.path.join(ROOT_DIR, "data", "anime_intelligence_v2.db")

def ensure_db_ready(force_nuke=False):
    """Initializes or connects to the database with manual authorization locks."""
    
    # 1. THE NUKE PROTOCOL (Requires 2FA)
    if force_nuke and os.path.exists(DB_PATH):
        print("\n" + "⚠️"*20)
        print(" CRITICAL WARNING: DATABASE DESTRUCTION PROTOCOL INITIATED")
        print(f" Target: {DB_PATH}")
        print("⚠️"*20)
        
        auth = input("\nType 'CONFIRM-NUKE' to authorize permanent deletion: ").strip()
        if auth == "CONFIRM-NUKE":
            os.remove(DB_PATH)
            print("🔥 DATABASE NUKED: Starting from zero.")
        else:
            print("🛑 Authorization failed or aborted. Exiting program to protect data.")
            sys.exit(1) # Instantly kills the script

    # 2. THE INITIALIZATION PROTOCOL (Requires 2FA)
    if not os.path.exists(DB_PATH):
        print("\n" + "⚠️"*20)
        print(f" ATTENTION: No database found at {DB_PATH}")
        print("⚠️"*20)
        
        auth = input("\nType 'INIT-VAULT' to authorize creating a new, empty schema: ").strip()
        if auth == "INIT-VAULT":
            print("✅ Authorization accepted. Initializing Pristine Schema...")
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute("""
                    CREATE TABLE anime_info (
                        id INTEGER PRIMARY KEY,
                        romaji_title TEXT,
                        english_title TEXT,
                        mal_score REAL,
                        season TEXT,
                        scored_by INTEGER,
                        studio TEXT,
                        avg_sentiment REAL,
                        consensus_json TEXT,
                        mal_synopsis TEXT,
                        release_year INTEGER
                    )
                """)
        else:
            print("🛑 Initialization aborted. Check your file paths and try again.")
            sys.exit(1)
            
    # 3. STANDARD OPERATING PROCEDURE
    else:
        print(f"📡 DATABASE DETECTED: Operating in Persistence Mode.")



def detect_last_season(_now=None):
    """
    Determines the most recently completed anime season based on the current date.
    Returns (season_name: str, year: int).

    Anime seasons and when they end:
      Winter (Jan-Mar), Spring (Apr-Jun), Summer (Jul-Sep), Fall (Oct-Dec)

    The mapping answers: "What season JUST finished?"
      Running in Jan-Mar  -> Fall of previous year just ended
      Running in Apr-Jun  -> Winter of current year just ended
      Running in Jul-Sep  -> Spring of current year just ended
      Running in Oct-Dec  -> Summer of current year just ended

    Args:
        _now: Override datetime for testing. Uses datetime.now() if None.
    """
    now = _now or datetime.now()
    month = now.month
    year = now.year

    if month <= 3:
        return ("fall", year - 1)
    elif month <= 6:
        return ("winter", year)
    elif month <= 9:
        return ("spring", year)
    else:
        return ("summer", year)


async def auto_seasonal_sweep(season=None, year=None):
    """
    Non-interactive entry point for GitHub Actions workflow_dispatch.
    Auto-detects the most recent season if no overrides are provided.

    Args:
        season: Optional season override (e.g., "winter", "spring").
        year: Optional year override (e.g., 2026).

    Returns:
        dict with season, year, and status info for workflow logging.
    """
    if season and year:
        target_season = season
        target_year = year
        print(f"📡 OVERRIDE MODE: Ingesting {target_season.upper()} {target_year}")
    else:
        target_season, target_year = detect_last_season()
        print(f"📡 AUTO-DETECT: Last completed season is {target_season.upper()} {target_year}")

    campaign_targets = [{"year": target_year, "season": target_season}]
    await execute_self_healing_campaign(campaign_targets)

    return {"season": target_season, "year": target_year}


def run_seasonal_audit(year, season_name):
    """
    Performs a targeted quality check on a specific season.
    Returns a list of IDs that failed to meet quality standards.
    """
    season_tag = f"{season_name.capitalize()}_{year}"
    failed_ids = []
    
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, english_title, consensus_json, avg_sentiment 
            FROM anime_info 
            WHERE season = ?
        """, (season_tag,))
        rows = cursor.fetchall()

        for row in rows:
            mal_id, title, c_json_str, sentiment = row
            try:
                data = json.loads(c_json_str) if c_json_str else None
                # Check for NULLs or missing intelligence packets
                if not data or not isinstance(data, dict):
                    failed_ids.append(mal_id)
                    continue
                
                # Check for low-density/failed distillation
                if not data.get('pros') or len(data.get('pros')) == 0:
                    pass
            except (json.JSONDecodeError, TypeError, KeyError):
                failed_ids.append(mal_id)
                
    return failed_ids

def sync_faiss_index(mal_ids):
    """
    Updates the FAISS vector index for the given MAL IDs.
    Called after ingestion to ensure newly vaulted shows are searchable.
    """
    if not mal_ids:
        print("📭 No IDs to sync to FAISS index.")
        return

    print(f"\n🔄 FAISS SYNC: Updating index for {len(mal_ids)} shows...")
    try:
        count = update_index(mal_ids)
        print(f"✅ FAISS SYNC COMPLETE: {count} vectors updated.")
    except Exception as e:
        print(f"⚠️  FAISS SYNC FAILED: {e}")
        print("   The FAISS index may be stale. Run build_index() to fully rebuild.")


async def execute_self_healing_campaign(targets):
    """
    The Loop: Ingest -> Audit -> Retry with Exponential Backoff and Stagnation Detection.
    Ensures high-fidelity data density without risking infinite loops.
    """
    import src.seasonal_ingestor_v2 as ingestor_mod
    ingestor_mod.DEFAULT_DB_PATH = DB_PATH
    ingestor = SeasonalIngestor(db_path=DB_PATH)

    ingested_ids = []

    for t in targets:
        attempt = 1
        max_attempts = 5
        last_gap_count = float('inf')

        while attempt <= max_attempts:
            print(f"\n📡 MISSION: {t['season'].upper()} {t['year']} (Attempt {attempt})")

            # 1. INGEST: Attempt to fill the vault
            await ingestor.ingest_season(t['year'], t['season'], target_ids=gaps if attempt > 1 else None)

            # 2. AUDIT: Immediately verify the results
            gaps = run_seasonal_audit(t['year'], t['season'])

            # SUCCESS CASE: All shows validated
            if not gaps:
                season_tag = f"{t['season'].capitalize()}_{t['year']}"
                with sqlite3.connect(DB_PATH) as conn:
                    rows = conn.execute("SELECT id FROM anime_info WHERE season = ? AND consensus_json IS NOT NULL", (season_tag,)).fetchall()
                    count = len(rows)

                if count > 0:
                    ingested_ids.extend(row[0] for row in rows)
                    print(f"✅ VERIFIED: {t['season'].upper()} {t['year']} is 100% complete.")
                    break
                else:
                    print(f"⏩ SEASON SKIPPED: No records found for {t['season'].upper()} {t['year']}. (Likely too early for community reviews). Moving to next target.")
                    break

            # PROGRESS CHECK: Detecting if the API is ignoring us
            current_gap_count = len(gaps) if gaps is not None else 0

            if current_gap_count == last_gap_count and gaps is not None:
                print(f"⚠️  STAGNATION: No new intelligence distilled in Attempt {attempt}.")
            last_gap_count = current_gap_count

            # FAILURE CASE: Exhausted retry budget
            if attempt == max_attempts:
                # Still collect whatever was successfully ingested
                season_tag = f"{t['season'].capitalize()}_{t['year']}"
                with sqlite3.connect(DB_PATH) as conn:
                    rows = conn.execute("SELECT id FROM anime_info WHERE season = ? AND consensus_json IS NOT NULL", (season_tag,)).fetchall()
                    ingested_ids.extend(row[0] for row in rows)
                print(f"🛑 MISSION ABORTED: {current_gap_count if gaps is not None else 'All'} shows persistently failing after {max_attempts} tries.")
                break

            # HEALING PHASE: Exponential backoff to clear rate limits
            wait_time = 15 * attempt
            print(f"⚠️  GAP DETECTED: {current_gap_count} shows failed validation. Retrying in {wait_time}s...")
            await asyncio.sleep(wait_time)
            attempt += 1

    # FAISS INDEX SYNC: Update vector embeddings for all ingested shows
    sync_faiss_index(ingested_ids)

    print(f"\n🏁 ALL CAMPAIGNS COMPLETE: Intelligence asset verified at {DB_PATH}")


# =========================================================================
# [ADDED] TARGETED DRIFT MITIGATION MODULE
# =========================================================================

async def _force_update_targets(target_dict):
    """
    [ADDED] Helper function: Takes a dictionary of {season_tag: [mal_ids]} 
    and forces the ingestor to re-process them by clearing their consensus.
    """
    import src.seasonal_ingestor_v2 as ingestor_mod
    ingestor_mod.DEFAULT_DB_PATH = DB_PATH
    ingestor = SeasonalIngestor(db_path=DB_PATH)

    all_target_ids = []

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        for season_tag, ids in target_dict.items():
            if not ids: continue

            # Trick the ingestor into reprocessing by deleting the old intelligence payload
            placeholders = ','.join(['?'] * len(ids))
            cursor.execute(f"UPDATE anime_info SET consensus_json = NULL WHERE id IN ({placeholders})", ids)
            conn.commit()

            # Parse season_tag (e.g., "Spring_2024")
            try:
                s_name, s_year = season_tag.split('_')
                s_year = int(s_year)
            except ValueError:
                print(f"⚠️ Could not parse season tag '{season_tag}'. Skipping.")
                continue

            print(f"\n🎯 EXECUTING TARGETED STRIKE: {season_tag.upper()} ({len(ids)} targets)")
            await asyncio.sleep(3)
            await ingestor.ingest_season(s_year, s_name.lower(), target_ids=ids)
            all_target_ids.extend(ids)

    # FAISS INDEX SYNC: Update vector embeddings for all re-processed shows
    sync_faiss_index(all_target_ids)

    print("\n🏁 TARGETED UPDATES COMPLETE.")

async def update_specific_targets(titles_list):
    """
    [MODIFIED] Capability A: Targeted Strike (Sniper Mode)
    Uses the Live API to get the MAL ID, clears its intelligence payload,
    and directly feeds it to the Ingestor's Sniper Mode.
    Leaves the seasonal bulk ingestion logic completely untouched.
    """
    # Preflight: fail fast if Jikan is unreachable so we don't half-ingest
    # the batch. Operator's mitigation on failure: un-pause the self-hosted
    # jikan-rest on Railway, set JIKAN_BASE_URL in env_variables.env to
    # the private URL, then rerun.
    if not _jikan_preflight_check():
        print("\n🚨 Aborting Targeted Strike — Jikan is not reachable.")
        print("   Mitigation options:")
        print("     1. Wait and retry (public Jikan is intermittently flaky).")
        print("     2. Un-pause Railway jikan-rest + Mongo, then set")
        print("        JIKAN_BASE_URL=https://jikan-rest-production-d553.up.railway.app/v4")
        print("        in env_variables.env and rerun.")
        print("     Note: Railway .internal URLs only resolve inside Railway's")
        print("     private network — from a local machine, you must use the")
        print("     public .up.railway.app URL.")
        return

    print(f"\n🔍 Resolving {len(titles_list)} specific targets via Live API...")

    import src.seasonal_ingestor_v2 as ingestor_mod
    ingestor_mod.DEFAULT_DB_PATH = DB_PATH
    ingestor = SeasonalIngestor(db_path=DB_PATH)

    ingested_ids = []

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()

        for t in titles_list:
            print(f"\n 🌐 Pinging Jikan API for definitive ID: '{t}'...")
            try:
                time.sleep(1.5) # Strict pacing for Jikan Rate Limits

                # Uses the module-level JIKAN_BASE_URL (public Jikan by
                # default, or the self-hosted instance when the env var
                # is set). Preflight above already confirmed reachability.
                search_url = f"{JIKAN_BASE_URL}/anime?q={t}&type=tv&limit=1"
                response = requests.get(search_url)

                if response.status_code == 200:
                    data = response.json().get('data', [])
                    if data:
                        show_data = data[0]
                        mal_id = show_data.get('mal_id')
                        title_en = show_data.get('title_english') or show_data.get('title')

                        print(f" ✨ Target Acquired: {title_en} (ID: {mal_id})")

                        # Set consensus to NULL to authorize the overwrite
                        cursor.execute("UPDATE anime_info SET consensus_json = NULL WHERE id = ?", (mal_id,))
                        conn.commit()

                        # Fire the Sniper Mode safely
                        await ingestor.ingest_single_anime(mal_id)
                        ingested_ids.append(mal_id)

                    else:
                        print(f" ❌ Target Ghosted: Jikan API found zero TV results for '{t}'.")
                else:
                    print(f" ❌ API Error: Jikan returned status {response.status_code} for '{t}'.")
            except Exception as e:
                print(f" ❌ Network Error during live search for '{t}': {e}")

    # FAISS INDEX SYNC: Update vector embeddings for targeted shows
    sync_faiss_index(ingested_ids)

    print("\n🏁 TARGETED UPDATES COMPLETE.")


async def update_specific_targets_by_id(mal_ids):
    """
    [ADDED 2026-08-29] Capability A2: Targeted Strike by MAL ID (Bypass Mode)

    Skips the Jikan title→ID lookup step and feeds MAL IDs directly to
    the ingestor's Sniper Mode. Metadata (title, synopsis, score, etc.)
    comes from the MAL v2 API via OAuth token; reviews are STILL fetched
    from Jikan (public or Railway per JIKAN_BASE_URL) — the bypass only
    removes the search endpoint from the dependency chain, not the
    reviews endpoint.

    When to use: you already have MAL IDs (from mal.net URLs, MAL search,
    or upstream integration) and don't want to pay the Jikan title-search
    round-trip. Manually verify each ID resolves to the expected show
    before running — no title-lookup safety net here.

    Same INSERT/UPSERT semantics as update_specific_targets(): fresh
    INSERT for new-to-vault IDs (via _save_to_db's ON CONFLICT clause);
    consensus_json overwrite for existing rows (cleared to NULL before
    ingest to authorize the overwrite).
    """
    print(f"\n🎯 DIRECT ID INGESTION: {len(mal_ids)} target(s)")
    print("   Bypassing Jikan title→ID lookup. Reviews still fetched from")
    print("   Jikan (public or Railway per JIKAN_BASE_URL).")

    import src.seasonal_ingestor_v2 as ingestor_mod
    ingestor_mod.DEFAULT_DB_PATH = DB_PATH
    ingestor = SeasonalIngestor(db_path=DB_PATH)

    ingested_ids = []
    failed_ids = []

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        for mal_id in mal_ids:
            print(f"\n → MAL ID {mal_id}")
            # Clear consensus for existing rows to force re-ingest. No-op
            # for new-to-vault IDs; ingest_single_anime will INSERT them
            # via _save_to_db's ON CONFLICT(id) DO UPDATE upsert.
            cursor.execute("UPDATE anime_info SET consensus_json = NULL WHERE id = ?", (mal_id,))
            conn.commit()

            try:
                await ingestor.ingest_single_anime(mal_id)
            except Exception as e:
                print(f"   ❌ Ingest raised for {mal_id}: {type(e).__name__}: {e}")
                failed_ids.append(mal_id)
                continue

            # ingest_single_anime doesn't raise on internal failure (Jikan
            # 5xx, distillation errors) — it prints diagnostics and returns
            # silently. Only way to know if the ingest actually landed is
            # to check whether the row now has non-NULL consensus_json,
            # matching _show_exists's success criterion.
            cursor.execute(
                "SELECT 1 FROM anime_info WHERE id = ? AND consensus_json IS NOT NULL",
                (mal_id,),
            )
            if cursor.fetchone():
                ingested_ids.append(mal_id)
            else:
                print(f"   ❌ Ingest completed WITHOUT saving consensus for {mal_id} — "
                      f"check Jikan/distillation errors above.")
                failed_ids.append(mal_id)

    # FAISS INDEX SYNC — only for IDs that actually landed
    sync_faiss_index(ingested_ids)

    print(f"\n🏁 BY-ID TARGETED UPDATES COMPLETE. "
          f"{len(ingested_ids)}/{len(mal_ids)} succeeded.")
    if failed_ids:
        print(f"   Failed IDs: {failed_ids}")
        print(f"   Common causes: Jikan returned 0 reviews (empty or blocked upstream),")
        print(f"   HTTP error, or distillation failure. See per-ID casualty above or")
        print(f"   query intelligence_quarantine table for the full error_message.")


async def update_recent_releases(years_back=2):
    """
    [ADDED] Capability B: The Rolling Window
    Updates all shows released within the last X years to capture late review velocity.
    """
    current_year = datetime.now().year
    cutoff_year = current_year - years_back
    print(f"\n📅 Initiating Rolling Window Update (Releases >= {cutoff_year})...")
    
    target_dict = {}
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, season FROM anime_info WHERE release_year >= ?", (cutoff_year,))
        rows = cursor.fetchall()
        
        for mal_id, season_tag in rows:
            if season_tag not in target_dict:
                target_dict[season_tag] = []
            target_dict[season_tag].append(mal_id)
            
    total_targets = sum(len(ids) for ids in target_dict.values())
    print(f"✅ Found {total_targets} targets in the rolling window.")
    
    if target_dict:
        await _force_update_targets(target_dict)

# =========================================================================

if __name__ == "__main__":
    ensure_db_ready(force_nuke=False) 

    # --- CHOOSE YOUR OPERATION MODE ---
    # Uncomment the ONE mode you wish to execute:
    """
    # MODE 1: Standard Seasonal Ingestion (Original)
    years = [2026] # or years = range(start_year, end_year) where the end_year is not included in the range
    seasons = ['winter'] # 'summer', 'winter', 'fall', 'spring'
    campaign_targets = [{'year': y, 'season': s} for y in years for s in seasons]
    asyncio.run(execute_self_healing_campaign(campaign_targets))
    """

    """
    # MODE 2: Targeted Strike by Title (uses Jikan for title→ID resolution)
    targets_to_update = ["Lycoris Recoil"]
    asyncio.run(update_specific_targets(targets_to_update))
    """

    
    # MODE 2a: Targeted Strike by MAL ID (Bypass Mode — no Jikan touch)
    # Use when Jikan is unreachable OR when you already have IDs.
    # Verify each ID via mal.net before running — no title-lookup safety net.
    # Reference IDs from 2026-08-29 ecchi batch attempt (needs Kanokon re-verify;
    # 3053 returned 'Ganbare! Kickers' from MAL v2, so that specific ID is wrong):
    mal_ids_to_ingest = [
        21,    # One Piece
    ]
    asyncio.run(update_specific_targets_by_id(mal_ids_to_ingest))
    

    """
    # MODE 3: Rolling Window Update (Last 2 Years)
    asyncio.run(update_recent_releases(years_back=2))
    """