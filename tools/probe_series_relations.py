"""
Phase A empirical probe for Initiative 1 (Multi-Season Series Consolidation).

Hits Jikan's /anime/{mal_id}/relations endpoint for a curated set of 20
franchises that span the edge-case shapes documented in IMPLEMENTATION_PLAN.md:
long-running franchises, reboot pairs, spin-offs, multiverses, summary/recap
films, split-cour final seasons, retitled sequel chains.

The goal is to verify (or refine) the clustering rules before any schema work:
    sequel | prequel | parent_story | side_story | summary -> same cluster
    alternative_setting | alternative_version | spin_off  -> separate cluster
    everything else (Adaptation / Character / Other ...)   -> flagged for review

Output:
    - data/series_relations_probe.json (uncommitted, full structured dump)
    - stdout: per-franchise summary + global relation-type tally + "surprise" list

No commit. Re-runnable; idempotent. Run from project root:
    python tools/probe_series_relations.py
"""

from __future__ import annotations

import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import requests

ROOT_DIR = Path(__file__).resolve().parent.parent
OUTPUT_PATH = ROOT_DIR / "data" / "series_relations_probe.json"

JIKAN_BASE = "https://api.jikan.moe/v4"
RATE_LIMIT_DELAY = 1.1  # Jikan caps ~3 req/s; conservative for back-to-back calls

# Clustering hypothesis from IMPLEMENTATION_PLAN.md (Phase A is meant to verify
# or refine these). Comparisons are case-insensitive — Jikan returns Title Case
# strings ("Side Story", "Spin-Off") but we normalize before lookup so the rule
# sets stay readable.
SAME_CLUSTER = {
    "sequel",
    "prequel",
    "parent story",
    "side story",
    "summary",
    "full story",   # included tentatively — a movie that retells the TV story
}
SEPARATE_CLUSTER = {
    "alternative setting",
    "alternative version",
    "spin-off",
}
NON_ANIME_OR_SOFT = {
    "adaptation",   # usually links to manga / LN — same story, different medium
    "character",    # shared characters, distinct work
    "other",        # catch-all; needs case-by-case review
}

# Anchor entries representing the edge-case shapes. Each anchor is ONE
# representative MAL ID per franchise — we are not yet traversing the
# full graph (Phase B will). MAL IDs are well-known public identifiers.
PROBE_SET = [
    # --- Long-running franchises ---
    {"franchise": "Gintama", "anchor_id": 918,
     "shape": "long-running w/ retitled continuations, films, recap"},
    {"franchise": "Attack on Titan", "anchor_id": 16498,
     "shape": "split-cour final season"},
    {"franchise": "Gundam UC (OVA)", "anchor_id": 6336,
     "shape": "OVA franchise w/ TV recompilation"},
    {"franchise": "Gundam: Iron-Blooded Orphans", "anchor_id": 31251,
     "shape": "sequel-only TV franchise"},
    {"franchise": "Frieren", "anchor_id": 52991,
     "shape": "early-series, Season 2 announced"},

    # --- Reboot pairs ---
    {"franchise": "Fullmetal Alchemist (2003)", "anchor_id": 121,
     "shape": "reboot pair — original (separate cluster expected from Brotherhood)"},
    {"franchise": "Fullmetal Alchemist: Brotherhood", "anchor_id": 5114,
     "shape": "reboot pair — remake (separate cluster expected from 2003)"},
    {"franchise": "Hunter x Hunter (1999)", "anchor_id": 136,
     "shape": "reboot pair — original"},
    {"franchise": "Hunter x Hunter (2011)", "anchor_id": 11061,
     "shape": "reboot pair — remake"},

    # --- Spin-offs and alt timelines ---
    {"franchise": "Code Geass: Lelouch of the Rebellion", "anchor_id": 1575,
     "shape": "main timeline w/ R2 sequel + Akito spin-off"},
    {"franchise": "Code Geass: Akito the Exiled", "anchor_id": 8888,
     "shape": "alt-setting spin-off (separate cluster expected)"},

    # --- Franchise multiverses ---
    {"franchise": "Super Dimension Fortress Macross (1982)", "anchor_id": 1088,
     "shape": "multiverse anchor — multiple unrelated continuities"},
    {"franchise": "Tenchi Muyo! Ryo-Ohki (OVA)", "anchor_id": 1409,
     "shape": "multiverse anchor — multiple unrelated continuities"},

    # --- Summary / recap films + sequel ---
    {"franchise": "Puella Magi Madoka Magica (TV)", "anchor_id": 9756,
     "shape": "TV + recap films + canon sequel (Rebellion)"},

    # --- Sequel-heavy modern franchises ---
    {"franchise": "Fire Force", "anchor_id": 38671,
     "shape": "sequel + planned future seasons"},
    {"franchise": "Re:Zero kara Hajimeru Isekai Seikatsu", "anchor_id": 31240,
     "shape": "sequels + side stories + recap"},
    {"franchise": "Boku no Hero Academia", "anchor_id": 31964,
     "shape": "long sequel chain w/ films"},
    {"franchise": "Kimetsu no Yaiba", "anchor_id": 38000,
     "shape": "TV + canon-continuation films"},
    {"franchise": "K-On!", "anchor_id": 5680,
     "shape": "sequel + film"},
    {"franchise": "JoJo's Bizarre Adventure (2012)", "anchor_id": 14719,
     "shape": "retitled sequel chain"},
]


def fetch_relations(mal_id: int) -> tuple[list[dict] | None, str]:
    """Hit /anime/{id}/relations and return (relations_list, error_str)."""
    url = f"{JIKAN_BASE}/anime/{mal_id}/relations"
    try:
        resp = requests.get(url, timeout=15)
        if resp.status_code == 429:
            time.sleep(4.0)
            resp = requests.get(url, timeout=15)
        if resp.status_code == 404:
            return None, "404 — MAL ID not found"
        resp.raise_for_status()
        return resp.json().get("data", []), ""
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def fetch_anime_name(mal_id: int) -> str:
    """Quick lookup of the anchor's canonical English/Romaji title."""
    url = f"{JIKAN_BASE}/anime/{mal_id}"
    try:
        resp = requests.get(url, timeout=15)
        if resp.status_code == 429:
            time.sleep(4.0)
            resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json().get("data", {})
        return data.get("title_english") or data.get("title") or "(unknown)"
    except Exception:
        return "(lookup failed)"


def classify_relation(name: str) -> str:
    key = name.strip().lower()
    if key in SAME_CLUSTER:
        return "SAME"
    if key in SEPARATE_CLUSTER:
        return "SEPARATE"
    if key in NON_ANIME_OR_SOFT:
        return "SOFT"
    return "UNKNOWN"


def main() -> int:
    # Windows console defaults to cp1252; anime titles regularly contain
    # characters outside that range (Gintama′, Madoka's ★, etc.). Force
    # UTF-8 with replacement so the probe never dies on a print() call.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    OUTPUT_PATH.parent.mkdir(exist_ok=True)
    results: list[dict] = []
    relation_tally: Counter[str] = Counter()
    relation_examples: dict[str, list[str]] = defaultdict(list)
    surprises: list[str] = []

    print("=" * 78)
    print("Phase A Empirical Relations Probe — Initiative 1")
    print(f"Probing {len(PROBE_SET)} franchises via Jikan v4")
    print("=" * 78)

    for idx, entry in enumerate(PROBE_SET, start=1):
        franchise = entry["franchise"]
        anchor_id = entry["anchor_id"]
        shape = entry["shape"]

        print(f"\n[{idx:02d}/{len(PROBE_SET):02d}] {franchise} (MAL {anchor_id})")
        print(f"      Shape:  {shape}")

        canonical = fetch_anime_name(anchor_id)
        time.sleep(RATE_LIMIT_DELAY)
        print(f"      Anchor: {canonical}")

        relations, err = fetch_relations(anchor_id)
        time.sleep(RATE_LIMIT_DELAY)

        if err:
            print(f"      ERROR: {err}")
            results.append({
                "franchise": franchise,
                "anchor_id": anchor_id,
                "anchor_name": canonical,
                "shape": shape,
                "error": err,
            })
            continue

        per_franchise_relations = defaultdict(list)
        for rel in relations or []:
            rel_name = rel.get("relation", "(no name)")
            for ent in rel.get("entry", []):
                if ent.get("type") != "anime":
                    continue
                per_franchise_relations[rel_name].append({
                    "mal_id": ent.get("mal_id"),
                    "name": ent.get("name"),
                })
                relation_tally[rel_name] += 1
                if len(relation_examples[rel_name]) < 3:
                    relation_examples[rel_name].append(
                        f"{franchise}: {anchor_id} -> {ent.get('mal_id')} ({ent.get('name')})"
                    )

        if not per_franchise_relations:
            print("      (no anime relations returned)")
        else:
            for rel_name in sorted(per_franchise_relations.keys()):
                classification = classify_relation(rel_name)
                items = per_franchise_relations[rel_name]
                marker = {"SAME": "  +", "SEPARATE": "  -", "SOFT": "  ~", "UNKNOWN": "  ?"}[classification]
                print(f"      {marker} [{classification}] {rel_name} (n={len(items)})")
                for it in items:
                    print(f"          {it['mal_id']:>6}  {it['name']}")
                if classification == "UNKNOWN":
                    surprises.append(
                        f"  - {franchise}: unrecognized relation '{rel_name}' "
                        f"({len(items)} anime entries)"
                    )

        results.append({
            "franchise": franchise,
            "anchor_id": anchor_id,
            "anchor_name": canonical,
            "shape": shape,
            "relations": {
                rel_name: per_franchise_relations[rel_name]
                for rel_name in sorted(per_franchise_relations.keys())
            },
        })

    # ------------------------------------------------------------------
    print("\n" + "=" * 78)
    print("Global Relation-Type Tally (anime entries only)")
    print("=" * 78)
    for rel_name, count in relation_tally.most_common():
        classification = classify_relation(rel_name)
        print(f"  [{classification:8s}] {rel_name:25s}  {count}")
    if not relation_tally:
        print("  (no relations observed — probe likely failed)")

    print("\n" + "=" * 78)
    print("Surprises (relation types not in the IMPLEMENTATION_PLAN ruleset)")
    print("=" * 78)
    if surprises:
        for s in surprises:
            print(s)
    else:
        print("  (none — every relation type fits an existing rule)")

    OUTPUT_PATH.write_text(json.dumps({
        "probe_set": PROBE_SET,
        "results": results,
        "relation_tally": dict(relation_tally),
        "relation_examples": dict(relation_examples),
        "surprises": surprises,
        "clustering_hypothesis": {
            "same_cluster": sorted(SAME_CLUSTER),
            "separate_cluster": sorted(SEPARATE_CLUSTER),
            "soft": sorted(NON_ANIME_OR_SOFT),
        },
    }, indent=2), encoding="utf-8")
    print(f"\nFull structured dump written to: {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
