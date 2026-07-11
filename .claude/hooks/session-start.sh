#!/bin/bash
# SessionStart hook: bootstrap the Python env for Claude Code cloud sessions.
#
# Cloud network facts this script is built around (verified 2026-07):
#   - PyPI (pypi.org / files.pythonhosted.org) is always reachable — the
#     reliable way to install/update uv. `uv self update` is not: cloud
#     sessions route all GitHub traffic through a proxy that 403s every
#     GitHub path outside the session's bound repos, at every network
#     access level. (uv misreports that 403 as "rate limit exceeded".)
#   - uv downloads managed CPython from releases.astral.sh, falling back to
#     GitHub releases (always 403, see above). The CDN is reachable only in
#     environments whose Custom network allowlist includes `*.astral.sh` —
#     there the pinned interpreter installs in seconds. Everywhere else,
#     fall back to the image's system Python and export UV_PYTHON so later
#     `uv run` calls don't retry the blocked download of the
#     .python-version pin. Both paths are healthy end states.
set -uo pipefail

# Only run in remote (cloud) environments — never touch a developer's machine.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

REPO_DIR="${CLAUDE_PROJECT_DIR:-$(git -C "$(dirname "$0")" rev-parse --show-toplevel)}"
cd "$REPO_DIR" || exit 1

log() { echo "[session-start] $*"; }
fail() { log "ERROR: $*"; exit 1; }

# pip --user installs into ~/.local/bin; ensure it shadows any system uv even
# if a future cloud image stops putting ~/.local/bin first on PATH.
export PATH="$HOME/.local/bin:$PATH"

# --- 1. Ensure a modern uv (from PyPI, not GitHub) ---------------------------
# The cloud image ships uv 0.8.17, which can't read this repo's lockfile
# (revision 3) and predates CPython 3.14 in its download catalog.
MIN_UV="0.11.0"
uv_ver="$(uv --version 2>/dev/null | awk '{print $2}')"
if [ "$(printf '%s\n' "$MIN_UV" "${uv_ver:-0}" | sort -V | head -1)" != "$MIN_UV" ]; then
  log "uv ${uv_ver:-missing} is too old; installing latest from PyPI..."
  # --break-system-packages retry covers images whose system Python is marked
  # externally-managed (PEP 668); --user keeps it out of system site-packages.
  python3 -m pip install --user --quiet --upgrade uv \
    || python3 -m pip install --user --quiet --break-system-packages --upgrade uv \
    || fail "could not install uv from PyPI"
  hash -r
  uv_ver="$(uv --version 2>/dev/null | awk '{print $2}')"
  if [ "$(printf '%s\n' "$MIN_UV" "${uv_ver:-0}" | sort -V | head -1)" != "$MIN_UV" ]; then
    fail "uv upgrade did not take effect (still resolving uv ${uv_ver:-missing}); is ~/.local/bin on PATH?"
  fi
  log "uv updated: uv $uv_ver"
fi

# --- 2. Interpreter + dependencies -------------------------------------------
PIN="$(cat .python-version 2>/dev/null || echo 3.14.2)"
FALLBACK="3.13"  # newest system Python in the cloud image

if uv python find "$PIN" >/dev/null 2>&1 || uv python install "$PIN" >/dev/null 2>&1; then
  log "Using pinned Python $PIN."
  uv sync --dev || fail "uv sync failed for pinned Python $PIN"
else
  log "Python $PIN unavailable (likely because this environment's allowlist doesn't cover releases.astral.sh); using system Python $FALLBACK instead."
  uv sync --dev -p "$FALLBACK" || fail "uv sync failed for fallback Python $FALLBACK"
  if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
    echo "export UV_PYTHON=$FALLBACK" >> "$CLAUDE_ENV_FILE"
    log "Exported UV_PYTHON=$FALLBACK (overrides the .python-version pin for uv commands)."
  fi
fi

# --- 3. PATH ------------------------------------------------------------------
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  echo "export PATH=\"$REPO_DIR/.venv/bin:$HOME/.local/bin:\$PATH\"" >> "$CLAUDE_ENV_FILE"
fi

log "Environment ready: $("$REPO_DIR/.venv/bin/python" --version 2>&1)"
