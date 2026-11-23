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
from . import inputs as inputs_mod
from . import job as job_mod
from . import layout as layout_mod
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
    EXIT_TIMEOUT,
    EXIT_UNREACHABLE,
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
    """Nạp config INI; sai ⇒ ``RoffError(EXIT_CONFIG)`` do :mod:`config` ném."""
    return config_mod.Config.load(os.environ.get("ROFF_CONFIG") or None)


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
    ssh_config = os.environ.get("ROFF_SSH_CONFIG")
    try:
        if ssh_config:
            hosts = fleet_mod.parse_ssh_config(ssh_config)
        else:
            hosts = fleet_mod.parse_ssh_config()
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
    shards = _int_option(getattr(args, "shards", 1) or 1, "--shards", minimum=1)
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


def _remote_item_path(
    host: str,
    item: str,
    inputs: Sequence[_Input],
    pushed: set[tuple[str, str]],
    layout_root: str,
) -> tuple[str, str, str, bool]:
    """Đường dẫn **remote** của 1 item của ``--each`` (trong kho dùng chung).

    Trả ``(đường dẫn remote, shared key, rel trong {key}/, item nằm trong input)``.

    * Item nằm trong 1 input thư mục ⇒ dùng đường dẫn tương đối trong bản copy
      của input đó (input đã nằm trong kho ⇒ **không đẩy lại**).
    * Item không nằm trong input nào ⇒ tự trở thành **1 nguồn riêng** trong kho
      (đẩy đúng 1 lần, các job sau dùng lại bản copy đó).
    """
    source_locals = [
        os.path.abspath(os.path.expanduser(source.value))
        for source in inputs
        if source.kind == "path"
    ]
    return inputs_mod.resolve_item_path(
        host, item, layout_root, source_locals, pushed
    )


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
    sync_back = list(getattr(args, "sync_back", None) or [])

    # input từ job khác → ưu tiên host đang giữ dữ liệu
    prefer_hosts: list[str] = []
    source_job: job_mod.Job | None = None
    for item in inputs:
        if item.kind == "job":
            source_job = job_mod.load_job(item.value)
            if prefer_same_host and source_job.host:
                prefer_hosts.append(source_job.host)

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

    ledger = slots_mod.SlotLedger()
    used_hosts: set[str] = set()
    pushed: set[tuple[str, str]] = set()

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
        """Đường dẫn **remote** của các path ``--sync-back``.

        Path nằm trong 1 input (nguồn local) ⇒ nằm trong **kho dùng chung**
        của host (``<layout_root>/inputs/<key>/…``) — input không còn được
        copy vào workdir (W3f). Còn lại ⇒ path trong workdir.
        """
        out: list[str] = []
        for raw in sync_back:
            text = str(raw).lstrip("/")
            remote = ""
            for source in path_sources:
                base = os.path.abspath(str(source.get("local") or ""))
                if not base or not os.path.isdir(base):
                    continue
                candidate = os.path.abspath(os.path.join(base, text))
                if not candidate.startswith(base + os.sep):
                    continue
                rel = os.path.relpath(candidate, base).replace(os.sep, "/")
                key = str(source.get("key") or "")
                remote = shared_mod.shared_remote_path(layout_root, key, rel)
                break
            if not remote:
                remote = f"{workdir.rstrip('/')}/{text}"
            out.append(remote)
        return out

    # ------------------------------------------------------------------
    # helper: 1 job của lô
    # ------------------------------------------------------------------
    def _prepare_spec(index: int, spec: dict) -> dict:
        """Tạo job → xin slot → chuẩn bị input → launch.

        Chuẩn bị input (hoặc setup toolchain) thất bại vì **host chết** ⇒ thử
        host khác (tối đa N lần, N = số host trong fleet). Lỗi do **sai cấu
        hình** (URL hỏng, thiếu tool) ⇒ **không** retry — báo lỗi luôn
        (W3c + W3m: exit 1/2, không phải 255).
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
                "sync_back_remote": _sync_back_remotes(workdir, layout_root),
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
                ensured = _ensure_tools(host, choice.profile, needs)

                # --- chuẩn bị input -------------------------------------------
                job_env: dict[str, str] = {}
                has_internet = bool(getattr(choice.profile, "internet", False))
                if queued:
                    # Host đang bận ⇒ job CHỜ trong hàng đợi: watcher sẽ chuẩn bị
                    # input + launch khi được cấp slot. Không clone/tải sớm (tốn
                    # băng thông + tạo dữ liệu ở chỗ chưa ai chạy).
                    options["input_kind"] = (
                        input_specs[0]["kind"] if input_specs else "path"
                    )
                else:
                    try:
                        prepared, primary_remote, primary_kind, env_extra = (
                            inputs_mod.prepare_inputs(
                                input_specs,
                                host,
                                workdir,
                                layout_root,
                                has_internet=has_internet,
                                source_job=source_job,
                            )
                        )
                    except RoffError as exc:
                        # Chuẩn bị input thất bại vì **host chết** ⇒ thử host
                        # khác (W3c). Lỗi **sai cấu hình** (exit 1/2) ⇒ không
                        # retry, báo lỗi luôn (W3m).
                        if exc.code == EXIT_UNREACHABLE and attempt + 1 < attempts:
                            retry_host = True
                            raise
                        raise
                    job_env.update(env_extra)
                    options["input_sources"] = [
                        {key: value for key, value in entry.items() if key != "value"}
                        for entry in prepared
                        if entry.get("kind") == "path"
                    ]
                    options["input_remote"] = primary_remote
                    options["input_kind"] = primary_kind
                    if primary_remote:
                        job_env["ROFF_IN"] = primary_remote

                # item của --each: đường dẫn trong kho dùng chung (ROFF_ITEM)
                if spec.get("item"):
                    options["item_local"] = os.path.abspath(str(spec["item"]))
                    if queued:
                        # watcher sẽ tính lại khi được cấp slot
                        options["item_inside_input"] = False
                    else:
                        item_remote, item_key, item_rel, item_inside = (
                            _remote_item_path(
                                host, str(spec["item"]), inputs, pushed, layout_root
                            )
                        )
                        job.item = item_remote
                        options["item_shared_key"] = item_key
                        options["item_shared_rel"] = item_rel
                        options["item_inside_input"] = item_inside

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
                    # tạo thư mục trên host (launch cần workdir có sẵn ⇒ exit 97 nếu thiếu)
                    layout_mod.remote_mkdirs(host, workdir, run=_shell_runner)
                    layout_mod.remote_mkdirs(
                        host, os.path.dirname(str(job.out_remote)), run=_shell_runner
                    )
                    if checkpoint_remote:
                        # Thư mục checkpoint phải có sẵn trên host (W3n).
                        layout_mod.remote_mkdirs(
                            host, checkpoint_remote, run=_shell_runner
                        )

                    # --- launch job ---------------------------------------------
                    job_mod.launch(
                        host,
                        job,
                        env={**job_env, **env},
                        cpus=cpus,
                        mem_mb=mem_mb,
                    )
                    if getattr(args, "checkpoint", None):
                        # Failover sau nay ⇒ $ROFF_RESUME=1
                        job_mod.set_job_options(job_id, resume=True)

                # --- spawn watcher --------------------------------------------
                # Job ở state ``queued`` cũng cần watcher: watcher sẽ xin slot,
                # chuẩn bị input rồi launch.
                _spawn_watcher(job_id)
                watcher_spawned = True

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
                }
            except Exception as exc:
                # Trả slot ĐÚNG lease đã chiếm + bỏ ticket khỏi hàng đợi.
                # Nếu không, lease rò ⇒ fleet bị khoá vĩnh viễn (mọi `roff do`
                # sau đó trả exit 10).
                _drop_ticket(ticket, lease)
                if job_id:
                    _release_slot(job_id)
                # Chuẩn bị input thất bại vì **host chết** ⇒ thử host khác (W3c).
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
    )
    if len(job_ids) > 1:
        payload["jobs"] = job_ids
        payload["count"] = len(job_ids)
    ensured_all = [item["ensured"] for item in results if item["ensured"]]
    if ensured_all:
        payload["ensured"] = ensured_all
    return payload, EXIT_OK


def _spawn_watcher(job_id: str) -> None:
    """Spawn watcher nền (trừ khi ``ROFF_NO_WATCHER=1`` — chỉ dùng cho test)."""
    if os.environ.get("ROFF_NO_WATCHER") == "1":
        return
    watcher_mod.spawn_watcher(job_id)


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
            pulled = bool(opts.get("pulled"))
            pull_error = str(opts.get("pull_error") or "")
            return (
                _ok(
                    EXIT_OK,
                    job=job.id,
                    state=state,
                    exit=0,
                    host=job.host,
                    artifacts=manifest,
                    pulled=pulled,
                    pull_error=pull_error or None,
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
    sync_paths = list(options.get("sync_back_remote") or options.get("sync_back") or [])
    for path in sync_paths:
        extra = transfer_mod.pull_path(job.host, str(path), local)
        files.extend(extra.files)
    if not result.ok:
        raise RoffError(
            EXIT_UNREACHABLE,
            "pull_failed",
            error=f"pull thất bại từ {job.host}",
            detail=result.error,
            job=job.id,
            host=job.host,
        )
    job_mod.set_job_options(job.id, manifest=files)
    return (
        _ok(
            job=job.id,
            host=job.host,
            local=local,
            bytes=result.bytes_transferred,
            files=files,
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
    if action == "stop":
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
                count=len(tunnels or []),
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
                    "skipped": [],
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
                    "skipped": [],
                    "freed_mb": 0.0,
                    "error": str(exc),
                }
            )
            continue
        out.append(result)
    return out


def _cmd_gc(args: argparse.Namespace) -> tuple[dict, int]:
    """``roff gc`` — dọn cache local theo LRU **và** kho input trên host.

    Giữ dưới ``cache_max_mb``; **không** xoá gì khi disk trống <
    ``cache_min_free_mb`` (để không làm máy local chết vì thiếu chỗ).

    Kho input trên host (``<layout_root>/inputs/``) được dọn riêng: xoá bản
    copy mà **không job nào dùng** và đã cũ hơn 7 ngày (W3g).
    """
    cfg = _load_config()
    root = cache_dir()
    payload: dict[str, Any] = {"ok": True, "exit_code": EXIT_OK}
    try:
        age_s = float(getattr(args, "max_age_days", 7.0) or 7.0) * 86400.0
    except (TypeError, ValueError):
        age_s = shared_mod.DEFAULT_STORE_MAX_AGE_S
    keep_keys = _store_keys_in_use()
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
        payload["freed_mb"] = round(
            sum(float(item.get("freed_mb") or 0.0) for item in hosts), 3
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
        payload["freed_mb"] = round(
            sum(float(item.get("freed_mb") or 0.0) for item in hosts), 3
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
    payload["freed_mb"] = round(
        freed / (1024.0 * 1024.0)
        + sum(float(item.get("freed_mb") or 0.0) for item in hosts),
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
    t_stop.add_argument("--host", required=True)
    t_stop.add_argument("--lport", required=True)
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
    # Chuẩn hoá quyền file state (W3h): file 0600, thư mục 0700 — kể cả file
    # đã tồn tại từ trước (mode 0664 do umask của máy tạo ra).
    try:
        perm.normalize_tree(state_dir())
    except Exception:  # pragma: no cover - lỗi quyền không được làm hỏng lệnh
        pass
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else EXIT_INPUT
        return code

    command = getattr(args, "command", None)
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
