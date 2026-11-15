"""Health check endpoint for monitoring and Railway healthchecks."""
import os
import sqlite3

import faiss
from fastapi import APIRouter

router = APIRouter()

SCRIPT_DIR = os.path.abspath(os.path.dirname(__file__))
ROOT_DIR = os.path.dirname(os.path.dirname(SCRIPT_DIR))
DATA_DIR = os.path.join(ROOT_DIR, "data")


@router.get("/api/health")
def health_check():
    """Returns system status: anime count, FAISS index dimensions, and DB status."""
    status = {"status": "ok", "anime_count": 0, "index_dims": 0, "db_online": False}

    # Check SQLite. Health endpoint is fail-soft on every probe — a
    # missing DB or FAISS index still returns 200 with the failing
    # dimension zeroed out, so the load balancer can distinguish "app up
    # but degraded" from "app down entirely."
    db_path = os.path.join(DATA_DIR, "anime_intelligence_v2.db")
    try:
        with sqlite3.connect(db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM anime_info").fetchone()[0]
            status["anime_count"] = count
            status["db_online"] = True
    except Exception:  # noqa: BLE001, S110 — health probe: any DB failure surfaces as db_online=False, not a 500
        pass

    # Check FAISS index
    index_path = os.path.join(DATA_DIR, "anime_vector_index.faiss")
    try:
        index = faiss.read_index(index_path)
        status["index_dims"] = index.d
    except Exception:  # noqa: BLE001, S110 — health probe: FAISS load failure surfaces as index_dims=0, not a 500
        pass

    return status
