#!/usr/bin/env bash
# Cài npm (và node nếu thiếu) vào toolchain của roff — idempotent.
#
# Dùng khi host có node nhưng **không** có npm (đúng case của fleet Muse:
# node v24 có sẵn, npm thiếu). Cài vào $ROFF_ROOT/toolchain/bin, không đụng
# hệ thống, không apt-get gì cả.
#
# Env:
#   ROFF_ROOT      (bắt buộc) layout root bền vững trên host
#   ROFF_TOOLCHAIN (tuỳ chọn) mặc định $ROFF_ROOT/toolchain
#   ROFF_HOST      (tuỳ chọn) alias host, chỉ để log
#   NODE_VERSION   (tuỳ chọn) ép version node khi cần cài node mới
#   ROFF_SETUP_FORCE (tuỳ chọn) "1" → cài lại dù đã có (stamp của host đã mất)
#
# Kết quả: in **một dòng JSON** ra stdout ở cuối:
#   {"name":"npm","ensured":true,"skipped":false,"detail":"..."}
# Mọi thông tin khác đi ra stderr để stdout chỉ còn JSON.
set -euo pipefail

NAME="npm"
ROFF_HOST="${ROFF_HOST:-unknown}"

log() { printf '%s\n' "$*" >&2; }

json_escape() {
  printf '%s' "${1-}" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e ':a;N;$!ba;s/\n/ /g'
}

emit() { # emit <ensured> <skipped> <detail>
  printf '{"name":"%s","ensured":%s,"skipped":%s,"detail":"%s","host":"%s"}\n' \
    "$NAME" "$1" "$2" "$(json_escape "${3-}")" "$(json_escape "$ROFF_HOST")"
}

fail() { # fail <detail>
  log "npm.sh: ${1-}"
  printf '{"name":"%s","ensured":false,"skipped":false,"error":"%s","detail":"%s","host":"%s"}\n' \
    "$NAME" "$(json_escape "${1-}")" "$(json_escape "${1-}")" "$(json_escape "$ROFF_HOST")"
  exit 1
}

if [ -z "${ROFF_ROOT:-}" ]; then
  fail "ROFF_ROOT chưa được đặt — không biết cài toolchain ở đâu"
fi

ROFF_TOOLCHAIN="${ROFF_TOOLCHAIN:-$ROFF_ROOT/toolchain}"
BIN_DIR="$ROFF_TOOLCHAIN/bin"
WORK_DIR="$ROFF_TOOLCHAIN/.npm-install"
CACHE_DIR="$ROFF_ROOT/cache/npm"
mkdir -p "$BIN_DIR" "$WORK_DIR" "$CACHE_DIR"

export PATH="$BIN_DIR:$PATH"

# --------------------------------------------------------------------------------------
# 1. Đã có npm chưa?
# --------------------------------------------------------------------------------------
npm_works() {
  local candidate="${1:-}"
  [ -n "$candidate" ] && [ -x "$candidate" ] || return 1
  "$candidate" --version >/dev/null 2>&1
}

EXISTING_NPM="$(command -v npm 2>/dev/null || true)"
# ROFF_SETUP_FORCE=1 → cài lại dù đã có (dùng khi stamp của host đã mất).
if [ "${ROFF_SETUP_FORCE:-0}" != "1" ] && npm_works "$EXISTING_NPM"; then
  emit false true "npm đã có sẵn: $("$EXISTING_NPM" --version 2>/dev/null || echo '?') ($EXISTING_NPM)"
  exit 0
fi

# --------------------------------------------------------------------------------------
# 2. Node ở đâu? Phiên bản nào?
# --------------------------------------------------------------------------------------
NODE_BIN="$(command -v node 2>/dev/null || true)"
DETECTED_VERSION=""
if [ -n "$NODE_BIN" ]; then
  DETECTED_VERSION="$("$NODE_BIN" --version 2>/dev/null || true)"
fi
# Env NODE_VERSION thắng; nếu không có gì thì lấy version đang chạy; cuối cùng mặc định.
NODE_VERSION="${NODE_VERSION:-${DETECTED_VERSION:-v22.14.0}}"
if [ -z "$NODE_BIN" ]; then
  log "không có node sẵn — sẽ cài node $NODE_VERSION"
fi

case "$(uname -m)" in
  x86_64|amd64) ARCH="x64" ;;
  aarch64|arm64) ARCH="arm64" ;;
  armv7l) ARCH="armv7l" ;;
  *) fail "kiến trúc không hỗ trợ: $(uname -m)" ;;
esac

PKG_DIR="node-${NODE_VERSION}-linux-${ARCH}"
TARBALL_URL="https://nodejs.org/dist/${NODE_VERSION}/${PKG_DIR}.tar.xz"
TARBALL="$WORK_DIR/${PKG_DIR}.tar.xz"

# --------------------------------------------------------------------------------------
# 3. Tải tarball — timeout + kiểm tra kích thước (CDN hay treo ở 0 byte)
# --------------------------------------------------------------------------------------
if [ ! -s "$TARBALL" ]; then
  log "tải $TARBALL_URL"
  CURL_ARGS=(-fL --connect-timeout 15 --max-time 900 --retry 2 --retry-delay 5 -o "$TARBALL" "$TARBALL_URL")
  if command -v timeout >/dev/null 2>&1; then
    timeout 900 curl "${CURL_ARGS[@]}" || true
  else
    curl "${CURL_ARGS[@]}" || true
  fi
fi

TARBALL_SIZE="$(wc -c < "$TARBALL" 2>/dev/null || echo 0)"
if [ "$TARBALL_SIZE" -lt 1000000 ]; then
  rm -f "$TARBALL"
  fail "tải nodejs.org thất bại hoặc rỗng ($TARBALL_SIZE byte) — host cần internet, thử host khác"
fi
if ! tar -tJf "$TARBALL" >/dev/null 2>&1; then
  rm -f "$TARBALL"
  fail "tarball hỏng (không giải nén được)"
fi

# --------------------------------------------------------------------------------------
# 4. Chỉ lấy phần npm — không cần copy cả node
# --------------------------------------------------------------------------------------
NPM_ROOT="$WORK_DIR/npm-root"
rm -rf "$NPM_ROOT"
mkdir -p "$NPM_ROOT"

if ! tar -xJf "$TARBALL" -C "$NPM_ROOT" --strip-components=2 \
      "${PKG_DIR}/lib/node_modules/npm" 2>/dev/null; then
  log "giải nén có chọn lọc thất bại — giải nén toàn bộ"
  FULL="$WORK_DIR/full"
  rm -rf "$FULL"
  mkdir -p "$FULL"
  tar -xJf "$TARBALL" -C "$FULL"
fi

SEARCH_DIRS="$NPM_ROOT"
[ -d "$WORK_DIR/full" ] && SEARCH_DIRS="$SEARCH_DIRS $WORK_DIR/full"
NPM_CLI="$(find $SEARCH_DIRS -type f -name npm-cli.js 2>/dev/null | head -1 || true)"
if [ -z "$NPM_CLI" ]; then
  fail "không tìm thấy npm-cli.js trong tarball node $NODE_VERSION"
fi

# Chuyển npm sang chỗ **cố định** — wrapper phải sống lâu hơn thư mục tạm.
NPM_PKG_DIR="$(dirname "$(dirname "$NPM_CLI")")"
NPM_DEST="$ROFF_TOOLCHAIN/npm"
rm -rf "$NPM_DEST"
mkdir -p "$NPM_DEST"
cp -a "$NPM_PKG_DIR"/. "$NPM_DEST"/
NPM_CLI="$NPM_DEST/bin/npm-cli.js"
NPX_CLI="$NPM_DEST/bin/npx-cli.js"
if [ ! -f "$NPX_CLI" ]; then
  NPX_CLI=""
fi

# --------------------------------------------------------------------------------------
# 5. Node dùng để chạy npm
# --------------------------------------------------------------------------------------
if [ -z "$NODE_BIN" ]; then
  FULL="$WORK_DIR/full"
  NODE_BIN="$FULL/${PKG_DIR}/bin/node"
  if [ ! -x "$NODE_BIN" ]; then
    fail "không có node để chạy npm"
  fi
  # Copy (không symlink) — thư mục tạm sẽ bị xoá ở cuối script.
  cp -f "$NODE_BIN" "$BIN_DIR/node"
  NODE_BIN="$BIN_DIR/node"
fi

# --------------------------------------------------------------------------------------
# 6. Wrapper trong $ROFF_TOOLCHAIN/bin
# --------------------------------------------------------------------------------------
cat > "$BIN_DIR/npm" <<EOF
#!/usr/bin/env bash
# npm do roff tạo — chạy npm-cli.js bằng node đã có; cache nằm trên mount bền vững.
export npm_config_cache="\${npm_config_cache:-$CACHE_DIR}"
exec "$NODE_BIN" "$NPM_CLI" "\$@"
EOF
chmod +x "$BIN_DIR/npm"

if [ -n "$NPX_CLI" ]; then
  cat > "$BIN_DIR/npx" <<EOF
#!/usr/bin/env bash
export npm_config_cache="\${npm_config_cache:-$CACHE_DIR}"
exec "$NODE_BIN" "$NPX_CLI" "\$@"
EOF
  chmod +x "$BIN_DIR/npx"
fi

# --------------------------------------------------------------------------------------
# 7. Kiểm tra
# --------------------------------------------------------------------------------------
export PATH="$BIN_DIR:$PATH"
NPM_VERSION="$(npm --version 2>/dev/null || true)"
if [ -z "$NPM_VERSION" ]; then
  fail "cài xong nhưng npm --version không chạy — xem lỗi ở stderr"
fi

rm -rf "$WORK_DIR"

emit true false "npm $NPM_VERSION cài tại $BIN_DIR/npm (node $NODE_BIN)"
