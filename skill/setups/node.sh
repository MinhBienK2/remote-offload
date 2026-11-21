#!/usr/bin/env bash
# Idempotent node bootstrap for ephemeral hosts (Colab/muse cells).
# Installs node+npm into $ROFF_ROOT/toolchain ONLY if missing; safe to re-run.
# After a host wipe, roff ensure re-runs this automatically (stamp is gone).
# ROFF_ROOT is the layout root (durable mount when the host has one), so the
# toolchain survives a refresh of a host that wipes $HOME.
set -euo pipefail
TC="${ROFF_ROOT:-$HOME/.roff}/toolchain"
mkdir -p "$TC/bin"
if command -v npm >/dev/null 2>&1 && command -v node >/dev/null 2>&1; then
  echo "node already present: $(node --version), npm $(npm --version)"
  exit 0
fi
NODE_VERSION="${NODE_VERSION:-v22.14.0}"
ARCH="$(uname -m)"
case "$ARCH" in
  x86_64) NARCH=x64 ;;
  aarch64) NARCH=arm64 ;;
  *) echo "unsupported arch $ARCH" >&2; exit 1 ;;
esac
TARBALL="node-${NODE_VERSION}-linux-${NARCH}.tar.xz"
cd "$TC"
if [ ! -x "$TC/node/bin/node" ]; then
  curl -fsSL "https://nodejs.org/dist/${NODE_VERSION}/${TARBALL}" -o "$TARBALL"
  # root tar restores archive ownership by default; durable mounts (Muse
  # /home/hatch) refuse chown — extract as the running user instead.
  tar --no-same-owner --no-same-permissions -xf "$TARBALL"
  mv "node-${NODE_VERSION}-linux-${NARCH}" node
  rm -f "$TARBALL"
fi
ln -sf "$TC/node/bin/node" "$TC/bin/node"
ln -sf "$TC/node/bin/npm" "$TC/bin/npm"
ln -sf "$TC/node/bin/npx" "$TC/bin/npx"
export PATH="$TC/bin:$PATH"
echo "installed node $(node --version), npm $(npm --version)"