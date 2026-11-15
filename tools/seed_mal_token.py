"""
TOOL: tools/seed_mal_token.py
PURPOSE: One-off seed of the mal_oauth_token Supabase singleton row
from the local token_data.json file. Initiative 3 Phase B.

Run order:
  1. Apply tools/supabase_setup_mal_token.sql via the Supabase SQL Editor.
  2. Ensure SUPABASE_URL + SUPABASE_SERVICE_KEY are in env_variables.env.
  3. Ensure ROOT_DIR/token_data.json holds a valid access_token +
     refresh_token (the project's current MAL OAuth credentials).
  4. python tools/seed_mal_token.py

Idempotent: re-running upserts the same row, so it's safe to run
multiple times during environment setup or after token rotation.

After this script lands the row and Phase C wires production refresh
paths through Supabase, this seed step is needed exactly once per
environment (initial bootstrap). Subsequent refreshes update the row
automatically.
"""
import json
import os
import sys

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from dotenv import load_dotenv

load_dotenv(os.path.join(ROOT_DIR, "env_variables.env"))

from analysis.mal_token_store import read_token, write_token  # noqa: E402

TOKEN_PATH = os.path.join(ROOT_DIR, "token_data.json")


def _preview(value: str | None) -> str:
    if not value or not isinstance(value, str):
        return repr(value)
    return f"...{value[-4:]} ({len(value)} chars)"


def main():
    if not os.environ.get("SUPABASE_URL") or not os.environ.get(
        "SUPABASE_SERVICE_KEY"
    ):
        print(
            "[ERROR] SUPABASE_URL and SUPABASE_SERVICE_KEY must be set in "
            "env_variables.env."
        )
        sys.exit(1)

    if not os.path.exists(TOKEN_PATH):
        print(f"[ERROR] {TOKEN_PATH} not found. Cannot seed.")
        sys.exit(1)

    with open(TOKEN_PATH) as f:
        token_data = json.load(f)

    if "access_token" not in token_data or "refresh_token" not in token_data:
        print(f"[ERROR] {TOKEN_PATH} missing access_token or refresh_token.")
        sys.exit(1)

    print(f"Seeding {TOKEN_PATH} -> mal_oauth_token singleton ...")
    print(f"  access_token:  {_preview(token_data.get('access_token'))}")
    print(f"  refresh_token: {_preview(token_data.get('refresh_token'))}")

    existing = read_token()
    if existing:
        print(
            f"  (existing row will be overwritten -- "
            f"prior refreshed_at: {existing.get('refreshed_at')})"
        )

    ok = write_token(token_data)
    if not ok:
        print("[ERROR] Seed write failed. Check Supabase config + transport.")
        sys.exit(2)

    confirmed = read_token()
    if not confirmed:
        print(
            "[ERROR] Post-write read returned None. Inconsistent state -- "
            "investigate."
        )
        sys.exit(2)

    print("[OK] Seed complete.")
    print(f"   access_token:  {_preview(confirmed.get('access_token'))}")
    print(f"   refresh_token: {_preview(confirmed.get('refresh_token'))}")
    print(f"   token_type:    {confirmed.get('token_type')}")
    print(f"   expires_in:    {confirmed.get('expires_in')}")
    print(f"   refreshed_at:  {confirmed.get('refreshed_at')}")


if __name__ == "__main__":
    main()
