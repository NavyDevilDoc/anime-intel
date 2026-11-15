"""
Shared dependencies for FastAPI routes.
Singleton instances are created once and reused across requests.
"""
import os
import sys

# --- BULLETPROOF PATH ANCHORING ---
SCRIPT_DIR = os.path.abspath(os.path.dirname(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from dotenv import load_dotenv

load_dotenv(os.path.join(ROOT_DIR, "env_variables.env"))

from analysis.recommendation_engine import RecommendationEngine

# Singleton engine instance — loaded once, shared across all requests.
# FAISS index is loaded on first search, stays in memory.
_engine = None


def get_engine() -> RecommendationEngine:
    global _engine
    if _engine is None:
        api_key = os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise RuntimeError("GOOGLE_API_KEY not found in environment.")
        _engine = RecommendationEngine(api_key=api_key)
    return _engine
