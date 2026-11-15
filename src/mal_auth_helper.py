"""
MODULE: src/mal_auth_helper.py
FUNCTION: Manages MAL OAuth2 PKCE Handshake and Lifecycle.
"""
import json
import os
import secrets

import requests
from dotenv import load_dotenv

# --- DYNAMIC PATH ANCHOR ---
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
env_path = os.path.join(ROOT_DIR, "env_variables.env")
load_dotenv(env_path)

CLIENT_ID = os.getenv("MAL_CLIENT_ID", "").strip()
CLIENT_SECRET = os.getenv("MAL_CLIENT_SECRET", "").strip()
TOKEN_PATH = os.path.join(ROOT_DIR, "token_data.json")

def generate_pkce_verifier():
    """Generates the plain verifier (exactly 128 chars)."""
    # 96 bytes urlsafe encoded yields exactly 128 characters
    return secrets.token_urlsafe(96)[:128] 

def print_auth_url(v):
    url = (
        f"https://myanimelist.net/v1/oauth2/authorize?"
        f"response_type=code&"
        f"client_id={CLIENT_ID}&"
        f"code_challenge={v}&"
        f"code_challenge_method=plain"
    )
    print("\n" + "📡"*20)
    print(f"STEP 1: VISIT THIS URL TO AUTHORIZE:\n\n{url}")
    print("\n" + "📡"*20)

def trade_code_for_token(auth_code, verifier):
    """The initial handshake."""
    url = "https://myanimelist.net/v1/oauth2/token"
    
    data = {
        'client_id': CLIENT_ID,
        'client_secret': CLIENT_SECRET,
        'code': auth_code.strip(),
        'code_verifier': verifier,
        'grant_type': 'authorization_code'
    }
    
    print(f"\n[DEBUG] Attempting swap with ID: {CLIENT_ID[:5]}...")
    response = requests.post(url, data=data)
    return response.json()

def pulse_check(access_token):
    """
    Lightweight MAL API call to verify a token is still valid.
    Hits the /users/@me endpoint (cheapest authenticated call).

    Args:
        access_token: The MAL access token to validate.

    Returns:
        True if the token is valid (200 response), False otherwise.
    """
    try:
        response = requests.get(
            "https://api.myanimelist.net/v2/users/@me",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        return response.status_code == 200
    except Exception:
        return False


def refresh_mal_token(token_path=None, return_data=False):
    """
    Trade a stored refresh_token for a new access_token + refresh_token pair.

    Source chain for the input refresh_token (Initiative 3):
        1. Supabase mal_oauth_token singleton — production canonical.
        2. token_data.json on disk — local dev fallback.

    On success, the new tokens are persisted to BOTH:
        - Supabase (production durability, source of truth).
        - Disk (local-dev consistency + insurance). Best-effort on
          Railway, where the writable layer is ephemeral and disk
          writes are lost on container restart — Supabase already
          holds the new tokens in that case.

    Args:
        token_path: Override path to token JSON file. Defaults to TOKEN_PATH.
        return_data: If True, return the new token dict on success (for GH Actions).
                     If False, return True/False for backward compatibility.

    Returns:
        - return_data=False: True if successful, False otherwise (original behavior).
        - return_data=True: New token dict on success, None on failure.
    """
    from analysis.mal_token_store import read_token, write_token

    path = token_path or TOKEN_PATH

    # Read refresh_token from Supabase first; fall back to disk.
    refresh_token = None
    stored = read_token()
    if stored and stored.get("refresh_token"):
        refresh_token = stored["refresh_token"]
    elif os.path.exists(path):
        with open(path) as f:
            refresh_token = json.load(f).get('refresh_token')

    if not refresh_token:
        print("[ERROR] No refresh_token available from Supabase or disk.")
        return None if return_data else False

    url = "https://myanimelist.net/v1/oauth2/token"
    data = {
        'client_id': CLIENT_ID,
        'client_secret': CLIENT_SECRET,
        'grant_type': 'refresh_token',
        'refresh_token': refresh_token
    }

    print("\nInitiating Token Refresh Protocol...")
    response = requests.post(url, data=data)

    if response.status_code != 200:
        print(f"[ERROR] Refresh Failed: {response.status_code} - {response.text}")
        return None if return_data else False

    new_token_data = response.json()

    # Write to Supabase first (production source of truth).
    supabase_ok = write_token(new_token_data)
    if supabase_ok:
        print("[OK] Token persisted to Supabase.")
    else:
        # Not catastrophic for local dev (no Supabase configured) but
        # worth surfacing — production refresh that misses Supabase
        # will get re-invalidated on the next container restart.
        print("[WARN] Supabase write skipped or failed; disk-only durability.")

    # Also write to disk (best-effort — Railway writable layer is ephemeral,
    # but local-dev workflows like pulse_check still inspect this file).
    try:
        with open(path, "w") as f:
            json.dump(new_token_data, f, indent=4)
        print("[OK] Token written to disk (token_data.json).")
    except OSError as e:
        print(f"[WARN] Disk write skipped ({e}); Supabase has the new token.")

    if return_data:
        return new_token_data
    return True

if __name__ == "__main__":
    print("1. Initialize New Token (Manual Auth)")
    print("2. Refresh Existing Token")
    choice = input("\nSelect an operation (1/2): ").strip()

    if choice == '1':
        v = generate_pkce_verifier()
        print_auth_url(v)
        code = input("\nPASTE AUTHORIZATION CODE HERE: ").strip()
        print("\n🚀 Executing Handshake (Bypassing Redirect URI)...")
        token_data = trade_code_for_token(code, v)
        
        if "access_token" in token_data:
            with open(TOKEN_PATH, "w") as f:
                json.dump(token_data, f, indent=4)
            print("\n" + "🏁"*20 + "\nMISSION SUCCESS: LINK ESTABLISHED\n" + "🏁"*20)
        else:
            print(f"\n❌ Handshake Failed: {token_data}")
            
    elif choice == '2':
        refresh_mal_token()
    else:
        print("Invalid choice.")