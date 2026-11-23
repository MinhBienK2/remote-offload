"""Dựng capability profile của 1 host.

Nguyên tắc quan trọng nhất: **MỘT round-trip ssh** cho mọi thông tin.
Mỗi lệnh ssh mất ~3,5 s (đo thật trên fleet Muse), nên script shell được
dựng sẵn, gửi qua stdin (`ssh <alias> sh -s`) và in ra các dòng
``ROFF_<KEY>=<value>`` để parse.

Không bao giờ suy đoán: cgroup không đọc được → ``None``, không có GPU →
``gpu is None``, không có internet → ``False``.

Module này **không in ra stdout** (quy tắc chung của roff).
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field, fields
from typing import Any, Sequence, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - chỉ để type checker
    from roffkit.config import Config

__all__ = [
    "Profile",
    "probe",
    "probe_many",
    "cached_profiles",
    "build_script",
    "physical_host_key",
    "detect_flapping",
    "same_physical_host",
    "DEFAULT_TOOLS",
    "DURABLE_CANDIDATES",
]


try:  # phần A (config.py) có thể chưa sẵn sàng -> fallback tối thiểu
    from roffkit.config import Config  # type: ignore
except ImportError:  # pragma: no cover - chỉ xảy ra khi chạy lẻ module này
    Config = Any  # type: ignore[assignment,misc]


try:  # B34: chính sách nice/ionice — MỘT nguồn sự thật (dùng chung 5 module)
    from roffkit.config import local_nice_prefix  # type: ignore
except ImportError:  # pragma: no cover - chỉ xảy ra khi chạy lẻ module này
    from .config import local_nice_prefix  # type: ignore


try:
    from roffkit import perm  # type: ignore
except ImportError:  # pragma: no cover - chạy module lẻ
    import types

    perm = types.SimpleNamespace(  # type: ignore[assignment]
        secure_dir=lambda path: None,
        secure_file=lambda path: None,
    )


# --------------------------------------------------------------------------
# hằng số
# --------------------------------------------------------------------------

#: Danh sách tool roff quan tâm. Script probe chỉ ghi tool **có thật**.
#:
#: Thứ tự của phần cũ **giữ nguyên** (cache probe cũ vẫn đọc được — chỉ hết
#: hạn bình thường); tool mới (W8a) chèn **cạnh nhóm** để đọc dễ.
#:
#: Nhóm "kiểm tra/lint" (``pytest``, ``mypy``, ``ruff``, ``eslint``, ``tsc``,
#: ``jest``) và nhóm "xử lý ảnh" (``convert``, ``magick``) là loại việc
#: người dùng hay chạy — trước đó chúng **không** được dò ⇒ host nào cũng
#: không khớp ⇒ ưu tiên "host có tool" rơi vào no-op (bug B18).
DEFAULT_TOOLS: tuple[str, ...] = (
    # media / ảnh
    "ffmpeg", "ffprobe", "convert", "magick",
    # JS / TS
    "node", "npm", "eslint", "tsc", "jest",
    # Python
    "python3", "pip3", "pytest", "mypy", "ruff",
    # VCS / mạng / đóng gói
    "git", "rsync", "tar", "curl", "wget",
    # biên dịch / build
    "gcc", "make", "cmake",
    # ngôn ngữ khác
    "go", "cargo", "rustc",
    "java", "mvn", "gradle",
    "docker", "podman",
    "deno", "bun", "pnpm", "yarn",
    "uv", "poetry",
    "dotnet",
    # trình duyệt
    "playwright", "chromium", "google-chrome",
)

#: Ứng viên layout bền vững (mount riêng, không bị xoá khi restart).
#: Trùng với ``layout.DURABLE_CANDIDATES`` trong hợp đồng API.
DURABLE_CANDIDATES: tuple[str, ...] = (
    "/home/hatch", "/mnt/data", "/data", "/workspace", "/scratch",
)

#: URL dùng để đo internet (hợp đồng API §6).
INTERNET_URL = "https://github.com"


def default_state_dir() -> str:
    """Thư mục state mặc định: ``~/.local/state/roff`` (ghi đè bằng $ROFF_STATE_DIR)."""
    env = os.environ.get("ROFF_STATE_DIR")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return os.path.join(os.path.expanduser("~"), ".local", "state", "roff")


# --------------------------------------------------------------------------
# dataclass
# --------------------------------------------------------------------------
@dataclass
class Profile:
    """Năng lực của 1 host, đo bằng 1 round-trip ssh.

    Các trường ``bridge_busy``, ``projects``, ``toolchains``, ``uptime_s``,
    ``restarts_24h`` là **mở rộng ngoài hợp đồng API** (báo cáo sẽ nêu rõ).
    """

    alias: str
    reachable: bool = False
    error: str | None = None
    detail: str | None = None
    hostname: str | None = None
    boot_id: str | None = None
    instance_token: str | None = None
    home: str | None = None
    layout_root: str | None = None
    durable: bool = False
    tools: dict[str, str] = field(default_factory=dict)
    gpu: dict | None = None
    cores: int = 0
    mem_total_mb: int = 0
    mem_available_mb: int = 0
    cgroup_mem_limit_mb: int | None = None
    cgroup_mem_used_mb: int | None = None
    load1: float = 0.0
    disk_free_mb: int = 0
    internet: bool = False
    probed_at: float = 0.0
    # --- mở rộng ngoài hợp đồng ---
    bridge_busy: bool = False
    projects: list[str] = field(default_factory=list)
    toolchains: list[str] = field(default_factory=list)
    uptime_s: float = 0.0
    restarts_24h: int = 0

    def to_dict(self) -> dict:
        """Trả về dict JSON-safe của profile."""
        out: dict[str, Any] = {}
        for f in fields(self):
            out[f.name] = getattr(self, f.name)
        return out

    @staticmethod
    def from_dict(d: dict) -> "Profile":
        """Dựng Profile từ dict (bỏ qua khoá lạ, thiếu khoá thì dùng default)."""
        known = {f.name for f in fields(Profile)}
        kwargs = {k: v for k, v in (d or {}).items() if k in known}
        kwargs.setdefault("alias", "")
        p = Profile(**kwargs)
        p.tools = dict(p.tools or {})
        p.projects = list(p.projects or [])
        p.toolchains = list(p.toolchains or [])
        return p


# --------------------------------------------------------------------------
# script probe (1 round-trip)
# --------------------------------------------------------------------------
_SCRIPT_TEMPLATE = r"""
# Không dùng `set -u`: một biến rỗng bất thường không được làm hỏng cả
# round-trip. Mọi biến đều được khởi tạo sẵn bên dưới.
LC_ALL=C
export LC_ALL

emit() {
  v=$(printf '%s' "${2-}" | tr '\n' ' ' | tr -d '\r')
  printf 'ROFF_%s=%s\n' "$1" "$v"
}

to_mb() {
  v=$(printf '%s' "${1-}" | tr -d '[:space:]')
  case "$v" in
    ''|max) return 0 ;;
    *[!0-9]*) return 0 ;;
  esac
  # giá trị sentinel của cgroup v1 = không giới hạn
  if [ "$v" -ge 1099511627776 ]; then return 0; fi
  printf '%s' "$((v / 1048576))"
}

# --- định danh máy -------------------------------------------------------
emit HOSTNAME "$(hostname 2>/dev/null || true)"
emit BOOT_ID "$(cat /proc/sys/kernel/random/boot_id 2>/dev/null || true)"
HD="${HOME:-}"
emit HOME "$HD"
emit CORES "$(nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null || echo 0)"
emit UPTIME_S "$(awk '{print $1}' /proc/uptime 2>/dev/null || true)"
emit LOAD1 "$(awk '{print $1}' /proc/loadavg 2>/dev/null || true)"

# --- RAM -----------------------------------------------------------------
MT=""
MA=""
if [ -r /proc/meminfo ]; then
  MT=$(awk '/^MemTotal:/ {print $2; exit}' /proc/meminfo)
  MA=$(awk '/^MemAvailable:/ {print $2; exit}' /proc/meminfo)
  if [ -z "$MA" ]; then
    MA=$(awk '/^(MemFree|Cached|Buffers):/ {s+=$2} END {print s+0}' /proc/meminfo)
  fi
fi
emit MEM_TOTAL_KB "$MT"
emit MEM_AVAILABLE_KB "$MA"

# --- GPU (None nếu không có) --------------------------------------------
if command -v nvidia-smi >/dev/null 2>&1; then
  emit GPU_PRESENT 1
  emit GPU_NAME "$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1)"
elif ls /dev/nvidia* >/dev/null 2>&1; then
  emit GPU_PRESENT 1
  emit GPU_DEVICES "$(ls /dev/nvidia* 2>/dev/null | tr '\n' ' ')"
fi

# --- layout bền vững -----------------------------------------------------
ROOT=""
DUR=0
HOME_DEV=""
if [ -n "$HD" ]; then
  HOME_DEV=$(df -P "$HD" 2>/dev/null | tail -n 1 | awk '{print $6}')
fi
for d in __CANDIDATES__; do
  [ -d "$d" ] || continue
  [ -w "$d" ] || continue
  DEV=$(df -P "$d" 2>/dev/null | tail -n 1 | awk '{print $6}')
  if [ -n "$HOME_DEV" ] && [ -n "$DEV" ] && [ "$DEV" = "$HOME_DEV" ]; then
    continue
  fi
  ROOT="$d/.roff"
  DUR=1
  break
done
if [ -z "$ROOT" ]; then
  ROOT="$HD/.roff"
  DUR=0
fi
emit LAYOUT_ROOT "$ROOT"
emit DURABLE "$DUR"

# --- disk trống (MB) trên layout root ------------------------------------
# layout root có thể chưa tồn tại (host mới) -> đo trên thư mục cha gần nhất
PROBE_DIR="$ROOT"
while [ ! -d "$PROBE_DIR" ] && [ "$PROBE_DIR" != "/" ]; do
  PROBE_DIR=$(dirname "$PROBE_DIR")
done
FREE=$(df -Pk "$PROBE_DIR" 2>/dev/null | tail -n 1 | awk '{print $4}')
case "$FREE" in ''|*[!0-9]*) FREE=0 ;; esac
emit DISK_FREE_MB "$((FREE / 1024))"
emit DISK_FREE_KB "$FREE"

# --- instance token (nằm ở $HOME để phát hiện wipe) ----------------------
# Chỉ báo token khi nó **lưu được** trên đĩa: token không ghi được mà vẫn
# báo ra sẽ đổi mỗi lần probe => bị hiểu nhầm thành host bị wipe.
TOK=""
TOKF="$HD/.roff-instance-token"
if [ -n "$HD" ] && [ -r "$TOKF" ]; then
  TOK=$(cat "$TOKF" 2>/dev/null || true)
fi
if [ -z "$TOK" ]; then
  NEW=$(head -c 16 /dev/urandom 2>/dev/null | od -An -tx1 2>/dev/null | tr -d ' \n')
  if [ -n "$NEW" ] && [ -n "$HD" ]; then
    if (umask 077; printf '%s' "$NEW" > "$TOKF.roff.$$" 2>/dev/null \
        && mv -f "$TOKF.roff.$$" "$TOKF" 2>/dev/null); then
      TOK="$NEW"
    else
      rm -f "$TOKF.roff.$$" 2>/dev/null || true
      TOK=""
    fi
  fi
fi
emit INSTANCE_TOKEN "$TOK"

# --- cgroup memory (v2 rồi v1) -------------------------------------------
CGREL=$(awk -F: '$1=="0" {print $3; exit}' /proc/self/cgroup 2>/dev/null || true)
LIM=""
CUR=""
for base in /sys/fs/cgroup "/sys/fs/cgroup${CGREL}"; do
  [ -r "$base/memory.max" ] || continue
  LIM=$(cat "$base/memory.max" 2>/dev/null || true)
  CUR=$(cat "$base/memory.current" 2>/dev/null || true)
  break
done
if [ -z "$LIM" ] && [ -z "$CUR" ] && [ -r /sys/fs/cgroup/memory/memory.limit_in_bytes ]; then
  LIM=$(cat /sys/fs/cgroup/memory/memory.limit_in_bytes 2>/dev/null || true)
  CUR=$(cat /sys/fs/cgroup/memory/memory.usage_in_bytes 2>/dev/null || true)
fi
emit CGROUP_LIMIT_MB "$(to_mb "$LIM")"
emit CGROUP_USED_MB "$(to_mb "$CUR")"

# --- tool trong PATH (chỉ ghi tool có thật) ------------------------------
for t in __TOOLS__; do
  p=$(command -v "$t" 2>/dev/null || true)
  if [ -n "$p" ]; then
    emit TOOL "$t=$p"
  fi
done

# --- project / toolchain đã có trên host (nguồn "ấm") --------------------
if [ -d "$ROOT/projects" ]; then
  for p in "$ROOT/projects"/*; do
    [ -d "$p" ] || continue
    emit PROJECT "$(basename "$p")"
  done
fi
if [ -d "$ROOT/toolchain" ]; then
  for p in "$ROOT/toolchain"/*; do
    [ -d "$p" ] || continue
    emit TOOLCHAIN "$(basename "$p")"
  done
fi

# --- internet ------------------------------------------------------------
# ROFF_PROBE_NO_NET=1 : bỏ qua đo internet (dùng khi test offline)
if [ "${ROFF_PROBE_NO_NET:-}" = "1" ]; then
  emit INTERNET_CODE ""
  emit INTERNET_RC 0
  emit INTERNET_ERR "skipped: ROFF_PROBE_NO_NET=1"
else
  ETT=$(mktemp 2>/dev/null || echo "/tmp/.roff-probe.$$")
  CODE=$(curl -sS -o /dev/null -w '%{http_code}' -m 8 __URL__ 2>"$ETT")
  RC=$?
  CERR=$(cat "$ETT" 2>/dev/null || true)
  rm -f "$ETT" 2>/dev/null || true
  emit INTERNET_CODE "$CODE"
  emit INTERNET_RC "$RC"
  emit INTERNET_ERR "$CERR"
fi

exit 0
"""


def build_script() -> str:
    """Dựng script shell gom MỌI thông tin trong 1 lần chạy trên host.

    Script dùng ``sh`` (không phải bash) để chạy được trên host tối giảu.
    """
    script = _SCRIPT_TEMPLATE
    script = script.replace("__TOOLS__", " ".join(DEFAULT_TOOLS))
    script = script.replace("__CANDIDATES__", " ".join(DURABLE_CANDIDATES))
    script = script.replace("__URL__", INTERNET_URL)
    return script


# --------------------------------------------------------------------------
# chạy ssh
# --------------------------------------------------------------------------
def _prefix_nice() -> list[str]:
    """Tiền tố ``nice``/``ionice`` cho tiến trình con (hợp đồng §1).

    **MỘT nguồn sự thật** (B34): gọi :func:`roffkit.config.local_nice_prefix`
    ⇒ probe cho ra **cùng** tiền tố với sshx/tunnel/job/transfer.
    """
    return local_nice_prefix()


def _config_args() -> list[str]:
    """Tham số ``-F`` cho ssh — **cùng nguồn** với ``fleet``/``sshx`` (B25).

    Probe phải đọc **đúng** file mà ``fleet`` đã fingerprint: nếu không,
    probe hỏi một máy còn job chạy ở máy khác.
    """
    try:
        from roffkit import fleet  # type: ignore
    except ImportError:  # pragma: no cover - chạy module lẻ
        from . import fleet  # type: ignore
    return fleet.ssh_config_args()


def ssh_command(alias: str) -> list[str]:
    """Lệnh ssh dùng để probe (script được gửi qua stdin).

    Có ``-F <path>`` khi có file config — ssh phải đọc **đúng** file mà
    ``fleet`` đã fingerprint (B25), nếu không probe sẽ hỏi **nhầm máy**.
    """
    return [
        "ssh",
        *_config_args(),
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=15",
        alias,
        "sh", "-s",
    ]


def _ssh_runner():
    """Runner ssh dùng chung của roff (``roffkit.sshx.run_shell``) nếu đã có.

    Hợp đồng §15b.1: **mọi** lệnh ssh đều đi qua ``sshx.run_shell`` (ControlMaster
    của roff, nice/ionice, BatchMode). Module này không sở hữu ``sshx.py`` nên
    nếu chưa có thì tự chạy ssh — logic tương đương.
    """
    try:
        from roffkit.sshx import run_shell  # type: ignore
    except ImportError:
        return None
    return run_shell


def _run_ssh(alias: str, script: str, timeout: int) -> tuple[int, str, str]:
    """Chạy 1 lệnh ssh, trả về ``(returncode, stdout, stderr)``.

    Đây là **seam duy nhất** để test: test monkeypatch hàm này, không cần mạng.
    Nếu ``roffkit.sshx`` đã có thì dùng runner dùng chung (ControlMaster của
    roff) — hợp đồng §15b.1.
    """
    runner = _ssh_runner()
    if runner is not None:
        result = runner(alias, script, timeout)
        # runner dùng chung trả RunResult (dataclass) — chuẩn hoá về tuple
        if isinstance(result, tuple):
            rc, out, err = result
        else:
            rc = getattr(result, "rc", 255)
            out = getattr(result, "stdout", "") or ""
            err = getattr(result, "stderr", "") or ""
        return int(rc), str(out), str(err)

    cmd = _prefix_nice() + ssh_command(alias)
    proc = subprocess.run(
        cmd,
        input=script.encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    return (
        proc.returncode,
        proc.stdout.decode("utf-8", "replace"),
        proc.stderr.decode("utf-8", "replace"),
    )


# --------------------------------------------------------------------------
# parse
# --------------------------------------------------------------------------
def _parse_output(text: str) -> dict[str, list[str]]:
    """Parse output của script thành ``{KEY: [value, ...]}``.

    Bỏ qua mọi dòng không bắt đầu bằng ``ROFF_`` (banner của bridge...).
    """
    values: dict[str, list[str]] = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line.startswith("ROFF_"):
            continue
        body = line[len("ROFF_"):]
        key, sep, value = body.partition("=")
        if not sep:
            continue
        key = key.strip()
        if not key:
            continue
        values.setdefault(key, []).append(value.strip())
    return values


def _one(values: dict[str, list[str]], key: str, default: str = "") -> str:
    got = values.get(key)
    if not got:
        return default
    return got[0]


def _as_int(raw: str, default: int = 0) -> int:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _as_float(raw: str, default: float = 0.0) -> float:
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _as_bool(raw: str, default: bool = False) -> bool:
    text = str(raw).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off", ""):
        return False
    return default


def _classify_ssh_error(detail: str) -> str:
    """Chuyển text lỗi ssh thành mã lỗi ngắn gọn."""
    low = (detail or "").lower()
    if "permission denied" in low or "publickey" in low:
        return "permission_denied"
    if "could not resolve" in low or "name or service not known" in low:
        return "dns_error"
    if "connection refused" in low:
        return "connection_refused"
    if "no route to host" in low:
        return "no_route_to_host"
    if "timed out" in low or "timeout" in low:
        return "timeout"
    if "connection closed" in low or "connection reset" in low:
        return "connection_lost"
    if "host key verification failed" in low:
        return "host_key_verification_failed"
    return "ssh_failed"


def _profile_from_values(
    alias: str,
    values: dict[str, list[str]],
) -> Profile:
    """Dựng Profile từ output đã parse."""
    p = Profile(alias=alias, reachable=True, probed_at=time.time())

    p.hostname = _one(values, "HOSTNAME") or None
    p.boot_id = _one(values, "BOOT_ID") or None
    p.home = _one(values, "HOME") or None
    p.instance_token = _one(values, "INSTANCE_TOKEN") or None
    p.layout_root = _one(values, "LAYOUT_ROOT") or None
    p.durable = _as_bool(_one(values, "DURABLE"), False)

    p.cores = _as_int(_one(values, "CORES"), 0)
    p.mem_total_mb = _as_int(_one(values, "MEM_TOTAL_KB"), 0) // 1024
    p.mem_available_mb = _as_int(_one(values, "MEM_AVAILABLE_KB"), 0) // 1024
    p.load1 = _as_float(_one(values, "LOAD1"), 0.0)
    p.disk_free_mb = _as_int(_one(values, "DISK_FREE_MB"), 0)

    limit_raw = _one(values, "CGROUP_LIMIT_MB")
    used_raw = _one(values, "CGROUP_USED_MB")
    p.cgroup_mem_limit_mb = _as_int(limit_raw, -1) if limit_raw else None
    p.cgroup_mem_used_mb = _as_int(used_raw, -1) if used_raw else None
    if p.cgroup_mem_limit_mb is not None and p.cgroup_mem_limit_mb < 0:
        p.cgroup_mem_limit_mb = None
    if p.cgroup_mem_used_mb is not None and p.cgroup_mem_used_mb < 0:
        p.cgroup_mem_used_mb = None

    for entry in values.get("TOOL", []):
        name, sep, path = entry.partition("=")
        if sep and name.strip():
            p.tools[name.strip()] = path.strip()
    p.projects = [x for x in values.get("PROJECT", []) if x]
    p.toolchains = [x for x in values.get("TOOLCHAIN", []) if x]

    if _as_bool(_one(values, "GPU_PRESENT"), False):
        gpu: dict[str, Any] = {}
        name = _one(values, "GPU_NAME")
        if name:
            gpu["name"] = name
        devices = _one(values, "GPU_DEVICES")
        if devices:
            gpu["devices"] = devices.split()
        p.gpu = gpu or {"present": True}

    p.uptime_s = _as_float(_one(values, "UPTIME_S"), 0.0)
    # restart trong 24h: chỉ có thể suy ra cận dưới từ uptime (máy vừa bật
    # lên ⇒ ít nhất 1 lần restart trong 24h). Không đo chính xác hơn được.
    p.restarts_24h = 1 if 0 < p.uptime_s < 86400 else 0

    code = _one(values, "INTERNET_CODE")
    err = _one(values, "INTERNET_ERR")
    p.internet = code == "200"
    hay = f"{code} {err}".lower()
    if not p.internet and ("429" in hay or "already active" in hay):
        # Bridge MỘT PHIÊN bị chặn -> KHÔNG phải mất internet.
        p.bridge_busy = True
        p.error = "bridge_busy: phiên SSH một số phiên đang bận (429 / already active)"
        p.detail = err or code
    return p


# --------------------------------------------------------------------------
# probe
# --------------------------------------------------------------------------
def probe(alias: str, timeout: int = 30) -> Profile:
    """Probe 1 host trong **một** round-trip ssh.

    Trả về Profile. Khi ssh lỗi → ``reachable=False``, ``error`` là mã lỗi
    ngắn và ``detail`` là **nguyên văn** text lỗi ssh (không cắt bớt).
    """
    script = build_script()
    try:
        rc, out, err = _run_ssh(alias, script, timeout)
    except subprocess.TimeoutExpired:
        return Profile(
            alias=alias,
            reachable=False,
            error="timeout",
            detail=f"ssh timeout sau {timeout}s",
            probed_at=time.time(),
        )
    except FileNotFoundError as exc:
        return Profile(
            alias=alias,
            reachable=False,
            error="ssh_not_found",
            detail=str(exc),
            probed_at=time.time(),
        )
    except OSError as exc:  # pragma: no cover - lỗi hệ thống hiếm
        return Profile(
            alias=alias,
            reachable=False,
            error="ssh_failed",
            detail=str(exc),
            probed_at=time.time(),
        )
    except Exception as exc:  # runner dùng chung có thể ném lỗi riêng
        detail = getattr(exc, "detail", None) or str(exc)
        return Profile(
            alias=alias,
            reachable=False,
            error=_classify_ssh_error(str(detail)),
            detail=str(detail),
            probed_at=time.time(),
        )

    if rc != 0:
        detail = err or out or ""
        return Profile(
            alias=alias,
            reachable=False,
            error=_classify_ssh_error(detail),
            detail=detail,
            probed_at=time.time(),
        )

    values = _parse_output(out)
    if not values:
        return Profile(
            alias=alias,
            reachable=False,
            error="no_output",
            detail=(out or err or "").strip(),
            probed_at=time.time(),
        )
    return _profile_from_values(alias, values)


def probe_many(
    aliases: list[str],
    timeout: int = 30,
    parallel: bool = False,
) -> list[Profile]:
    """Probe nhiều host.

    Mặc định **SERIAL**: bridge một phiên không chịu được đồng thời (bug R2).
    ``parallel=True`` chỉ dùng khi caller biết chắc.
    """
    if not aliases:
        return []
    if parallel:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=min(len(aliases), 4)) as pool:
            return list(pool.map(lambda a: probe(a, timeout), aliases))
    return [probe(alias, timeout) for alias in aliases]


# --------------------------------------------------------------------------
# nhận diện máy vật lý / phát hiện flap (API-CONTRACT §15b.5)
# --------------------------------------------------------------------------
def physical_host_key(profile: Profile) -> tuple:
    """Khoá nhận diện máy VẬT LÝ: ``(hostname, boot_id, instance_token)``.

    Hai alias trỏ cùng máy vật lý khi 3 giá trị này giống nhau. Rỗng (host
    không với được) thì khoá là ``(None, None, None)`` — không đủ để kết luận
    là cùng máy.
    """
    return (
        getattr(profile, "hostname", None),
        getattr(profile, "boot_id", None),
        getattr(profile, "instance_token", None),
    )


def same_physical_host(a: Profile, b: Profile) -> bool:
    """Cùng ``boot_id`` + cùng ``instance_token`` + cùng ``hostname``.

    Profile không đọc được ``boot_id``/``instance_token`` (host không với
    được) thì **không** kết luận là cùng máy — thiếu dữ liệu không phải là
    bằng chứng.
    """
    boot = getattr(a, "boot_id", None)
    token = getattr(a, "instance_token", None)
    if not boot or not token:
        return False
    return (
        boot == getattr(b, "boot_id", None)
        and token == getattr(b, "instance_token", None)
        and getattr(a, "hostname", None) == getattr(b, "hostname", None)
    )


def detect_flapping(profiles: list[Profile]) -> list[dict]:
    """Phát hiện alias đang flap.

    Nhóm profile theo ``(hostname, boot_id)``; nếu trong một nhóm có nhiều
    ``instance_token`` khác nhau thì các alias đó trỏ cùng một VM/boot nhưng
    khác container ⇒ alias **flap** (đổi instance giữa chừng).

    Trả về list dict::

        {"boot_id": ..., "hostname": ..., "aliases": [...], "tokens": {alias: token}}
    """
    groups: dict[tuple, dict[str, str]] = {}
    for profile in profiles or []:
        hostname = getattr(profile, "hostname", None)
        boot_id = getattr(profile, "boot_id", None)
        token = getattr(profile, "instance_token", None)
        if not boot_id or not token:
            # host không với được / chưa có token -> không đủ dữ liệu
            continue
        key = (hostname, boot_id)
        groups.setdefault(key, {})[getattr(profile, "alias", "?")] = token

    out: list[dict] = []
    for (hostname, boot_id), tokens in sorted(groups.items(), key=lambda kv: str(kv[0])):
        if len(set(tokens.values())) < 2:
            continue
        out.append(
            {
                "boot_id": boot_id,
                "hostname": hostname,
                "aliases": sorted(tokens),
                "tokens": dict(tokens),
            }
        )
    return out


# --------------------------------------------------------------------------
# cache
# --------------------------------------------------------------------------
def _cache_path(state_dir: str | None = None) -> str:
    base = os.path.abspath(os.path.expanduser(state_dir or default_state_dir()))
    return os.path.join(base, "probes.json")


def _read_cache(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_cache(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    perm.secure_dir(os.path.dirname(path))
    tmp = f"{path}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        perm.secure_file(tmp)
        os.replace(tmp, path)
        perm.secure_file(path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _fingerprint(alias: str) -> str:
    """Fingerprint tham số ssh của alias (xem :func:`fleet.ssh_fingerprint`).

    Dùng để phát hiện "``~/.ssh/config`` đã đổi" ⇒ cache probe cũ **hết
    hạn** (bug B14: profile của máy cũ vẫn được dùng sau khi config đổi).
    """
    try:
        try:
            from roffkit import fleet  # type: ignore
        except ImportError:  # pragma: no cover - chạy module lẻ
            from . import fleet  # type: ignore
        return fleet.ssh_fingerprint(alias)
    except Exception:  # pragma: no cover - config hỏng thì coi như không có
        return ""


def cache_entries(state_dir: str | None = None) -> dict:
    """Đọc **nguyên văn** cache probe (``probes.json``) — không probe mạng.

    Dùng cho ``roff doctor`` (mặc định KHÔNG probe) và cho việc kiểm tra
    fingerprint mà không đụng tới host.
    """
    return _read_cache(_cache_path(state_dir))


def _profile_from_cache(entry: dict, alias: str) -> Profile | None:
    """Profile từ 1 entry cache; ``None`` nếu entry **hỏng**.

    Entry hỏng = không phải dict, hoặc ``profile.alias`` **không** bằng
    ``alias`` (kể cả khi rỗng). Trường hợp này phải probe lại: nếu không,
    ``Profile.from_dict`` sẽ sinh ra một "host ma" không tên (``alias=""``),
    host thật biến mất khỏi danh sách và ``--host`` trả ``unknown_host``
    cho máy đang sống.
    """
    raw = entry.get("profile")
    if not isinstance(raw, dict):
        return None
    try:
        profile = Profile.from_dict(raw)
    except Exception:  # pragma: no cover - dict lạ ⇒ coi như hỏng
        return None
    if profile.alias != alias:
        return None
    return profile


def cached_profiles(
    aliases: list[str],
    cfg: "Config",
    state_dir: str | None = None,
    force: Sequence[str] | None = None,
) -> list[Profile]:
    """Trả về profile của các alias, dùng cache ``~/.local/state/roff/probes.json``.

    TTL = ``cfg.probe_cache_s`` giây, dựa trên ``time.time()``.
    Host không có cache (hoặc cache hết hạn) sẽ được probe lại (serial).

    Args:
        aliases: danh sách alias cần profile.
        cfg: config (dùng ``probe_cache_s``).
        state_dir: thư mục state (mặc định ``~/.local/state/roff``).
        force: các alias **luôn** được probe lại, bỏ qua cache (kể cả cache
            còn hạn) — dùng cho ``--host``: host được ghim phải luôn được đo
            thật, cache cũ không được che nó.
    """
    ttl = int(getattr(cfg, "probe_cache_s", 60) or 0)
    path = _cache_path(state_dir)
    cache = _read_cache(path)
    now = time.time()
    forced = {str(alias) for alias in (force or [])}

    out: list[Profile] = []
    dirty = False
    for alias in aliases:
        entry = cache.get(alias)
        fresh = False
        if alias not in forced and isinstance(entry, dict):
            ts = entry.get("ts")
            # B14: config đổi ⇒ fingerprint đổi ⇒ cache cũ **hết hạn**,
            # phải probe lại (entry không có fingerprint ⇒ cache kiểu cũ ⇒
            # cũng coi như hết hạn).
            if isinstance(ts, (int, float)) and (now - ts) < ttl:
                if entry.get("fingerprint") == _fingerprint(alias):
                    fresh = True
        if fresh:
            profile = _profile_from_cache(entry, alias)
            if profile is not None:
                out.append(profile)
                continue
            # entry hỏng (profile rỗng / alias lệch) ⇒ coi như cache miss,
            # probe lại. Tuyệt đối không trả profile có alias sai.
        profile = probe(alias)
        out.append(profile)
        # Khoá cache = alias **thực tế** của profile (giữ đúng tên alias đã
        # biết) ⇒ entry luôn tự nhất quán với profile bên trong nó.
        cache_key = profile.alias or alias
        cache[cache_key] = {
            "ts": time.time(),
            "fingerprint": _fingerprint(alias),
            "profile": profile.to_dict(),
        }
        dirty = True

    if dirty:
        _write_cache(path, cache)
    return out
