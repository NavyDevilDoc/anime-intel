"""MAL API client used by the seasonal ingestor.

Reads the MAL OAuth `access_token` via the Supabase → env → disk
fallback chain established by PR #82. The Supabase singleton
(`mal_oauth_token`) is the canonical source in production; env / disk
are fallbacks for local dev and pre-#82 legacy paths respectively.

PR #82 originally moved the canonical MAL token to Supabase and
dropped the disk-materialization step from
`.github/workflows/seasonal-ingest.yml`. That PR updated the
fetcher-side clients (`analysis.watchlist_fetcher`,
`src.mal_auth_helper`) but missed this ingestor-side client, which
continued to read only from disk. The first seasonal ingest after
PR #82 surfaced the gap as a hard `FileNotFoundError` at MALClient
init time. This module now mirrors the fetcher's fallback chain.
"""
import json
import logging
import os
import sys

# --- BULLETPROOF PATH ANCHORING ---
# Ensures `from analysis.mal_token_store import read_token` resolves
# regardless of how the ingestor gets invoked.
SCRIPT_DIR = os.path.abspath(os.path.dirname(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import requests

logger = logging.getLogger(__name__)


class MALClient:
    def __init__(self, token_path="token_data.json"):
        self.token_path = token_path
        self.session = requests.Session()
        self.headers = {}  # Re-initializing the explicit attribute for the ingestor
        self._load_token()

    def _load_token(self):
        """Load the MAL access_token via Supabase → env → disk fallback.

        Mirrors analysis.watchlist_fetcher._read_mal_token (PR #82).
        Raises RuntimeError only when all three sources miss.
        """
        token = self._read_supabase_token()
        source = "supabase"

        if not token:
            token = os.environ.get("MAL_ACCESS_TOKEN")
            source = "env" if token else source

        if not token and os.path.exists(self.token_path):
            with open(self.token_path, encoding="utf-8") as f:
                token_data = json.load(f)
                token = token_data.get("access_token")
                source = "disk" if token else source

        if not token:
            raise RuntimeError(
                "MAL access_token not found. Checked Supabase "
                "(mal_oauth_token), MAL_ACCESS_TOKEN env, and disk "
                f"({self.token_path}). Run tools/seed_mal_token.py to "
                "bootstrap the Supabase row."
            )

        logger.info(f"MALClient loaded MAL access_token via {source}.")
        self.token = token
        self.headers = {"Authorization": f"Bearer {self.token}"}
        self.session.headers.update(self.headers)

    def refresh_and_reload(self):
        """Rotate the MAL OAuth token via refresh_token and reload headers.

        Called by get_with_auto_refresh on 401; also usable standalone for
        pre-run pulse checks. Mirrors the self-healing behavior the
        seasonal-ingest workflow gained in PR #82 (pre-run refresh trigger)
        so local + one-off ingest runs are equally resilient.
        """
        from src.mal_auth_helper import refresh_mal_token
        refresh_mal_token()  # writes new access + refresh tokens to Supabase + disk
        self._load_token()   # re-reads and updates self.token / self.headers / session

    def get_with_auto_refresh(self, url, params=None, timeout=30):
        """GET a MAL API resource with automatic token-refresh on 401.

        Retries once after refresh; propagates any other status via the
        returned response (caller's responsibility to raise_for_status).
        Network errors bubble up unchanged — refresh only fires on 401.
        If refresh itself raises, the exception propagates rather than
        masking the underlying auth-infrastructure failure.
        """
        response = requests.get(url, headers=self.headers, params=params, timeout=timeout)
        if response.status_code == 401:
            logger.warning(f"MAL API 401 for {url} — refreshing token and retrying once.")
            self.refresh_and_reload()
            response = requests.get(url, headers=self.headers, params=params, timeout=timeout)
        return response

    @staticmethod
    def _read_supabase_token() -> str | None:
        """Read access_token from the Supabase singleton, defensively.

        Returns None on any failure — caller falls through to env/disk.
        Isolated as a staticmethod so tests can patch this one seam
        without stubbing the full Supabase client.
        """
        try:
            from analysis.mal_token_store import read_token
            row = read_token()
        except Exception as e:  # noqa: BLE001 — Supabase transport failure must degrade to env / disk fallback, not crash MALClient init
            logger.warning(f"MALClient Supabase token read failed: {e}")
            return None
        if row and row.get("access_token"):
            return row["access_token"]
        return None
