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

try:  # B25: nguồn sự thật duy nhất cho đường dẫn ssh-config
    from roffkit import fleet  # type: ignore
except ImportError:  # pragma: no cover - chạy module lẻ
    from . import fleet  # type: ignore

try:  # B34: chính sách nice/ionice — MỘT nguồn sự thật (dùng chung 5 module)
    from roffkit.config import local_nice_prefix  # type: ignore
except ImportError:  # pragma: no cover - chạy module lẻ
    from .config import local_nice_prefix  # type: ignore

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


def _fingerprint(alias: str) -> str:
    """Fingerprint tham số ssh của alias (8 hex) — xem :func:`fleet.ssh_fingerprint`.

    Lỗi đọc config ⇒ fallback về hash của chính alias (vẫn tất định, và
    **không** làm mất khả năng phân biệt hai alias khác nhau).
    """
    try:
        try:
            from roffkit import fleet  # type: ignore
        except ImportError:  # pragma: no cover - chạy module lẻ
            from . import fleet  # type: ignore
        return fleet.ssh_fingerprint(alias)
    except Exception:  # pragma: no cover - config hỏng thì vẫn phải chạy được
        return hashlib.sha256(str(alias).encode("utf-8")).hexdigest()[:8]


def control_path(alias: str) -> Path:
    """Đường dẫn socket ControlMaster cho một alias.

    Tên socket **chứa fingerprint** của tham số ssh đã resolve
    (``<alias>-<8 hex>.sock``) — sửa bug **B14**: trước đây socket khoá theo
    *alias* ⇒ đổi ``~/.ssh/config`` (đổi port, thay máy) vẫn tái dùng master
    cũ ⇒ roff nói chuyện với **máy cũ**. Đổi config ⇒ fingerprint đổi ⇒
    socket khác ⇒ master mới.

    Cùng alias + cùng config ⇒ cùng socket (tái dùng master vẫn hoạt động).

    Luôn trả về đường dẫn **≤ :data:`UNIX_SOCKET_MAX_PATH` byte**. Nếu đường
    dẫn tự nhiên quá dài (ví dụ ``$ROFF_STATE_DIR`` nằm sâu trong ``/tmp``),
    dùng đường dẫn ngắn có hash — vẫn **tất định**, nên cùng một alias +
    state dir luôn dùng chung một socket, không mất khả năng tái dùng master.
    """
    safe = _short_alias(alias)
    natural = cm_dir() / f"{safe}-{_fingerprint(alias)}.sock"
    if len(str(natural)) <= UNIX_SOCKET_MAX_PATH:
        return natural

    digest = hashlib.sha256(str(natural).encode("utf-8")).hexdigest()[:12]
    runtime = _runtime_dir()
    fallback = runtime / f"cm-{digest}-{safe}.sock"
    if len(str(fallback)) <= UNIX_SOCKET_MAX_PATH:
        return fallback

    # Vẫn dài (runtime dir kỳ lạ) → cắt bớt phần tên alias.
    keep = UNIX_SOCKET_MAX_PATH - len(str(Path(runtime) / f"cm-{digest}-")) - len(".sock")
    return Path(runtime) / f"cm-{digest}-{safe[:max(1, keep)]}.sock"


def ensure_cm_dir(alias: str | None = None) -> None:
    """Tạo thư mục chứa socket — ``ssh`` **không** tự tạo.

    Thiếu thư mục thì ``ssh`` chết ngay với
    ``unix_listener: cannot bind to path ...: No such file or directory``
    ⇒ **mọi** nơi dựng argv có ``ControlPath`` đều phải gọi hàm này trước.

    Args:
        alias: alias sắp dùng — khi state dir quá sâu, đường dẫn socket rơi về
            thư mục runtime, nên cần tạo thêm thư mục đó.
    """
    paths = {cm_dir(), control_path(alias or "x").parent}
    for path in paths:
        try:
            path.mkdir(parents=True, exist_ok=True)
            os.chmod(path, 0o700)
        except OSError:
            pass


def _runtime_dir() -> Path:
    """Thư mục runtime dùng cho socket khi state dir quá sâu."""
    return Path(os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/roff-{os.getuid()}")


def _fallback_suffix(alias: str) -> str:
    """Hậu tố tên socket khi phải dùng đường dẫn ngắn (thư mục runtime).

    Hậu tố **không** đổi theo fingerprint ⇒ vẫn nhận ra được socket của
    fingerprint cũ của cùng alias.
    """
    safe = _short_alias(alias)
    runtime = _runtime_dir()
    keep = UNIX_SOCKET_MAX_PATH - len(str(runtime / f"cm-{'0' * 12}-")) - len(".sock")
    return f"-{safe[:max(1, keep)]}.sock"


def _socket_candidates(alias: str) -> list[Path]:
    """Socket ControlMaster của **alias** này (chỉ trong thư mục của roff).

    Gồm socket hiện tại (``<alias>-<fingerprint>.sock``), socket của
    fingerprint **cũ** (``<alias>-<hex cũ>.sock``), socket kiểu cũ
    (``<alias>.sock``) và bản rút gọn trong thư mục runtime (khi state dir quá
    sâu). Socket của alias **khác** không bao giờ bị đụng.
    """
    safe = _short_alias(alias)
    out: list[Path] = []

    directory = cm_dir()
    try:
        names = os.listdir(directory)
    except OSError:
        names = []
    for name in names:
        if not name.endswith(".sock"):
            continue
        if name == f"{safe}.sock" or name.startswith(f"{safe}-"):
            out.append(directory / name)

    runtime = _runtime_dir()
    suffix = _fallback_suffix(alias)
    try:
        runtime_names = os.listdir(runtime)
    except OSError:
        runtime_names = []
    for name in runtime_names:
        if name.startswith("cm-") and name.endswith(suffix):
            out.append(runtime / name)

    return out


def _remove_if_dead(path: Path, alias: str) -> bool:
    """Xoá socket nếu **không còn master nào sống** trên nó.

    ``ssh -O check`` chỉ nối socket **local** (không đi qua mạng) nên rẻ.
    """
    try:
        if not path.exists():
            return False
    except OSError:
        return False

    # Master còn sống không? (nối socket local, ~vài ms)
    try:
        check = subprocess.run(
            ["ssh", *ssh_config_args(),
             "-o", f"ControlPath={path}", "-O", "check", alias],
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


def clean_stale_socket(alias: str) -> bool:
    """Xoá socket ControlMaster còn sót nếu master đã chết.

    OpenSSH **không** xoá socket cũ trước khi bind. Socket sót lại (agent bị
    kill giữa lúc dựng master) làm ssh thử bind lại với hậu tố ngẫu nhiên —
    và nếu đường dẫn sát giới hạn 108 byte thì ssh chết với thông báo
    "unix_listener: path ... too long" dù đường dẫn gốc vẫn vừa.

    **W5c (B14)**: dọn luôn socket của **fingerprint cũ** (đổi
    ``~/.ssh/config`` ⇒ socket mới khác tên) — nếu không, socket cũ nằm lại
    trong thư mục cm và làm rối. Chỉ dọn socket **của roff** (trong
    ``<state>/cm/``) và **chỉ khi** không còn master nào sống trên nó.

    Trả ``True`` nếu đã xoá ít nhất một socket.
    """
    removed = False
    # Socket hiện tại (có thể nằm ở thư mục runtime khi state dir quá sâu —
    # xem control_path) + các socket cũ còn nằm trong thư mục cm của roff.
    current = Path(control_path(alias))
    if _remove_if_dead(current, alias):
        removed = True
    for path in _socket_candidates(alias):
        if path == current:
            continue
        if _remove_if_dead(path, alias):
            removed = True
    return removed


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
    """Đường dẫn ssh-config mà roff truyền cho ssh (``-F``), hoặc "".

    Nguồn sự thật duy nhất: :func:`roffkit.fleet.ssh_config_path` (⇒
    :func:`roffkit.fleet.ssh_config_file`). Cùng một đường dẫn mà
    ``fleet`` dùng để *fingerprint* ⇒ ssh đọc **đúng** file roff đã đọc.

    Vì sao phải truyền ``-F`` **tường minh** (B25): OpenSSH mở rộng ``~`` theo
    **passwd entry** (``getpwuid``), **KHÔNG** theo ``$HOME`` — ssh mặc định
    đọc ``~/.ssh/config`` của *passwd home*, còn roff fingerprint
    ``$HOME/.ssh/config``. Nếu hai file khác nhau: ssh đọc **file A**, roff
    fingerprint **file B** ⇒ đổi config không đổi fingerprint ⇒ tái dùng
    ControlMaster cũ ⇒ roff nói chuyện với **máy cũ** (bug B14).

    Rỗng ⇒ không có file config nào để truyền (ssh tự lo, như cũ).
    """
    return fleet.ssh_config_path()


def ssh_config_args() -> list[str]:
    """Tham số ``-F <path>`` cho ssh (rỗng nếu không có config nào).

    Luôn truyền ``-F`` khi có đường dẫn — xem :func:`ssh_config_path` để biết
    vì sao phải truyền tường minh (OpenSSH đọc ``~/.ssh/config`` theo *passwd
    home*, không theo ``$HOME``).

    Nguồn sự thật duy nhất: :func:`roffkit.fleet.ssh_config_args`.
    """
    return fleet.ssh_config_args()


def ssh_base(alias: str, timeout: float = DEFAULT_TIMEOUT) -> list[str]:
    """Dựng argv của ssh cho một alias.

    Luôn có ``BatchMode=yes`` — host cần mật khẩu sẽ **fail** thay vì treo chờ
    nhập tay. Có ``ControlMaster=auto`` để các lệnh sau tái dùng kết nối.
    """
    ensure_cm_dir(alias)
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
    """Tiền tố nice/ionice — mọi tiến trình local của roff chạy nhẹ.

    **MỘT nguồn sự thật** (B34): gọi :func:`roffkit.config.local_nice_prefix`
    ⇒ cùng tiền tố với probe/tunnel/job/transfer, và ``ROFF_NICE`` /
    ``ROFF_IONICE`` có tác dụng với **mọi** đường (kể cả rsync/tar).
    """
    return local_nice_prefix()


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
        ["ssh", *ssh_config_args(),
         "-o", f"ControlPath={control_path(alias)}", "-O", "check", alias],
        capture_output=True, text=True, timeout=15)
    return proc.returncode == 0


def close_master(alias: str) -> bool:
    """Đóng ControlMaster của alias (dùng khi cần dọn)."""
    proc = subprocess.run(
        ["ssh", *ssh_config_args(),
         "-o", f"ControlPath={control_path(alias)}", "-O", "exit", alias],
        capture_output=True, text=True, timeout=15)
    return proc.returncode == 0


def quote(value: str) -> str:
    """Quote một giá trị để nhúng vào script shell."""
    return shlex.quote(value)


def now() -> float:
    """``time.time()`` — điểm neo thời gian dùng chung trong ledger."""
    return time.time()
