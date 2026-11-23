"""Kho input dùng chung trên host — mỗi (host, nguồn local) chỉ đẩy **1 lần**.

Băng thông local→host trên fleet Muse chỉ **0,5–1 MB/s** (đo thật 23/11/2026),
còn CPU host thì không phải nút thắt. Bản cũ đẩy **toàn bộ** input vào
``workdir`` **riêng cho từng job** ⇒ ``--each`` 10 item trên 4 host = 10 lần
đẩy 10 GB ⇒ giờ.

Kho nằm dưới ``layout_root``::

    {layout_root}/inputs/{key}/            # bản copy dùng chung của 1 nguồn
    {layout_root}/inputs/{key}.complete    # marker JSON (CẠNH, không nằm trong {key}/)
    {layout_root}/inputs/{key}.lock/       # mutex tạm (mkdir), xoá sau khi xong
    {layout_root}/inputs/{key}.lock/owner  # ai đang giữ lock (pid + máy)

``key = sha256(abspath(nguồn local))[:16]`` ⇒ cùng một nguồn trên cùng một host
luôn trỏ tới cùng một bản copy ⇒ **N job trên cùng host chỉ đẩy 1 lần**.

Marker ghi ``size`` + ``mtime`` của nguồn local:

* không đổi ⇒ **dùng lại**, không truyền lại (điều kiện quan trọng nhất);
* đổi ⇒ dọn bản copy cũ rồi đẩy lại.

Cây input trong kho bị khoá **read-only** (``chmod -R a-w``) để job nào sửa
input của job khác **fail loudly** thay vì hỏng dữ liệu âm thầm. Marker nằm
ngoài ``{key}/`` nên không bị ảnh hưởng.

Module này không in ra stdout (quy tắc chung của roff).
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import shlex
import shutil
import socket
import sys
import tempfile
import threading
import time
import uuid
from typing import Any

from . import sshx
from . import transfer as transfer_mod
from .errors import EXIT_INPUT, EXIT_UNREACHABLE, RoffError

__all__ = [
    "shared_key",
    "shared_root",
    "shared_dir",
    "shared_marker",
    "shared_lock",
    "shared_rel",
    "shared_remote_path",
    "ensure_shared_input",
    "gc_shared_store",
]


#: Số giây chờ marker khi người khác đang đẩy cùng một nguồn.
MARKER_WAIT_S = 60.0

#: Lock quá hạn (giây) ⇒ coi là chết (agent bị kill) ⇒ dọn rồi chiếm lại.
#:
#: **PHẢI nhỏ hơn** :data:`MARKER_WAIT_S`. Nếu không, nhánh "lock chết" không
#: bao giờ kịp chạy trong 1 lần chờ (mỗi lần thử chỉ chờ ``MARKER_WAIT_S``
#: giây) ⇒ phải quay vòng ~30 lần (tức **~30 phút**) mới dọn được 1 lock mồ
#: côi ⇒ mọi lần đẩy sau đó đều fail với ``shared_input_busy``.
LOCK_STALE_S = 30.0

#: Lock quá hạn *cứng* (giây) ⇒ dọn luôn, kể cả khi chủ lock còn sống (chủ bị
#: kẹt / máy treo) ⇒ không bao giờ kẹt vĩnh viễn.
LOCK_HARD_STALE_S = 900.0

#: Số lần thử nhả lock (host flap ⇒ lệnh ssh hỏng lần đầu).
RELEASE_ATTEMPTS = 3

#: Giãn cách giữa 2 lần thử nhả lock (giây).
RELEASE_RETRY_DELAY_S = 1.0

#: Chu kỳ poll marker (giây).
POLL_INTERVAL = 1.0

#: Chu kỳ **chạm lock** (giây) trong lúc đang đẩy.
#:
#: Một lần đẩy có thể kéo dài **vài giờ** (băng thông local→host trên fleet
#: Muse chỉ 0,5–1 MB/s) ⇒ nếu không chạm lock thì mtime của nó đứng yên ⇒
#: sau :data:`LOCK_STALE_S` giây người khác tưởng lock mồ côi và xoá ⇒
#: **đẩy trùng** (2 job cùng đẩy 1 nguồn, ghi đè lên nhau). Nhịp chạm
#: (10 s) **nhanh hơn nhiều** ngưỡng stale (30 s) ⇒ lock còn sống thì không
#: bao giờ bị tưởng là mồ côi.
LOCK_HEARTBEAT_S = 10.0

#: Timeout mặc định của 1 lần đẩy (giây).
DEFAULT_TIMEOUT = 3600.0

#: Tuổi tối đa của 1 bản copy trong kho trước khi bị dọn (giây) — 7 ngày.
DEFAULT_STORE_MAX_AGE_S = 7 * 24 * 3600


# --------------------------------------------------------------------------
# đường dẫn trong kho
# --------------------------------------------------------------------------
def _abs(path: str) -> str:
    """Đường dẫn local absolute (resolve ``~``)."""
    return os.path.abspath(os.path.expanduser(str(path)))


def shared_key(local_path: str) -> str:
    """Khoá của 1 nguồn local = ``sha256(abspath)[:16]``.

    Ổn định theo **đường dẫn** (không theo nội dung): cùng một nguồn trên mọi
    host đều cho cùng một key ⇒ kho của host nào cũng trỏ tới cùng một vị trí.
    """
    absolute = _abs(local_path)
    return hashlib.sha256(absolute.encode("utf-8")).hexdigest()[:16]


def shared_root(layout_root: str) -> str:
    """Thư mục gốc kho input trên host: ``{layout_root}/inputs``."""
    return f"{str(layout_root).rstrip('/')}/inputs"


def shared_dir(layout_root: str, local_path: str) -> str:
    """Thư mục chứa bản copy dùng chung của 1 nguồn: ``{root}/{key}``."""
    return f"{shared_root(layout_root)}/{shared_key(local_path)}"


def shared_marker(layout_root: str, local_path: str) -> str:
    """Marker (JSON) của bản copy: ``{root}/{key}.complete``.

    Nằm **CẠNH** thư mục bản copy (không nằm trong ``{key}/``) để việc khoá
    cây input read-only không ảnh hưởng tới marker.
    """
    return f"{shared_root(layout_root)}/{shared_key(local_path)}.complete"


def shared_lock(layout_root: str, local_path: str) -> str:
    """Mutex tạm khi đẩy: ``{root}/{key}.lock`` (thư mục — ``mkdir`` là atomic)."""
    return f"{shared_root(layout_root)}/{shared_key(local_path)}.lock"


def shared_rel(local_path: str) -> str:
    """Vị trí của nguồn **bên trong** ``{key}/``.

    Thư mục ⇒ ``""`` (nội dung thư mục nằm thẳng trong ``{key}/``); file ⇒
    ``basename`` (file nằm trong ``{key}/{basename}``).
    """
    absolute = _abs(local_path)
    if os.path.isdir(absolute):
        return ""
    return os.path.basename(absolute)


def shared_remote_path(layout_root: str, key: str, rel: str) -> str:
    """Đường dẫn remote trong kho: ``{root}/{key}`` (+ ``/{rel}`` nếu có)."""
    base = f"{shared_root(layout_root)}/{key}"
    return f"{base}/{rel}" if rel else base


# --------------------------------------------------------------------------
# seam cho test
# --------------------------------------------------------------------------
def _run_shell(
    host: str, script: str, timeout: float = 60.0
) -> tuple[int, str, str]:
    """Chạy 1 script trên host; trả ``(rc, stdout, stderr)``.

    Đây là **seam duy nhất** cho test: test monkeypatch hàm này, không cần mạng.
    """
    result = sshx.run_shell(host, script, timeout=timeout)
    return (int(result.rc), result.stdout or "", result.stderr or "")


def _sleep(seconds: float) -> None:
    """Ngủ (seam cho test)."""
    time.sleep(seconds)


def run_shell(host: str, script: str, timeout: float = 60.0) -> tuple[int, str, str]:
    """Chạy 1 script sh trên host — bọc công khai của :func:`_run_shell`.

    Watcher dùng hàm này (vd tạo thư mục làm việc trên host) ⇒ test chỉ cần
    monkeypatch ``_run_shell`` (seam duy nhất) là đủ, không cần mạng.
    """
    return _run_shell(host, script, timeout=timeout)


# --------------------------------------------------------------------------
# chủ lock + cảnh báo
# --------------------------------------------------------------------------
def _warn(message: str) -> None:
    """Ghi cảnh báo ra **stderr** (stdout chỉ dành cho JSON của CLI)."""
    try:
        sys.stderr.write("roff: " + str(message) + "\n")
        sys.stderr.flush()
    except Exception:  # pragma: no cover - stderr hỏng
        pass


def _local_host() -> str:
    """Tên máy local (để biết lock do tiến trình local nào giữ)."""
    try:
        return socket.gethostname()
    except OSError:  # pragma: no cover
        return ""


def _owner_payload() -> dict:
    """Thông tin chủ lock: tiến trình local đang giữ mutex."""
    return {
        "id": uuid.uuid4().hex,
        "pid": os.getpid(),
        "host": _local_host(),
        "started": time.time(),
    }


def _owner_alive(owner: dict) -> bool:
    """Chủ lock còn sống không?

    Chủ lock là tiến trình **local** (agent đẩy input) ⇒ chỉ kiểm được khi
    chủ nằm trên cùng máy local. Chủ ở máy khác ⇒ không kiểm được ⇒ coi như
    đã chết (nhưng tuổi lock vẫn là chốt chặn cuối).
    """
    if not owner:
        return False
    host = str(owner.get("host") or "")
    if host and host != _local_host():
        return False
    try:
        pid = int(owner.get("pid"))
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Tiến trình của user khác ⇒ vẫn còn sống.
        return True
    except OSError:  # pragma: no cover
        return False
    return True


#: Lock đang giữ: ``{(host, lock_path): token owner}`` — để nhả khi tiến
#: trình chết (atexit). Bản chìa khoá của kho input — không được sót.
_HELD_LOCKS: dict[tuple[str, str], str] = {}


class _LockHeartbeat:
    """Chạm lock mỗi vài giây trong lúc đang đẩy (giữ cho lock còn "sống").

    Một lần đẩy có thể kéo dài **vài giờ** ⇒ nếu không chạm lock, mtime của
    nó đứng yên ⇒ người khác tưởng lock mồ côi và xoá ⇒ **đẩy trùng**.

    Chỉ chạm khi lock **vẫn của mình** (token owner khớp) ⇒ không đụng
    lock của tiến trình khác vừa chiếm được.
    """

    def __init__(
        self,
        host: str,
        lock_path: str,
        owner_id: str = "",
        interval: float = LOCK_HEARTBEAT_S,
    ) -> None:
        self._host = host
        self._lock_path = lock_path
        self._owner_id = owner_id
        self._interval = max(1.0, float(interval))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _script(self) -> str:
        owner_file = shlex.quote(os.path.join(self._lock_path, "owner"))
        quoted = shlex.quote(self._lock_path)
        return (
            "if grep -q " + shlex.quote(self._owner_id) + " " + owner_file
            + " 2>/dev/null; then touch " + quoted + "; fi"
        )

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                _run_shell(self._host, self._script())
            except Exception:  # pragma: no cover - host flap ⇒ thử lại vòng sau
                pass

    def start(self) -> None:
        """Bắt đầu nhịp chạm (thread daemon ⇒ không giữ tiến trình sống)."""
        if self._thread is not None or not self._owner_id:
            return
        self._thread = threading.Thread(
            target=self._run, name="roff-lock-heartbeat", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Dừng nhịp chạm (gọi trong ``finally``)."""
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)


def _atexit_release_locks() -> None:
    """Nhả mọi lock còn giữ khi tiến trình kết thúc (atexit).

    ``try/finally`` trong :func:`ensure_shared_input` đã nhả lock khi có
    exception; ``atexit`` phủ thêm trường hợp tiến trình **thoát bình thường**
    (``sys.exit``) — trường hợp `finally` không chạy.
    """
    for (host, lock_path), owner_id in list(_HELD_LOCKS.items()):
        try:
            _release_lock(host, lock_path, owner_id)
        except Exception:  # pragma: no cover - không được ném lúc thoát
            pass
        finally:
            _HELD_LOCKS.pop((host, lock_path), None)


atexit.register(_atexit_release_locks)


# --------------------------------------------------------------------------
# trạng thái nguồn local
# --------------------------------------------------------------------------
def _source_signature(local_path: str) -> tuple[int, float]:
    """``(size, mtime)`` của nguồn local — dùng để biết nguồn có đổi không.

    Thư mục: ``size`` = tổng kích thước file trong cây, ``mtime`` = mtime mới
    nhất. Mtime của riêng thư mục chỉ đổi khi thêm/xoá entry, **không** đổi khi
    sửa nội dung file bên trong ⇒ phải gom cả cây, nếu không thì sửa input
    sẽ không được phát hiện (job chạy với dữ liệu cũ ⇒ hỏng âm thầm).
    """
    absolute = _abs(local_path)
    stat = os.stat(absolute)
    if not os.path.isdir(absolute):
        return int(stat.st_size), float(stat.st_mtime)
    total = 0
    newest = float(stat.st_mtime)
    for dirpath, dirnames, filenames in os.walk(absolute):
        for name in list(dirnames) + list(filenames):
            try:
                child = os.stat(os.path.join(dirpath, name))
            except OSError:
                continue
            total += int(child.st_size)
            newest = max(newest, float(child.st_mtime))
    return total, newest


def _marker_matches(marker: dict, size: int, mtime: float) -> bool:
    """Marker có còn khớp với nguồn local không."""
    if not marker:
        return False
    try:
        marked_size = int(marker.get("size"))
        marked_mtime = float(marker.get("mtime"))
    except (TypeError, ValueError):
        return False
    if marked_size != int(size):
        return False
    return abs(marked_mtime - float(mtime)) <= 1e-6


# --------------------------------------------------------------------------
# thao tác trên host
# --------------------------------------------------------------------------
def _read_marker(host: str, marker_path: str, target_dir: str = "") -> dict:
    """Đọc marker trên host (``{}`` nếu chưa có / không đọc được).

    Chỉ coi là **có** marker khi bản copy còn tồn tại: nếu bản copy bị xoá mà
    marker còn thì phải đẩy lại — dùng lại một path không tồn tại thì job chết
    ngay. Kiểm tra này gộp chung 1 round-trip nên không tốn thêm gì.
    """
    check = ""
    if target_dir:
        check = "test -d " + shlex.quote(target_dir) + " && "
    script = check + "cat " + shlex.quote(marker_path) + " 2>/dev/null || true"
    rc, out, _err = _run_shell(host, script)
    if rc != 0 or not out.strip():
        return {}
    try:
        data = json.loads(out)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _write_marker(host: str, marker_path: str, payload: dict) -> None:
    """Ghi marker JSON trên host (nguyên văn, không escape shell lỗi)."""
    script = (
        "mkdir -p " + shlex.quote(os.path.dirname(marker_path)) + " && "
        "printf '%s' " + shlex.quote(json.dumps(payload, ensure_ascii=False))
        + " > " + shlex.quote(marker_path)
    )
    rc, _out, err = _run_shell(host, script)
    if rc != 0:
        raise RoffError(
            EXIT_UNREACHABLE,
            "shared_marker_failed",
            error=f"không ghi được marker input trên host {host}",
            detail=(err or "").strip() or f"rc={rc}",
            host=host,
        )


def _purge(host: str, target_dir: str, marker_path: str) -> None:
    """Xoá bản copy cũ + marker cũ.

    Phải ``chmod -R u+w`` trước vì cây input trong kho bị khoá read-only —
    không mở quyền ghi thì ``rm -rf`` fail và bản cũ sót lại.
    """
    script = (
        "chmod -R u+w " + shlex.quote(target_dir) + " 2>/dev/null || true; "
        "rm -rf " + shlex.quote(target_dir) + "; "
        "rm -f " + shlex.quote(marker_path)
    )
    _run_shell(host, script)


def _acquire_lock(host: str, lock_path: str) -> tuple[bool, dict]:
    """Chiếm mutex bằng ``mkdir`` (atomic).

    ``mkdir`` fail ⇒ thư mục lock đã tồn tại ⇒ có người khác đang đẩy.

    Ghi luôn file ``owner`` **bên trong** lock ⇒ biết ai đang giữ (pid + máy
    local) ⇒ không xoá nhầm lock của tiến trình còn sống. Việc ghi owner là
    **best-effort** và tách riêng khỏi lệnh ``mkdir``: nếu gộp chung, một lỗi
    ghi file sẽ làm ta tưởng "chưa chiếm được lock" trong khi lock **đã** do
    ta tạo ⇒ lock mồ côi do chính ta.

    Returns:
        ``(đã chiếm được, thông tin chủ lock)``.
    """
    owner = _owner_payload()
    parent = os.path.dirname(lock_path)
    rc, _out, _err = _run_shell(
        host,
        "mkdir -p " + shlex.quote(parent)
        + " && mkdir " + shlex.quote(lock_path) + " 2>/dev/null",
    )
    if rc != 0:
        return False, owner
    try:
        _run_shell(
            host,
            "printf '%s' "
            + shlex.quote(json.dumps(owner, ensure_ascii=False))
            + " > "
            + shlex.quote(os.path.join(lock_path, "owner")),
        )
    except Exception:  # pragma: no cover - ghi owner hỏng không chặn đẩy
        pass
    return True, owner


def _release_script(lock_path: str) -> str:
    """Lệnh nhả lock: ``rm -rf`` + **kiểm tra lại** lock đã biến mất.

    ``rm -rf`` (không phải ``rmdir``) vì lock **có** file ``owner`` bên trong
    ⇒ ``rmdir`` luôn fail và bỏ lại lock mồ côi. ``test ! -e`` là bước
    xác nhận: rc = 0 ⇒ lock đã đi thật (không im lặng bỏ sót).
    """
    quoted = shlex.quote(lock_path)
    return "rm -rf " + quoted + " && test ! -e " + quoted


def _release_lock(host: str, lock_path: str, owner_id: str = "") -> bool:
    """Nhả mutex — phải **chắc chắn** xong (không để lại lock mồ côi).

    Vì sao phải cẩn thận: lock mồ côi ⇒ **mọi** lần đẩy sau đó của nguồn đó
    đều thấy "lock bận" + "chưa có marker" ⇒ kho input kẹt **vĩnh viễn**
    (đã xảy ra thật trên fleet Muse 23/11/2026: job treo 2 giờ, lặp vô hạn).

    Cách làm:

    1. ``rm -rf`` (xoá được cả thư mục **không rỗng**) rồi **kiểm tra lại**
       lock đã biến mất chưa;
    2. thử lại :data:`RELEASE_ATTEMPTS` lần (host flap ⇒ lệnh ssh hỏng lần
       đầu, lệnh ``rm`` **không hề chạy**);
    3. vẫn còn ⇒ giao lệnh xoá **tách tiến trình** trên host (``nohup``) để
       lệnh không chết theo tiến trình local, rồi **báo lại** (không im lặng).

    Returns:
        ``True`` nếu xác nhận lock đã biến mất, ``False`` nếu vẫn còn.
    """
    script = _release_script(lock_path)
    detail = ""
    for attempt in range(1, RELEASE_ATTEMPTS + 1):
        rc, _out, err = _run_shell(host, script)
        detail = (err or "").strip()
        if rc == 0:
            return True
        if attempt < RELEASE_ATTEMPTS:
            _sleep(RELEASE_RETRY_DELAY_S)
    # Vẫn còn (hoặc không xác nhận được) ⇒ lần cuối: lệnh tách tiến trình.
    _detached_release(host, lock_path, owner_id)
    if not _lock_exists(host, lock_path):
        return True
    _warn(
        "không nhả được lock " + lock_path + " trên " + str(host)
        + " (rc=" + (detail or "?") + ") — kho input có thể bị kẹt"
    )
    return False


def _detached_release(host: str, lock_path: str, owner_id: str = "") -> None:
    """Xoá lock bằng lệnh **tách tiến trình** trên host (``nohup``).

    Lệnh không chết theo tiến trình local ⇒ kể cả khi agent bị kill đúng lúc
    đó, lock vẫn được nhả (thay vì bỏ lại lock mồ côi ⇒ kẹt vĩnh viễn).

    Chỉ xoá khi lock **vẫn của mình** (token owner khớp) ⇒ không xoá nhầm
    lock của tiến trình khác vừa chiếm được.
    """
    if not owner_id:
        return
    owner_file = shlex.quote(os.path.join(lock_path, "owner"))
    quoted = shlex.quote(lock_path)
    script = (
        "nohup sh -c \""
        + "if grep -q " + shlex.quote(owner_id) + " " + owner_file + " 2>/dev/null; "
        + "then rm -rf " + quoted + "; fi"
        + "\" >/dev/null 2>&1 &"
    )
    try:
        _run_shell(host, script)
    except Exception:  # pragma: no cover - không được che lỗi gốc
        pass


def _lock_exists(host: str, lock_path: str) -> bool:
    """Lock còn tồn tại trên host không?

    ``test -e`` trả ``0`` (còn), ``1`` (không còn) hoặc ``>1`` (lệnh hỏng —
    host chết). Trường hợp hỏng **không** được coi là "đã đi" ⇒ coi như
    lock **còn** (thận trọng: đừng xoá nhầm).
    """
    rc, _out, _err = _run_shell(host, "test -e " + shlex.quote(lock_path))
    if rc == 1:
        return False
    return True


def _lock_age(host: str, lock_path: str) -> float | None:
    """Tuổi của lock (giây); ``None`` nếu không đọc được."""
    script = "stat -c %Y " + shlex.quote(lock_path) + " 2>/dev/null || true"
    rc, out, _err = _run_shell(host, script)
    if rc != 0 or not out.strip():
        return None
    try:
        stamp = float(out.strip().splitlines()[0])
    except (ValueError, IndexError):
        return None
    return max(0.0, time.time() - stamp)


def _lock_owner(host: str, lock_path: str) -> dict:
    """Thông tin chủ lock (file ``owner`` trong lock); ``{}`` nếu không có."""
    script = (
        "cat " + shlex.quote(os.path.join(lock_path, "owner")) + " 2>/dev/null || true"
    )
    rc, out, _err = _run_shell(host, script)
    if rc != 0 or not out.strip():
        return {}
    try:
        data = json.loads(out)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _lock_is_stale(host: str, lock_path: str) -> bool:
    """Lock đã chết chưa (để dọn rồi chiếm lại được)?

    * lock quá :data:`LOCK_STALE_S` **và** chủ lock không còn sống ⇒ chết;
    * chủ lock **còn sống** ⇒ KHÔNG xoá (trừ khi quá :data:`LOCK_HARD_STALE_S`
      — chủ bị kẹt ⇒ không được kẹt vĩnh viễn).
    """
    age = _lock_age(host, lock_path)
    if age is None or age <= LOCK_STALE_S:
        return False
    owner = _lock_owner(host, lock_path)
    if owner and _owner_alive(owner):
        return age > LOCK_HARD_STALE_S
    return True


def _lock_dirty(host: str, lock_path: str) -> None:
    """Dọn lock chết (quá hạn) để chiếm lại được."""
    _run_shell(host, "rm -rf " + shlex.quote(lock_path) + " 2>/dev/null || true")


def _push_source(
    host: str, local_path: str, target: str, kind: str, timeout: float = DEFAULT_TIMEOUT
) -> None:
    """Đẩy nguồn local vào kho (dùng lại :func:`roffkit.transfer.push`).

    Thư mục ⇒ nội dung thư mục nằm trong ``target``; file ⇒ đóng gói vào thư
    mục tạm (hardlink ⇒ không copy byte) rồi đẩy, nên file nằm ở
    ``target/{basename}``.
    """
    seconds = max(1, int(timeout))
    if kind == "dir":
        result = transfer_mod.push(host, local_path, target, timeout=seconds)
        _check_push(result, host, target)
        return
    staging = tempfile.mkdtemp(prefix="roff-shared-")
    try:
        staged = os.path.join(staging, os.path.basename(local_path))
        try:
            os.link(local_path, staged)
        except OSError:
            shutil.copy2(local_path, staged)
        result = transfer_mod.push(host, staging, target, timeout=seconds)
        _check_push(result, host, target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _check_push(result: Any, host: str, target: str) -> None:
    """``transfer.push`` trả kết quả chứ không ném lỗi ⇒ phải tự kiểm."""
    if getattr(result, "ok", False):
        return
    raise RoffError(
        EXIT_UNREACHABLE,
        "shared_push_failed",
        error=f"không đẩy được input lên host {host}",
        detail=str(getattr(result, "error", "") or "push thất bại"),
        host=host,
        remote=target,
    )


# --------------------------------------------------------------------------
# API chính
# --------------------------------------------------------------------------
def ensure_shared_input(
    host: str,
    local_path: str,
    layout_root: str,
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    """Bảo đảm host có **một** bản copy dùng chung của nguồn local.

    Trả về **đường dẫn remote** của bản copy:

    * nguồn là thư mục ⇒ ``{layout_root}/inputs/{key}`` (chứa nội dung thư mục);
    * nguồn là file ⇒ ``{layout_root}/inputs/{key}/{basename}``.

    Không truyền lại khi bản copy còn hợp lệ (marker khớp ``size``/``mtime`` của
    nguồn) — đây là điều kiện quan trọng nhất: nhiều job trên cùng host phải
    chỉ đẩy **một lần**.

    Raises:
        RoffError: ``EXIT_INPUT`` nếu nguồn local không tồn tại;
            ``EXIT_UNREACHABLE`` nếu đẩy lên host thất bại hoặc bị kẹt lock.
    """
    absolute = _abs(local_path)
    if not os.path.exists(absolute):
        raise RoffError(
            EXIT_INPUT,
            "input_not_found",
            error=f"không có input local: {local_path}",
            input=str(local_path),
        )

    key = shared_key(absolute)
    kind = "dir" if os.path.isdir(absolute) else "file"
    basename = os.path.basename(absolute)
    target = shared_dir(layout_root, absolute)
    marker_path = shared_marker(layout_root, absolute)
    lock_path = shared_lock(layout_root, absolute)
    size, mtime = _source_signature(absolute)

    def _remote() -> str:
        return shared_remote_path(layout_root, key, shared_rel(absolute))

    # 1) đã có bản copy còn hợp lệ ⇒ dùng lại, KHÔNG truyền lại.
    if _marker_matches(_read_marker(host, marker_path, target), size, mtime):
        return _remote()

    # 2) chưa có marker ⇒ chiếm mutex (mkdir là atomic) rồi mới đẩy.
    deadline = time.monotonic() + MARKER_WAIT_S
    owner: dict = {}
    while True:
        acquired, owner = _acquire_lock(host, lock_path)
        if acquired:
            break
        # Có người khác đang đẩy cùng nguồn ⇒ chờ marker.
        marker = _read_marker(host, marker_path, target)
        if _marker_matches(marker, size, mtime):
            return _remote()
        if time.monotonic() >= deadline:
            raise RoffError(
                EXIT_UNREACHABLE,
                "shared_input_busy",
                error=f"có tiến trình khác đang đẩy input {basename} lên {host}",
                detail=f"lock {lock_path} vẫn còn sau {MARKER_WAIT_S:.0f}s",
                host=host,
            )
        if _lock_is_stale(host, lock_path):
            # Lock chết (agent bị kill / host flap) ⇒ dọn rồi chiếm lại **ngay**,
            # không chờ tới deadline (trước đây phải quay vòng ~30 lần ≈ 30 phút).
            _lock_dirty(host, lock_path)
            continue
        _sleep(POLL_INTERVAL)

    _HELD_LOCKS[(host, lock_path)] = str(owner.get("id") or "")
    heartbeat = _LockHeartbeat(host, lock_path, str(owner.get("id") or ""))
    heartbeat.start()
    try:
        # 3) dưới lock: đọc lại marker (người khác có thể vừa xong).
        marker = _read_marker(host, marker_path, target)
        if _marker_matches(marker, size, mtime):
            return _remote()
        if marker:
            # Nguồn local đã đổi ⇒ dọn bản copy cũ rồi đẩy lại.
            _purge(host, target, marker_path)
        _push_source(host, absolute, target, kind, timeout)
        # 4) khoá cây input read-only để job sửa input của job khác bị fail
        #    loudly thay vì hỏng dữ liệu âm thầm.
        _run_shell(host, "chmod -R a-w " + shlex.quote(target) + " 2>/dev/null || true")
        # 5) ghi marker ⇒ lần sau dùng lại, không truyền lại.
        _write_marker(
            host,
            marker_path,
            {
                "source": absolute,
                "kind": kind,
                "size": int(size),
                "mtime": float(mtime),
                "basename": basename,
            },
        )
    finally:
        # 6) luôn nhả mutex (kể cả khi lỗi) — nếu không, lần sau kẹt vĩnh viễn.
        heartbeat.stop()
        _HELD_LOCKS.pop((host, lock_path), None)
        _safe_release_lock(host, lock_path, owner)
    return _remote()


def _safe_release_lock(host: str, lock_path: str, owner: dict) -> bool:
    """Nhả lock mà **không** che lỗi gốc; báo lại nếu lock còn lại.

    Nếu đang có exception bay thì ghi thêm chi tiết vào exception ⇒ watcher
    đưa vào event ⇒ người nghiệm thu thấy được (không im lặng).
    """
    try:
        released = _release_lock(host, lock_path, str(owner.get("id") or ""))
    except Exception as exc:  # pragma: no cover - ssh hỏng
        _warn("không nhả được lock " + str(lock_path) + ": " + str(exc))
        released = False
    if released:
        return True
    current = sys.exc_info()[1]
    if isinstance(current, RoffError):
        current.fields["lock_release_failed"] = str(lock_path)
    return False


# --------------------------------------------------------------------------
# dọn kho input trên host (W3g)
# --------------------------------------------------------------------------
def _list_store(host: str, layout_root: str) -> list[dict[str, Any]]:
    """Liệt kê entry trong ``<layout_root>/inputs/`` (1 round-trip).

    Mỗi entry: ``{"name", "kind", "mtime", "size_kb"}``.  ``kind`` = ``"dir"``
    (bản copy) | ``"file"`` (marker ``.complete``) | ``"lock"`` (mutex tạm).
    """
    root = shared_root(layout_root)
    script = (
        "root=" + shlex.quote(root) + "; "
        'if [ -d "$root" ]; then '
        'for p in "$root"/*; do '
        '[ -e "$p" ] || continue; '
        'b=$(basename "$p"); '
        'if [ -d "$p" ]; then t=dir; else t=file; fi; '
        'm=$(stat -c %Y "$p" 2>/dev/null || echo 0); '
        's=$(du -sk "$p" 2>/dev/null | cut -f1); '
        'echo "$t|$b|$m|${s:-0}"; '
        "done; fi"
    )
    rc, out, _err = _run_shell(host, script)
    if rc != 0:
        return []
    entries: list[dict[str, Any]] = []
    for line in out.splitlines():
        parts = line.strip().split("|")
        if len(parts) < 3:
            continue
        kind, name, mtime = parts[0], parts[1], parts[2]
        size_kb = parts[3] if len(parts) > 3 else "0"
        try:
            entries.append(
                {
                    "name": name,
                    "kind": kind,
                    "mtime": float(mtime),
                    "size_kb": int(size_kb or 0),
                }
            )
        except (TypeError, ValueError):
            continue
    return entries


def _host_unreachable(host: str, root: str) -> bool:
    """Host còn với được không? (1 round-trip cho **cả** kho)

    ``test -d`` trả ``0`` (kho tồn tại) | ``1`` (kho chưa có ⇒ host vẫn sống)
    | ``>1`` (lệnh hỏng ⇒ **không** với được host).
    """
    try:
        rc, _out, _err = _run_shell(host, "test -d " + shlex.quote(root))
    except Exception:
        return True
    return rc not in (0, 1)


def _gc_lock_reason(
    host: str, lock_path: str, key: str, keep: set[str]
) -> str:
    """Quyết định với 1 lock ``<key>.lock`` khi ``gc`` (B22).

    * ``"remove"`` — lock **đã chết** (quá hạn + chủ không còn sống) ⇒ dọn;
    * ``"in_use"`` — key đang có job chạy/xếp hàng dùng ⇒ **giữ**;
    * ``"locked"`` — lock còn sống ⇒ **giữ**;
    * ``"host_unreachable"`` — không biết lock còn sống hay không ⇒ **giữ**.

    Dùng lại **đúng** cơ chế sẵn có (:func:`_lock_is_stale`) ⇒ cùng ngưỡng
    với đường **đẩy** ⇒ không phát minh cơ chế khoá mới.
    """
    if key in keep:
        return "in_use"
    try:
        stale = _lock_is_stale(host, lock_path)
    except Exception:
        # ssh hỏng ⇒ không đoán ⇒ giữ (nhưng **không** làm hỏng cả lệnh gc).
        return "host_unreachable"
    return "remove" if stale else "locked"


def _gc_remove_lock(host: str, lock_path: str) -> bool:
    """Dọn 1 lock chết; ``True`` nếu **xác nhận** lock đã biến mất.

    Xoá xong mà kiểm tra lại thấy lock còn ⇒ coi như **xoá hỏng** (host có
    thể chết giữa chừng) ⇒ không được tự nhận là đã dọn.
    """
    try:
        _lock_dirty(host, lock_path)
        return not _lock_exists(host, lock_path)
    except Exception:
        return False


def gc_shared_store(
    host: str,
    layout_root: str,
    keep_keys: Any = (),
    max_age_s: float = DEFAULT_STORE_MAX_AGE_S,
    now: float | None = None,
) -> dict[str, Any]:
    """Dọn kho input trên host — xoá bản copy không còn job nào dùng.

    Quy tắc an toàn (thiếu 1 quy tắc là xoá nhầm dữ liệu):

    * bản copy mà job **đang chạy/xếp hàng** dùng ⇒ giữ (``keep_keys``);
    * bản copy **đang được đẩy** (lock ``<key>.lock`` còn) ⇒ giữ;
    * bản copy **chưa có marker** ``.complete`` ⇒ giữ (có thể đang dở);
    * bản copy **mới hơn** ``max_age_s`` ⇒ giữ;
    * chỉ xoá khi đủ cả 4 điều kiện trên ⇒ cũ **và** không được dùng.

    Ngoài ra (B22): lock **mồ côi** (``<key>.lock`` quá hạn mà chủ đã chết —
    không có file ``owner``) cũng được dọn — không ai dùng nữa mà nó làm kho
    phình mãi. Cùng ngưỡng với đường **đẩy** (:data:`LOCK_STALE_S` /
    :data:`LOCK_HARD_STALE_S`) ⇒ không phát minh cơ chế khoá mới.

    Cơ chế lock/marker **dùng lại** sẵn có (``shared_lock`` + ``.complete``)
    — không phát minh cơ chế khoá mới.

    Returns:
        dict: ``{"removed": [...], "removed_locks": [...], "skipped": [...],
        "skipped_locks": [...], "freed_mb": float, "host": host}``.

        * ``removed_locks`` — **key** của lock mồ côi đã dọn (đường dẫn đầy
          đủ: ``{root}/{key}.lock``);
        * ``skipped_locks`` — ``{"key", "reason"}`` cho lock **giữ lại**
          (``in_use`` / ``locked`` / ``host_unreachable`` / ``remove_failed``).
    """
    moment = time.time() if now is None else float(now)
    keep = {str(key) for key in (keep_keys or []) if key}
    entries = _list_store(host, layout_root)
    root = shared_root(layout_root)
    dirs = {
        entry["name"]: entry
        for entry in entries
        if entry.get("kind") == "dir"
    }
    markers = {
        entry["name"]: entry
        for entry in entries
        if entry.get("kind") == "file" and str(entry.get("name", "")).endswith(".complete")
    }
    locks = {
        str(entry.get("name", ""))[: -len(".lock")]
        for entry in entries
        if entry.get("kind") == "dir" and str(entry.get("name", "")).endswith(".lock")
    }

    removed: list[str] = []
    skipped: list[dict[str, str]] = []
    removed_locks: list[str] = []
    skipped_locks: list[dict[str, str]] = []
    freed_kb = 0

    # --- lock mồ côi (B22) --------------------------------------------------
    # ``<key>.lock`` không có chủ (hoặc chủ đã chết) + quá hạn ⇒ không ai
    # dùng nữa ⇒ dọn, nếu không kho input phình mãi trên host.
    lock_entries = [
        entry
        for entry in entries
        if entry.get("kind") == "dir"
        and str(entry.get("name", "")).endswith(".lock")
    ]
    if lock_entries and _host_unreachable(host, root):
        # Host chết ⇒ không biết lock nào còn sống ⇒ **giữ hết** (nhưng báo).
        for entry in lock_entries:
            name = str(entry.get("name") or "")
            skipped_locks.append(
                {"key": name[: -len(".lock")], "reason": "host_unreachable"}
            )
    else:
        for entry in lock_entries:
            name = str(entry.get("name") or "")
            key = name[: -len(".lock")]
            lock_path = f"{root}/{name}"
            reason = _gc_lock_reason(host, lock_path, key, keep)
            if reason != "remove":
                skipped_locks.append({"key": key, "reason": reason})
                continue
            if _gc_remove_lock(host, lock_path):
                removed_locks.append(key)
            else:
                skipped_locks.append({"key": key, "reason": "remove_failed"})

    # --- bản copy ------------------------------------------------------------
    for name, entry in dirs.items():
        if name.endswith(".lock"):
            # Thư mục lock — xử lý ở nhánh trên, không phải bản copy ⇒
            # **không bao giờ** xoá ở đây.
            continue
        if name in locks:
            skipped.append({"key": name, "reason": "locked"})
            continue
        if name in keep:
            skipped.append({"key": name, "reason": "in_use"})
            continue
        if f"{name}.complete" not in markers:
            # Chưa có marker ⇒ bản copy có thể đang dở ⇒ không đụng.
            skipped.append({"key": name, "reason": "no_marker"})
            continue
        age = moment - float(entry.get("mtime") or 0.0)
        if age < float(max_age_s):
            skipped.append({"key": name, "reason": "too_recent"})
            continue
        target = f"{root}/{name}"
        marker_path = f"{root}/{name}.complete"
        _purge(host, target, marker_path)
        removed.append(target)
        freed_kb += int(entry.get("size_kb") or 0)

    return {
        "host": host,
        "removed": removed,
        "removed_locks": removed_locks,
        "skipped": skipped,
        "skipped_locks": skipped_locks,
        "freed_mb": round(freed_kb / 1024.0, 3),
    }