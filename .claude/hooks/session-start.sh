#!/bin/bash
set -euo pipefail

# Only run in remote (cloud) environments — never touch a developer's local machine.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

REPO_DIR="${CLAUDE_PROJECT_DIR:-$(git -C "$(dirname "$0")" rev-parse --show-toplevel)}"
cd "$REPO_DIR"

# --- Dependencies -----------------------------------------------------------
# .python-version pins 3.14.2 (Windows local dev), which has no Linux prebuilt.
# Cloud uses Python 3.13 (available as a system interpreter) instead.
# uv sync is idempotent: fast no-op when the lockfile hash is already satisfied.
echo "[session-start] Syncing dependencies (uv sync --dev --python 3.13)..."
uv sync --dev --python 3.13

# --- PATH export ------------------------------------------------------------
# Write on every startup|resume; the env file may be fresh after a resume.
VENV_BIN="$REPO_DIR/.venv/bin"
echo "export PATH=\"$VENV_BIN:\$PATH\"" >> "$CLAUDE_ENV_FILE"
echo "[session-start] Exported $VENV_BIN to PATH."
