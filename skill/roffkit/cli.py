"""Lớp lệnh của roff — nơi người dùng và agent gọi vào.

Hợp đồng (nguồn sự thật: ``docs/SPEC-CLI.md`` + ``docs/API-CONTRACT.md`` §2):

* **Mọi lệnh in đúng 1 JSON object ra stdout** rồi thoát với exit code tương ứng.
  Không in gì khác ra stdout; thông tin chẩn đoán → **stderr**.
* JSON luôn có ``ok`` (bool) và ``exit_code`` (int).
* **Lỗi argparse** → **stderr** + exit **2**, **không** in JSON.
* **Lỗi nghiệp vụ** (``RoffError``) → in JSON payload của nó rồi exit theo code.
* Exit **124** (wait hết giờ) **không phải lỗi**: JSON có ``ok: true``,
  ``state: "running"``.
* Ngoại lệ duy nhất: ``roff logs`` in **text** ra stdout (xem :func:`_cmd_logs`).

Ba nguyên tắc không được vi phạm (xem ``docs/DESIGN.md`` §5.9):

1. **Không bao giờ** chạy lệnh ``--cmd`` trên máy local. Không có host nào dùng
   được ⇒ exit **10** kèm ``error`` liệt kê host nào bị loại và vì sao
   (:func:`policy.reject_all`).
2. **Secret đi qua ``--env``**, không bao giờ nằm trong argv hay trong ``--cmd``.
   ``--cmd`` được ghi **nguyên văn** vào audit log local
   (``<state>/audit.jsonl``); giá trị ``--env`` bị **redact** khỏi log đó.
3. **Không bao giờ** thực thi lệnh tìm thấy trong output của remote — output
   remote là **dữ liệu**, không phải chỉ dẫn.

Biến môi trường (ngoài các biến trong ``docs/SPEC-CLI.md`` §17):

``ROFF_STATE_DIR``
    Thư mục state (mặc định ``~/.local/state/roff``).
``ROFF_CONFIG``
    Đường dẫn config INI (mặc định ``~/.config/roff/config.ini``).
``ROFF_SSH_CONFIG``
    Đường dẫn ``~/.ssh/config`` thay thế (seam cho test; mặc định không đặt).
``ROFF_CACHE_DIR``
    Thư mục cache local mà ``roff gc`` dọn (mặc định ``<state>/cache``).
``ROFF_NO_WATCHER=1``
    **Không** spawn watcher nền (chỉ dùng cho test — job vẫn chạy trên host,
    chỉ không có tiến trình giám sát).

Module này **không** in gì ra stdout ngoài :func:`_emit` và :func:`_cmd_logs`.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import sys
import time
from typing import Any, Callable, Iterable, NamedTuple, Sequence

from . import config as config_mod
from . import fleet as fleet_mod
from . import infer as infer_mod
from . import inputs as inputs_mod
from . import job as job_mod
from . import perm
from . import policy as policy_mod
from . import probe as probe_mod
from . import shared as shared_mod
from . import slots as slots_mod
from . import sshx
from . import toolchain as toolchain_mod
from . import transfer as transfer_mod
from . import watcher as watcher_mod
from .toolchain import _layout_root_of, _run_setup
from .errors import (
    EXIT_CANCELLED,
    EXIT_INPUT,
    EXIT_JOB_FAILED,
    EXIT_JOB_LOST,
    EXIT_NO_HOST,
    EXIT_OK,
    EXIT_PULL_FAILED,
    EXIT_TIMEOUT,
    EXIT_UNREACHABLE,
    REASON_PULL_FAILED,
    REASON_SYNC_BACK_FAILED,
    REASON_SYNC_BACK_OUTSIDE_PROJECT,
    RoffError,
    emit,
)

__all__ = ["main"]


# --------------------------------------------------------------------------
# hằng số
# --------------------------------------------------------------------------

#: Số giây chờ xin slot trước khi bỏ (hết slot ⇒ exit 10).
SLOT_WAIT_S = 900.0


def _slot_wait_seconds() -> float:
    """Số giây chờ xin slot (``ROFF_SLOT_WAIT_S`` để chỉnh/seam cho test)."""
    raw = os.environ.get("ROFF_SLOT_WAIT_S")
    if not raw:
        return SLOT_WAIT_S
    try:
        return max(0.0, float(raw))
    except ValueError:
        return SLOT_WAIT_S

#: Số dòng log mặc định của ``roff logs``.
DEFAULT_LOG_TAIL = 200

#: Timeout mặc định của 1 job (giây) — cũng là mặc định của ``roff wait``.
DEFAULT_TIMEOUT = 7200

#: Ngưỡng RAM tối thiểu (MB) cho 1 host (khớp ``policy.MIN_MEM_MB``).
MIN_MEM_MB = 512

#: Placeholder thay cho giá trị secret bị redact khỏi audit log.
REDACTED = "***"

_SIZE_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([kmgt]?)b?\s*$", re.IGNORECASE)
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


# --------------------------------------------------------------------------
# tiện ích chung
# --------------------------------------------------------------------------
def state_dir() -> str:
    """Thư mục state của roff (``$ROFF_STATE_DIR`` → ``~/.local/state/roff``)."""
    env = os.environ.get("ROFF_STATE_DIR")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return os.path.join(os.path.expanduser("~"), ".local", "state", "roff")


def cache_dir() -> str:
    """Thư mục cache local (mục tiêu của ``roff gc``)."""
    env = os.environ.get("ROFF_CACHE_DIR")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return os.path.join(state_dir(), "cache")


def audit_path() -> str:
    """Đường dẫn audit log local (JSONL)."""
    return os.path.join(state_dir(), "audit.jsonl")


def _emit(payload: dict, code: int = EXIT_OK) -> int:
    """In ``payload`` thành 1 JSON object ra stdout và trả exit code.

    Dùng :func:`roffkit.errors.emit` (nguồn sự thật duy nhất của cách in JSON)
    nhưng **không** thoát tiến trình: hàm trả code để :func:`main` quyết định.
    """
    try:
        emit(payload, code)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else code
    return code


def _ok(exit_code: int = EXIT_OK, **fields: Any) -> dict:
    """Dựng payload thành công: ``ok``, ``exit_code`` + khoá có giá trị."""
    payload: dict[str, Any] = {"ok": True, "exit_code": exit_code}
    for key, value in fields.items():
        if value is None:
            continue
        payload[key] = value
    return payload


def _not_ok(exit_code: int, **fields: Any) -> dict:
    """Dựng payload ``ok: false`` (vd job mất, job bị huỷ)."""
    payload: dict[str, Any] = {"ok": False, "exit_code": exit_code}
    for key, value in fields.items():
        if value is None:
            continue
        payload[key] = value
    return payload


def _fail(exc: RoffError) -> dict:
    """Dựng payload lỗi từ :class:`RoffError`.

    Giữ ``ok`` do chính lỗi khai báo (``wait_for`` dùng ``ok=True`` cho
    exit 124 — **không phải lỗi**), luôn có ``exit_code``, và bỏ mọi khoá
    ``None`` trừ ``reason``/``error``/``detail`` chỉ khi chúng có giá trị.
    """
    raw = exc.payload()
    payload: dict[str, Any] = {
        "ok": bool(raw.get("ok", False)),
        "exit_code": int(raw.get("exit_code", exc.code)),
    }
    for key in ("reason", "error", "detail"):
        value = raw.get(key)
        if value:
            payload[key] = value
    for key, value in raw.items():
        if key in ("ok", "exit_code", "reason", "error", "detail"):
            continue
        if value is None:
            continue
        payload[key] = value
    return payload


def _int_option(value: Any, option: str, minimum: int = 0) -> int:
    """Đọc 1 option số nguyên; sai ⇒ ``RoffError(EXIT_INPUT)``."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise RoffError(
            EXIT_INPUT, "invalid_option", error=f"{option} không phải số: {value!r}"
        ) from None
    if number < minimum:
        raise RoffError(
            EXIT_INPUT,
            "invalid_option",
            error=f"{option} phải >= {minimum} (nhận {number})",
        )
    return number


def _float_option(
    value: Any, option: str, minimum: float = 0.0, default: float = 0.0
) -> float:
    """Đọc 1 option số thực; sai ⇒ ``RoffError(EXIT_INPUT)``.

    Khác :func:`_int_option` ở chỗ **phân biệt "không truyền" với "truyền 0"**:
    ``None`` ⇒ lấy ``default``; còn ``0`` là giá trị **hợp lệ** (người dùng
    gõ rõ ràng) và **không** bị coi là falsy ⇒ không được nuốt.
    """
    if value is None:
        return float(default)
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise RoffError(
            EXIT_INPUT, "invalid_option", error=f"{option} không phải số: {value!r}"
        ) from None
    if number < minimum:
        raise RoffError(
            EXIT_INPUT,
            "invalid_option",
            error=f"{option} phải >= {_fmt_num(minimum)} (nhận {_fmt_num(number)})",
        )
    return number


def _fmt_num(number: float) -> str:
    """Số cho người đọc: ``-1`` thay vì ``-1.0`` (``0.0`` ⇒ ``0``)."""
    value = float(number)
    return str(int(value)) if value.is_integer() else str(value)


def _parse_size(text: str, option: str) -> int:
    """Đọc kích thước (``600M``, ``2G``, ``1024``) → **MB**."""
    match = _SIZE_RE.match(str(text))
    if match is None:
        raise RoffError(
            EXIT_INPUT,
            "invalid_size",
            error=f"{option} không hợp lệ: {text!r} (vd 600M, 2G)",
        )
    factor = {"": 1.0, "k": 1.0 / 1024, "m": 1.0, "g": 1024.0, "t": 1024.0 * 1024.0}
    megabytes = float(match.group(1)) * factor[match.group(2).lower()]
    return max(1, int(round(megabytes)))


def _parse_env(pairs: Sequence[str]) -> dict[str, str]:
    """Parse ``--env KEY=VALUE`` (lặp lại được) → dict.

    Sai định dạng ⇒ ``RoffError(EXIT_INPUT)``. Giá trị **không** bao giờ được
    in ra log hay đưa vào argv.
    """
    out: dict[str, str] = {}
    for raw in pairs or []:
        text = str(raw)
        if "=" not in text:
            raise RoffError(
                EXIT_INPUT,
                "invalid_env",
                error=f"--env phải có dạng KEY=VALUE: {text!r}",
            )
        key, _, value = text.partition("=")
        key = key.strip()
        if not _ENV_KEY_RE.match(key):
            raise RoffError(
                EXIT_INPUT,
                "invalid_env",
                error=f"--env có tên biến không hợp lệ: {key!r}",
            )
        out[key] = value
    return out


def _safe_name(text: str, fallback: str = "project") -> str:
    """Tên project an toàn cho đường dẫn trên host."""
    cleaned = _SAFE_NAME_RE.sub("-", str(text or "").strip()).strip("-.")
    return cleaned or fallback


def _default_project_name() -> str:
    """Tên project mặc định: tên thư mục làm việc hiện tại."""
    return _safe_name(os.path.basename(os.path.abspath(os.getcwd())) or "project")


def _shell_runner(host: str, script: str) -> tuple[int, str, str]:
    """Bọc :func:`roffkit.sshx.run_shell` thành protocol của :mod:`layout`.

    ``layout`` cần một runner ``(host, script) -> (rc, stdout, stderr)``;
    ``sshx.run_shell`` trả về đối tượng :class:`RunResult` nên cần bọc lại.
    """
    result = sshx.run_shell(host, script, timeout=60)
    return (int(result.rc), result.stdout or "", result.stderr or "")


# --------------------------------------------------------------------------
# audit log (nguyên tắc 2)
# --------------------------------------------------------------------------
def _redact(text: str, secrets: Iterable[str]) -> str:
    """Thay mọi giá trị secret bằng ``***`` trong ``text``."""
    out = str(text)
    for secret in secrets or []:
        if secret:
            out = out.replace(secret, REDACTED)
    return out


def write_audit(entry: dict) -> str:
    """Ghi 1 dòng JSONL vào audit log local; trả đường dẫn file.

    ``--cmd`` được ghi **nguyên văn**; giá trị ``--env`` bị redact (không
    bao giờ ghi giá trị secret xuống đĩa local).

    Audit log chứa dữ liệu người dùng ⇒ mode **0600** (xem :mod:`roffkit.perm`).
    """
    path = audit_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        perm.secure_dir(os.path.dirname(path))
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        perm.secure_file(path)
    except OSError:
        pass
    return path


def _audit_do(cmd: str, env: dict[str, str], job_id: str, host: str, project: str) -> str:
    """Ghi audit cho 1 lần ``roff do``.

    Nguyên tắc: ``cmd`` nguyên văn (trừ phần trùng với giá trị ``--env``,
    vốn không được nằm trong ``--cmd``); **không** ghi giá trị secret.
    """
    entry = {
        "ts": time.time(),
        "event": "do",
        "job": job_id,
        "host": host,
        "project": project,
        "cmd": _redact(cmd, env.values()),
        "env_keys": sorted(env.keys()),
    }
    return write_audit(entry)


# --------------------------------------------------------------------------
# config / fleet / probe
# --------------------------------------------------------------------------
def _load_config() -> config_mod.Config:
    """Nạp config INI; sai ⇒ ``RoffError(EXIT_CONFIG)`` do :mod:`config` ném.

    **B27**: ``ROFF_CONFIG`` trỏ tới file **không tồn tại** ⇒ exit **3**,
    ``reason="config_missing"``. Đây là điểm vào nguy hiểm nhất (chỗ gửi
    job/dữ liệu lên máy khác): bỏ qua im lặng sẽ khiến config rỗng ⇒
    ``fleet_include = ["*"]`` ⇒ fleet lấy **MỌI** host trong ``~/.ssh/config``
    (mất luôn ``exclude`` của người dùng).

    Không có ``ROFF_CONFIG`` ⇒ ``~/.config/roff/config.ini``; thiếu file ⇒
    config mặc định (chạy được ngay khi mới cài) — **không** phải lỗi.
    """
    return config_mod.Config.load(
        os.environ.get("ROFF_CONFIG") or None, strict=True
    )


def _fleet_flags(cfg: config_mod.Config) -> dict:
    """Cờ JSON báo fleet có bị giới hạn hay không (B27).

    Fleet **có** giới hạn ⇒ trả ``{}`` (payload không đổi).
    Fleet **không** giới hạn (``include`` hiệu lực là ``["*"]``) ⇒ thêm
    ``fleet_unrestricted: true`` + ``fleet_warning`` (text tiếng Việt).
    """
    if not cfg.fleet_unrestricted():
        return {}
    return {
        "fleet_unrestricted": True,
        "fleet_warning": cfg.fleet_warning(),
    }


def _fleet_aliases(cfg: config_mod.Config) -> list[str]:
    """Danh sách alias của fleet (theo include/exclude của config)."""
    return fleet_mod.env_fleet_aliases(cfg)


def _ssh_config_aliases() -> list[str] | None:
    """Mọi alias có trong ``~/.ssh/config`` — **KHÔNG** lọc theo glob của fleet.

    Dùng để phân biệt "alias không tồn tại" (gõ sai) với "host có thật nhưng
    đang chết / không với được".

    Trả ``None`` khi **đọc/parse config lỗi** ⇒ không biết alias nào tồn tại
    ⇒ không được kết luận ``unknown_host`` (phải probe thật và báo
    ``host_unreachable`` + lỗi ssh gốc).
    """
    try:
        # Một nguồn sự thật duy nhất (B25): ``ssh_config_file()`` là **đúng**
        # file mà roff fingerprint (và truyền ``-F`` cho ssh) — không đọc
        # ``$ROFF_SSH_CONFIG`` bằng tay, không lệch với phía ssh.
        hosts = fleet_mod.parse_ssh_config(fleet_mod.ssh_config_file())
    except Exception:  # config ssh hỏng ⇒ không biết alias nào có thật
        return None
    return [host.alias for host in hosts]


def _probe_forced_host(alias: str, cfg: config_mod.Config) -> list[Any]:
    """Probe host được ghim bằng ``--host`` — **bypass cache** (luôn đo mới).

    Host **không** nằm trong fleet vẫn được probe: người dùng đã ép host ⇒ họ
    có thể muốn 1 máy nằm ngoài include-glob.

    Raises:
        RoffError: ``EXIT_UNREACHABLE`` (255).

        * ``unknown_host`` — alias **không** có trong ``~/.ssh/config``, hoặc
          ssh báo "không tìm thấy host" (``detail`` = lỗi ssh gốc);
        * ``host_unreachable`` — host có thật nhưng ssh lỗi (timeout, refused,
          permission denied…) (``detail`` = lỗi ssh gốc).
    """
    profiles = probe_mod.cached_profiles([alias], cfg, force=[alias])
    profile = profiles[0] if profiles else None
    if profile is not None and getattr(profile, "reachable", False):
        return profiles

    detail = ""
    if profile is not None:
        detail = str(
            getattr(profile, "detail", None) or getattr(profile, "error", None) or ""
        )
    aliases = _ssh_config_aliases()
    # ``aliases is None`` ⇒ **đọc/parse config lỗi** ⇒ không biết alias nào
    # có thật ⇒ KHÔNG được kết luận ``unknown_host`` (W3d).
    known = None if aliases is None else alias in aliases
    low = detail.lower()
    not_found = (
        "could not resolve" in low
        or "name or service not known" in low
        or "host not found" in low
        or "no such host" in low
        or "unknown host" in low
    )
    if known is False and not_found:
        # Alias **không có** trong ~/.ssh/config và ssh cũng không tìm thấy
        # ⇒ gõ sai tên host.
        raise RoffError(
            EXIT_UNREACHABLE,
            "unknown_host",
            error=f"không có host nào tên {alias!r} trong ~/.ssh/config",
            detail=detail or f"ssh: Could not resolve hostname {alias}",
            host=alias,
        )
    # Host **có thật** (có trong ~/.ssh/config) nhưng không với được ⇒ KHÔNG
    # bao giờ trả `unknown_host` (bug: host sống bị báo "không có host nào").
    raise RoffError(
        EXIT_UNREACHABLE,
        "host_unreachable",
        error=f"không với được host {alias}",
        detail=detail or f"ssh lỗi khi probe {alias}",
        host=alias,
    )


def _busy_map() -> dict[str, int]:
    """Số job roff đang **bận** trên mỗi host (cho điểm "đang bận").

    Gồm cả job **đang xếp hàng** (chờ slot): nếu không, host ít tải được chọn
    lại nhiều lần hơn cần (mỗi job mới lại thấy host "rảnh" rồi xếp hàng).
    """
    try:
        return slots_mod.SlotLedger().busy_counts()
    except Exception:  # pragma: no cover - sổ hỏng thì coi như không bận
        return {}


def _running_map() -> dict[str, int]:
    """Số job đang **giữ slot** trên mỗi host (luật "đã đầy slot")."""
    try:
        return slots_mod.SlotLedger().counts()
    except Exception:  # pragma: no cover - sổ hỏng thì coi như không bận
        return {}


# --------------------------------------------------------------------------
# chọn host
# --------------------------------------------------------------------------
def _infer_needs(cmd: str) -> list[str]:
    """Tool **suy ra** từ câu lệnh — tiêu chí **MỀM** (xem S37/S38).

    Chỉ đọc **văn bản** của ``--cmd`` (:func:`roffkit.infer.infer_tools`) ⇒
    **không** gọi ssh, **không** đọc file ⇒ gọi được ở đường chạy nóng.

    Kết quả dùng để **ưu tiên** host có sẵn tool đó. Suy ra có thể **sai**
    (``sh build.sh`` không ai biết nó cần gì) ⇒ tool suy ra:

    * **không** được loại host (host thiếu tool vẫn là ứng viên hợp lệ);
    * **không** được kích hoạt bước *cài* tool — chỉ ``--need`` mới được cài;
    * ``--need`` vẫn là tiêu chí **cứng** (thiếu tool ⇒ loại host, exit 10).

    Lỗi suy ra (lệnh hỏng, bảng alias hỏng …) ⇒ coi như **không** suy ra gì:
    suy ra là phỏng đoán, không được làm hỏng việc chạy job.
    """
    try:
        return [str(tool) for tool in (infer_mod.infer_tools(str(cmd or "")) or [])]
    except Exception:  # pragma: no cover - suy ra hỏng ⇒ coi như không suy ra
        return []


class _HostChoice(NamedTuple):
    """Kết quả chọn host cho 1 job."""

    host: str
    profile: Any
    reasons: list[str]
    rejected: dict[str, str]
    via_local: bool = False


def _best_mem(profiles: Sequence[Any]) -> tuple[str, int]:
    """Host nhiều RAM khả dụng nhất + số MB (để báo "best is …")."""
    best_alias = ""
    best_mb = 0
    for profile in profiles or []:
        available = int(getattr(profile, "mem_available_mb", 0) or 0)
        if available > best_mb:
            best_mb = available
            best_alias = str(getattr(profile, "alias", ""))
    return best_alias, best_mb


def _select_host(
    profiles: Sequence[Any],
    need: policy_mod.Need,
    cfg: config_mod.Config,
    busy: dict[str, int],
    prefer_hosts: Sequence[str],
    forced: str | None,
    wants_internet: bool,
    ignore_slots: bool = False,
    ignore_busy: bool = False,
    running: dict[str, int] | None = None,
    inferred_needs: Sequence[str] | None = None,
) -> _HostChoice:
    """Chọn host cho job (ép host nếu ``--host``), hoặc ném lỗi exit 10.

    * ``--host`` ⇒ bỏ qua scheduler, nhưng host vẫn phải **reachable**.
    * Không host nào dùng được ⇒ ``RoffError(EXIT_NO_HOST)`` kèm lý do loại
      của **từng** host (:func:`policy.reject_all`).
    * ``ignore_slots=True`` ⇒ host **đã đầy slot** vẫn là ứng viên hợp lệ
      (đầy slot = "đang bận" ⇒ job sẽ xếp hàng, KHÔNG phải "không dùng
      được"). Lý do loại vẫn được giữ để in ra.
    * ``ignore_busy=True`` ⇒ host **bridge đang bận** vẫn là ứng viên hợp lệ
      (bridge bận = "đang bận" ⇒ job sẽ xếp hàng).
    * ``inferred_needs`` = tool **suy ra** từ câu lệnh — tiêu chí **MỀM**:
      host có tool đó được **ưu tiên**, host không có **vẫn** là ứng viên hợp
      lệ (chỉ bị đẩy xuống cuối). Rỗng ⇒ hành vi **không đổi**.
    """
    if forced:
        profile = next(
            (p for p in profiles if getattr(p, "alias", "") == forced), None
        )
        if profile is None:
            raise RoffError(
                EXIT_UNREACHABLE,
                "unknown_host",
                error=f"không có host nào tên {forced!r} trong fleet",
                detail=f"ssh: Could not resolve hostname {forced}",
                host=forced,
            )
        if not getattr(profile, "reachable", False):
            raise RoffError(
                EXIT_UNREACHABLE,
                "host_unreachable",
                error=f"không với được host {forced}",
                detail=getattr(profile, "detail", None) or getattr(profile, "error", None),
                host=forced,
            )
        return _HostChoice(
            host=forced,
            profile=profile,
            reasons=[f"host bị ép qua --host {forced}"],
            rejected={},
        )

    def _select(busy_map: dict[str, int]) -> Any:
        return policy_mod.select(
            profiles,
            need,
            cfg,
            busy=busy_map,
            prefer_hosts=list(prefer_hosts or []),
            ignore_slots=ignore_slots,
            ignore_busy=ignore_busy,
            running=running,
            # Tool suy ra: tiêu chí **MỀM** (ưu tiên, không loại host).
            inferred_needs=list(inferred_needs or []),
        )

    choice = _select(busy)
    via_local = False
    if choice is None and wants_internet:
        # Không host nào có internet → vẫn chạy được: tải qua local rồi đẩy lên.
        need.internet = False
        choice = _select(busy)
        via_local = choice is not None
    if choice is None:
        rejected = policy_mod.reject_all(
            profiles, need, cfg, busy, ignore_busy=ignore_busy, running=running
        )
        best_alias, best_mb = _best_mem(profiles)
        parts = [f"{alias}: {why}" for alias, why in rejected.items()]
        message = "không có host nào dùng được"
        if need.mem_mb:
            message += f" (cần ≥ {max(MIN_MEM_MB, need.mem_mb)} MB RAM)"
        if best_alias:
            message += f"; nhiều RAM nhất: {best_mb} MB trên {best_alias}"
        if parts:
            message += " — " + "; ".join(parts)
        raise RoffError(
            EXIT_NO_HOST,
            "no_host",
            error=message,
            rejected=rejected,
            best={"host": best_alias, "mem_available_mb": best_mb} if best_alias else None,
        )

    profile = next(
        (p for p in profiles if getattr(p, "alias", "") == choice.host), None
    )
    reasons = list(choice.reasons)
    if via_local:
        reasons.append("host không có internet → tải input qua local rồi đẩy lên")
    return _HostChoice(
        host=choice.host,
        profile=profile,
        reasons=reasons,
        rejected=dict(choice.rejected),
        via_local=via_local,
    )


# --------------------------------------------------------------------------
# input
# --------------------------------------------------------------------------
class _Input(NamedTuple):
    """Một input của job."""

    kind: str  # "path" | "git" | "http" | "job"
    value: str


def _parse_input(spec: str) -> _Input:
    """Parse 1 ``--in``: path local | ``git:<url>`` | ``http(s)://…`` | ``job:<id>``."""
    text = str(spec).strip()
    if text.startswith("git:"):
        return _Input("git", text[4:].strip())
    if text.startswith("job:"):
        return _Input("job", text[4:].strip())
    if text.startswith("http://") or text.startswith("https://"):
        return _Input("http", text)
    return _Input("path", text)


def _push_local_path(host: str, local_path: str, remote_dir: str) -> str:
    """Đẩy 1 file/thư mục local lên host; trả **đường dẫn remote** tương ứng.

    Thư mục → nội dung nằm trong ``remote_dir`` (đường dẫn remote = ``remote_dir``).
    File → nằm trong ``remote_dir/<tên>`` (đường dẫn remote = ``remote_dir/<tên>``).
    """
    return inputs_mod.push_local_path(host, local_path, remote_dir)


def _clone_git_on_host(host: str, url: str, remote_dir: str) -> str:
    """Clone git **trên host** (không đi qua máy local) → trả đường dẫn remote."""
    return inputs_mod.clone_git_on_host(host, url, remote_dir)


def _download_http_locally(url: str) -> str:
    """Tải URL về máy local (chỉ dùng khi host **không** có internet)."""
    return inputs_mod.download_http_locally(url)


# --------------------------------------------------------------------------
# toolchain / ensure
# --------------------------------------------------------------------------
# Phần toolchain nằm ở :mod:`roffkit.toolchain` vì watcher cũng phải đảm bảo
# toolchain trên host mới khi failover (mà watcher không được import cli — cli
# import watcher, import ngược lại là import vòng).
def _ensure_tools(host: str, profile: Any, tools: Sequence[str]) -> list[dict]:
    """Cài các tool còn thiếu trên host (bọc :func:`roffkit.toolchain.ensure_tools`)."""
    return toolchain_mod.ensure_tools(host, profile, tools)


# --------------------------------------------------------------------------
# slot
# --------------------------------------------------------------------------
def _reconcile_slots(exclude: Sequence[str] = ()) -> dict:
    """Dọn ticket/lease **mồ côi** của sổ slot (W3a).

    Đọc ledger job local ⇒ tập owner còn sống (job tồn tại **và** state không
    terminal) ⇒ bỏ mọi ticket mà owner không nằm trong tập đó, hoặc ticket quá
    hạn (mặc định 24 h). Lease mà watcher đã chết ⇒ thu hồi slot **và** đánh
    dấu job chủ là ``lost`` (reason ``watcher_died``).

    Không bao giờ đụng tới lease của job trong ``exclude`` (job đang chạy
    reconcile).
    """
    try:
        return slots_mod.reconcile_slots(exclude_owners=list(exclude or []))
    except Exception:  # pragma: no cover - sổ hỏng thì bỏ qua
        return {"removed": [], "lost_jobs": []}


def _release_slot(job_id: str) -> None:
    """Trả slot đã chiếm của 1 job (idempotent).

    Nếu job đang **xếp hàng** (chưa có slot) thì ticket của nó cũng bị bỏ
    khỏi hàng đợi — nếu không, chỗ trong hàng đợi bị chiếm vô ích.
    """
    if not job_id:
        return
    try:
        options = job_mod.job_options(job_id)
    except Exception:  # pragma: no cover
        return
    ticket = options.get("slot_ticket")
    if not ticket:
        return
    try:
        slots_mod.SlotLedger().cancel_slot(str(ticket))
    except Exception:  # pragma: no cover - release idempotent
        pass
    try:
        job_mod.set_job_options(job_id, slot_ticket="", slot_released=True)
    except Exception:  # pragma: no cover
        pass


def _mark_job_lost(job_id: str, reason: str) -> None:
    """Đánh dấu job ``lost`` — dùng khi job đã tạo mà không ai theo dõi.

    Trạng thái ``queued`` mà không có watcher ⇒ job **bị bỏ rơi**: agent đợi
    mãi cũng không có kết quả. Đánh ``lost`` + reason ⇒ ``roff wait`` trả về
    ngay với lý do rõ ràng.
    """
    if not job_id:
        return
    try:
        job = job_mod.load_job(job_id)
    except Exception:  # pragma: no cover
        return
    if job.state in job_mod.TERMINAL_STATES:
        return
    job.state = "lost"
    job.reason = reason
    try:
        job_mod.save_job(job)
    except Exception:  # pragma: no cover
        pass


# --------------------------------------------------------------------------
# lệnh: do
# --------------------------------------------------------------------------
def _job_specs(args: argparse.Namespace) -> list[dict]:
    """Danh sách "job spec" cho 1 lần ``roff do``.

    * ``--each '<glob>'`` ⇒ 1 job mỗi item (mỗi job được chọn host khác nhau).
    * ``--shards N`` (N>1) ⇒ N job, mỗi job 1 shard.
    * còn lại ⇒ 1 job.
    """
    specs: list[dict] = []
    items: list[str] = []
    each = getattr(args, "each", None)
    if each:
        pattern = os.path.expanduser(str(each))
        items = sorted(glob.glob(pattern))
        if not items:
            raise RoffError(
                EXIT_INPUT,
                "no_items",
                error=f"--each không khớp file nào: {each}",
                each=str(each),
            )
    # ``--shards`` mặc định là 1. **0 là giá trị người dùng gõ** ⇒ không được
    # nuốt bằng ``or`` (nuốt ⇒ thông điệp lỗi nói "1" thay vì "0" ⇒ người
    # đọc không biết mình đã gõ sai gì).
    raw_shards = getattr(args, "shards", None)
    shards = 1 if raw_shards is None else _int_option(raw_shards, "--shards", minimum=1)
    if items:
        for item in items:
            specs.append(
                {"item": item, "shard": None, "shards": None, "items": len(items)}
            )
    elif shards > 1:
        for index in range(shards):
            specs.append(
                {"item": None, "shard": index, "shards": shards, "items": None}
            )
    else:
        specs.append({"item": None, "shard": None, "shards": None, "items": None})
    return specs


# --------------------------------------------------------------------------
# `--sync-back` — ánh xạ path local ⇄ remote (B26)
# --------------------------------------------------------------------------
def _rel_inside(base: str, target: str) -> str | None:
    """Đường dẫn ``target`` **tương đối** so với ``base``; ``None`` nếu ngoài.

    * ``target == base`` ⇒ ``""`` (trùng chính nó);
    * ``target`` nằm trong ``base`` ⇒ đường dẫn tương đối, dấu phân tách
      ``/`` (dùng được cho đường dẫn remote);
    * ``target`` nằm ngoài ``base`` ⇒ ``None``.
    """
    root = os.path.abspath(str(base or ""))
    path = os.path.abspath(str(target or ""))
    if not root or not path:
        return None
    if path == root:
        if os.path.isdir(root):
            return ""
        # Input là **file** ⇒ trong kho dùng chung nó nằm ở ``{key}/{tên}``.
        return os.path.basename(root)
    if path.startswith(root.rstrip(os.sep) + os.sep):
        return os.path.relpath(path, root).replace(os.sep, "/")
    return None


def _sync_back_targets(
    project_root: str,
    sync_back: Sequence[str],
    path_sources: Sequence[dict],
) -> list[str]:
    """Đích **local** của mỗi path ``--sync-back`` (B26) + kiểm tra hợp lệ.

    * ``p`` **tương đối** ⇒ đích local = ``abspath(join(project_root, p))``;
    * ``p`` **tuyệt đối** ⇒ giữ nguyên ``p``.

    Path nằm ngoài ``project_root`` **và** ngoài mọi input source ⇒ **từ
    chối ngay** (exit 2, ``sync_back_outside_project``): không có nơi nào trên
    host để tìm file đó ⇒ im lặng làm cái gì đó vô nghĩa (B26).

    Raises:
        RoffError: ``EXIT_INPUT`` + ``sync_back_outside_project``.
    """
    out: list[str] = []
    for raw in sync_back:
        text = os.path.expanduser(str(raw))
        if os.path.isabs(text):
            local = os.path.abspath(text)
        else:
            local = os.path.abspath(os.path.join(project_root, text))
        inside = _rel_inside(project_root, local) is not None
        if not inside:
            for source in path_sources:
                base = str(source.get("local") or "")
                if base and _rel_inside(base, local) is not None:
                    inside = True
                    break
        if not inside:
            raise RoffError(
                EXIT_INPUT,
                REASON_SYNC_BACK_OUTSIDE_PROJECT,
                error=(
                    f"path --sync-back nằm ngoài project: {raw!r} "
                    f"(project root: {project_root})"
                ),
                path=str(raw),
                project_root=project_root,
            )
        out.append(local)
    return out


def _cmd_do(args: argparse.Namespace) -> tuple[dict, int]:
    """``roff do`` — lệnh chính (thứ tự theo ``docs/SPEC-CLI.md`` §16).

    validate → nạp config → chờ ``--after`` → probe fleet → chọn host →
    **tạo job** → xin slot → ensure toolchain → push input → launch job →
    spawn watcher → trả JSON.

    Thứ tự "**tạo job TRƯỚC, xin slot SAU**" là bắt buộc (W3a): ticket slot
    phải biết **chủ** (job id) để ticket mồ côi (job chết trước khi watcher
    chạy) nhận ra và dọn được — nếu không, 1 ticket mồ côi ở đầu hàng kẹt
    **vĩnh viễn** mọi job sau của host đó.
    """
    # --- 1. validate toàn bộ option (sai ⇒ exit 2, chưa làm gì) -----------
    cmd = str(getattr(args, "cmd", "") or "").strip()
    if not cmd:
        raise RoffError(EXIT_INPUT, "missing_cmd", error="--cmd không được rỗng")
    # Tool **suy ra** từ câu lệnh — tính **1 lần** (không lặp lại cho mỗi job
    # của ``--each``/``--shards``), **không** gọi ssh ⇒ đường chạy nóng OK.
    # Chỉ dùng để **ưu tiên** host; KHÔNG dùng để loại host hay để cài tool.
    inferred_needs = _infer_needs(cmd)
    timeout = _int_option(getattr(args, "timeout", DEFAULT_TIMEOUT), "--timeout")
    if timeout <= 0:
        raise RoffError(
            EXIT_INPUT,
            "invalid_timeout",
            error=f"--timeout phải > 0 (nhận {timeout})",
        )
    retry = _int_option(getattr(args, "retry", 1), "--retry")
    cpus = _int_option(getattr(args, "cpus", 0) or 0, "--cpus")
    priority = str(getattr(args, "priority", "normal") or "normal").lower()
    if priority not in ("high", "normal"):
        raise RoffError(
            EXIT_INPUT,
            "invalid_priority",
            error=f"--priority phải là high|normal (nhận {priority!r})",
        )
    env = _parse_env(getattr(args, "env", None) or [])
    mem_mb = _parse_size(args.mem, "--mem") if getattr(args, "mem", None) else 0
    disk_mb = _parse_size(args.disk, "--disk") if getattr(args, "disk", None) else 0
    inputs = [_parse_input(spec) for spec in (getattr(args, "inputs", None) or [])]
    sync_back = list(getattr(args, "sync_back", None) or [])
    for item in inputs:
        if item.kind == "path":
            local = os.path.abspath(os.path.expanduser(item.value))
            if not os.path.exists(local):
                raise RoffError(
                    EXIT_INPUT,
                    "input_not_found",
                    error=f"không có input local: {item.value}",
                    input=item.value,
                )
        elif item.kind == "job":
            if not item.value:
                raise RoffError(
                    EXIT_INPUT, "invalid_input", error="--in job: cần job id"
                )

    # Kế hoạch input (CHƯA chạm vào host): mọi input đều được ghi vào ledger
    # ⇒ watcher dựng lại được khi failover sang host khác (input git:/http:/job:
    # không phải path nên không "đẩy lại" được, phải dựng lại từ đầu).
    input_specs = [{"kind": item.kind, "value": item.value} for item in inputs]
    path_sources: list[dict[str, Any]] = []
    for item in inputs:
        if item.kind != "path":
            continue
        local = os.path.abspath(os.path.expanduser(item.value))
        path_sources.append(
            {
                "local": local,
                "key": shared_mod.shared_key(local),
                "rel": shared_mod.shared_rel(local),
            }
        )

    # --- `--sync-back`: gốc project = thư mục làm việc lúc gọi `roff do` ---
    # Đích local tính được ngay (không phụ thuộc host) ⇒ path nằm ngoài
    # project **và** ngoài mọi input bị **từ chối ngay** (exit 2), trước khi
    # đụng vào fleet — đừng tạo job rồi mới biết là vô nghĩa (B26).
    project_root = os.path.abspath(os.getcwd())
    sync_back_local = _sync_back_targets(project_root, sync_back, path_sources)

    after = str(getattr(args, "after", "") or "").strip()
    after_id = ""
    if after:
        after_id = after.split(":", 1)[1] if after.startswith("job:") else after
        if not after_id:
            raise RoffError(
                EXIT_INPUT, "invalid_after", error=f"--after không hợp lệ: {after!r}"
            )
    specs = _job_specs(args)

    # --- 2. nạp config (sai ⇒ exit 3) ------------------------------------
    cfg = _load_config()

    # --- 3. chờ --after (poll ledger cục bộ) ------------------------------
    if after_id:
        _wait_dependency(after_id, timeout)

    # --- 4. probe fleet (cache 60 s) ---------------------------------------
    aliases = _fleet_aliases(cfg)
    forced_host = str(getattr(args, "host", None) or "").strip()
    if forced_host:
        # ``--host``: đo **trực tiếp** host được ghim (bypass cache). Cache cũ
        # không bao giờ được che host này — nếu không, 1 entry cache hỏng khiến
        # host thật biến mất khỏi danh sách ⇒ "unknown_host" cho máy đang sống.
        profiles = _probe_forced_host(forced_host, cfg)
    else:
        profiles = probe_mod.cached_profiles(aliases, cfg)
        if not profiles:
            raise RoffError(
                EXIT_NO_HOST,
                "empty_fleet",
                error="fleet rỗng: không có alias nào khớp config",
                aliases=aliases,
            )

    project = _safe_name(
        getattr(args, "name", None) or _default_project_name(), "project"
    )
    needs = list(getattr(args, "need", None) or [])
    wants_internet = any(item.kind == "http" for item in inputs)
    prefer_same_host = bool(getattr(args, "prefer_same_host", True))

    # input từ job khác → ưu tiên host đang giữ dữ liệu
    prefer_hosts: list[str] = []
    source_job: job_mod.Job | None = None
    for item in inputs:
        if item.kind == "job":
            source_job = job_mod.load_job(item.value)
            if prefer_same_host and source_job.host:
                prefer_hosts.append(source_job.host)

    ledger = slots_mod.SlotLedger()
    used_hosts: set[str] = set()

    job_ids: list[str] = []
    created_jobs: list[dict] = []
    host_reasons: list[str] = []
    first_host = ""
    first_job = ""
    first_reasons: list[str] = []
    results: list[dict] = []

    # ------------------------------------------------------------------
    # helper: chọn host
    # ------------------------------------------------------------------
    def _pick_host(
        need: policy_mod.Need,
        busy: dict[str, int],
        running: dict[str, int],
        tried: set[str],
    ) -> _HostChoice:
        """Chọn host cho 1 job.

        Ưu tiên host **chưa dùng** trong lô. Nếu không còn host nào dùng được
        ⇒ dùng lại host đã dùng (ít tải nhất) và **bỏ qua** 2 luật "đang bận"
        ("đã đầy slot" + "bridge đang bận"): 2 luật đó ⇒ job **xếp hàng**,
        không phải "không dùng được" (W3b).

        Tool **suy ra** từ lệnh (``inferred_needs``) đi xuyên suốt tới
        :func:`policy.select` — tiêu chí **MỜM** (ưu tiên host có sẵn tool,
        không loại host thiếu tool).
        """
        remaining = [
            p
            for p in profiles
            if getattr(p, "alias", "") not in used_hosts
            and getattr(p, "alias", "") not in tried
        ]
        if remaining:
            try:
                return _select_host(
                    remaining,
                    need,
                    cfg,
                    busy,
                    prefer_hosts,
                    forced_host,
                    wants_internet,
                    running=running,
                    inferred_needs=inferred_needs,
                )
            except RoffError as exc:
                if exc.code != EXIT_NO_HOST:
                    raise
        fallback = [p for p in profiles if getattr(p, "alias", "") not in tried]
        if not fallback:
            raise RoffError(
                EXIT_NO_HOST,
                "no_host",
                error="không còn host nào dùng được",
                rejected={},
            )
        return _select_host(
            fallback,
            need,
            cfg,
            busy,
            prefer_hosts,
            forced_host,
            wants_internet,
            ignore_slots=True,
            ignore_busy=True,
            running=running,
            inferred_needs=inferred_needs,
        )

    def _drop_ticket(ticket: str, lease: Any) -> None:
        """Trả slot / bỏ ticket khi job không chạy được trên host này."""
        if not ticket:
            return
        try:
            if lease is not None:
                ledger.release(lease)
            else:
                ledger.cancel_slot(ticket)
        except Exception:  # pragma: no cover - release idempotent
            pass

    def _sync_back_remotes(workdir: str, layout_root: str) -> list[str]:
        """Đường dẫn **remote** của các path ``--sync-back`` (B26).

        Ánh xạ theo **đích local** (đã tính ở :func:`_sync_back_targets`):

        * đích local nằm trong 1 input (nguồn local) ⇒ nằm trong **kho dùng
          chung** của host (``<layout_root>/inputs/<key>/…``) — input không
          còn được copy vào workdir (W3f);
        * đích local nằm trong ``project_root`` ⇒ ``<workdir>/<relpath>`` —
          cwd của job **chính là** workdir (``$ROFF_WORK``) ⇒ file job tạo ra
          nằm đúng chỗ (vd ``--sync-back report.md`` ⇒ ``<workdir>/report.md``).
        """
        out: list[str] = []
        for local in sync_back_local:
            remote = ""
            for source in path_sources:
                base = os.path.abspath(str(source.get("local") or ""))
                if not base:
                    continue
                rel = _rel_inside(base, local)
                if rel is None:
                    continue
                key = str(source.get("key") or "")
                remote = shared_mod.shared_remote_path(layout_root, key, rel)
                break
            if not remote:
                rel = _rel_inside(project_root, local) or ""
                remote = f"{workdir.rstrip('/')}/{rel}" if rel else workdir
            out.append(remote)
        return out

    # ------------------------------------------------------------------
    # helper: 1 job của lô
    # ------------------------------------------------------------------
    def _prepare_spec(index: int, spec: dict) -> dict:
        """Tạo job → xin slot → **giao việc đẩy input + launch cho watcher**.

        (B36/C10) ``do`` **không** đẩy input và **không** launch nữa ⇒ vòng
        lặp ``--each`` chỉ làm việc **cục bộ** (rẻ) cho từng item ⇒ lô được
        gửi **song song**, các job **chạy chồng nhau**.

        Setup toolchain (``--need``) thất bại vì **host chết** ⇒ thử host khác
        (tối đa N lần, N = số host trong fleet). Lỗi do **sai cấu hình** (URL
        hỏng, thiếu tool) ⇒ **không** retry — báo lỗi luôn (W3c + W3m: exit
        1/2, không phải 255).

        Lỗi **đẩy input** (host chết lúc rsync, URL hỏng, file local biến mất)
        **không** còn lộ ra ở đây mà lộ ra ở **watcher** (xem báo cáo C10).
        """
        nonlocal job_ids, created_jobs, used_hosts
        tried: set[str] = set()
        attempts = 1 if forced_host else max(1, len(profiles))
        job_id = ""
        job: job_mod.Job | None = None
        nonlocal first_job, first_host, first_reasons
        for attempt in range(attempts):
            busy = _busy_map()
            running = _running_map()
            need = policy_mod.Need(
                # CHỈ tool ghi rõ bằng ``--need`` ⇒ tiêu chí **CỨNG** (thiếu
                # tool ⇒ loại host). Tool **suy ra** cố ý KHÔNG nằm ở đây:
                # suy ra là phỏng đoán, không được loại host (S38).
                tools=list(needs),
                mem_mb=mem_mb,
                disk_mb=disk_mb,
                cpus=cpus,
                internet=wants_internet,
                project=project,
            )
            try:
                choice = _pick_host(need, busy, running, tried)
            except RoffError as exc:
                # Vẫn phải fail (cả fleet chết) ⇒ báo kèm job đã tạo để agent
                # không mất dấu (W2d).
                exc.fields["jobs"] = list(created_jobs)
                raise
            host = choice.host
            layout_root = _layout_root_of(choice.profile)
            workdir = f"{layout_root}/projects/{project}"
            if len(specs) > 1:
                # Lô nhiều job ⇒ mỗi job một thư mục làm việc riêng (không đụng nhau)
                workdir = f"{workdir}/part-{index}"
            if spec.get("shard") is not None:
                workdir = f"{workdir}/shard-{spec['shard']}"

            out_local = ""
            if getattr(args, "out", None):
                out_local = os.path.abspath(os.path.expanduser(str(args.out)))

            # --- tạo job TRƯỚC khi xin slot (ticket phải có chủ) ---------
            if job is None:
                job_id = job_mod.new_job_id()
                job = job_mod.Job(
                    id=job_id,
                    cmd=cmd,
                    host=host,
                    project=project,
                    created_at=time.time(),
                    state="queued",
                    cpus=cpus,
                    mem_mb=mem_mb,
                    out_remote=f"{layout_root}/jobs/{job_id}/out",
                    out_local=out_local or None,
                    shard=spec.get("shard"),
                    shards=spec.get("shards"),
                    needs_retry=not bool(getattr(args, "side_effects", False)),
                )
                job_mod.save_job(job)
            else:
                # Host cũ chết ⇒ chuyển **cùng job** sang host mới.
                job.host = host
                job.out_remote = f"{layout_root}/jobs/{job_id}/out"
                job_mod.save_job(job)

            # --- reconcile sổ slot TRƯỚC khi xin slot mới ----------------
            # Dọn ticket/lease mồ côi (job đã chết / watcher đã chết) để
            # host không bị kẹt vĩnh viễn (W3a).
            _reconcile_slots(exclude=[job_id])

            # --- xin slot (KHÔNG chờ) --------------------------------------
            # ``roff do`` **không bao giờ** block chờ slot: hết slot là "đang
            # bận" (job xếp hàng), không phải lỗi. 1 ticket duy nhất giữ
            # đúng vị trí FIFO cho tới khi watcher được cấp slot.
            # ``slots = 0`` trong config ⇒ bị coi là "không đặt" ⇒ dùng 1
            # (giữ **nguyên** hành vi cũ: người dùng có thể đang dùng 0 để
            # "tắt" host — đổi hành vi này là quyết định sản phẩm, không phải
            # việc sửa lỗi ở đây).
            slots = int(getattr(cfg.host(host), "slots", 1) or 1)
            ticket = ledger.request_slot(host, slots, priority, owner=job_id)
            lease = ledger.try_acquire(ticket, owner=job_id)
            queued = lease is None
            reasons = list(choice.reasons)
            if queued:
                reasons = reasons + [
                    f"host {host} đang hết slot → job xếp hàng (FIFO)"
                ]

            checkpoint_remote = ""
            if getattr(args, "checkpoint", None):
                # Checkpoint phải là path **phía host** (W3n): path local vô
                # nghĩa vì lệnh chạy trên host.
                checkpoint_remote = f"{workdir.rstrip('/')}/.checkpoint"

            options: dict[str, Any] = {
                "retry": retry,
                "timeout": timeout,
                "priority": priority,
                "needs": list(needs),
                "mem_mb": mem_mb,
                "disk_mb": disk_mb,
                "cpus": cpus,
                "workdir": workdir,
                "layout_root": layout_root,
                "host_reasons": reasons,
                "prefer_hosts": list(prefer_hosts),
                "sync_back": list(sync_back),
                "sync_back_local": list(sync_back_local),
                "sync_back_remote": _sync_back_remotes(workdir, layout_root),
                # Gốc project local (thư mục làm việc lúc gọi `roff do`) ⇒
                # watcher biết đường dẫn local đích của `--sync-back` (B26).
                "project_root": project_root,
                "checkpoint": checkpoint_remote,
                "items": spec.get("items"),
                "slot_ticket": ticket,
                "slot_host": host,
                "slot_slots": slots,
                "input_dir": "",
                "input_remote": "",
                "via_local": choice.via_local,
                # host có internet không (watcher dùng để biết ai tải URL)
                "host_internet": bool(getattr(choice.profile, "internet", False)),
                # (W2c) mọi input được ghi lại ⇒ watcher dựng lại được khi
                # failover (git:/http:/job: không "đẩy lại" được).
                "input_specs": [dict(spec) for spec in input_specs],
                "input_sources": [dict(source) for source in path_sources],
                # (B36/C10) Job được khởi động bởi **watcher**, không phải bởi
                # ``do``: ``do`` chỉ tạo job + xin slot, còn **đẩy input** và
                # **launch** do watcher lo (đúng đường dẫn của nhánh "job xếp
                # hàng"). Nhờ vậy vòng lặp ``--each`` **không** làm việc từ xa
                # tuần tự nữa ⇒ các job của một lô **chạy chồng nhau**.
                "deferred_start": True,
            }
            if path_sources:
                first_path = path_sources[0]
                options["input_dir"] = first_path["local"]
                options["input_local"] = first_path["local"]
                options["input_shared_key"] = first_path["key"]
                options["input_shared_rel"] = first_path["rel"]
            job_mod.set_job_options(job_id, **options)

            watcher_spawned = False
            retry_host = False
            try:
                # --- ensure toolchain nếu thiếu -----------------------------
                # CHỈ cài tool ghi rõ bằng ``--need`` (``needs``). Tool **suy
                # ra** từ lệnh KHÔNG được cài: suy ra có thể sai ⇒ cài theo
                # suy ra là rủi ro (S38).
                ensured = _ensure_tools(host, choice.profile, needs)

                # --- chuẩn bị input: HOÃN cho watcher (B36/C10) --------------
                # ``do`` **không** đẩy input và **không** launch: watcher tự
                # làm (đúng đường dẫn của nhánh "job xếp hàng"). Lý do (B36 —
                # đo thật 23/11/2026, `docs/evidence/final-20261123/s18-submit-latency.txt`):
                # mỗi vòng lặp ``--each`` làm ~7 round-trip ssh (đọc marker +
                # rsync + chmod + ghi marker + mkdir ×2 + launch) ⇒ **~21–26 s
                # mỗi item** ⇒ các job của lô **không bao giờ chạy chồng nhau**
                # ⇒ fleet không tính toán song song. Hoãn việc đẩy input cho
                # watcher ⇒ mỗi watcher tự đẩy + launch **song song** ⇒ lô
                # chạy chồng nhau, và ``do`` trả JSON **ngay**.
                #
                # Hệ quả (đánh đổi, nói rõ): lỗi **đẩy input** (host chết,
                # URL hỏng, file local biến mất) **không** còn lộ ra ở ``do``
                # mà lộ ra ở **watcher** ⇒ ``do`` trả exit 0, còn ``roff wait``
                # mới báo lỗi (reason rõ ràng trong ledger). Việc **failover**
                # sang host khác vẫn còn nguyên (watcher tự failover).
                job_env: dict[str, str] = {}
                options["input_kind"] = (
                    input_specs[0]["kind"] if input_specs else "path"
                )

                # item của --each: đường dẫn trong kho dùng chung (ROFF_ITEM)
                # ⇒ watcher tự tính lại (và tự đẩy) lúc launch.
                if spec.get("item"):
                    options["item_local"] = os.path.abspath(str(spec["item"]))
                    options["item_inside_input"] = False

                job_env["ROFF_IN_DIR"] = workdir
                if checkpoint_remote:
                    job_env["ROFF_CHECKPOINT"] = checkpoint_remote
                job_mod.set_job_options(job_id, **options)

                # Env phải sống sót qua failover: watcher đọc lại file env cục bộ
                # khi launch lại. Chỉ giữ biến **không** phụ thuộc host (secret của
                # người dùng + ROFF_URL/ROFF_SOURCE_JOB); $ROFF_IN/$ROFF_IN_DIR phụ
                # thuộc host nên watcher phải tính lại (xem báo cáo).
                local_env = {
                    key: value
                    for key, value in job_env.items()
                    if key in ("ROFF_URL", "ROFF_SOURCE_JOB")
                }
                local_env.update(env)
                job_mod.save_job_env(job_id, local_env)

                if not queued:
                    # Đã **chiếm slot** ⇒ job được coi là ``launched`` (hợp
                    # đồng §1.2 của `docs/SPEC-CLI.md`). Việc launch **thật**
                    # do watcher lo (xem ``deferred_start`` ở trên) — state này
                    # có nghĩa "đã nhận slot, watcher sẽ khởi chạy", chưa phải
                    # "process trên host đã chạy".
                    job.state = "launched"
                    job_mod.save_job(job)

                if _watcher_will_run():
                    # --- spawn watcher ----------------------------------------
                    # Job ở state ``queued`` cũng cần watcher: watcher sẽ xin
                    # slot, chuẩn bị input rồi launch.
                    watcher_spawned = _spawn_watcher(job_id)
                if not watcher_spawned and not queued:
                    # --- đường dẫn NỘI TUYẾN ------------------------------------
                    # Không có watcher nào sẽ khởi chạy job (chế độ
                    # ``ROFF_NO_WATCHER=1`` — chỉ dùng cho test) ⇒ **không ai**
                    # đẩy input + launch ⇒ `do` phải tự làm, và phải **báo lỗi
                    # ngay** (đúng như trước B36). Đường dẫn mặc định (có
                    # watcher) là đường dẫn **hoãn** ở trên.
                    #
                    # Job **xếp hàng** (chưa có slot) thì KHÔNG launch ở đây —
                    # watcher sẽ tự xin slot rồi launch (như trước).
                    try:
                        watcher_mod.start_now(job_id)
                    except RoffError as exc:
                        if (
                            exc.code == EXIT_UNREACHABLE
                            and attempt + 1 < attempts
                            and _is_retryable_start(exc)
                        ):
                            # Host chết lúc **chuẩn bị input** ⇒ thử host khác
                            # (W3c). Lỗi **launch** thì không retry (host còn
                            # sống — chỉ job hỏng).
                            retry_host = True
                            raise
                        reason = str(
                            job_mod.job_options(job_id).get("start_error") or ""
                        ) or "launch_failed"
                        _mark_job_lost(job_id, reason)
                        raise

                _audit_do(cmd, env, job_id, host, project)

                job_ids.append(job_id)
                created_jobs.append({"job": job_id, "host": host})
                if not first_job:
                    first_job = job_id
                    first_host = host
                    first_reasons = list(reasons)
                used_hosts.add(host)
                return {
                    "job": job_id,
                    "host": host,
                    "item": spec.get("item"),
                    "shard": spec.get("shard"),
                    "ensured": ensured,
                    "state": "queued" if queued else "launched",
                    "host_reasons": list(reasons),
                    # Tool **suy ra** từ lệnh (minh bạch — S37). Rỗng ⇒ `[]`.
                    "inferred_needs": list(inferred_needs),
                }
            except Exception as exc:
                # Trả slot ĐÚNG lease đã chiếm + bỏ ticket khỏi hàng đợi.
                # Nếu không, lease rò ⇒ fleet bị khoá vĩnh viễn (mọi `roff do`
                # sau đó trả exit 10).
                _drop_ticket(ticket, lease)
                if job_id:
                    _release_slot(job_id)
                # Host chết lúc chuẩn bị input ⇒ thử host khác (W3c).
                if retry_host:
                    tried.add(host)
                    continue
                if job_id and not watcher_spawned:
                    # Job đã tạo nhưng watcher **chưa** chạy ⇒ không ai theo dõi
                    # ⇒ đánh dấu ``lost`` để không bỏ rơi job (W2d).
                    reason = "launch_failed"
                    if isinstance(exc, RoffError) and exc.reason == "setup_failed":
                        reason = "setup_failed"
                    _mark_job_lost(job_id, reason)
                    created_jobs.append({"job": job_id, "host": host})
                if isinstance(exc, RoffError):
                    # Agent cần biết job nào đã có để vẫn ``roff wait`` được.
                    exc.fields["jobs"] = list(created_jobs)
                raise

    for index, spec in enumerate(specs):
        results.append(_prepare_spec(index, spec))
        host_reasons = list(results[-1].get("host_reasons") or host_reasons)

    payload = _ok(
        job=first_job,
        state=job_mod.load_job(first_job).state if first_job else "queued",
        host=first_host,
        host_reasons=first_reasons or host_reasons,
        ledger=job_mod.job_path(first_job) if first_job else None,
        next=[f"roff wait {first_job} --timeout {timeout}"] if first_job else [],
        # Tool **suy ra** từ lệnh (minh bạch — S37): `[]` khi không suy ra
        # được (vd `echo hi`).
        inferred_needs=list(inferred_needs),
    )
    # B27: báo "fleet đang mở" ngay trong JSON của `roff do` (máy đọc được).
    payload.update(_fleet_flags(cfg))
    if len(job_ids) > 1:
        payload["jobs"] = job_ids
        payload["count"] = len(job_ids)
    ensured_all = [item["ensured"] for item in results if item["ensured"]]
    if ensured_all:
        payload["ensured"] = ensured_all
    return payload, EXIT_OK


def _spawn_watcher(job_id: str) -> bool:
    """Spawn watcher nền (trừ khi ``ROFF_NO_WATCHER=1`` — chỉ dùng cho test).

    Trả ``True`` nếu watcher **đã** được spawn. ``False`` ⇒ **không có ai**
    sẽ khởi chạy job ⇒ ``roff do`` phải tự làm (đường dẫn nội tuyến) — nếu
    không, job sẽ được tạo mà **không bao giờ chạy** (B36/C10).
    """
    if os.environ.get("ROFF_NO_WATCHER") == "1":
        return False
    try:
        watcher_mod.spawn_watcher(job_id)
    except Exception as exc:  # pragma: no cover - fork/exec hỏng
        # Không có watcher ⇒ không ai đẩy input + launch ⇒ `do` tự làm.
        try:
            watcher_mod.record_event(
                job_id, "watcher_spawn_failed", detail=str(exc)
            )
        except Exception:  # pragma: no cover - ledger hỏng
            pass
        return False
    return True


def _is_retryable_start(exc: RoffError) -> bool:
    """Lỗi khởi chạy job này có **đáng thử host khác** không?

    Chỉ lỗi **chuẩn bị input** (host chết lúc rsync/clone) mới đáng thử
    host khác (W3c). Lỗi **launch** (host còn sống, chỉ job hỏng) thì không.
    """
    reason = str(exc.reason or "")
    return reason in ("input_push_failed", "host_unreachable", "shared_push_failed")


def _watcher_will_run() -> bool:
    """Có watcher nào sẽ khởi chạy job không?

    Chế độ ``ROFF_NO_WATCHER=1`` (chỉ dùng cho test) ⇒ **không có watcher**
    ⇒ không ai đẩy input + launch ⇒ ``roff do`` phải tự làm (đường dẫn
    **nội tuyến**), nếu không job sẽ được tạo mà **không bao giờ chạy**.
    """
    return os.environ.get("ROFF_NO_WATCHER") != "1"


def _prepare_job_input(
    host: str,
    source_job: job_mod.Job | None,
    workdir: str,
    job_env: dict[str, str],
) -> str:
    """Chuẩn bị input lấy từ job khác (``--in job:<id>``).

    Cùng host ⇒ dùng thẳng thư mục output trên host (không truyền gì).
    Khác host ⇒ stream host→host **qua pipe ở local, không ghi đĩa local**.
    """
    remote = inputs_mod.prepare_job_input(host, source_job, workdir)
    if source_job is not None:
        job_env["ROFF_SOURCE_JOB"] = str(source_job.id)
    return remote


def _wait_dependency(job_id: str, timeout: int) -> None:
    """Chờ job phụ thuộc (``--after job:X``) xong; fail ⇒ ném lỗi tương ứng."""
    try:
        job = watcher_mod.wait_for(job_id, timeout=float(timeout))
    except RoffError as exc:
        if exc.code == EXIT_TIMEOUT:
            raise RoffError(
                EXIT_TIMEOUT,
                "timeout",
                error=f"job phụ thuộc {job_id} chưa xong sau {timeout}s",
                job=job_id,
                state="running",
            ) from None
        raise
    if job.state == "exited" and (job.exit_code or 0) != 0:
        raise RoffError(
            EXIT_JOB_FAILED,
            "job_failed",
            error=f"job phụ thuộc {job_id} đã fail (exit {job.exit_code})",
            job=job_id,
            exit=job.exit_code,
        )
    if job.state == "lost":
        raise RoffError(
            EXIT_JOB_LOST,
            job.reason or "host_restarted",
            error=f"job phụ thuộc {job_id} bị mất ({job.reason})",
            job=job_id,
        )


# --------------------------------------------------------------------------
# lệnh: status / result / wait / jobs
# --------------------------------------------------------------------------
def _pull_info(options: dict) -> dict:
    """Trạng thái pull của job từ khối options của ledger (B23).

    Trả ``{"pulled": bool, "pull_error": str, "pull_failed": bool}``.

    ``pull_failed`` = **ĐÃ** thử pull và **hỏng** ⇒ artifact chưa về local.

    Phân biệt 3 trạng thái (quan trọng — job cũ không có khoá ``pulled``):

    * không có khoá ``pulled`` (job cũ / watcher chưa chạy) ⇒ **không** biết
      ⇒ coi như không hỏng (giữ hành vi cũ: ``roff wait`` trả 0);
    * ``pulled`` = ``true`` ⇒ kéo xong;
    * ``pulled`` = ``false`` (hoặc ``pull_error`` khác rỗng) ⇒ pull **hỏng**
      ⇒ ``roff wait`` phải báo lỗi (exit 74), **không** phải báo thành công.
    """
    raw = options.get("pulled")
    pull_error = str(options.get("pull_error") or "")
    return {
        "pulled": bool(raw),
        "pull_error": pull_error,
        "pull_failed": raw is False or bool(pull_error),
    }


def _sync_back_info(options: dict) -> dict:
    """Trạng thái ``--sync-back`` của job từ ledger (B26).

    Trả ``{"sync_back_error": str, "sync_back_failed": bool}``.

    ``sync_back_error`` khác rỗng ⇒ có path ``--sync-back`` **không về** local
    ⇒ ``roff wait``/``roff result`` phải báo lỗi (exit 74, cùng họ với
    ``pull_failed``) — **không** được báo thành công rồi để agent đọc file
    rỗng (bài học của B23).
    """
    error = str(options.get("sync_back_error") or "")
    return {"sync_back_error": error, "sync_back_failed": bool(error)}


def _pull_sync_back_paths(
    job: job_mod.Job, options: dict, fallback_dir: str
) -> tuple[list[dict], str]:
    """Kéo lại các path ``--sync-back`` (B26).

    Mỗi cặp ``(sync_back_remote[i], sync_back_local[i])`` được kéo về **đúng**
    đường dẫn local của nó. Job cũ (chưa có ``sync_back_local``) ⇒ kéo vào
    thư mục ``fallback_dir`` (thư mục ``--local``) như trước.

    Trả ``(manifest, error)`` — ``error`` khác rỗng ⇒ có path **không** kéo
    về được (KHÔNG nuốt lỗi).
    """
    remotes = list(options.get("sync_back_remote") or [])
    locals_ = list(options.get("sync_back_local") or [])
    files: list[dict] = []
    errors: list[str] = []
    for index, remote in enumerate(remotes):
        text = str(remote or "")
        if not text:
            continue
        local = str(locals_[index]) if index < len(locals_) else ""
        if not local:
            local = fallback_dir
        try:
            parent = os.path.dirname(local)
            if parent:
                os.makedirs(parent, exist_ok=True)
            extra = transfer_mod.pull_path(job.host, text, local)
        except Exception as exc:
            errors.append(f"{local}: {exc}")
            continue
        if extra is None:
            errors.append(f"{local}: pull_path không trả về kết quả")
            continue
        if getattr(extra, "ok", False):
            files.extend(list(getattr(extra, "files", []) or []))
        else:
            errors.append(
                f"{local}: {getattr(extra, 'error', '') or 'pull thất bại'}"
            )
    return files, "; ".join(errors)


def _job_summary(job: job_mod.Job) -> dict:
    """Tóm tắt 1 job cho JSON."""
    return {
        "job": job.id,
        "state": job.state,
        "exit": job.exit_code,
        "host": job.host,
        "project": job.project,
        "created_at": job.created_at,
    }


def _cmd_status(args: argparse.Namespace) -> tuple[dict, int]:
    """``roff status <job_id>`` — đọc ledger 1 lần (exit 2 nếu không có job)."""
    job = job_mod.load_job(args.job_id)
    options = job_mod.job_options(job.id)
    info = _pull_info(options)
    sync_info = _sync_back_info(options)
    payload = _ok(
        job=job.id,
        state=job.state,
        exit=job.exit_code,
        host=job.host,
        project=job.project,
        created_at=job.created_at,
        heartbeat=job.heartbeat or None,
        reason=job.reason,
        needs_retry=job.needs_retry,
        pulled=info["pulled"],
        pull_error=info["pull_error"] or None,
        sync_back_error=sync_info["sync_back_error"] or None,
        ledger=job_mod.job_path(job.id),
    )
    if options.get("host_reasons"):
        payload["host_reasons"] = list(options["host_reasons"])
    return payload, EXIT_OK


def _cmd_result(args: argparse.Namespace) -> tuple[dict, int]:
    """``roff result <job_id>`` — JSON đầy đủ của job."""
    job = job_mod.load_job(args.job_id)
    options = job_mod.job_options(job.id)
    artifacts = list(options.get("manifest") or [])
    events = list(options.get("events") or [])
    info = _pull_info(options)
    sync_info = _sync_back_info(options)
    finished = None
    for event in events:
        if event.get("event") == "finished":
            finished = event.get("ts")
    attempts = []
    for attempt in job.attempts or []:
        attempts.append(
            {
                "host": attempt.get("host"),
                "pid": attempt.get("pid"),
                "boot_id": attempt.get("boot_id"),
                "instance_token": attempt.get("instance_token"),
                "started": attempt.get("started"),
                "failover": bool(attempt.get("failover")),
            }
        )
    payload = _ok(
        job=job.id,
        state=job.state,
        exit=job.exit_code,
        host=job.host,
        project=job.project,
        cmd=job.cmd,
        created_at=job.created_at,
        finished_at=finished,
        attempts=attempts,
        artifacts=artifacts,
        out_local=job.out_local,
        out_remote=job.out_remote,
        reason=job.reason,
        needs_retry=job.needs_retry,
        pulled=info["pulled"],
        pull_error=info["pull_error"] or None,
        sync_back_error=sync_info["sync_back_error"] or None,
        host_reasons=list(options.get("host_reasons") or []),
        events=events[-20:],
        ledger=job_mod.job_path(job.id),
    )
    code = EXIT_OK
    if job.state == "exited" and (job.exit_code or 0) != 0:
        code = EXIT_JOB_FAILED
    elif job.state == "lost":
        code = EXIT_JOB_LOST
    elif job.state == "cancelled":
        code = EXIT_CANCELLED
    elif job.state == "exited" and info["pull_failed"]:
        # Job **remote** chạy xong (exit 0) nhưng artifact chưa về local ⇒
        # `roff result` cũng phải nói ra, không được báo "thành công" (B23).
        code = EXIT_PULL_FAILED
        payload["ok"] = False
        payload["exit_code"] = EXIT_PULL_FAILED
        payload["reason"] = REASON_PULL_FAILED
        payload["error"] = (
            "job chạy xong nhưng artifact chưa về local — kéo lại bằng "
            "`roff pull <job>` (KHÔNG cần chạy lại job)"
        )
    elif job.state == "exited" and sync_info["sync_back_failed"]:
        # Job **remote** chạy xong (exit 0) nhưng path `--sync-back` chưa về
        # local ⇒ cũng phải nói ra (B26). `pulled` giữ nguyên: artifact chính
        # đã về thì đã về.
        code = EXIT_PULL_FAILED
        payload["ok"] = False
        payload["exit_code"] = EXIT_PULL_FAILED
        payload["reason"] = REASON_SYNC_BACK_FAILED
        payload["error"] = (
            "job chạy xong nhưng path --sync-back chưa về local — kéo lại bằng "
            "`roff pull <job>` (KHÔNG cần chạy lại job)"
        )
    return payload, code


def _cmd_wait(args: argparse.Namespace) -> tuple[dict, int]:
    """``roff wait <job_id> [--timeout S]`` — poll **ledger cục bộ**.

    Exit 124 (hết giờ) **không phải lỗi**: ``ok: true``, ``state: "running"``.
    """
    timeout = _int_option(getattr(args, "timeout", DEFAULT_TIMEOUT), "--timeout")
    if timeout <= 0:
        raise RoffError(
            EXIT_INPUT,
            "invalid_timeout",
            error=f"--timeout phải > 0 (nhận {timeout})",
        )
    try:
        job = watcher_mod.wait_for(args.job_id, timeout=float(timeout))
    except RoffError as exc:
        if exc.code == EXIT_TIMEOUT:
            # Hết giờ **không phải lỗi**: job vẫn đang chờ (queued) hoặc đang
            # chạy (running) — báo đúng trạng thái thật của job.
            state = str(exc.fields.get("state") or "running")
            return (
                _ok(
                    EXIT_TIMEOUT,
                    job=args.job_id,
                    state=state,
                    reason="timeout",
                ),
                EXIT_TIMEOUT,
            )
        raise

    state = job.state
    if state == "exited":
        code = int(job.exit_code or 0)
        if code == 0:
            opts = job_mod.job_options(job.id)
            manifest = list(opts.get("manifest") or [])
            info = _pull_info(opts)
            sync_info = _sync_back_info(opts)
            if info["pull_failed"]:
                # Job **remote** chạy xong tốt (exit 0) nhưng artifact **chưa
                # về** local ⇒ KHÔNG được báo thành công, nếu không agent đọc
                # thư mục --out rỗng mà không có cảnh báo nào (B23).
                # Exit 74 (EXIT_PULL_FAILED) ⇒ agent kéo lại bằng `roff pull`,
                # **không** chạy lại job.
                detail = info["pull_error"] or "artifact chưa kéo về local"
                return (
                    _not_ok(
                        EXIT_PULL_FAILED,
                        job=job.id,
                        state=state,
                        exit=0,
                        host=job.host,
                        reason=REASON_PULL_FAILED,
                        error=(
                            "job chạy xong nhưng artifact chưa về local — kéo lại "
                            "bằng `roff pull <job>` (KHÔNG cần chạy lại job)"
                        ),
                        detail=detail,
                        pulled=False,
                        pull_error=detail,
                        artifacts=manifest,
                    ),
                    EXIT_PULL_FAILED,
                )
            if sync_info["sync_back_failed"]:
                # Job **remote** chạy xong tốt (exit 0) nhưng path
                # `--sync-back` **chưa về** local ⇒ KHÔNG được báo thành công
                # (B26) — cùng họ exit 74 với `pull_failed`: kéo lại bằng
                # `roff pull`, **không** chạy lại job. `pulled` giữ nguyên
                # (artifact chính đã về thì đã về).
                detail = sync_info["sync_back_error"]
                return (
                    _not_ok(
                        EXIT_PULL_FAILED,
                        job=job.id,
                        state=state,
                        exit=0,
                        host=job.host,
                        reason=REASON_SYNC_BACK_FAILED,
                        error=(
                            "job chạy xong nhưng path --sync-back chưa về local — "
                            "kéo lại bằng `roff pull <job>` (KHÔNG cần chạy lại job)"
                        ),
                        detail=detail,
                        pulled=info["pulled"],
                        sync_back_error=detail,
                        artifacts=manifest,
                    ),
                    EXIT_PULL_FAILED,
                )
            return (
                _ok(
                    EXIT_OK,
                    job=job.id,
                    state=state,
                    exit=0,
                    host=job.host,
                    artifacts=manifest,
                    pulled=info["pulled"],
                    pull_error=info["pull_error"] or None,
                ),
                EXIT_OK,
            )
        return (
            _ok(
                EXIT_JOB_FAILED,
                job=job.id,
                state=state,
                exit=code,
                host=job.host,
                reason="job_failed",
            ),
            EXIT_JOB_FAILED,
        )
    if state == "lost":
        return (
            _not_ok(
                EXIT_JOB_LOST,
                job=job.id,
                state=state,
                host=job.host,
                reason=job.reason or "host_restarted",
                error=f"job bị mất: {job.reason or 'host_restarted'}",
            ),
            EXIT_JOB_LOST,
        )
    if state == "cancelled":
        return (
            _not_ok(
                EXIT_CANCELLED,
                job=job.id,
                state=state,
                exit=EXIT_CANCELLED,
                host=job.host,
                reason="cancelled",
            ),
            EXIT_CANCELLED,
        )
    if state == job_mod.UNKNOWN_STATE:
        # B13: job định dạng cũ (ledger không có khoá ``state``) ⇒ KHÔNG biết
        # kết quả. Trả **ngay** (không treo tới hết timeout) với lý do rõ.
        return (
            _not_ok(
                EXIT_JOB_FAILED,
                job=job.id,
                state=state,
                host=job.host,
                reason="unknown_state",
                error=(
                    "job ở định dạng cũ (ledger không có khoá 'state') — không biết "
                    "kết quả; dọn bằng 'roff gc'"
                ),
            ),
            EXIT_JOB_FAILED,
        )
    return (
        _ok(EXIT_OK, job=job.id, state=state, host=job.host),
        EXIT_OK,
    )


def _cmd_jobs(args: argparse.Namespace) -> tuple[dict, int]:
    """``roff jobs [--state S] [--host H]`` — danh sách job (mới nhất trước)."""
    jobs = job_mod.list_jobs(state=getattr(args, "state", None))
    host_filter = getattr(args, "host", None)
    if host_filter:
        jobs = [job for job in jobs if job.host == host_filter]
    jobs.sort(key=lambda item: item.created_at, reverse=True)
    return (
        _ok(jobs=[_job_summary(job) for job in jobs], count=len(jobs)),
        EXIT_OK,
    )


# --------------------------------------------------------------------------
# lệnh: logs / cancel
# --------------------------------------------------------------------------
def _cmd_logs(args: argparse.Namespace) -> tuple[dict, int]:
    """``roff logs <job_id> [--tail N] [--grep PATTERN]`` — in **text** ra stdout.

    ⚠ Lệnh **duy nhất** không in JSON: stdout là log dạng text (mặc định 200
    dòng cuối). Khi lệnh **lỗi** (vd không có job) thì in JSON lỗi ra stdout
    theo hợp đồng chung, kèm exit code tương ứng.
    """
    job = job_mod.load_job(args.job_id)
    tail = getattr(args, "tail", None)
    tail = DEFAULT_LOG_TAIL if tail is None else _int_option(tail, "--tail", minimum=1)
    text = job_mod.read_log(job.host, job, tail=tail, grep=getattr(args, "grep", None))
    if text:
        sys.stdout.write(text if text.endswith("\n") else text + "\n")
    else:
        # stdout chỉ chứa text của log ⇒ chẩn đoán đi stderr.
        sys.stderr.write(
            f"roff logs: log của job {job.id} trên {job.host} rỗng "
            "(job chưa chạy, hoặc không với được host)\n"
        )
    return {}, EXIT_OK


def _cmd_cancel(args: argparse.Namespace) -> tuple[dict, int]:
    """``roff cancel <job_id>`` — kill **process group** theo pid đã ghi nhận."""
    job = job_mod.load_job(args.job_id)
    if job.state in job_mod.TERMINAL_STATES:
        # Job đã kết thúc ⇒ **không** ghi đè trạng thái thật (bug cũ: cancel
        # 1 job đã ``exited`` làm nó thành ``cancelled`` ⇒ mất kết quả).
        if job.state == "cancelled":
            return (
                _not_ok(
                    EXIT_CANCELLED,
                    job=job.id,
                    state="cancelled",
                    exit=EXIT_CANCELLED,
                    host=job.host,
                    reason="cancelled",
                    error="job đã ở trạng thái cancelled",
                ),
                EXIT_CANCELLED,
            )
        return (
            _not_ok(
                EXIT_OK,
                job=job.id,
                state=job.state,
                exit=job.exit_code,
                host=job.host,
                reason=job.state,
                error=f"job đã ở trạng thái {job.state} — không đổi",
            ),
            EXIT_OK,
        )
    if job.state == "queued":
        # Job chưa chạy (đang xếp hàng chờ slot) ⇒ không có process để kill:
        # bỏ ticket khỏi hàng đợi + đánh dấu cancelled. Watcher thấy trạng
        # thái terminal thì tự thoát.
        job.state = "cancelled"
        job.exit_code = EXIT_CANCELLED
        job_mod.save_job(job)
        _release_slot(job.id)
        return (
            _ok(
                EXIT_OK,
                job=job.id,
                state="cancelled",
                exit=EXIT_CANCELLED,
                host=job.host,
                killed=False,
                pid=None,
            ),
            EXIT_OK,
        )
    result = job_mod.cancel(job.host, job)
    if not result.get("ok"):
        raise RoffError(
            EXIT_UNREACHABLE,
            "cancel_failed",
            error=f"không huỷ được job {job.id} trên {job.host}",
            detail=result.get("detail") or None,
            job=job.id,
            host=job.host,
        )
    _release_slot(job.id)
    return (
        _ok(
            EXIT_OK,
            job=job.id,
            state="cancelled",
            exit=EXIT_CANCELLED,
            host=job.host,
            killed=True,
            pid=result.get("pid"),
        ),
        EXIT_OK,
    )


# --------------------------------------------------------------------------
# lệnh: hosts / probe
# --------------------------------------------------------------------------
def _cmd_hosts(args: argparse.Namespace) -> tuple[dict, int]:
    """``roff hosts [--parallel]`` — probe **mọi** host trong fleet.

    Mặc định **serial** (bridge một phiên chỉ nhận 1 phiên tại 1 thời điểm —
    bug R2); chỉ ``--parallel`` mới chạy song song.
    """
    cfg = _load_config()
    aliases = _fleet_aliases(cfg)
    profiles = probe_mod.probe_many(
        aliases, timeout=30, parallel=bool(getattr(args, "parallel", False))
    )
    payload = _ok(
        hosts=[profile.to_dict() for profile in profiles], count=len(profiles)
    )
    payload.update(_fleet_flags(cfg))
    flapping = probe_mod.detect_flapping(profiles)
    if flapping:
        payload["flapping"] = flapping
    return payload, EXIT_OK


def _cmd_probe(args: argparse.Namespace) -> tuple[dict, int]:
    """``roff probe <host>`` — JSON của 1 host (255 + ``detail`` nếu không với)."""
    profile = probe_mod.probe(args.host)
    if not profile.reachable:
        raise RoffError(
            EXIT_UNREACHABLE,
            "host_unreachable",
            error=f"không với được host {args.host}",
            detail=profile.detail or profile.error,
            host=args.host,
            reachable=False,
        )
    payload = _ok(**profile.to_dict())
    return payload, EXIT_OK


# --------------------------------------------------------------------------
# lệnh: push / pull
# --------------------------------------------------------------------------
def _cmd_push(args: argparse.Namespace) -> tuple[dict, int]:
    """``roff push <host> <local-dir> --name <project>`` — đẩy project lên host."""
    name = _safe_name(getattr(args, "name", None) or "", "")
    if not name:
        raise RoffError(
            EXIT_INPUT, "missing_name", error="--name là bắt buộc (tên project)"
        )
    profile = probe_mod.probe(args.host)
    if not profile.reachable:
        raise RoffError(
            EXIT_UNREACHABLE,
            "host_unreachable",
            error=f"không với được host {args.host}",
            detail=profile.detail or profile.error,
            host=args.host,
            reachable=False,
        )
    layout_root = _layout_root_of(profile)
    remote = f"{layout_root}/projects/{name}"
    result = transfer_mod.push(args.host, args.local_dir, remote)
    if not result.ok:
        raise RoffError(
            EXIT_UNREACHABLE,
            "push_failed",
            error=f"push thất bại lên {args.host}",
            detail=result.error,
            host=args.host,
            remote=remote,
        )
    return (
        _ok(
            host=args.host,
            remote=remote,
            bytes=result.bytes_transferred,
            files=result.files,
        ),
        EXIT_OK,
    )


def _cmd_pull(args: argparse.Namespace) -> tuple[dict, int]:
    """``roff pull <job_id> --local <dir>`` — kéo artifact + manifest sha256."""
    job = job_mod.load_job(args.job_id)
    local = getattr(args, "local", None)
    if not local:
        raise RoffError(
            EXIT_INPUT, "missing_local", error="--local là bắt buộc (thư mục đích)"
        )
    local = os.path.abspath(os.path.expanduser(str(local)))
    remote = str(job.out_remote or "")
    if not remote:
        raise RoffError(
            EXIT_INPUT,
            "invalid_job",
            error=f"job {job.id} chưa có thư mục output trên host",
            job=job.id,
        )
    result = transfer_mod.pull(job.host, remote, local)
    files = list(result.files)
    options = job_mod.job_options(job.id)
    # Path `--sync-back`: kéo về **đúng** đường dẫn local của từng path
    # (B26) — không phải vào thư mục `--local`.
    sync_files, sync_error = _pull_sync_back_paths(job, options, local)
    files.extend(sync_files)
    if sync_error:
        # KHÔNG nuốt lỗi (bài học B23) ⇒ `roff wait` vẫn báo lỗi (74).
        job_mod.set_job_options(job.id, sync_back_error=sync_error)
        watcher_mod.record_event(
            job.id, "sync_back_failed", detail=sync_error, host=job.host
        )
    else:
        # Kéo lại thành công ⇒ xoá lỗi cũ ⇒ `roff wait` trở lại exit 0.
        job_mod.set_job_options(job.id, sync_back_error="")
    if not result.ok:
        detail = str(result.error or "pull thất bại")
        # Ghi lại thất bại vào ledger ⇒ `roff wait`/`roff result` vẫn báo lỗi
        # (74) thay vì báo thành công, và agent biết phải kéo lại (B23).
        job_mod.set_job_options(job.id, pulled=False, pull_error=detail)
        watcher_mod.record_event(
            job.id, "pull_failed", detail=detail, host=job.host
        )
        raise RoffError(
            EXIT_UNREACHABLE,
            REASON_PULL_FAILED,
            error=f"pull thất bại từ {job.host}",
            detail=detail,
            job=job.id,
            host=job.host,
        )
    # Kéo lại thành công ⇒ `pulled=true` + manifest mới ⇒ `roff wait` trả 0
    # trở lại (job không còn lỗi pull nữa) — B23.
    job_mod.set_job_options(job.id, manifest=files, pulled=True, pull_error="")
    if sync_error:
        # Artifact đã về nhưng path `--sync-back` chưa về ⇒ báo lỗi rõ ràng
        # (exit 74, `sync_back_failed`) thay vì báo thành công (B26).
        raise RoffError(
            EXIT_PULL_FAILED,
            REASON_SYNC_BACK_FAILED,
            error=(
                "artifact đã về local nhưng path --sync-back chưa về — kéo lại "
                "bằng `roff pull <job>` (KHÔNG cần chạy lại job)"
            ),
            detail=sync_error,
            job=job.id,
            host=job.host,
            pulled=True,
            sync_back_error=sync_error,
            files=files,
        )
    if job.reason in (REASON_PULL_FAILED, REASON_SYNC_BACK_FAILED):
        job.reason = None
        job_mod.save_job(job)
    watcher_mod.record_event(
        job.id, "pulled", files=len(files), bytes=int(result.bytes_transferred or 0)
    )
    return (
        _ok(
            job=job.id,
            host=job.host,
            local=local,
            bytes=result.bytes_transferred,
            files=files,
            pulled=True,
        ),
        EXIT_OK,
    )


# --------------------------------------------------------------------------
# lệnh: ensure
# --------------------------------------------------------------------------
def _cmd_ensure(args: argparse.Namespace) -> tuple[dict, int]:
    """``roff ensure <host> --setup <path> --name <name>`` — cài toolchain idempotent."""
    name = _safe_name(getattr(args, "name", None) or "", "")
    if not name:
        raise RoffError(
            EXIT_INPUT, "missing_name", error="--name là bắt buộc (tên toolchain)"
        )
    setup = getattr(args, "setup", None)
    if not setup:
        raise RoffError(
            EXIT_INPUT, "missing_setup", error="--setup là bắt buộc (script setup)"
        )
    setup = os.path.abspath(os.path.expanduser(str(setup)))
    if not os.path.isfile(setup):
        raise RoffError(
            EXIT_INPUT,
            "setup_not_found",
            error=f"không có script setup: {setup}",
            setup=str(setup),
        )
    profile = probe_mod.probe(args.host)
    if not profile.reachable:
        raise RoffError(
            EXIT_UNREACHABLE,
            "host_unreachable",
            error=f"không với được host {args.host}",
            detail=profile.detail or profile.error,
            host=args.host,
            reachable=False,
        )
    result = _run_setup(args.host, profile, setup, name)
    payload = _ok(
        host=result["host"],
        name=result["name"],
        ensured=result["ensured"],
        skipped=result["skipped"],
        detail=result["detail"],
    )
    return payload, EXIT_OK


# --------------------------------------------------------------------------
# lệnh: tunnel
# --------------------------------------------------------------------------
def _doctor_module() -> Any:
    """Module ``roffkit.doctor`` — import lười (như tunnel)."""
    try:
        from . import doctor as doctor_mod  # type: ignore
    except ImportError:
        raise RoffError(
            EXIT_INPUT,
            "doctor_unavailable",
            error="module roffkit.doctor chưa có trong bản này",
        ) from None
    return doctor_mod


def _cmd_doctor(args: argparse.Namespace) -> tuple[dict, int]:
    """``roff doctor [--probe]`` — chẩn đoán sức khoẻ hệ thống (chỉ **báo**).

    Mặc định KHÔNG probe mạng (đọc cache ``probes.json``) ⇒ chạy trong < 1 s;
    ``--probe`` mới probe thật host.
    """
    doctor_mod = _doctor_module()
    payload = doctor_mod.run_checks(
        probe_now=bool(getattr(args, "probe", False)),
        state_dir=state_dir(),
    )
    return payload, int(payload.get("exit_code") or EXIT_OK)


def _tunnel_module() -> Any:
    """Module ``roffkit.tunnel`` (phần D sở hữu) — import lười."""
    try:
        from . import tunnel as tunnel_mod  # type: ignore
    except ImportError:
        raise RoffError(
            EXIT_INPUT,
            "tunnel_unavailable",
            error=(
                "module roffkit.tunnel chưa có trong bản này — "
                "tính năng tunnel chưa sẵn sàng"
            ),
        ) from None
    return tunnel_mod


def _remote_listening_of(tunnel_mod: Any, host: str, rport: int, tunnel: Any) -> bool | None:
    """``remote_listening`` của 1 tunnel: ``True`` / ``False`` / ``None`` (không biết).

    Ưu tiên thuộc tính công khai ``Tunnel.remote_listening``; nếu module
    ``tunnel`` chưa có thuộc tính đó thì dùng helper dò LISTEN trên host
    (null = **không kiểm tra được**, khác hẳn ``False``).
    """
    value = getattr(tunnel, "remote_listening", None)
    if isinstance(value, bool):
        return value
    probe = getattr(tunnel_mod, "_remote_listening", None)
    if callable(probe):
        try:
            result = probe(host, rport)
        except Exception:  # pragma: no cover - lỗi mạng không được làm hỏng CLI
            return None
        return result if isinstance(result, bool) else None
    return None


def _cmd_tunnel(args: argparse.Namespace) -> tuple[dict, int]:
    """``roff tunnel start|stop|list``."""
    tunnel_mod = _tunnel_module()
    action = getattr(args, "tunnel_action", None)
    if action == "start":
        host = getattr(args, "host", None)
        lport = _int_option(getattr(args, "lport", None) or 0, "--lport", minimum=1)
        rport = _int_option(getattr(args, "rport", None) or 0, "--rport", minimum=1)
        tunnel = tunnel_mod.tunnel_start(host, lport, rport)
        return (
            _ok(
                alias=getattr(tunnel, "alias", host),
                lport=getattr(tunnel, "lport", lport),
                rport=getattr(tunnel, "rport", rport),
                remote_listening=_remote_listening_of(tunnel_mod, host, rport, tunnel),
                master_pid=getattr(tunnel, "master_pid", None),
                note=getattr(tunnel, "note", None),
            ),
            EXIT_OK,
        )
    if action == "link":
        from_alias = getattr(args, "from_alias", None)
        to_alias = getattr(args, "to_alias", None)
        rport = _int_option(getattr(args, "rport", None) or 0, "--rport", minimum=1)
        via_raw = getattr(args, "via_lport", None)
        via = _int_option(via_raw, "--via-lport", minimum=1) if via_raw else None
        to_raw = getattr(args, "to_port", None)
        to_port = _int_option(to_raw, "--to-port", minimum=1) if to_raw else None
        result = tunnel_mod.tunnel_link(from_alias, to_alias, rport, via, to_port)
        return (
            {
                "ok": True,
                "exit_code": EXIT_OK,
                "from": result.get("from_alias"),
                "to": result.get("to_alias"),
                "rport": result.get("rport"),
                "to_port": result.get("to_port"),
                "via_lport": result.get("via_lport"),
                "a_listening": result.get("a_listening"),
                "b_listening": result.get("b_listening"),
                "note": result.get("note"),
            },
            EXIT_OK,
        )
    if action == "stop":
        from_alias = getattr(args, "from_alias", None)
        if from_alias:
            # Dọn link phối hợp: `roff tunnel stop --from A --rport P`
            rport = _int_option(getattr(args, "rport", None) or 0, "--rport", minimum=1)
            result = tunnel_mod.tunnel_stop_link(from_alias, rport)
            payload = _ok(**{"from": from_alias, "rport": rport})
            if isinstance(result, dict):
                for key, value in result.items():
                    if key in payload or key in ("from_alias", "to_alias"):
                        continue
                    if value is None:
                        continue
                    payload[key] = value
            return payload, EXIT_OK
        host = getattr(args, "host", None)
        lport = _int_option(getattr(args, "lport", None) or 0, "--lport", minimum=1)
        result = tunnel_mod.tunnel_stop(host, lport)
        payload = _ok(host=host, lport=lport)
        if isinstance(result, dict):
            for key, value in result.items():
                if key not in payload and value is not None:
                    payload[key] = value
        return payload, EXIT_OK
    if action == "list":
        tunnels = tunnel_mod.tunnel_list()
        links = []
        lister = getattr(tunnel_mod, "link_list", None)
        if callable(lister):
            try:
                links = list(lister() or [])
            except Exception:  # pragma: no cover - lỗi đọc state không làm hỏng lệnh
                links = []
        return (
            _ok(
                tunnels=[
                    {
                        "alias": getattr(item, "alias", None),
                        "lport": getattr(item, "lport", None),
                        "rport": getattr(item, "rport", None),
                        "master_pid": getattr(item, "master_pid", None),
                        "note": getattr(item, "note", None),
                    }
                    for item in tunnels or []
                ],
                links=links,
                count=len(tunnels or []) + len(links),
            ),
            EXIT_OK,
        )
    raise RoffError(
        EXIT_INPUT,
        "invalid_tunnel_action",
        error="tunnel cần start|stop|list",
    )


# --------------------------------------------------------------------------
# lệnh: gc
# --------------------------------------------------------------------------
def _store_keys_in_use() -> set[str]:
    """Key kho input mà job **đang chạy / xếp hàng** còn dùng (W3g).

    Job đã kết thúc (terminal) không giữ kho ⇒ kho của nó có thể dọn.
    """
    keys: set[str] = set()
    try:
        jobs = job_mod.list_jobs()
    except Exception:  # pragma: no cover - ledger hỏng
        return keys
    for job in jobs:
        if job.state in job_mod.TERMINAL_STATES:
            continue
        try:
            options = job_mod.job_options(job.id)
        except Exception:  # pragma: no cover
            continue
        for name in ("input_shared_key", "item_shared_key"):
            key = str(options.get(name) or "")
            if key:
                keys.add(key)
        for source in options.get("input_sources") or []:
            if isinstance(source, dict):
                key = str(source.get("key") or "")
                if key:
                    keys.add(key)
    return keys


def _gc_host_stores(
    keep_keys: set[str], max_age_s: float | None = None
) -> list[dict[str, Any]]:
    """Dọn kho input trên **mọi host** của fleet (W3g).

    An toàn: không bao giờ xoá kho mà job đang chạy/xếp hàng dùng; kho còn
    lock (đang đẩy) hoặc chưa có marker cũng được giữ.
    """
    out: list[dict[str, Any]] = []
    try:
        cfg = _load_config()
        aliases = _fleet_aliases(cfg)
        profiles = probe_mod.cached_profiles(aliases, cfg)
    except Exception as exc:  # pragma: no cover - probe lỗi
        return [{"host": "", "error": str(exc), "removed": [], "freed_mb": 0.0}]
    age = shared_mod.DEFAULT_STORE_MAX_AGE_S if max_age_s is None else float(max_age_s)
    for profile in profiles:
        alias = str(getattr(profile, "alias", "") or "")
        layout_root = str(getattr(profile, "layout_root", "") or "")
        if not alias or not layout_root:
            continue
        if not getattr(profile, "reachable", False):
            out.append(
                {
                    "host": alias,
                    "removed": [],
                    "removed_locks": [],
                    "skipped": [],
                    "skipped_locks": [],
                    "freed_mb": 0.0,
                    "error": str(getattr(profile, "error", "") or "host unreachable"),
                }
            )
            continue
        try:
            result = shared_mod.gc_shared_store(
                alias, layout_root, keep_keys=keep_keys, max_age_s=age
            )
        except Exception as exc:
            out.append(
                {
                    "host": alias,
                    "removed": [],
                    "removed_locks": [],
                    "skipped": [],
                    "skipped_locks": [],
                    "freed_mb": 0.0,
                    "error": str(exc),
                }
            )
            continue
        out.append(result)
    return out


def _gc_removed_locks(hosts: list[dict[str, Any]]) -> list[str]:
    """Lock mồ côi đã dọn, gom theo ``host:key`` (B22).

    Mỗi host có thể dọn nhiều lock ⇒ cần **tên host** để biết lock nào nằm
    ở đâu (``<layout_root>/inputs/<key>.lock``).
    """
    out: list[str] = []
    for item in hosts:
        host = str(item.get("host") or "")
        for key in item.get("removed_locks") or []:
            out.append(f"{host}:{key}")
    return out


def _cmd_gc(args: argparse.Namespace) -> tuple[dict, int]:
    """``roff gc`` — dọn cache local theo LRU **và** kho input trên host.

    Giữ dưới ``cache_max_mb``; **không** xoá gì khi disk trống <
    ``cache_min_free_mb`` (để không làm máy local chết vì thiếu chỗ).

    Kho input trên host (``<layout_root>/inputs/``) được dọn riêng: xoá bản
    copy mà **không job nào dùng** và đã cũ hơn ``--max-age-days`` ngày (mặc
    định 7). ``--max-age-days 0`` ⇒ dọn **mọi** bản copy không còn ai dùng.

    Ngưỡng tuổi đọc bằng kiểm tra **None** (không truyền) — **không** dùng
    ``or``: 0 là falsy ⇒ bị nuốt thành mặc định ⇒ ``gc`` không bao giờ dọn
    được kho input ⇒ kho input phía host **phình vô hạn** (B32).
    """
    # Validate option **trước** khi nạp config: input sai ⇒ exit 2 (chưa làm
    # gì), config sai ⇒ exit 3 — đúng thứ tự "validate → nạp config" của
    # ``roff do``.
    age_days = _float_option(
        getattr(args, "max_age_days", None),
        "--max-age-days",
        minimum=0.0,
        default=shared_mod.DEFAULT_STORE_MAX_AGE_S / 86400.0,
    )
    age_s = age_days * 86400.0
    job_days = _float_option(
        getattr(args, "job_max_age_days", None),
        "--job-max-age-days",
        minimum=0.0,
        default=float(job_mod.DEFAULT_GC_MAX_AGE_DAYS),
    )
    cfg = _load_config()
    root = cache_dir()
    payload: dict[str, Any] = {"ok": True, "exit_code": EXIT_OK}
    # Lock mồ côi đã dọn trên host (B22) — luôn có trong JSON, kể cả khi
    # không dọn được gì.
    payload["removed_locks"] = []
    payload["removed_locks_count"] = 0
    keep_keys = _store_keys_in_use()

    # --- dọn ledger job (W5b / B13) ------------------------------------------------
    # Job định dạng cũ (không có khoá ``state``) là **rác**: 951 file trong
    # ledger của người dùng ⇒ ``roff wait`` treo hết timeout. Job terminal quá
    # hạn cũng dọn. Job **đang chạy / xếp hàng** thì giữ.
    try:
        job_gc = job_mod.gc_jobs(state_dir=state_dir(), max_age_days=job_days)
    except Exception as exc:  # pragma: no cover - lỗi dọn không làm hỏng gc
        job_gc = {"removed": [], "removed_count": 0, "freed_bytes": 0,
                  "error": str(exc)}
    payload["removed_jobs"] = list(job_gc.get("removed") or [])
    payload["removed_jobs_count"] = int(job_gc.get("removed_count") or 0)
    payload["skipped_jobs"] = list(job_gc.get("skipped") or [])
    payload["freed_jobs_mb"] = round(
        float(job_gc.get("freed_bytes") or 0) / (1024.0 * 1024.0), 3
    )
    # Dọn ticket/lease mồ côi của sổ slot (W3a) — host bị kẹt vĩnh viễn vì
    # 1 ticket của job đã chết là lỗi của *sổ slot*, không phải của cache.
    slot_result = _reconcile_slots()
    payload["slots"] = {
        "removed": list(slot_result.get("removed") or []),
        "lost_jobs": list(slot_result.get("lost_jobs") or []),
    }
    if not os.path.isdir(root):
        payload["freed_mb"] = 0
        payload["removed"] = []
        payload["cache"] = root
        hosts = _gc_host_stores(keep_keys, age_s)
        payload["hosts"] = hosts
        payload["removed_locks"] = _gc_removed_locks(hosts)
        payload["removed_locks_count"] = len(payload["removed_locks"])
        payload["freed_mb"] = round(
            sum(float(item.get("freed_mb") or 0.0) for item in hosts)
            + payload["freed_jobs_mb"],
            3,
        )
        return payload, EXIT_OK

    try:
        usage = shutil.disk_usage(root)
        free_mb = int(usage.free / (1024 * 1024))
    except OSError:
        free_mb = 0

    entries: list[tuple[float, int, str]] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            full = os.path.join(dirpath, name)
            try:
                stat = os.stat(full)
            except OSError:
                continue
            entries.append((stat.st_mtime, stat.st_size, full))
    entries.sort()  # cũ nhất trước ⇒ LRU
    total_bytes = sum(size for _mtime, size, _path in entries)

    payload["cache"] = root
    payload["cache_mb"] = round(total_bytes / (1024.0 * 1024.0), 3)
    payload["free_mb"] = free_mb

    if free_mb < int(cfg.cache_min_free_mb):
        payload["skipped"] = True
        payload["reason"] = (
            f"disk trống {free_mb} MB < cache_min_free_mb {cfg.cache_min_free_mb} — "
            "không xoá gì"
        )
        payload["freed_mb"] = 0
        payload["removed"] = []
        hosts = _gc_host_stores(keep_keys, age_s)
        payload["hosts"] = hosts
        payload["removed_locks"] = _gc_removed_locks(hosts)
        payload["removed_locks_count"] = len(payload["removed_locks"])
        payload["freed_mb"] = round(
            sum(float(item.get("freed_mb") or 0.0) for item in hosts)
            + payload["freed_jobs_mb"],
            3,
        )
        return payload, EXIT_OK

    max_bytes = int(cfg.cache_max_mb) * 1024 * 1024
    removed: list[str] = []
    freed = 0
    for _mtime, size, path in entries:
        if total_bytes - freed <= max_bytes:
            break
        try:
            os.unlink(path)
        except OSError:
            continue
        removed.append(path)
        freed += size
    payload["skipped"] = False
    payload["removed"] = removed
    payload["removed_count"] = len(removed)
    hosts = _gc_host_stores(keep_keys, age_s)
    payload["hosts"] = hosts
    payload["removed_locks"] = _gc_removed_locks(hosts)
    payload["removed_locks_count"] = len(payload["removed_locks"])
    payload["freed_mb"] = round(
        freed / (1024.0 * 1024.0)
        + sum(float(item.get("freed_mb") or 0.0) for item in hosts)
        + payload["freed_jobs_mb"],
        3,
    )
    return payload, EXIT_OK


# --------------------------------------------------------------------------
# lệnh nội bộ: __watch
# --------------------------------------------------------------------------
def _cmd_watch(args: argparse.Namespace) -> tuple[dict, int]:
    """``roff __watch <job_id>`` — **nội bộ**, do watcher gọi.

    Chạy vòng lặp giám sát rồi trả slot đã chiếm (job đã kết thúc).
    """
    job_id = args.job_id
    code = EXIT_OK
    try:
        watcher_mod.watch(job_id)
    except RoffError as exc:
        code = exc.code
    # CHỈ trả slot khi job đã kết thúc. Watcher có thể tự nối lại (job còn
    # sống nhưng hết thời gian theo dõi) — lúc đó watcher MỚI vẫn đang giữ
    # job, trả slot ở đây ⇒ host nhận thêm job ⇒ vượt RAM (Muse chỉ còn
    # 450–900 MB) ⇒ OOM đúng dịch vụ production của người dùng.
    terminal = False
    try:
        terminal = job_mod.load_job(job_id).state in job_mod.TERMINAL_STATES
    except Exception:
        terminal = False
    if terminal:
        _release_slot(job_id)
    try:
        job = job_mod.load_job(job_id)
    except RoffError:
        return _ok(code, job=job_id), code
    return (
        _ok(
            code,
            job=job.id,
            state=job.state,
            exit=job.exit_code,
            host=job.host,
            reason=job.reason,
        ),
        code,
    )


# --------------------------------------------------------------------------
# argparse
# --------------------------------------------------------------------------
class _Parser(argparse.ArgumentParser):
    """Parser mà **mọi** output của argparse đi ra stderr.

    Nhờ vậy stdout của roff **chỉ** chứa JSON (trừ ``roff logs``).
    """

    def print_help(self, file: Any = None) -> None:  # type: ignore[override]
        super().print_help(file or sys.stderr)

    def print_usage(self, file: Any = None) -> None:  # type: ignore[override]
        super().print_usage(file or sys.stderr)

    def error(self, message: str) -> None:  # type: ignore[override]
        """Lỗi argparse ⇒ usage + message ra stderr, exit 2 (KHÔNG in JSON)."""
        self.print_usage(sys.stderr)
        sys.stderr.write(f"{self.prog}: lỗi: {message}\n")
        raise SystemExit(2)

    def exit(self, status: int = 0, message: str | None = None) -> None:  # type: ignore[override]
        """Thoát với code (message nếu có đi ra stderr)."""
        if message:
            sys.stderr.write(message)
        raise SystemExit(status)


def _build_parser() -> _Parser:
    """Dựng parser của toàn bộ lệnh."""
    parser = _Parser(prog="roff", description="roff — chạy việc nặng trên host khác")
    sub = parser.add_subparsers(dest="command", metavar="<lệnh>")

    # --- do ---------------------------------------------------------------
    do = sub.add_parser("do", help="chạy 1 lệnh trên host khác (lệnh chính)")
    do.add_argument("--cmd", required=True, help="lệnh shell chạy trên host")
    do.add_argument(
        "--in", dest="inputs", action="append", default=[],
        help="input: path local | git:<url> | http(s)://… | job:<id> (lặp lại được)",
    )
    do.add_argument("--out", help="thư mục local để kéo artifact về")
    do.add_argument(
        "--need", dest="need", action="append", default=[],
        help="tool cần có trên host (lặp lại được)",
    )
    do.add_argument("--cpus", default="0", help="số core cấp (export $ROFF_CPUS)")
    do.add_argument("--mem", help="ngân sách RAM (vd 600M, 2G)")
    do.add_argument("--disk", help="ngân sách disk (vd 2G)")
    do.add_argument("--each", help="glob item cho lô")
    do.add_argument("--shards", default="1", help="số shard")
    do.add_argument("--after", help="chờ job khác xong (vd job:261123-101530-ab12)")
    do.add_argument(
        "--sync-back", dest="sync_back", action="append", default=[],
        help="kéo về path chỉ định trong project (lặp lại được)",
    )
    do.add_argument("--checkpoint", help="thư mục checkpoint định kỳ")
    do.add_argument(
        "--env", dest="env", action="append", default=[],
        help="biến môi trường KEY=VALUE (secret đi qua đây)",
    )
    do.add_argument("--retry", default="1", help="số lần failover")
    do.add_argument(
        "--side-effects", dest="side_effects", action="store_true",
        help="job không chạy lại được ⇒ không retry",
    )
    do.add_argument(
        "--timeout", default=str(DEFAULT_TIMEOUT),
        help="giới hạn giây (0 bị từ chối)",
    )
    do.add_argument("--priority", default="normal", help="high | normal")
    do.add_argument(
        "--prefer-same-host", dest="prefer_same_host",
        action=argparse.BooleanOptionalAction, default=True,
        help="ưu tiên host đang giữ dữ liệu (mặc định bật)",
    )
    do.add_argument("--host", help="ép host, bỏ qua scheduler")
    do.add_argument("--name", help="tên project (mặc định tự sinh)")

    # --- status / result / wait / jobs ------------------------------------
    status = sub.add_parser("status", help="trạng thái 1 job (đọc ledger)")
    status.add_argument("job_id")

    result = sub.add_parser("result", help="kết quả đầy đủ của 1 job")
    result.add_argument("job_id")

    wait = sub.add_parser("wait", help="chờ job tới trạng thái cuối")
    wait.add_argument("job_id")
    wait.add_argument(
        "--timeout", default=str(DEFAULT_TIMEOUT), help="số giây chờ tối đa"
    )

    jobs = sub.add_parser("jobs", help="danh sách job trong ledger")
    jobs.add_argument("--state", help="lọc theo trạng thái")
    jobs.add_argument("--host", help="lọc theo host")

    # --- logs / cancel ----------------------------------------------------
    logs = sub.add_parser("logs", help="in log job ra stdout (text, không phải JSON)")
    logs.add_argument("job_id")
    logs.add_argument("--tail", help="số dòng cuối (mặc định 200)")
    logs.add_argument("--grep", help="chỉ lấy dòng chứa PATTERN")

    cancel = sub.add_parser("cancel", help="huỷ job (kill process group)")
    cancel.add_argument("job_id")

    # --- hosts / probe ----------------------------------------------------
    hosts = sub.add_parser("hosts", help="probe mọi host trong fleet")
    hosts.add_argument(
        "--parallel", action="store_true", help="probe song song (mặc định serial)"
    )
    hosts.add_argument(
        "--json", action="store_true", help="tương thích — output vốn đã là JSON"
    )

    probe = sub.add_parser("probe", help="probe 1 host")
    probe.add_argument("host")

    # --- push / pull ------------------------------------------------------
    push = sub.add_parser("push", help="đẩy project lên host")
    push.add_argument("host")
    push.add_argument("local_dir")
    push.add_argument("--name", required=True, help="tên project")

    pull = sub.add_parser("pull", help="kéo artifact của 1 job về local")
    pull.add_argument("job_id")
    pull.add_argument("--local", required=True, help="thư mục đích local")

    # --- ensure -----------------------------------------------------------
    ensure = sub.add_parser("ensure", help="cài toolchain trên host (idempotent)")
    ensure.add_argument("host")
    ensure.add_argument("--setup", required=True, help="đường dẫn script setup")
    ensure.add_argument("--name", required=True, help="tên toolchain")

    # --- tunnel -----------------------------------------------------------
    tunnel = sub.add_parser("tunnel", help="cổng chuyển tiếp (start|stop|list)")
    tunnel_sub = tunnel.add_subparsers(dest="tunnel_action", metavar="<hành động>")
    t_start = tunnel_sub.add_parser("start")
    t_start.add_argument("--host", required=True)
    t_start.add_argument("--lport", required=True)
    t_start.add_argument("--rport", required=True)
    t_stop = tunnel_sub.add_parser("stop")
    t_stop.add_argument("--host", help="alias host (dừng tunnel đơn)")
    t_stop.add_argument("--lport", help="cổng local (dừng tunnel đơn)")
    t_stop.add_argument(
        "--from", dest="from_alias",
        help="alias máy nhận (dừng link phối hợp)",
    )
    t_stop.add_argument(
        "--rport", help="cổng listener trên máy nhận (dừng link phối hợp)",
    )
    t_link = tunnel_sub.add_parser(
        "link", help="nối phối hợp giữa 2 máy (A nhận, B phục vụ)"
    )
    t_link.add_argument("--from", dest="from_alias", required=True,
                        help="alias máy nhận kết nối (listener trên máy này)")
    t_link.add_argument("--to", dest="to_alias", required=True,
                        help="alias máy phục vụ (dịch vụ đang LISTEN trên máy này)")
    t_link.add_argument("--rport", required=True,
                        help="cổng listener trên máy --from")
    t_link.add_argument("--to-port", dest="to_port",
                        help="cổng dịch vụ trên máy --to (mặc định = --rport)")
    t_link.add_argument("--via-lport", dest="via_lport",
                        help="cổng trung gian trên máy local (mặc định tự chọn 18500-18599)")
    tunnel_sub.add_parser("list")

    # --- gc ---------------------------------------------------------------
    gc = sub.add_parser("gc", help="dọn cache local theo LRU + kho input trên host")
    gc.add_argument(
        "--max-age-days",
        dest="max_age_days",
        type=float,
        default=7.0,
        help="xoá bản copy trong kho input của host nếu cũ hơn N ngày (mặc định 7)",
    )
    gc.add_argument(
        "--job-max-age-days",
        dest="job_max_age_days",
        type=float,
        default=30.0,
        help=(
            "xoá job terminal trong ledger nếu cũ hơn N ngày (mặc định 30); "
            "job định dạng cũ (không có khoá 'state') luôn bị xoá"
        ),
    )

    # --- doctor ------------------------------------------------------------
    doctor = sub.add_parser(
        "doctor", help="chẩn đoán sức khoẻ hệ thống (chỉ báo, không sửa gì)"
    )
    doctor.add_argument(
        "--probe",
        action="store_true",
        help="probe thật host (chậm vài giây); mặc định đọc cache",
    )

    # --- __watch (nội bộ) -------------------------------------------------
    watch = sub.add_parser("__watch", help=argparse.SUPPRESS)
    watch.add_argument("job_id")

    return parser


_HANDLERS: dict[str, Callable[[argparse.Namespace], tuple[dict, int]]] = {
    "do": _cmd_do,
    "status": _cmd_status,
    "wait": _cmd_wait,
    "result": _cmd_result,
    "logs": _cmd_logs,
    "cancel": _cmd_cancel,
    "jobs": _cmd_jobs,
    "hosts": _cmd_hosts,
    "probe": _cmd_probe,
    "push": _cmd_push,
    "pull": _cmd_pull,
    "ensure": _cmd_ensure,
    "tunnel": _cmd_tunnel,
    "gc": _cmd_gc,
    "doctor": _cmd_doctor,
    "__watch": _cmd_watch,
}


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------
def main(argv: Sequence[str] | None = None) -> int:
    """Điểm vào của CLI.

    * Lỗi argparse → **stderr** + exit **2** (không in JSON).
    * ``RoffError`` → in JSON payload của nó + exit theo code.
    * Mọi lệnh khác → in **1 JSON object** ra stdout + exit code tương ứng.
    """
    parser = _build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else EXIT_INPUT
        return code

    command = getattr(args, "command", None)
    # Chuẩn hoá quyền file state (W3h): file 0600, thư mục 0700 — kể cả file
    # đã tồn tại từ trước (mode 0664 do umask của máy tạo ra).
    # ``doctor`` **chỉ báo** ⇒ không được sửa gì, kể cả quyền file.
    if command != "doctor":
        try:
            perm.normalize_tree(state_dir())
        except Exception:  # pragma: no cover - lỗi quyền không được làm hỏng lệnh
            pass
    if not command:
        parser.print_usage(sys.stderr)
        sys.stderr.write("roff: cần một lệnh (xem 'roff --help')\n")
        return EXIT_INPUT

    handler = _HANDLERS.get(command)
    if handler is None:  # pragma: no cover - argparse đã chặn
        parser.print_usage(sys.stderr)
        sys.stderr.write(f"roff: lệnh không hợp lệ: {command}\n")
        return EXIT_INPUT

    try:
        payload, code = handler(args)
    except RoffError as exc:
        payload, code = _fail(exc), exc.code
    except KeyboardInterrupt:  # pragma: no cover
        sys.stderr.write("roff: bị gián đoạn\n")
        return EXIT_CANCELLED
    except Exception as exc:  # lỗi nội bộ — không để crash với traceback
        sys.stderr.write(f"roff: lỗi nội bộ: {exc!r}\n")
        payload = _fail(
            RoffError(
                EXIT_JOB_FAILED,
                "internal_error",
                error=f"lỗi nội bộ của roff: {exc}",
            )
        )
        code = EXIT_JOB_FAILED

    if command == "logs" and not payload:
        # ``roff logs`` in text ra stdout ⇒ không in JSON.
        return code
    return _emit(payload, code)
