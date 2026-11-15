"""Item 2 Phase A — Jikan `/anime/{id}/staff` coverage probe.

Samples ~240 shows from the vault across a 4×4 grid of era × popularity
tier, hits Jikan's staff endpoint for each, and reports per-role fill
rates plus name-format observations. Purpose is to inform the Phase B
schema decisions:

  - Should we key on person.mal_id or normalized name? (mal_id — confirmed
    from initial probe)
  - Do we need to handle multiple people per role? (Yes — series with
    multi-season entries have multiple directors)
  - Do we need to handle multiple roles per person? (Yes — Watanabe holds
    Director + Episode Director + Script + Storyboard on Bebop)
  - What's the realistic fill rate per role we care about?
  - Which roles are worth ingesting?

One-off diagnostic. Delete after Phase A findings are captured in
ENGINE_WORK_PROGRESS.md and the schema design lands.

Cost: $0 (Jikan is free). Wallclock ~5-8 min for 240 shows at ~1.5s each.
"""
from __future__ import annotations

import json
import os
import random
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(str(ROOT / "env_variables.env"))

DB_PATH = ROOT / "data" / "anime_intelligence_v2.db"
# JIKAN_BASE_URL is required — the public api.jikan.moe fallback was
# retired because the upstream is in maintenance mode / brownout. Empty
# string here means "no default"; validated at main() entry below.
JIKAN_BASE = (os.environ.get("JIKAN_BASE_URL") or "").strip()

# Roles we plan to ingest for Item 2. Every other role Jikan returns is
# noted but not treated as a fill-rate target.
TARGET_ROLES = {
    "Director",
    "Music",
    "Original Creator",
    "Series Composition",
    "Character Design",
}

SAMPLES_PER_BUCKET = 15  # 4 eras × 4 popularity tiers × 15 = 240 shows


def load_vault_sample(seed: int = 20260914) -> list[dict]:
    """Stratified sample: 4 eras × 4 popularity tiers × SAMPLES_PER_BUCKET.

    Popularity bands mirror probe_popularity_bias.py so tier semantics
    stay comparable across diagnostics. Eras are hard-coded decade bins.
    """
    rng = random.Random(seed)
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.cursor()
    cur.execute(
        "SELECT id, english_title, release_year, scored_by "
        "FROM anime_info "
        "WHERE consensus_json IS NOT NULL AND release_year IS NOT NULL"
    )
    rows = cur.fetchall()
    conn.close()

    # Popularity thresholds from the current vault
    scored_sorted = sorted((r[3] or 0) for r in rows)
    n = len(scored_sorted)

    def pct(p: float) -> int:
        return scored_sorted[min(n - 1, int(n * p))]

    p25, p50, p75 = pct(0.25), pct(0.50), pct(0.75)

    def pop_tier(votes: int) -> str:
        if votes < p25:
            return "obscure"
        if votes < p50:
            return "niche"
        if votes < p75:
            return "known"
        return "popular_plus"

    def era_tier(year: int) -> str | None:
        if year < 2000:
            return "pre_2000"
        if year < 2010:
            return "2000s"
        if year < 2020:
            return "2010s"
        if year < 2030:
            return "2020s"
        return None

    buckets: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for mal_id, title, year, votes in rows:
        era = era_tier(year)
        if era is None:
            continue
        pop = pop_tier(votes or 0)
        buckets[(era, pop)].append({
            "mal_id": mal_id,
            "title": title,
            "release_year": year,
            "scored_by": votes or 0,
        })

    sample: list[dict] = []
    for key, entries in buckets.items():
        rng.shuffle(entries)
        picks = entries[:SAMPLES_PER_BUCKET]
        for p in picks:
            p["era"] = key[0]
            p["pop_tier"] = key[1]
        sample.extend(picks)

    return sample


def fetch_staff(mal_id: int, retries: int = 2) -> tuple[list[dict] | None, int, str | None]:
    """Return (staff_list, http_status, error_message).

    staff_list is a list of {name, person_mal_id, positions[]} dicts.
    None on any non-2xx or malformed response."""
    url = f"{JIKAN_BASE}/anime/{mal_id}/staff"
    last_err = None
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, timeout=15)
            if r.status_code == 200:
                data = r.json().get("data") or []
                staff = []
                for entry in data:
                    person = entry.get("person") or {}
                    staff.append({
                        "person_mal_id": person.get("mal_id"),
                        "name": person.get("name", ""),
                        "positions": entry.get("positions") or [],
                    })
                return staff, 200, None
            if r.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            return None, r.status_code, f"HTTP {r.status_code}"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            time.sleep(1 + attempt)
    return None, 0, last_err


def analyze(records: list[dict]) -> dict:
    """Compute per-role fill rates + name-format stats + role distribution."""
    n = len(records)
    role_fill: Counter[str] = Counter()  # role -> shows with >=1 person in that role
    role_people_count: dict[str, list[int]] = defaultdict(list)  # role -> [people_per_show]
    person_role_multiplicity: list[int] = []  # positions[] length per person
    all_roles: Counter[str] = Counter()
    name_format_examples: list[str] = []
    error_codes: Counter[str] = Counter()

    for rec in records:
        staff = rec.get("staff")
        if staff is None:
            error_codes[str(rec.get("error") or "unknown")] += 1
            continue

        roles_present: set[str] = set()
        role_people_here: Counter[str] = Counter()
        for entry in staff:
            person_role_multiplicity.append(len(entry["positions"]))
            for pos in entry["positions"]:
                all_roles[pos] += 1
                roles_present.add(pos)
                role_people_here[pos] += 1
            # Collect a few name format samples
            if len(name_format_examples) < 20 and entry["name"]:
                name_format_examples.append(entry["name"])

        for role in TARGET_ROLES:
            if role in roles_present:
                role_fill[role] += 1
                role_people_count[role].append(role_people_here[role])

    # Per-era-and-tier fill rates
    tier_fill: dict[tuple[str, str], dict] = defaultdict(lambda: {"n": 0, "roles": Counter()})
    for rec in records:
        if rec.get("staff") is None:
            continue
        key = (rec.get("era"), rec.get("pop_tier"))
        tier_fill[key]["n"] += 1
        roles_present = {p for entry in rec["staff"] for p in entry["positions"]}
        for role in TARGET_ROLES:
            if role in roles_present:
                tier_fill[key]["roles"][role] += 1

    return {
        "n_total": n,
        "n_successful": sum(1 for r in records if r.get("staff") is not None),
        "n_empty_staff": sum(1 for r in records if r.get("staff") == []),
        "error_codes": dict(error_codes),
        "target_role_fill": {r: role_fill[r] for r in TARGET_ROLES},
        "target_role_avg_people": {
            r: (sum(role_people_count[r]) / len(role_people_count[r])
                if role_people_count[r] else 0.0)
            for r in TARGET_ROLES
        },
        "target_role_max_people": {
            r: (max(role_people_count[r]) if role_people_count[r] else 0)
            for r in TARGET_ROLES
        },
        "person_role_multiplicity_avg": (
            sum(person_role_multiplicity) / len(person_role_multiplicity)
            if person_role_multiplicity else 0.0
        ),
        "person_role_multiplicity_max": (
            max(person_role_multiplicity) if person_role_multiplicity else 0
        ),
        "top_20_roles_seen": all_roles.most_common(20),
        "name_format_examples": name_format_examples,
        "tier_fill": {
            f"{k[0]}/{k[1]}": {"n": v["n"], "roles": dict(v["roles"])}
            for k, v in sorted(tier_fill.items())
        },
    }


def main():
    if not JIKAN_BASE:
        print(
            "ERROR: JIKAN_BASE_URL env var is required.\n"
            "  The public api.jikan.moe fallback was retired. Set\n"
            "  JIKAN_BASE_URL to the self-hosted Railway instance "
            "(e.g.,\n"
            "  https://jikan-rest-production-d553.up.railway.app/v4) "
            "before running.",
            file=sys.stderr,
        )
        return 2
    print(f"[probe] Sampling vault. Jikan base: {JIKAN_BASE}")
    sample = load_vault_sample()
    print(f"[probe] Selected {len(sample)} shows across {len({(s['era'], s['pop_tier']) for s in sample})} tiers")

    records: list[dict] = []
    for i, show in enumerate(sample, 1):
        staff, status, err = fetch_staff(show["mal_id"])
        record = {**show, "staff": staff, "http_status": status, "error": err}
        records.append(record)
        if i % 25 == 0 or i == len(sample):
            n_ok = sum(1 for r in records if r["staff"] is not None)
            print(f"[probe] {i}/{len(sample)}  ok={n_ok}  last={show['title'][:40]}  status={status}")
        # Small delay to be gentle on MAL upstream even via our Jikan
        time.sleep(0.3)

    analysis = analyze(records)

    out_path = ROOT / "data" / "probe_jikan_staff_results.json"
    with open(out_path, "w") as f:
        json.dump({
            "sample_size": len(records),
            "analysis": analysis,
            "raw_records": [
                {k: v for k, v in r.items() if k != "staff"} | {"staff_count": len(r["staff"]) if r["staff"] is not None else None}
                for r in records
            ],
        }, f, indent=2)

    print(f"\n[probe] Results written to {out_path}\n")

    n_total = analysis["n_total"]
    n_ok = analysis["n_successful"]
    print("=" * 80)
    print(f"OVERALL: {n_ok}/{n_total} shows returned staff data "
          f"({100*n_ok/n_total:.1f}%)")
    print(f"  Empty staff (returned but 0 entries): {analysis['n_empty_staff']}")
    print(f"  Error codes: {analysis['error_codes']}")
    print()
    print("TARGET ROLE FILL RATES (of successful responses):")
    for role in sorted(TARGET_ROLES):
        count = analysis["target_role_fill"][role]
        avg = analysis["target_role_avg_people"][role]
        mx = analysis["target_role_max_people"][role]
        print(f"  {role:<20}  {count}/{n_ok} ({100*count/max(n_ok,1):.1f}%)  "
              f"avg={avg:.1f}  max={mx}")
    print()
    print(f"Person role multiplicity: avg={analysis['person_role_multiplicity_avg']:.1f}, "
          f"max={analysis['person_role_multiplicity_max']}")
    print()
    print("PER-TIER FILL RATES (era/popularity):")
    for key, v in analysis["tier_fill"].items():
        n = v["n"]
        print(f"  {key:<25}  n={n:<3}  ", end="")
        for role in ["Director", "Music", "Original Creator", "Series Composition", "Character Design"]:
            c = v["roles"].get(role, 0)
            pct_str = f"{100*c/max(n,1):.0f}%"
            print(f"  {role[:3]}={pct_str:<5}", end="")
        print()
    print()
    print("TOP 20 ROLES SEEN (across all shows):")
    for role, count in analysis["top_20_roles_seen"]:
        print(f"  {role:<30}  {count}")
    print()
    print("NAME FORMAT EXAMPLES:")
    for name in analysis["name_format_examples"][:15]:
        print(f"  {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
