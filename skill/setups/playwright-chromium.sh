#!/usr/bin/env bash
# Idempotent chromium bootstrap for remote headless e2e.
# Installs the browser into ~/.cache/ms-playwright and is safe to re-run.
# If the project pins its own playwright version, change the version below to
# match it — the browser build and the playwright package must agree.
set -euo pipefail
export PATH="$HOME/.roff/toolchain/bin:$HOME/.local/bin:$PATH"
command -v npx >/dev/null || { echo "ensure node first" >&2; exit 1; }
if [ ! -d "$HOME/.cache/ms-playwright" ] || ! ls "$HOME/.cache/ms-playwright"/chromium-* >/dev/null 2>&1; then
  timeout 900 npx -y playwright@latest install --with-deps chromium \
    || { echo "chromium download stalled/failed (weak host or CDN); pick another host or use executablePath" >&2; exit 1; }
fi
echo "chromium ready: $(ls -d "$HOME/.cache/ms-playwright"/chromium-* 2>/dev/null | head -1)"
