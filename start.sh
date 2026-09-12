#!/usr/bin/env bash
# ZCode Universal Model Gateway - macOS/Linux launcher.
#
# Finds a Python 3.11+ interpreter, installs dependencies if needed, then
# starts the gateway. No machine-specific paths are hard-coded: if you have
# activated a conda env or a venv, that "python" is used automatically.
#
# Never put API keys in this file - export them in your shell instead.
# User-facing text stays ASCII here; the gateway prints the Chinese banner.

set -u
cd "$(dirname "$0")"

if [ -z "${GATEWAY_HOST:-}" ]; then GATEWAY_HOST="127.0.0.1"; fi
if [ -z "${GATEWAY_PORT:-}" ]; then GATEWAY_PORT="8787"; fi

# --- find a suitable Python -------------------------------------------------
PY=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then
    if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
      PY="$candidate"
      break
    fi
  fi
done

if [ -z "$PY" ]; then
  echo "[ERROR] No Python 3.11+ found." >&2
  echo "        Install Python 3 or Miniconda and make sure it is on your PATH," >&2
  echo "        then run this script again." >&2
  exit 1
fi

# --- dependencies -----------------------------------------------------------
if ! "$PY" -c 'import fastapi, uvicorn, httpx, yaml, pydantic' >/dev/null 2>&1; then
  echo "Installing dependencies (first run only)..."
  "$PY" -m pip install --disable-pip-version-check -r requirements.txt || {
    echo "[ERROR] Failed to install dependencies. Check your network/proxy and retry." >&2
    exit 1
  }
fi

# --- run --------------------------------------------------------------------
exec "$PY" -m gateway
