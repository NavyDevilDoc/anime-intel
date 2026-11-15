# dev-backend.ps1
#
# Starts the FastAPI backend locally for engine iteration.
#
# Usage (from repo root):
#   .\scripts\dev-backend.ps1
#
# Reads env from env_variables.env via python-dotenv (already how the app
# boots inside api/dependencies.py). Runs uvicorn on port 8000 with --reload
# so code changes hot-restart. Log level is `info` — bump to `debug` on the
# command line if you need to see per-request tracing.
#
# Pairs with tools\query_probe.py for a fast direct-import iteration loop
# that skips the HTTP layer entirely.

$ErrorActionPreference = "Stop"

# Anchor to repo root so this works regardless of where the user invokes
# the script from. Script lives at <repo>\scripts\dev-backend.ps1, so the
# repo root is the script's parent directory.
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

Write-Host "Starting uvicorn on http://localhost:8000 (reload enabled)..." -ForegroundColor Cyan
uvicorn api.main:app --reload --port 8000 --log-level info
