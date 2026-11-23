#!/usr/bin/env bash
# Cài Chromium cho Playwright vào toolchain bền vững của roff — idempotent.
#
# Browser nằm ở $ROFF_ROOT/ms-playwright nên **sống sót restart** và dùng lại
# được cho mọi job sau (tải browser là bước chậm nhất trên máy yếu).
#
# Env:
#   ROFF_ROOT            (bắt buộc) layout root bền vững trên host
#   ROFF_TOOLCHAIN       (tuỳ chọn) mặc định $ROFF_ROOT/toolchain
#   ROFF_HOST            (tuỳ chọn) alias host, chỉ để log
#   PLAYWRIGHT_VERSION   (tuỳ chọn) mặc định "latest"
#   ROFF_PLAYWRIGHT_DEPS (tuỳ chọn) "1" → thêm --with-deps (cần apt-get, chậm)
#   ROFF_SETUP_FORCE     (tuỳ chọn) "1" → tải lại browser dù đã có (stamp đã mất)
#
# Lưu ý: browser build và Playwright package phải cùng version — nếu project
# ghim playwright@1.44 thì đặt PLAYWRIGHT_VERSION=1.44 khi gọi script.
#
# Kết quả: in **một dòng JSON** ra stdout ở cuối:
#   {"name":"playwright-chromium","ensured":true,"skipped":false,"detail":"..."}
set -euo pipefail

NAME="playwright-chromium"
ROFF_HOST="${ROFF_HOST:-unknown}"
PLAYWRIGHT_VERSION="${PLAYWRIGHT_VERSION:-latest}"
INSTALL_TIMEOUT="${ROFF_PLAYWRIGHT_TIMEOUT:-900}"

log() { printf '%s\n' "$*" >&2; }

json_escape() {
  printf '%s' "${1-}" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e ':a;N;$!ba;s/\n/ /g'
}

emit() { # emit <ensured> <skipped> <detail>
  printf '{"name":"%s","ensured":%s,"skipped":%s,"detail":"%s","host":"%s"}\n' \
    "$NAME" "$1" "$2" "$(json_escape "${3-}")" "$(json_escape "$ROFF_HOST")"
}

fail() { # fail <detail>
  log "$NAME.sh: ${1-}"
  printf '{"name":"%s","ensured":false,"skipped":false,"error":"%s","detail":"%s","host":"%s"}\n' \
    "$NAME" "$(json_escape "${1-}")" "$(json_escape "${1-}")" "$(json_escape "$ROFF_HOST")"
  exit 1
}

if [ -z "${ROFF_ROOT:-}" ]; then
  fail "ROFF_ROOT chưa được đặt — không biết cài browser ở đâu"
fi

ROFF_TOOLCHAIN="${ROFF_TOOLCHAIN:-$ROFF_ROOT/toolchain}"
export PATH="$ROFF_TOOLCHAIN/bin:$PATH"
export PLAYWRIGHT_BROWSERS_PATH="$ROFF_ROOT/ms-playwright"
export PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT="${PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT:-30000}"
mkdir -p "$PLAYWRIGHT_BROWSERS_PATH"

# --------------------------------------------------------------------------------------
# 1. Node/npx có sẵn không?
# --------------------------------------------------------------------------------------
NPX_BIN="$(command -v npx 2>/dev/null || true)"
if [ -z "$NPX_BIN" ]; then
  fail "không có npx — chạy 'roff ensure --setup setups/npm.sh' trước"
fi

# --------------------------------------------------------------------------------------
# 2. Chromium đã có chưa? (tìm bản build đã tải, kể cả revision cũ)
# --------------------------------------------------------------------------------------
existing_dir=""
for dir in "$PLAYWRIGHT_BROWSERS_PATH"/chromium-*; do
  [ -d "$dir" ] || continue
  if [ -n "$(find "$dir" -type f -size +0 2>/dev/null | head -1)" ]; then
    existing_dir="$dir"
    break
  fi
done

if [ -n "$existing_dir" ] && [ "${ROFF_SETUP_FORCE:-0}" != "1" ]; then
  emit false true "chromium đã có sẵn: $existing_dir"
  exit 0
fi

# --------------------------------------------------------------------------------------
# 3. Tải browser — có timeout vì CDN hay treo ở 0 byte
# --------------------------------------------------------------------------------------
INSTALL_ARGS=(-y "playwright@${PLAYWRIGHT_VERSION}" install chromium)
if [ "${ROFF_PLAYWRIGHT_DEPS:-0}" = "1" ]; then
  log "cài kèm system deps (--with-deps) — chậm, cần apt-get"
  INSTALL_ARGS+=(--with-deps)
fi

log "cài chromium: npx ${INSTALL_ARGS[*]}"
if command -v timeout >/dev/null 2>&1; then
  timeout "$INSTALL_TIMEOUT" "$NPX_BIN" "${INSTALL_ARGS[@]}" || true
else
  "$NPX_BIN" "${INSTALL_ARGS[@]}" || true
fi

# --------------------------------------------------------------------------------------
# 4. Kiểm tra kết quả — file 0 byte là thất bại, không phải thành công
# --------------------------------------------------------------------------------------
installed_dir=""
for dir in "$PLAYWRIGHT_BROWSERS_PATH"/chromium-*; do
  [ -d "$dir" ] || continue
  if [ -n "$(find "$dir" -type f -size +0 2>/dev/null | head -1)" ]; then
    installed_dir="$dir"
    break
  fi
done

if [ -z "$installed_dir" ]; then
  fail "tải chromium thất bại hoặc file rỗng (timeout ${INSTALL_TIMEOUT}s) — máy yếu / CDN bị chặn; thử host khác hoặc dùng executablePath"
fi

SIZE_KB="$(du -sk "$installed_dir" 2>/dev/null | cut -f1)"
if [ "${SIZE_KB:-0}" -lt 10000 ]; then
  fail "chromium tải về quá nhỏ (${SIZE_KB:-0} KB) — tải dở dang, thử host khác"
fi

emit true false "chromium $PLAYWRIGHT_VERSION cài tại $installed_dir (${SIZE_KB:-?} KB)"
