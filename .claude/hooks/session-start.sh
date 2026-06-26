#!/bin/bash
set -euo pipefail

# Only run in remote (cloud) environments — never touch a developer's local machine.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

REPO_DIR="${CLAUDE_PROJECT_DIR:-$(git -C "$(dirname "$0")" rev-parse --show-toplevel)}"
cd "$REPO_DIR"

# --- uv self-update ---------------------------------------------------------
# Cloud images may ship an old uv whose Python catalog predates 3.14 GA.
# Self-updating ensures the version manifest includes all current CPython builds.
echo "[session-start] Updating uv..."
uv self update || true

# --- Dependencies -----------------------------------------------------------
# uv reads .python-version (3.14.2) and downloads the interpreter if absent.
# uv sync is idempotent: fast no-op when the lockfile hash is already satisfied.
echo "[session-start] Syncing dependencies (uv sync --dev)..."
uv sync --dev

# --- PATH export ------------------------------------------------------------
# Write on every startup|resume; the env file may be fresh after a resume.
# Guard against CLAUDE_ENV_FILE being unbound (set -u would crash otherwise).
VENV_BIN="$REPO_DIR/.venv/bin"
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  echo "export PATH=\"$VENV_BIN:\$PATH\"" >> "$CLAUDE_ENV_FILE"
  echo "[session-start] Exported $VENV_BIN to PATH."
fi
