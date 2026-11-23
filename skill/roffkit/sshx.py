"""Lớp ssh trung tâm: một chỗ duy nhất để chạy lệnh trên host.

Tách ra riêng để:
  * **một** chỗ quyết định ``nice``/``ionice`` (máy local phải nhẹ — mục tiêu
    chính của skill);
  * **một** chỗ quản lý ControlMaster (đo thật: lần đầu 4,9 s, lần sau 1,3 s —
    mỗi lệnh ssh mất ~3,5 s nên phải tiết kiệm);
  * **một** chỗ phân loại lỗi ssh (auth bị từ chối ≠ host chết ≠ bridge bận);
  * **một** chỗ bảo đảm secret không bao giờ nằm trong argv.

Module này không in gì ra stdout.
"""
from __future__ import annotations

import hashlib
import os
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

try:
    from roffkit.errors import EXIT_UNREACHABLE, RoffError
except ImportError:  # pragma: no cover
    from .errors import EXIT_UNREACHABLE, RoffError

DEFAULT_TIMEOUT = 60.0
CONNECT_TIMEOUT = 15
SERVER_ALIVE_INTERVAL = 30
SERVER_ALIVE_COUNT_MAX = 5
CONTROL_PERSIST = 300

#: ngưỡng phân biệt "chết" với "chậm"
SLOW_HOST_SECONDS = 120.0


def state_dir() -> Path:
    """Thư mục state của roff (mặc định ``~/.local/state/roff``)."""
    env = os.environ.get("ROFF_STATE_DIR")
    base = Path(env) if env else Path.home() / ".local" / "state" / "roff"
    return base


def cm_dir() -> Path:
    """Thư mục chứa socket ControlMaster."""
    return state_dir() / "cm"


#: ``sockaddr_un.sun_path`` trên Linux dài tối đa 108 byte (kể cả NUL).
#:
#: OpenSSH có hai lỗi liên quan tới giới hạn này:
#:
#: 1. ``ControlPath too long (>= 108 bytes)`` — chết ngay khi dựng master.
#: 2. ``unix_listener: path ... too long for Unix domain socket`` — khi socket
#:    cũ còn sót, OpenSSH **không xoá** mà thử bind lại với một hậu tố ngẫu
#:    nhiên dài tới 17 byte. Nếu đường dẫn gốc đã sát 108 byte thì bản có hậu
#:    tố vượt giới hạn và ssh chết với thông báo gây hiểu nhầm.
#:
#: Nên ta giữ đường dẫn **≤ 88 byte** — vừa để bản có hậu tố (88 + 17 = 105)
#: vẫn vừa khít giới hạn.
UNIX_SOCKET_MAX_PATH = 88


def _short_alias(alias: str) -> str:
    """Tên file an toàn cho alias."""
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in alias)


def control_path(alias: str) -> Path:
    """Đường dẫn socket ControlMaster cho một alias.

    Luôn trả về đường dẫn **≤ :data:`UNIX_SOCKET_MAX_PATH` byte**. Nếu đường
    dẫn tự nhiên quá dài (ví dụ ``$ROFF_STATE_DIR`` nằm sâu trong ``/tmp``),
    dùng đường dẫn ngắn có hash — vẫn **tất định**, nên cùng một alias +
    state dir luôn dùng chung một socket, không mất khả năng tái dùng master.
    """
    safe = _short_alias(alias)
    natural = cm_dir() / f"{safe}.sock"
    if len(str(natural)) <= UNIX_SOCKET_MAX_PATH:
        return natural

    digest = hashlib.sha256(str(natural).encode("utf-8")).hexdigest()[:12]
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/roff-{os.getuid()}"
    fallback = Path(runtime) / f"cm-{digest}-{safe}.sock"
    if len(str(fallback)) <= UNIX_SOCKET_MAX_PATH:
        return fallback

    # Vẫn dài (runtime dir kỳ lạ) → cắt bớt phần tên alias.
    keep = UNIX_SOCKET_MAX_PATH - len(str(Path(runtime) / f"cm-{digest}-")) - len(".sock")
    return Path(runtime) / f"cm-{digest}-{safe[:max(1, keep)]}.sock"


def ensure_cm_dir() -> None:
    """Tạo thư mục chứa socket (ssh sẽ không tự tạo)."""
    path = control_path("x").parent
    try:
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, 0o700)
    except OSError:
        pass


def clean_stale_socket(alias: str) -> bool:
    """Xoá socket ControlMaster còn sót nếu master đã chết.

    OpenSSH **không** xoá socket cũ trước khi bind. Socket sót lại (agent bị
    kill giữa lúc dựng master) làm ssh thử bind lại với hậu tố ngẫu nhiên —
    và nếu đường dẫn sát giới hạn 108 byte thì ssh chết với thông báo
    "unix_listener: path ... too long" dù đường dẫn gốc vẫn vừa.

    ``ssh -O check`` chỉ nối socket **local** (không đi qua mạng) nên rẻ.
    Trả ``True`` nếu đã xoá.

    Không bao giờ đụng vào socket của tiến trình khác: chỉ xoá socket **của
    roff** (trong thư mục cm của roff) và **chỉ khi** không còn master nào
    sống trên nó.
    """
    path = Path(control_path(alias))
    try:
        if not path.exists():
            return False
    except OSError:
        return False

    # Master còn sống không? (nối socket local, ~vài ms)
    try:
        check = subprocess.run(
            ["ssh", "-o", f"ControlPath={path}", "-O", "check", alias],
            capture_output=True, text=True, timeout=10,
        )
        if check.returncode == 0:
            return False
    except (OSError, subprocess.SubprocessError):
        pass

    try:
        path.unlink()
        return True
    except OSError:
        return False


class RunResult(NamedTuple):
    """Kết quả một lệnh remote.

    Là ``NamedTuple`` nên dùng được cả hai kiểu:

    * ``rc, out, err = sshx.run_shell(host, script)``  (theo hợp đồng API §6)
    * ``result.rc`` / ``result.stdout`` / ``result.stderr``
    """

    rc: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.rc == 0

@dataclass
class HostError(RoffError):
    """Lỗi khi không với được host. ``reason`` phân biệt các nguyên nhân.

    * ``auth_refused`` — key bị từ chối (lỗi phía người dùng, KHÔNG phải host chết)
    * ``host_unreachable`` — host chết / mất mạng
    * ``bridge_busy`` — bridge chỉ cho một phiên (429 / "already active")
    * ``timeout`` — quá thời gian
    """

    def __init__(self, reason: str, detail: str, **fields):
        super().__init__(EXIT_UNREACHABLE, reason=reason, detail=detail, **fields)
        self.reason = reason


def classify_ssh_error(stderr: str, rc: int) -> str:
    """Phân loại lỗi ssh thành reason.

    Thứ tự kiểm tra quan trọng: ``Permission denied`` phải được nhận ra **trước**
    mọi kiểu lỗi khác, vì đó là lỗi phía người dùng (key bị từ chối), không phải
    host chết — báo sai thì agent sẽ đi tìm host khác trong khi vấn đề là key.
    """
    text = (stderr or "").lower()
    if "permission denied" in text or "publickey" in text or "host key verification failed" in text:
        return "auth_refused"
    if "429" in text or "already active" in text or "too many requests" in text:
        return "bridge_busy"
    if "timed out" in text or "timeout" in text:
        return "timeout"
    if "connection refused" in text or "connection reset" in text or "no route to host" in text:
        return "host_unreachable"
    if "could not resolve" in text or "name or service not known" in text:
        return "host_unreachable"
    if rc == 255:
        return "host_unreachable"
    return "host_unreachable"


def ssh_config_path() -> str:
    """Đường dẫn ``~/.ssh/config`` **thay thế** (``$ROFF_SSH_CONFIG``), hoặc "".

    Rỗng ⇒ dùng config mặc định của ssh (``~/.ssh/config``).

    Cần thiết vì ``fleet.parse_ssh_config()`` đọc file thay thế này: nếu ssh
    thật vẫn đọc ``~/.ssh/config`` thì alias trùng tên sẽ trỏ sang **máy
    khác** — roff lên kế hoạch trên máy A nhưng lệnh chạy trên máy B.
    """
    env = os.environ.get("ROFF_SSH_CONFIG")
    if not env:
        return ""
    return os.path.abspath(os.path.expanduser(env))


def ssh_config_args() -> list[str]:
    """Tham số ``-F <path>`` cho ssh (rỗng nếu không có config thay thế)."""
    path = ssh_config_path()
    return ["-F", path] if path else []


def ssh_base(alias: str, timeout: float = DEFAULT_TIMEOUT) -> list[str]:
    """Dựng argv của ssh cho một alias.

    Luôn có ``BatchMode=yes`` — host cần mật khẩu sẽ **fail** thay vì treo chờ
    nhập tay. Có ``ControlMaster=auto`` để các lệnh sau tái dùng kết nối.
    """
    ensure_cm_dir()
    # Socket master còn sót (agent bị kill giữa lúc dựng master) làm ssh thử
    # bind lại với hậu tố ngẫu nhiên và chết với thông báo sai lệch.
    # `ssh -O check` chỉ nối socket local nên rẻ, và chỉ chạy khi socket tồn tại.
    clean_stale_socket(alias)
    return [
        "ssh",
        *ssh_config_args(),
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={CONNECT_TIMEOUT}",
        "-o", f"ServerAliveInterval={SERVER_ALIVE_INTERVAL}",
        "-o", f"ServerAliveCountMax={SERVER_ALIVE_COUNT_MAX}",
        "-o", "ControlMaster=auto",
        "-o", f"ControlPath={control_path(alias)}",
        "-o", f"ControlPersist={CONTROL_PERSIST}",
        "-o", "LogLevel=ERROR",
        alias,
    ]


def _prefix() -> list[str]:
    """Tiền tố nice/ionice — mọi tiến trình local của roff chạy nhẹ."""
    nice = os.environ.get("ROFF_NICE", "19")
    ionice = os.environ.get("ROFF_IONICE", "idle")
    prefix: list[str] = []
    if ionice:
        prefix += ["ionice", "-c", ionice]
    if nice:
        prefix += ["nice", "-n", nice]
    return prefix


def run_shell(host: str, script: str, timeout: float = DEFAULT_TIMEOUT,
              env: dict[str, str] | None = None,
              check: bool = False) -> RunResult:
    """Chạy một script shell trên host, trả ``(rc, stdout, stderr)``.

    Vài lệnh được gộp vào **một** round-trip: mỗi lệnh ssh mất ~3,5 s qua reverse
    tunnel nên gom là điều bắt buộc, không phải tối ưu.

    :param check: ``True`` ⇒ lỗi ssh ném :class:`HostError` với reason đã phân loại.
    """
    cmd = _prefix() + ssh_base(host) + [script]
    run_env = dict(os.environ)
    if env:
        run_env.update(env)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, env=run_env)
    except subprocess.TimeoutExpired as exc:
        result = RunResult(rc=255, stdout=_as_text(exc.stdout),
                           stderr=f"timeout sau {timeout}s")
        if check:
            raise HostError("timeout", result.stderr or "timeout") from exc
        return result
    result = RunResult(rc=proc.returncode, stdout=proc.stdout or "", stderr=proc.stderr or "")
    if check and result.rc != 0:
        reason = classify_ssh_error(result.stderr, result.rc)
        raise HostError(reason, result.stderr.strip() or f"rc={result.rc}")
    return result


def _as_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def run_argv(host: str, argv: list[str], timeout: float = DEFAULT_TIMEOUT,
             check: bool = False) -> RunResult:
    """Chạy một lệnh có argv (vd ``rsync``) trên host.

    `argv` phải **không** chứa secret: chúng sẽ hiện trong ``ps`` trên host.
    """
    cmd = _prefix() + ssh_base(host)[:-1] + [host] + argv
    run_env = dict(os.environ)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, env=run_env)
    except subprocess.TimeoutExpired as exc:
        result = RunResult(rc=255, stdout=_as_text(exc.stdout),
                           stderr=f"timeout sau {timeout}s")
        if check:
            raise HostError("timeout", result.stderr or "timeout") from exc
        return result
    result = RunResult(rc=proc.returncode, stdout=proc.stdout or "", stderr=proc.stderr or "")
    if check and result.rc != 0:
        raise HostError(classify_ssh_error(result.stderr, result.rc),
                        result.stderr.strip() or f"rc={result.rc}")
    return result


def master_alive(alias: str) -> bool:
    """ControlMaster của alias còn sống không."""
    proc = subprocess.run(
        ["ssh", "-o", f"ControlPath={control_path(alias)}", "-O", "check", alias],
        capture_output=True, text=True, timeout=15)
    return proc.returncode == 0


def close_master(alias: str) -> bool:
    """Đóng ControlMaster của alias (dùng khi cần dọn)."""
    proc = subprocess.run(
        ["ssh", "-o", f"ControlPath={control_path(alias)}", "-O", "exit", alias],
        capture_output=True, text=True, timeout=15)
    return proc.returncode == 0


def quote(value: str) -> str:
    """Quote một giá trị để nhúng vào script shell."""
    return shlex.quote(value)


def now() -> float:
    """``time.time()`` — điểm neo thời gian dùng chung trong ledger."""
    return time.time()
