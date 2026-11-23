"""Vòng đời job trên host: tạo, launch, đọc trạng thái, huỷ.

Nguyên tắc (đo thật + API-CONTRACT §10):

* **Mỗi lệnh ssh ~3,5 s** ⇒ gom **mọi** thứ vào **một** round-trip
  (:func:`remote_state` hỏi trong 1 lần: exit file, boot_id, instance_token,
  workdir, process còn sống không).
* Job phải **sống sót khi ssh rớt** ⇒ ``setsid nohup`` (SID riêng).
* **Không bao giờ** ``pkill`` theo pattern — chỉ kill theo **pid đã ghi nhận**
  (bug R19), và kiểm tra ``/proc/<pid>/cmdline`` để không kill nhầm pid đã tái sinh.
* Secret (``--env``) **không** được nằm trong argv (bug R7): env gửi qua
  kênh stdin dưới dạng file mode 600 trên host.
* ``boot_id`` + ``instance_token`` phải được ghi **lúc launch** để sau đó
  phân biệt *host restart* với *host wipe* với *job tự biến mất*.

Module này **không in ra stdout** (quy tắc chung của roff).
"""

from __future__ import annotations

import io
import json
import os
import shlex
import subprocess
import tarfile
import time
import uuid
from dataclasses import dataclass, field, fields
from typing import Any

try:  # phần A — fallback tối giểu để test chạy được khi A chưa merge
    from .errors import (
        EXIT_CANCELLED,
        EXIT_INPUT,
        EXIT_NO_WORKDIR,
        EXIT_TIMEOUT,
        EXIT_UNREACHABLE,
        RoffError,
    )
except ImportError:  # pragma: no cover - module chạy đứng riêng (không có package)
    from typing import Any as _Any

    EXIT_CANCELLED = 130
    EXIT_INPUT = 2
    EXIT_NO_WORKDIR = 97
    EXIT_TIMEOUT = 124
    EXIT_UNREACHABLE = 255

    class RoffError(Exception):  # type: ignore[no-redef]
        """Bản sao tối thiểu của ``roffkit.errors.RoffError``."""

        def __init__(self, code: int, reason: str = "", **fields: _Any) -> None:
            super().__init__(reason or f"exit {code}")
            self.code = code
            self.reason = reason
            self.fields = fields

        def payload(self) -> dict:
            out: dict[str, _Any] = {"ok": False, "exit_code": self.code}
            if self.reason:
                out["reason"] = self.reason
            out.update(self.fields)
            return out


try:  # lớp ssh trung tâm (ControlMaster) — không định nghĩa lại option ssh ở đây
    from . import sshx
except ImportError:  # pragma: no cover - module chạy đứng riêng (không có package)
    sshx = None  # type: ignore[assignment]

try:  # B25: nguồn sự thật cho tham số ``-F`` (dùng khi rơi vào fallback)
    from . import fleet
except ImportError:  # pragma: no cover - module chạy đứng riêng (không có package)
    fleet = None  # type: ignore[assignment]


try:
    from . import perm
except ImportError:  # pragma: no cover - chạy module lẻ
    import types

    perm = types.SimpleNamespace(  # type: ignore[assignment]
        secure_dir=lambda path: None,
        secure_file=lambda path: None,
    )


try:  # B34/C9: chính sách nice/ionice — MỘT nguồn sự thật cho 5 module
    from .config import local_nice_prefix, remote_nice_args
except ImportError:  # pragma: no cover - module chạy đứng riêng (không có package)
    def local_nice_prefix() -> list[str]:  # type: ignore[no-redef]
        """Fallback tối thiểu — xem :func:`roffkit.config.local_nice_prefix`.

        Module chạy **đứng riêng** ⇒ không có ``config.py`` ⇒ **không có**
        chính sách nice/ionice ⇒ không bọc. Chỉ là chế độ dự phòng; đường
        chính của ``roff`` luôn import được ``config.py``.
        """
        return []

    def remote_nice_args(host=None, config=None) -> tuple[str, str]:  # type: ignore[no-redef]
        """Fallback tối thiểu — xem :func:`roffkit.config.remote_nice_args`.

        Không đọc được config ⇒ dùng **mặc định an toàn** (``nice 19`` + I/O
        ``idle``) để job vẫn không cạnh tranh với dịch vụ production của
        người dùng trên cùng máy đó (C9).
        """
        return ("19", "3")


__all__ = [
    "JOB_STATES",
    "TERMINAL_STATES",
    "UNKNOWN_STATE",
    "gc_jobs",
    "Job",
    "new_job_id",
    "ledger_dir",
    "save_job",
    "load_job",
    "list_jobs",
    "launch",
    "remote_state",
    "cancel",
    "heartbeat_path",
    "job_options",
    "set_job_options",
    "env_path",
    "save_job_env",
    "load_job_env",
    "remote_job_dir",
    "remote_workdir",
    "read_log",
]


# --------------------------------------------------------------------------
# hằng số
# --------------------------------------------------------------------------
JOB_STATES: tuple[str, ...] = (
    "queued",
    "launched",
    "running",
    "exited",
    "lost",
    "cancelled",
    "unknown",
)
"""Trạng thái job (hợp đồng API §10)."""

TERMINAL_STATES: tuple[str, ...] = ("exited", "lost", "cancelled", "unknown")
"""Trạng thái kết thúc — watcher dừng, ``roff wait`` trả về."""

UNKNOWN_STATE = "unknown"
"""Trạng thái của job **định dạng cũ** (file ledger không có khoá ``state``).

Bug **B13** (đo thật 23/11/2026): 951/997 file job trong ledger của người
dùng là định dạng cũ ⇒ ``Job.from_dict`` lấy default của dataclass ⇒
``state == "queued"`` ⇒ job **không bao giờ** terminal ⇒ ``roff wait`` treo
hết timeout (exit 124) và ``reconcile_slots`` giữ slot của ticket không ai
theo dõi.

``unknown`` là trạng thái **terminal**: không giữ slot, ``roff wait`` trả về
ngay với lý do rõ, ``roff jobs`` hiển thị ``"unknown"`` (không ẩn đi — xem
``docs/SPEC-CLI.md`` §7)."""

#: Khoá chứa tuỳ chọn phụ của job trong file ledger.
OPTIONS_KEY = "_roff"

#: Timeout (giây) cho 1 lần launch.
LAUNCH_TIMEOUT = 120

#: Placeholder trong meta.json — host điền sau khi biết pid/boot_id/token.
PID_PLACEHOLDER = "__ROFF_PID__"
BOOT_ID_PLACEHOLDER = "__ROFF_BOOT_ID__"
INSTANCE_TOKEN_PLACEHOLDER = "__ROFF_INSTANCE_TOKEN__"


# --------------------------------------------------------------------------
# dataclass
# --------------------------------------------------------------------------
@dataclass
class Job:
    """Một job offload (bản ghi trong ledger local)."""

    id: str
    cmd: str
    host: str
    project: str
    created_at: float = field(default_factory=time.time)
    state: str = "queued"
    exit_code: int | None = None
    reason: str | None = None
    attempts: list[dict] = field(default_factory=list)
    """Lịch sử chạy: mỗi lần launch thêm 1 entry (host/pid/boot_id/token…)."""
    cpus: int = 0
    mem_mb: int = 0
    out_remote: str | None = None
    """``$ROFF_OUT`` trên host = ``<layout_root>/jobs/<job_id>/out``."""
    out_local: str | None = None
    shard: int | None = None
    shards: int | None = None
    item: str | None = None
    needs_retry: bool = True
    """False khi job có ``--side-effects`` ⇒ không bao giờ retry."""
    heartbeat: float = 0.0

    def to_dict(self) -> dict:
        """Trả về dict JSON-safe của job."""
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @staticmethod
    def from_dict(d: dict) -> "Job":
        """Dựng Job từ dict (bỏ qua khoá lạ, thiếu khoá thì dùng default).

        **B13**: dict **không có khoá ``state``** ⇒ ``state = "unknown"``,
        KHÔNG phải ``"queued"``. Không được dựa vào mặc định của dataclass cho
        trường "thiếu = giá trị nào đó": job định dạng cũ bị coi là *đang xếp
        hàng* mãi mãi ⇒ ``roff wait`` treo hết timeout.
        """
        known = {f.name for f in fields(Job)}
        kwargs = {k: v for k, v in (d or {}).items() if k in known}
        kwargs.setdefault("id", "")
        kwargs.setdefault("cmd", "")
        kwargs.setdefault("host", "")
        kwargs.setdefault("project", "")
        kwargs.setdefault("created_at", 0.0)
        if not kwargs.get("state"):
            kwargs["state"] = UNKNOWN_STATE
        job = Job(**kwargs)
        job.attempts = [a for a in (job.attempts or []) if isinstance(a, dict)]
        return job

    def last_attempt(self) -> dict | None:
        """Attempt gần nhất (dùng để so boot_id/token với lúc launch)."""
        for attempt in reversed(self.attempts or []):
            if isinstance(attempt, dict):
                return attempt
        return None


# --------------------------------------------------------------------------
# ledger (cục bộ)
# --------------------------------------------------------------------------
def _state_dir() -> str:
    """Thư mục state của roff (``$ROFF_STATE_DIR`` → ``~/.local/state/roff``)."""
    env = os.environ.get("ROFF_STATE_DIR")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return os.path.join(os.path.expanduser("~"), ".local", "state", "roff")


def ledger_dir(state_dir: str | None = None) -> str:
    """Thư mục ledger job: ``~/.local/state/roff/jobs``.

    ``state_dir`` cho phép đọc/ghi ledger ở thư mục khác (vd công cụ dọn
    dẹp gọi với state dir riêng) — mặc định lấy ``$ROFF_STATE_DIR``.
    """
    base = state_dir if state_dir else _state_dir()
    return os.path.join(base, "jobs")


def job_path(job_id: str, state_dir: str | None = None) -> str:
    """Đường dẫn file ledger của 1 job."""
    return os.path.join(ledger_dir(state_dir), f"{job_id}.json")


def heartbeat_path(job_id: str, state_dir: str | None = None) -> str:
    """Đường dẫn file heartbeat (watcher chạm vào mỗi vòng)."""
    return os.path.join(ledger_dir(state_dir), f"{job_id}.heartbeat")


def env_path(job_id: str, state_dir: str | None = None) -> str:
    """Đường dẫn file env local (mode 600) chứa secret của job."""
    return os.path.join(ledger_dir(state_dir), f"{job_id}.env")


def new_job_id() -> str:
    """Sinh job id dạng ``YYMMDD-HHMMSS-xxxx`` (4 hex)."""
    stamp = time.strftime("%y%m%d-%H%M%S", time.localtime())
    return f"{stamp}-{uuid.uuid4().hex[:4]}"


def _atomic_write_json(path: str, data: dict) -> None:
    """Ghi JSON atomically (tmp + rename) để không hỏng ledger giữa chừng.

    File state chứa dữ liệu người dùng ⇒ mode **0600**; thư mục chứa nó ⇒
    **0700** (xem :mod:`roffkit.perm`).
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    perm.secure_dir(os.path.dirname(path))
    tmp = f"{path}.tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        perm.secure_file(tmp)
        os.replace(tmp, path)
        perm.secure_file(path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _read_json(path: str) -> dict:
    """Đọc JSON từ file; lỗi → dict rỗng."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_job(job: Job, state_dir: str | None = None) -> None:
    """Ghi job vào ledger (giữ nguyên khối tuỳ chọn phụ ``_roff``)."""
    data = job.to_dict()
    existing = _read_json(job_path(job.id, state_dir))
    options = existing.get(OPTIONS_KEY)
    if isinstance(options, dict):
        data[OPTIONS_KEY] = options
    _atomic_write_json(job_path(job.id, state_dir), data)


def load_job(job_id: str, state_dir: str | None = None) -> Job:
    """Đọc job từ ledger.

    Raises:
        RoffError: ``EXIT_INPUT`` nếu không có job đó.
    """
    path = job_path(job_id, state_dir)
    if not os.path.isfile(path):
        raise RoffError(
            EXIT_INPUT,
            "job_not_found",
            error=f"không có job {job_id}",
            job=job_id,
        )
    data = _read_json(path)
    if not data:
        raise RoffError(
            EXIT_INPUT,
            "job_unreadable",
            error=f"ledger hỏng: {path}",
            job=job_id,
        )
    return Job.from_dict(data)


def list_jobs(state: str | None = None, state_dir: str | None = None) -> list[Job]:
    """Liệt kê job trong ledger (lọc theo ``state`` nếu có).

    ``state_dir`` ⇒ đọc ledger ở thư mục khác (mặc định ``$ROFF_STATE_DIR``).
    """
    out: list[Job] = []
    directory = ledger_dir(state_dir)
    if not os.path.isdir(directory):
        return out
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".json"):
            continue
        job_id = name[: -len(".json")]
        try:
            job = load_job(job_id, state_dir)
        except RoffError:
            continue
        if state and job.state != state:
            continue
        out.append(job)
    return out


# --------------------------------------------------------------------------
# dọn ledger (B13)
# --------------------------------------------------------------------------
#: Số ngày mặc định để coi 1 job terminal là "cũ" ⇒ xoá.
DEFAULT_GC_MAX_AGE_DAYS = 30.0


def gc_jobs(
    state_dir: str | None = None,
    max_age_days: float = DEFAULT_GC_MAX_AGE_DAYS,
) -> dict:
    """Dọn ledger job: xoá job **rác** + job terminal đã cũ (W5b).

    Luật:

    * file job **không có khoá ``state``** (định dạng cũ) ⇒ **rác** ⇒ xoá
      (đó là 951 job ma của bug B13);
    * job **terminal** đã cũ hơn ``max_age_days`` (mặc định 30) ⇒ xoá;
    * job **đang chạy / xếp hàng** ⇒ **giữ**;
    * file **không đọc được** (JSON hỏng) ⇒ **giữ** và báo ``skipped`` —
      không xoá thứ mình không hiểu.

    Xoá luôn file phụ của job bị xoá (``<id>.heartbeat``, ``<id>.env``) — file
    ``.env`` chứa secret, giữ lại là rác.

    Returns:
        ``{"removed": [job_id...], "removed_count": N, "skipped": [...],
        "freed_bytes": N}``.
    """
    directory = ledger_dir(state_dir)
    result: dict[str, Any] = {
        "removed": [],
        "removed_count": 0,
        "skipped": [],
        "freed_bytes": 0,
    }
    if not os.path.isdir(directory):
        return result

    try:
        days = float(max_age_days)
    except (TypeError, ValueError):
        days = DEFAULT_GC_MAX_AGE_DAYS
    cutoff = time.time() - max(0.0, days) * 86400.0

    removed: list[str] = []
    skipped: list[str] = []
    freed = 0
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".json"):
            continue
        job_id = name[: -len(".json")]
        path = os.path.join(directory, name)
        data = _read_json(path)
        if not data:
            # JSON hỏng / rỗng ⇒ KHÔNG xoá (không xoá thứ mình không hiểu).
            skipped.append(job_id)
            continue
        legacy = "state" not in data
        state = str(data.get("state") or "")
        if not legacy and state not in TERMINAL_STATES:
            continue  # đang chạy / xếp hàng ⇒ giữ
        created = 0.0
        try:
            created = float(data.get("created_at") or 0.0)
        except (TypeError, ValueError):
            created = 0.0
        if created <= 0.0:
            try:
                created = os.path.getmtime(path)
            except OSError:
                created = 0.0
        if not legacy and created > cutoff:
            continue  # terminal nhưng còn mới ⇒ giữ

        size = 0
        for suffix in (".json", ".heartbeat", ".env"):
            side = os.path.join(directory, f"{job_id}{suffix}")
            try:
                size += os.path.getsize(side)
                os.unlink(side)
            except OSError:
                pass
        freed += size
        removed.append(job_id)

    result["removed"] = removed
    result["removed_count"] = len(removed)
    result["skipped"] = skipped
    result["freed_bytes"] = freed
    return result


# --------------------------------------------------------------------------
# tuỳ chọn phụ + secret
# --------------------------------------------------------------------------
def job_options(job_id: str, state_dir: str | None = None) -> dict:
    """Khối tuỳ chọn phụ của job (retry, input, needs, workdir, events…)."""
    options = _read_json(job_path(job_id, state_dir)).get(OPTIONS_KEY)
    return dict(options) if isinstance(options, dict) else {}


def set_job_options(job_id: str, state_dir: str | None = None, **options: Any) -> dict:
    """Ghi/tách khối tuỳ chọn phụ của job; trả về khối mới."""
    path = job_path(job_id, state_dir)
    data = _read_json(path)
    current = data.get(OPTIONS_KEY)
    current = dict(current) if isinstance(current, dict) else {}
    current.update(options)
    data[OPTIONS_KEY] = current
    _atomic_write_json(path, data)
    return current


def save_job_env(job_id: str, env: dict[str, str]) -> str:
    """Ghi biến môi trường (có secret) ra file local **mode 600**; trả path."""
    os.makedirs(ledger_dir(), exist_ok=True)
    perm.secure_dir(ledger_dir())
    path = env_path(job_id)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        for key, value in (env or {}).items():
            fh.write(f"{key}={shlex.quote(str(value))}\n")
    os.chmod(path, 0o600)
    return path


def load_job_env(job_id: str) -> dict[str, str]:
    """Đọc lại env đã lưu (dùng khi failover phải launch lại)."""
    out: dict[str, str] = {}
    try:
        with open(env_path(job_id), "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                out[key.strip()] = _unquote(value.strip())
    except OSError:
        return {}
    return out


def _unquote(value: str) -> str:
    """Bỏ dấu nháy shell do :func:`save_job_env` ghi."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    try:
        parts = shlex.split(value)
    except ValueError:
        return value
    return parts[0] if parts else value


# --------------------------------------------------------------------------
# đường dẫn remote
# --------------------------------------------------------------------------
def remote_job_dir(job: Job) -> str:
    """Thư mục job trên host: ``<layout_root>/jobs/<job_id>``.

    Suy ra từ :attr:`Job.out_remote` (``$ROFF_OUT`` = ``<jobdir>/out``) vì
    hợp đồng API §10 không có trường ``layout_root`` trong ``Job``.

    Raises:
        RoffError: ``EXIT_INPUT`` nếu ``out_remote`` chưa đặt / sai quy ước.
    """
    out = str(job.out_remote or "").strip()
    if not out:
        raise RoffError(
            EXIT_INPUT,
            "missing_out_remote",
            error="job.out_remote chưa đặt (cần <layout_root>/jobs/<job_id>/out)",
            job=job.id,
        )
    out = out.rstrip("/")
    if os.path.basename(out) != "out":
        raise RoffError(
            EXIT_INPUT,
            "bad_out_remote",
            error=f"job.out_remote phải kết thúc bằng '/out': {out}",
            job=job.id,
        )
    return os.path.dirname(out)


def remote_workdir(job: Job) -> str:
    """Thư mục làm việc (cwd) của job: ``<layout_root>/projects/<project>``.

    Thứ tự ưu tiên: **ledger hiện tại** → attempt đã ghi nhận → suy ra từ
    ``layout_root`` (tuỳ chọn) → suy ra từ thư mục job.

    Ledger đứng trước attempt vì attempt là **lịch sử**: sau failover, attempt
    gần nhất vẫn trỏ tới workdir của host **cũ**, còn ledger đã được watcher
    cập nhật sang workdir của host mới. Ưu tiên attempt sẽ khiến job chạy ở
    thư mục không tồn tại trên host mới ⇒ exit 97.
    """
    options = job_options(job.id)
    if options.get("workdir"):
        return str(options["workdir"])
    attempt = job.last_attempt()
    if attempt and attempt.get("workdir"):
        return str(attempt["workdir"])
    if options.get("layout_root") and job.project:
        return f"{str(options['layout_root']).rstrip('/')}/projects/{job.project}"
    if not job.project:
        raise RoffError(
            EXIT_INPUT,
            "missing_project",
            error="job.project rỗng: không xác định được thư mục làm việc",
            job=job.id,
        )
    job_dir = remote_job_dir(job)
    layout_root = os.path.dirname(os.path.dirname(job_dir))
    return f"{layout_root}/projects/{job.project}"


# --------------------------------------------------------------------------
# chạy lệnh con (seam cho test)
# --------------------------------------------------------------------------
def _prefix_nice() -> list[str]:
    """Tiền tố ``ionice``/``nice`` cho tiến trình con (Q18).

    **MỘT nguồn sự thật** (B34): gọi :func:`roffkit.config.local_nice_prefix`
    ⇒ cùng tiền tố với probe/sshx/tunnel/transfer.
    """
    return local_nice_prefix()


def _config_args() -> list[str]:
    """Tham số ``-F`` — **cùng nguồn** với :mod:`roffkit.fleet` (B25).

    Chỉ dùng ở nhánh fallback (khi ``sshx`` không import được). Quy tắc y
    hệt mọi lệnh ssh khác: ssh phải đọc **đúng** file mà roff đã
    fingerprint, nếu không có thể nối **nhầm máy** (B14/B25).
    """
    if fleet is None:
        return []
    return fleet.ssh_config_args()


def _ssh_base(alias: str) -> list[str]:
    """Argv ssh cho 1 alias — **tái sử dụng** của :mod:`roffkit.sshx`.

    ``sshx`` quản lý ControlMaster: đo thật, lệnh đầu ~7 s, các lệnh sau
    0,9–1,4 s (không có ControlMaster thì mỗi lệnh mất 3,5–7 s qua reverse
    tunnel ⇒ watcher poll nhiều lần sẽ lãng phí hàng chục giây).
    """
    if sshx is not None:
        return sshx.ssh_base(alias)
    return ["ssh", *_config_args(),
            "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", alias]


def _run_ssh(
    alias: str, script: str, timeout: int = 30, stdin: bytes | None = None
) -> tuple[int, str, str]:
    """Chạy 1 script shell trên host qua ssh.

    Đây là **seam duy nhất** cho test: test monkeypatch hàm này, không cần mạng.

    Args:
        alias: alias host.
        script: script POSIX sh, truyền nguyên vẹn làm argv cho remote shell.
        timeout: giới hạn giây.
        stdin: payload gửi kèm (vd tar) — không có thì không gửi gì.

    Returns:
        ``(returncode, stdout, stderr)``.
    """
    cmd = _prefix_nice() + _ssh_base(alias) + [script]
    try:
        proc = subprocess.run(
            cmd,
            input=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=max(1, int(timeout)),
        )
    except subprocess.TimeoutExpired:
        return 124, "", f"ssh timeout sau {timeout}s"
    except FileNotFoundError as exc:
        return 127, "", str(exc)
    return (
        proc.returncode,
        proc.stdout.decode("utf-8", "replace"),
        proc.stderr.decode("utf-8", "replace"),
    )


def _parse_kv(text: str) -> dict[str, str]:
    """Parse output ``KEY=value`` (bỏ qua dòng lạ, vd banner của bridge)."""
    out: dict[str, str] = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key:
            out[key] = value.strip()
    return out


# --------------------------------------------------------------------------
# script remote
# --------------------------------------------------------------------------
_RUN_SH = r"""#!/bin/sh
# Wrapper của job (sinh từ phía local, đặt trong <jobdir>).
set -u
JOBDIR=$(cd "$(dirname "$0")" 2>/dev/null && pwd)
echo $$ > "$JOBDIR/pid"
if [ -f "$JOBDIR/env" ]; then
  set -a
  . "$JOBDIR/env"
  set +a
fi
cd "$ROFF_WORK" 2>/dev/null || cd "$JOBDIR"
# Ưu tiên CPU/I-O của **job trên máy remote** (C9): mặc định `nice 19` +
# I/O `idle` ⇒ job KHÔNG cạnh tranh với postgres/telegram-bot/cloudflared
# của chính người dùng đang chạy trên cùng máy đó.
#   * `nice`/`ionice` kiểm tra **trên host** (`command -v`) ⇒ host không có
#     thì job vẫn chạy bình thường, KHÔNG chết;
#   * `__NICE__`/`__IONICE__` rỗng ⇒ không bọc (nice = 0 / ionice = none);
#   * KHÔNG dùng `exec`: `run.sh` phải còn sống để ghi `$JOBDIR/exit` — sau
#     `exec`, `rc=$?` không bao giờ chạy ⇒ job không bao giờ có exit code.
if [ -n "__NICE__" ] && command -v nice >/dev/null 2>&1; then
  if [ -n "__IONICE__" ] && command -v ionice >/dev/null 2>&1; then
    nice -n __NICE__ ionice -c__IONICE__ -- sh -c "$(cat "$JOBDIR/cmd")"
  else
    nice -n __NICE__ -- sh -c "$(cat "$JOBDIR/cmd")"
  fi
elif [ -n "__IONICE__" ] && command -v ionice >/dev/null 2>&1; then
  ionice -c__IONICE__ -- sh -c "$(cat "$JOBDIR/cmd")"
else
  sh -c "$(cat "$JOBDIR/cmd")"
fi
rc=$?
printf '%s\n' "$rc" > "$JOBDIR/exit"
exit $rc
"""


_LAUNCH_SH = r"""#!/bin/sh
# Script launch (sinh từ phía local, đặt trong <jobdir>).
set -u
JOBDIR=$(cd "$(dirname "$0")" 2>/dev/null && pwd)
OUTDIR=__OUTDIR__
WORKDIR=__WORKDIR__
JOBID=__JOBID__
HOST=__HOST__
CPUS=__CPUS__
MEMMB=__MEMMB__
LAYOUT=__LAYOUT__

BOOT=$(cat /proc/sys/kernel/random/boot_id 2>/dev/null || echo '')
TOKFILE="$HOME/.roff-instance-token"
if [ ! -s "$TOKFILE" ]; then
  umask 077
  head -c 16 /dev/urandom 2>/dev/null | od -An -tx1 | tr -d ' \n' > "$TOKFILE.tmp" 2>/dev/null \
    && mv -f "$TOKFILE.tmp" "$TOKFILE"
fi
TOKEN=$(cat "$TOKFILE" 2>/dev/null || echo '')

if [ ! -d "$WORKDIR" ]; then
  echo "ROFF_ERR=workdir_missing"
  exit 97
fi
mkdir -p "$OUTDIR"

export ROFF_WORK="$WORKDIR"
export ROFF_OUT="$OUTDIR"
export ROFF_JOB="$JOBID"
export ROFF_HOST="$HOST"
export ROFF_CPUS="$CPUS"
export ROFF_MEM_MB="$MEMMB"
export ROFF_LAYOUT="$LAYOUT"
export ROFF_IN_DIR="$WORKDIR"
__EXTRA_EXPORTS__

cd "$WORKDIR" || exit 97
setsid nohup "$JOBDIR/run.sh" > "$JOBDIR/log" 2>&1 < /dev/null &

i=0
while [ "$i" -lt 100 ]; do
  if [ -s "$JOBDIR/pid" ]; then break; fi
  sleep 0.05
  i=$((i + 1))
done
PID=$(head -n1 "$JOBDIR/pid" 2>/dev/null | tr -d '[:space:]')
if [ -n "$PID" ]; then
  sed -i -e "s/\"__ROFF_PID__\"/$PID/" \
         -e "s/\"__ROFF_BOOT_ID__\"/\"$BOOT\"/" \
         -e "s/\"__ROFF_INSTANCE_TOKEN__\"/\"$TOKEN\"/" "$JOBDIR/meta.json"
fi
echo "ROFF_PID=$PID"
echo "ROFF_BOOT_ID=$BOOT"
echo "ROFF_INSTANCE_TOKEN=$TOKEN"
exit 0
"""


def _build_launch_script(
    out_dir: str,
    work_dir: str,
    job: Job,
    cpus: int,
    mem_mb: int,
    layout_root: str,
) -> str:
    """Dựng nội dung ``launch.sh`` cho 1 job."""
    options = job_options(job.id)
    extra: list[str] = []
    if job.shard is not None:
        extra.append(f"export ROFF_SHARD={shlex.quote(str(job.shard))}")
    if job.shards is not None:
        extra.append(f"export ROFF_SHARDS={shlex.quote(str(job.shards))}")
    if job.item is not None:
        extra.append(f"export ROFF_ITEM={shlex.quote(str(job.item))}")
    items = options.get("items")
    if items:
        extra.append(f"export ROFF_ITEMS={shlex.quote(str(items))}")
    if options.get("input_remote"):
        extra.append(f"export ROFF_IN={shlex.quote(str(options['input_remote']))}")
    if job.project:
        extra.append(f"export ROFF_PROJECT={shlex.quote(str(job.project))}")
    if options.get("resume"):
        extra.append("export ROFF_RESUME=1")
    else:
        extra.append("export ROFF_RESUME=0")

    return (
        _LAUNCH_SH.replace("__OUTDIR__", shlex.quote(out_dir))
        .replace("__WORKDIR__", shlex.quote(work_dir))
        .replace("__JOBID__", shlex.quote(job.id))
        .replace("__HOST__", shlex.quote(job.host))
        .replace("__CPUS__", shlex.quote(str(cpus)))
        .replace("__MEMMB__", shlex.quote(str(mem_mb)))
        .replace("__LAYOUT__", shlex.quote(layout_root))
        .replace("__EXTRA_EXPORTS__", "\n".join(extra))
    )


def _tar_bytes(members: dict[str, tuple[bytes, int]]) -> bytes:
    """Đóng gói ``{tên: (nội dung, mode)}`` thành tar trong RAM."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, (payload, mode) in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mode = mode
            info.mtime = int(time.time())
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def _env_file(env: dict[str, str] | None) -> bytes:
    """Nội dung file env (nạp được bằng ``.`` trong shell, giá trị an toàn)."""
    lines = []
    for key, value in (env or {}).items():
        lines.append(f"{key}={shlex.quote(str(value))}")
    return ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")


# --------------------------------------------------------------------------
# launch
# --------------------------------------------------------------------------
def launch(
    host: str,
    job: Job,
    env: dict[str, str] | None = None,
    cpus: int = 0,
    mem_mb: int = 0,
) -> None:
    """Khởi chạy job trên host (1 round-trip ssh).

    Trên host: tạo ``<jobdir>``, ghi ``env`` (mode **600**), ``cmd``,
    ``run.sh``, ``meta.json``; khởi chạy bằng ``setsid nohup`` để **sống sót
    khi ssh rớt**; ghi ``pid`` + ``boot_id`` + ``instance_token`` vào
    ``meta.json`` ngay lúc launch (cần để sau này biết host có restart/wipe
    không, và để huỷ đúng process).

    Args:
        host: alias host.
        job: job cần chạy (``out_remote`` phải trỏ tới ``<jobdir>/out``).
        env: biến môi trường cho job (secret **không** nằm trong argv).
        cpus: số core cấp cho job (``$ROFF_CPUS``).
        mem_mb: ngân sách RAM (``$ROFF_MEM_MB``).

    Raises:
        RoffError: ``EXIT_NO_WORKDIR`` (97) nếu thư mục làm việc không có;
            ``EXIT_UNREACHABLE`` (255) nếu ssh lỗi.
    """
    job_dir = remote_job_dir(job)
    out_dir = str(job.out_remote).rstrip("/")
    work_dir = remote_workdir(job)
    layout_root = os.path.dirname(os.path.dirname(job_dir))

    meta = {
        "job_id": job.id,
        "cmd": job.cmd,
        "host": host,
        "pid": PID_PLACEHOLDER,
        "boot_id": BOOT_ID_PLACEHOLDER,
        "instance_token": INSTANCE_TOKEN_PLACEHOLDER,
        "started": time.time(),
        "cpus": int(cpus),
        "mem_mb": int(mem_mb),
        "project": job.project,
        "workdir": work_dir,
        "out": out_dir,
        "shard": job.shard,
        "shards": job.shards,
        "item": job.item,
    }
    # Chính sách nice/ionice cho **lệnh job chạy trên host** (C9) — chốt ở
    # phía local rồi điền vào `run.sh` (máy remote có thể không có `nice`).
    nice_value, ionice_value = remote_nice_args(host)
    run_sh = _RUN_SH.replace("__NICE__", nice_value).replace("__IONICE__", ionice_value)
    members = {
        "env": (_env_file(env), 0o600),
        "cmd": (str(job.cmd).encode("utf-8"), 0o600),
        "run.sh": (run_sh.encode("utf-8"), 0o700),
        "launch.sh": (
            _build_launch_script(out_dir, work_dir, job, cpus, mem_mb, layout_root).encode(
                "utf-8"
            ),
            0o700,
        ),
        "meta.json": (json.dumps(meta, ensure_ascii=False).encode("utf-8"), 0o644),
    }
    payload = _tar_bytes(members)

    script = (
        f"mkdir -p {shlex.quote(job_dir)} "
        f"&& tar xpf - -C {shlex.quote(job_dir)} "
        f"&& sh {shlex.quote(os.path.join(job_dir, 'launch.sh'))}"
    )
    rc, out, err = _run_ssh(host, script, LAUNCH_TIMEOUT, stdin=payload)
    if rc != 0:
        if "ROFF_ERR=workdir_missing" in out:
            raise RoffError(
                EXIT_NO_WORKDIR,
                "workdir_missing",
                error=f"thư mục làm việc không tồn tại trên host: {work_dir}",
                job=job.id,
                host=host,
                workdir=work_dir,
            )
        raise RoffError(
            EXIT_UNREACHABLE,
            detail=(err or out or f"rc={rc} khi launch job trên {host}").strip(),
            job=job.id,
            host=host,
        )

    data = _parse_kv(out)
    pid_text = str(data.get("ROFF_PID") or "").strip()
    attempt = {
        "attempt": len(job.attempts or []),
        "host": host,
        "pid": int(pid_text) if pid_text.isdigit() else None,
        "boot_id": data.get("ROFF_BOOT_ID") or None,
        "instance_token": data.get("ROFF_INSTANCE_TOKEN") or None,
        "started": time.time(),
        "job_dir": job_dir,
        "workdir": work_dir,
        "failover": bool(job.attempts),
    }
    job.attempts = list(job.attempts or []) + [attempt]
    job.state = "launched"
    save_job(job)


# --------------------------------------------------------------------------
# remote_state
# --------------------------------------------------------------------------
_STATE_SH = r"""set -u
JOBDIR=__JOBDIR__
WORKDIR=__WORKDIR__
if [ -f "$JOBDIR/exit" ]; then
  echo "ROFF_EXIT_FILE=1"
  echo "ROFF_EXIT_CODE=$(head -n1 "$JOBDIR/exit" 2>/dev/null | tr -d '[:space:]')"
else
  echo "ROFF_EXIT_FILE=0"
fi
echo "ROFF_BOOT_ID=$(cat /proc/sys/kernel/random/boot_id 2>/dev/null || echo '')"
echo "ROFF_INSTANCE_TOKEN=$(cat "$HOME/.roff-instance-token" 2>/dev/null || echo '')"
if [ -d "$WORKDIR" ]; then echo "ROFF_WORKDIR_EXISTS=1"; else echo "ROFF_WORKDIR_EXISTS=0"; fi
PID=""
if [ -f "$JOBDIR/pid" ]; then
  PID=$(head -n1 "$JOBDIR/pid" 2>/dev/null | tr -d '[:space:]')
fi
echo "ROFF_PID=$PID"
ALIVE=0
if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then ALIVE=1; fi
echo "ROFF_PID_ALIVE=$ALIVE"
OOM=0
if [ -n "$PID" ] && [ "$ALIVE" = "0" ]; then
  if dmesg 2>/dev/null | grep -q "Killed process $PID"; then OOM=1; fi
fi
echo "ROFF_OOM=$OOM"
"""


def remote_state(host: str, job: Job, timeout: int = 30) -> dict:
    """Trạng thái job trên host — **một** round-trip ssh duy nhất.

    Gom vào 1 lần hỏi: exit file có không (+ exit code), ``boot_id`` hiện tại,
    ``instance_token`` hiện tại, workdir còn không, process còn sống không.

    Args:
        host: alias host.
        job: job cần kiểm tra (so với attempt đã ghi nhận lúc launch).
        timeout: giới hạn giây.

    Returns:
        dict với các khoá ``state``, ``exit``, ``boot_id``, ``instance_token``,
        ``workdir_exists``, ``pid``, ``pid_alive``, ``oom``, ``reachable``,
        ``reason``, ``error``, ``detail``.

    ``state``:
        * ``exited`` — có exit file;
        * ``lost`` — job chắc chắn mất (``reason``: ``host_restarted`` /
          ``host_wiped`` / ``vanished`` / ``oom_killed``);
        * ``running`` — còn chạy;
        * ``unreachable`` — **không xác minh được gì** (chưa được failover!).
    """
    job_dir = remote_job_dir(job)
    work_dir = remote_workdir(job)
    script = (
        _STATE_SH.replace("__JOBDIR__", shlex.quote(job_dir)).replace(
            "__WORKDIR__", shlex.quote(work_dir)
        )
    )
    rc, out, err = _run_ssh(host, script, timeout)
    if rc != 0:
        detail = (err or out or f"rc={rc}").strip()
        return {
            "state": "unreachable",
            "exit": None,
            "boot_id": None,
            "instance_token": None,
            "workdir_exists": False,
            "pid": None,
            "pid_alive": False,
            "oom": False,
            "reachable": False,
            "reason": None,
            "error": "ssh_unreachable" if rc != 124 else "ssh_timeout",
            "detail": detail,
        }

    data = _parse_kv(out)
    exit_file = data.get("ROFF_EXIT_FILE") == "1"
    exit_code: int | None = None
    if exit_file:
        text = str(data.get("ROFF_EXIT_CODE") or "").strip()
        exit_code = int(text) if text.lstrip("-").isdigit() else None
    boot_now = data.get("ROFF_BOOT_ID") or None
    token_now = data.get("ROFF_INSTANCE_TOKEN") or None
    workdir_exists = data.get("ROFF_WORKDIR_EXISTS") == "1"
    pid_text = str(data.get("ROFF_PID") or "").strip()
    pid = int(pid_text) if pid_text.isdigit() else None
    pid_alive = data.get("ROFF_PID_ALIVE") == "1"
    oom = data.get("ROFF_OOM") == "1"

    attempt = job.last_attempt()
    boot_before = (attempt or {}).get("boot_id")
    token_before = (attempt or {}).get("instance_token")

    state = "running"
    reason: str | None = None
    if exit_file:
        state = "exited"
    elif attempt is None:
        state = "queued"
    elif boot_before and boot_now and boot_before != boot_now:
        state, reason = "lost", "host_restarted"
    elif token_before and token_now and token_before != token_now:
        state, reason = "lost", "host_wiped"
    elif not pid_alive:
        state, reason = "lost", "oom_killed" if oom else "vanished"

    return {
        "state": state,
        "exit": exit_code,
        "boot_id": boot_now,
        "instance_token": token_now,
        "workdir_exists": workdir_exists,
        "pid": pid,
        "pid_alive": pid_alive,
        "oom": oom,
        "reachable": True,
        "reason": reason,
        "error": None,
        "detail": None,
    }


# --------------------------------------------------------------------------
# cancel
# --------------------------------------------------------------------------
_CANCEL_SH = r"""set -u
JOBDIR=__JOBDIR__
KILLPID=__KILLPID__
if [ -z "$KILLPID" ] && [ -f "$JOBDIR/pid" ]; then
  KILLPID=$(head -n1 "$JOBDIR/pid" 2>/dev/null | tr -d '[:space:]')
fi
KILLED=0
if [ -n "$KILLPID" ]; then
  CMDLINE=$(tr '\0' ' ' < "/proc/$KILLPID/cmdline" 2>/dev/null || echo '')
  case "$CMDLINE" in
    *"$JOBDIR"*) ;;
    *) CMDLINE="" ;;
  esac
  if [ -n "$CMDLINE" ]; then
    __KILL__
    i=0
    while [ "$i" -lt 20 ]; do
      kill -0 "$KILLPID" 2>/dev/null || break
      sleep 0.25
      i=$((i + 1))
    done
    if kill -0 "$KILLPID" 2>/dev/null; then
      __KILLKILL__
    fi
    KILLED=1
  fi
fi
echo "ROFF_PID=$KILLPID"
echo "ROFF_KILLED=$KILLED"
if [ -n "$KILLPID" ] && kill -0 "$KILLPID" 2>/dev/null; then
  echo "ROFF_PID_ALIVE=1"
else
  echo "ROFF_PID_ALIVE=0"
fi
"""


def cancel(host: str, job: Job) -> dict:
    """Huỷ job trên host.

    Kill **process group** của job theo **pid đã ghi nhận lúc launch** —
    **không bao giờ** ``pkill`` theo pattern (bug R19), và kiểm tra
    ``/proc/<pid>/cmdline`` để không kill nhầm một pid đã tái sinh.

    Args:
        host: alias host.
        job: job cần huỷ.

    Returns:
        dict: ``{"ok", "killed", "pid", "detail"}``.
    """
    job_dir = remote_job_dir(job)
    attempt = job.last_attempt() or {}
    pid = attempt.get("pid")
    pid_text = str(pid).strip() if pid else ""

    if pid_text.isdigit():
        kill_cmd = (
            f"kill -TERM -{pid_text} 2>/dev/null || kill -TERM {pid_text} 2>/dev/null || true"
        )
        kill_kill_cmd = (
            f"kill -KILL -{pid_text} 2>/dev/null || kill -KILL {pid_text} 2>/dev/null || true"
        )
    else:
        kill_cmd = (
            'kill -TERM -"$KILLPID" 2>/dev/null || kill -TERM "$KILLPID" 2>/dev/null || true'
        )
        kill_kill_cmd = (
            'kill -KILL -"$KILLPID" 2>/dev/null || kill -KILL "$KILLPID" 2>/dev/null || true'
        )

    script = (
        _CANCEL_SH.replace("__JOBDIR__", shlex.quote(job_dir))
        .replace("__KILLPID__", shlex.quote(pid_text))
        .replace("__KILLKILL__", kill_kill_cmd)
        .replace("__KILL__", kill_cmd)
    )
    rc, out, err = _run_ssh(host, script, 60)
    data = _parse_kv(out)
    killed = data.get("ROFF_KILLED") == "1"

    job.state = "cancelled"
    job.exit_code = EXIT_CANCELLED
    save_job(job)

    return {
        "ok": rc == 0 and killed,
        "killed": killed,
        "pid": int(pid_text) if pid_text.isdigit() else None,
        "detail": (err or "").strip() or None,
        "host": host,
        "job": job.id,
    }


# --------------------------------------------------------------------------
# log
# --------------------------------------------------------------------------
def read_log(
    host: str, job: Job, tail: int | None = None, grep: str | None = None
) -> str:
    """Đọc log job trên host (stdout+stderr).

    Args:
        host: alias host.
        job: job cần đọc log.
        tail: chỉ lấy N dòng cuối.
        grep: chỉ lấy dòng khớp pattern.
    """
    job_dir = remote_job_dir(job)
    parts = []
    if tail is not None:
        parts.append(f"tail -n {int(tail)}")
    if grep:
        parts.append(f"grep -F -- {shlex.quote(str(grep))}")
    if not parts:
        parts.append("cat")
    pipeline = " | ".join(parts)
    script = f"{pipeline} {shlex.quote(os.path.join(job_dir, 'log'))} 2>/dev/null"
    rc, out, err = _run_ssh(host, script, 60)
    if rc != 0:
        return ""
    return out
