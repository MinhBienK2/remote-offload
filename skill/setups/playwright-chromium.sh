#!/usr/bin/env bash
# Idempotent chromium bootstrap for remote headless e2e.
# Installs the browser into $ROFF_ROOT/ms-playwright and is safe to re-run.
# If the project pins its own playwright version, change the version below to
# match it — the browser build and the playwright package must agree.
set -euo pipefail
export ROFF_ROOT="${ROFF_ROOT:-$HOME/.roff}"
export PATH="$ROFF_ROOT/toolchain/bin:$HOME/.local/bin:$PATH"
export PLAYWRIGHT_BROWSERS_PATH="$ROFF_ROOT/ms-playwright"
command -v npx >/dev/null || { echo "ensure node first" >&2; exit 1; }
if [ ! -d "$PLAYWRIGHT_BROWSERS_PATH" ] || ! ls "$PLAYWRIGHT_BROWSERS_PATH"/chromium-* >/dev/null 2>&1; then
  timeout 900 npx -y playwright@latest install --with-deps chromium \
    || { echo "chromium download stalled/failed (weak host or CDN); pick another host or use executablePath" >&2; exit 1; }
fi
echo "chromium ready: $(ls -d "$PLAYWRIGHT_BROWSERS_PATH"/chromium-* 2>/dev/null | head -1)"
