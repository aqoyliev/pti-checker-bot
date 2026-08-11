#!/bin/bash
# SessionStart hook for Claude Code on the web.
#
# Installs the system + Python dependencies a fresh remote container needs so
# that linting (`ruff check .`) and tests (`pytest`) work out of the box.
# Runs synchronously: the session waits until deps are ready, which avoids races
# where Claude tries to run tests/linters before they're installed.
set -euo pipefail

# Only run in Claude Code's remote (web) environment. Local dev manages its own
# virtualenv / Docker setup and shouldn't be touched by this hook.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-$(pwd)}"

# --- system deps -------------------------------------------------------------
# ffmpeg/ffprobe are required by utils.gemini.extract_frames (video → frames).
# Best-effort: a blocked package mirror under a strict network policy shouldn't
# abort the whole session — only the video path would be affected.
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "[session-start] Installing ffmpeg…"
  if command -v apt-get >/dev/null 2>&1; then
    # `apt-get update` can fail hard on unrelated third-party repos (e.g. a
    # forbidden PPA). That must not block installing ffmpeg from the base
    # image's already-present package lists, so tolerate the update failure.
    apt-get update -qq || echo "[session-start] WARN: apt-get update had errors; continuing with cached lists."
    apt-get install -y --no-install-recommends ffmpeg \
      || echo "[session-start] WARN: could not install ffmpeg; video frame extraction will be unavailable."
  else
    echo "[session-start] WARN: apt-get not available; skipping ffmpeg install."
  fi
fi

# --- python deps -------------------------------------------------------------
# Debian's base interpreter is PEP 668 "externally managed"; pass the override
# when this pip supports it so installs don't error out.
BSP=""
if python3 -m pip install --help 2>/dev/null | grep -q -- "--break-system-packages"; then
  BSP="--break-system-packages"
fi

echo "[session-start] Installing Python runtime dependencies…"
python3 -m pip install --no-cache-dir $BSP -r requirements.txt

# The base image's system `cryptography` loads its Rust bindings via cffi, but
# `_cffi_backend` isn't present here — without it `import google.genai` (and thus
# the bot / tests) panics. Production installs cryptography from a pip wheel that
# bundles this, so cffi is web-container-only and stays out of requirements.txt.
echo "[session-start] Ensuring cffi backend is available…"
python3 -c "import _cffi_backend" 2>/dev/null || python3 -m pip install --no-cache-dir $BSP cffi

echo "[session-start] Installing dev tooling (ruff, pytest)…"
python3 -m pip install --no-cache-dir $BSP -r requirements-dev.txt

echo "[session-start] Setup complete."
