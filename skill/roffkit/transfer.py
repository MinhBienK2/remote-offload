"""Truyền dữ liệu giữa máy local và host Muse (và giữa hai host).

Số đo thật trên fleet (DESIGN.md §3.3) → quyết định thiết kế:

* local→Muse chỉ **0,5–1 MB/s** và rất không ổn định ⇒ **ưu tiên không truyền**;
  mọi lần truyền đều phải là *một* lần, không phải N lần.
* host↔host trực tiếp đã hỏng (cloudflared SSH banner timeout) ⇒
  :func:`stream_between` chuyển dữ liệu qua **pipe ở local**, **không ghi đĩa
  local** (Q43).
* rsync **luôn** có ``--partial`` (resume được khi đứt giữa push — kịch bản S6)
  và ``--no-owner --no-group`` (bug R4: ``chown`` trên durable mount).
* **không nén** file đã nén (mp4/mov/mkv/jpg/png/zip/gz…); nén ``-z`` chỉ khi
  cây dữ liệu chỉ chứa file nén được (text/code).
* mọi lệnh ssh/rsync chạy ``nice -n 19 ionice -c3`` (Q18).

Module này **không in ra stdout** (quy tắc chung của roff).
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import shutil
import subprocess
import time
import zlib
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator, Sequence

try:  # phần A (errors/config) và phần B (slots) — fallback tối thiểu
    from .errors import EXIT_INPUT, EXIT_NO_HOST, RoffError
except ImportError:  # pragma: no cover - TODO(claude): hợp nhất sau khi A/B merge
    from typing import Any as _Any

    EXIT_INPUT = 2
    EXIT_NO_HOST = 10
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


try:
    from .config import Config
except ImportError:  # pragma: no cover - TODO(claude): hợp nhất sau khi A/B merge
    from dataclasses import dataclass as _dataclass

    @_dataclass
    class Config:  # type: ignore[no-redef]
        """Config tối thiểu (chỉ dùng khi phần A chưa có)."""

        path: str | None = None
        max_parallel_transfers: int = 2
        nice: int = 19
        ionice: str = "idle"


try:
    from .slots import Lease, SlotLedger
except ImportError:  # pragma: no cover - TODO(claude): hợp nhất sau khi A/B merge
    import threading
    import time as _time
    import uuid as _uuid
    from dataclasses import dataclass as _dataclass

    @_dataclass
    class Lease:  # type: ignore[no-redef]
        """Phiếu chiếm 1 slot (bản tối giểu)."""

        host: str
        ticket: str
        acquired_at: float

    class SlotLedger:  # type: ignore[no-redef]
        """Sổ slot tối giểu, chỉ trong 1 tiến trình (khi phần B chưa có)."""

        def __init__(self, state_dir: str | None = None) -> None:
            self.state_dir = state_dir or ""
            self._lock = threading.Lock()
            self._used: dict[str, int] = {}
            self._cond = threading.Condition(self._lock)

        def acquire(
            self,
            host: str,
            slots: int,
            timeout: float,
            priority: str = "normal",
        ) -> Lease:
            deadline = _time.monotonic() + max(0.0, float(timeout))
            with self._cond:
                while self._used.get(host, 0) >= max(1, int(slots)):
                    remaining = deadline - _time.monotonic()
                    if remaining <= 0:
                        raise RoffError(
                            EXIT_NO_HOST,
                            f"no slot on {host}",
                            host=host,
                            timeout=float(timeout),
                        )
                    self._cond.wait(min(0.05, remaining))
                self._used[host] = self._used.get(host, 0) + 1
            return Lease(host=host, ticket=f"{host}-{_uuid.uuid4().hex[:12]}", acquired_at=_time.time())

        def release(self, lease: Lease) -> None:
            if lease is None:
                return
            with self._cond:
                host = getattr(lease, "host", "")
                self._used[host] = max(0, self._used.get(host, 0) - 1)
                self._cond.notify_all()


try:  # lớp ssh trung tâm (ControlMaster) — không định nghĩa lại option ssh ở đây
    from . import sshx
except ImportError:  # pragma: no cover - TODO(claude): hợp nhất sau khi sshx merge
    sshx = None  # type: ignore[assignment]

try:  # B25: nguồn sự thật cho tham số ``-F`` (dùng khi rơi vào fallback)
    from . import fleet
except ImportError:  # pragma: no cover - TODO(claude): hợp nhất sau khi sshx merge
    fleet = None  # type: ignore[assignment]


__all__ = [
    "TransferResult",
    "DEFAULT_EXCLUDES",
    "INCOMPRESSIBLE_SUFFIXES",
    "COMPRESSIBLE_SUFFIXES",
    "COMPRESS_DOMINANT_RATIO",
    "COMPRESS_MIN_SAVING",
    "should_compress_batch",
    "host_has_rsync",
    "push",
    "pull",
    "pull_path",
    "stream_between",
    "transfer_slot",
]


# --------------------------------------------------------------------------
# hằng số
# --------------------------------------------------------------------------
DEFAULT_EXCLUDES: tuple[str, ...] = (
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".next",
    "dist",
    "build",
    ".cache",
)
"""Thư mục không đẩy lên host (rác build, cài đặt lại được ở host)."""

INCOMPRESSIBLE_SUFFIXES: tuple[str, ...] = (
    ".mp4", ".mov", ".mkv", ".avi", ".webm", ".jpg", ".jpeg",
    ".png", ".gif", ".zip", ".gz", ".xz", ".bz2", ".zst", ".7z", ".pdf",
)
"""Đuôi file **đã nén**: nén lại chỉ tốn CPU, không giảm byte (DESIGN §5.8)."""

COMPRESSIBLE_SUFFIXES: tuple[str, ...] = (
    ".txt", ".json", ".jsonl", ".md", ".csv", ".tsv", ".log", ".xml", ".html",
    ".htm", ".css", ".js", ".jsx", ".ts", ".tsx", ".py", ".sh", ".bash", ".rb",
    ".go", ".rs", ".java", ".c", ".h", ".cpp", ".hpp", ".cs", ".php", ".yaml",
    ".yml", ".toml", ".ini", ".cfg", ".conf", ".sql", ".env.example", ".patch",
    ".diff", ".tex", ".rst", ".srt",
)
"""Đuôi file **text** — nén được nhiều (W3i)."""

COMPRESS_DOMINANT_RATIO = 0.8
"""Tỉ lệ byte "nén được" cần đạt để coi là **kiểu nội dung chủ đạo**."""

COMPRESS_MIN_SAVING = 0.10
"""Nén phải tiết kiệm **ít nhất** 10 % mới đáng (W3i)."""

#: Ngân sách đo thử nén (byte) — đo trên mẫu, không nén cả cây.
COMPRESS_PROBE_BUDGET = 4 * 1024 * 1024

#: Số file tối đa lấy làm mẫu đo.
COMPRESS_PROBE_FILES = 16

#: Timeout ngắn khi dò rsync trên host (giây) — dò nhanh, không chặn push.
RSYNC_PROBE_TIMEOUT = 8

#: Cache trong bộ nhớ: alias → có rsync hay không.
_RSYNC_CACHE: dict[str, bool] = {}

#: Bucket dùng trong sổ slot cho *mọi* luồng truyền của máy local.
TRANSFER_BUCKET = "__transfer__"

#: Timeout mặc định cho các hàm không có tham số timeout.
DEFAULT_TIMEOUT = 3600

#: Bảng tra ionice class → số.
_IONICE_CLASSES = {
    "idle": "3",
    "best-effort": "2",
    "be": "2",
    "none": "0",
    "realtime": "1",
}


@dataclass
class TransferResult:
    """Kết quả 1 lần truyền dữ liệu."""

    ok: bool = False
    bytes_transferred: int = 0
    files: list[dict] = field(default_factory=list)
    """Manifest: ``[{"path":..., "sha256":..., "bytes":...}]`` (chỉ file **mới**)."""
    resumed: bool = False
    """True nếu phát hiện dữ liệu cũ được tái sử dụng (resume)."""
    error: str | None = None


# --------------------------------------------------------------------------
# config / tiền tố nice
# --------------------------------------------------------------------------
def _config() -> Any | None:
    """Config hiện tại, hoặc ``None`` nếu không đọc được.

    **B27** — ``strict=False`` (cố ý): transfer đọc config **sau khi** job đã
    được *ghim host* trong ledger. Nếu ở đây ném lỗi (``config_missing``)
    thì watcher/transfer **chết** ⇒ job mất **im lặng** — tệ hơn hẳn việc
    dùng cấu hình mặc định. ``roff do`` (điểm vào của người dùng) mới là
    chỗ phải **fail to** khi ``ROFF_CONFIG`` trỏ tới file không tồn tại.
    """
    try:
        return Config.load(os.environ.get("ROFF_CONFIG") or None, strict=False)
    except Exception:
        return None


def _nice_level() -> int:
    """Mức ``nice`` (mặc định 19 — thấp nhất, không làm phiền máy local)."""
    cfg = _config()
    value = getattr(cfg, "nice", 19) if cfg is not None else 19
    try:
        return int(value)
    except (TypeError, ValueError):
        return 19


def _ionice_class() -> str:
    """Lớp I/O (``3`` = idle)."""
    cfg = _config()
    value = getattr(cfg, "ionice", "idle") if cfg is not None else "idle"
    return _IONICE_CLASSES.get(str(value).strip().lower(), "3")


def _prefix_nice() -> list[str]:
    """Tiền tố ``nice -n 19 ionice -c3`` cho tiến trình con (Q18)."""
    parts: list[str] = []
    if shutil.which("nice"):
        parts += ["nice", "-n", str(_nice_level())]
    if shutil.which("ionice"):
        parts += ["ionice", "-c" + _ionice_class()]
    return parts


def _max_parallel_transfers() -> int:
    """Trần số luồng truyền đồng thời toàn máy (Q37, mặc định 2)."""
    cfg = _config()
    value = getattr(cfg, "max_parallel_transfers", 2) if cfg is not None else 2
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = 2
    return max(1, value)


def _state_dir() -> str:
    """Thư mục state của roff (``$ROFF_STATE_DIR`` → ``~/.local/state/roff``)."""
    env = os.environ.get("ROFF_STATE_DIR")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return os.path.join(os.path.expanduser("~"), ".local", "state", "roff")


# --------------------------------------------------------------------------
# chạy lệnh con (seam cho test)
# --------------------------------------------------------------------------
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
    0,9–1,4 s (mỗi lệnh ssh qua reverse tunnel mất 3,5–7 s nếu không có
    ControlMaster).
    """
    if sshx is not None:
        return sshx.ssh_base(alias)
    return ["ssh", *_config_args(),
            "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", alias]


def _ssh_argv(alias: str, script: str) -> list[str]:
    """Lệnh ssh chạy 1 script trên host (script truyền nguyên vẹn qua argv)."""
    return _ssh_base(alias) + [script]


def _ssh_transport(alias: str) -> str:
    """Chuỗi ``-e`` cho rsync (dùng chung option với ssh của roff)."""
    parts = _ssh_base(alias)[:-1]  # bỏ alias (rsync tự thêm host)
    return " ".join(shlex.quote(part) for part in parts)


def _run_ssh(
    alias: str, script: str, timeout: int, stdin: bytes | None = None
) -> tuple[int, str, str]:
    """Chạy 1 script shell trên host qua ssh.

    Đây là **seam duy nhất** cho test: test monkeypatch hàm này, không cần mạng.

    Args:
        alias: alias host trong ``~/.ssh/config``.
        script: script POSIX sh, truyền nguyên vẹn làm argv cho remote shell.
        timeout: giới hạn giây.
        stdin: payload gửi kèm (vd tar) — ``None`` nghĩa là không gửi gì.

    Returns:
        ``(returncode, stdout, stderr)``.
    """
    cmd = _prefix_nice() + _ssh_argv(alias, script)
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


def _run_cmd(
    cmd: list[str], timeout: int, stdin: bytes | None = None
) -> tuple[int, str, str]:
    """Chạy 1 lệnh local (rsync/tar) và capture stdout/stderr.

    Seam cho test (giống :func:`_run_ssh`).
    """
    try:
        proc = subprocess.run(
            cmd,
            input=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=max(1, int(timeout)),
        )
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout sau {timeout}s"
    except FileNotFoundError as exc:
        return 127, "", str(exc)
    return (
        proc.returncode,
        proc.stdout.decode("utf-8", "replace"),
        proc.stderr.decode("utf-8", "replace"),
    )


def _popen(cmd: list[str], **kwargs: Any) -> subprocess.Popen:
    """Mở tiến trình con (seam cho test; dùng cho pipeline tar|ssh)."""
    return subprocess.Popen(cmd, **kwargs)


def _pipeline(
    cmds: list[list[str]], timeout: int, capture: bool = True
) -> tuple[int, str, str]:
    """Chạy nhiều lệnh nối tiếp qua pipe (không ghi đĩa local).

    ``cmds[0].stdout`` nối vào ``cmds[1].stdin`` … Dữ liệu chỉ đi qua pipe,
    **không bao giờ** chạm đĩa local (Q43).

    Args:
        cmds: danh sách lệnh.
        timeout: giới hạn giây cho toàn pipeline.
        capture: ``True`` = capture stdout của lệnh cuối; ``False`` = đổ ra
            ``/dev/null`` (không đọc hết vào RAM).
    """
    procs: list[subprocess.Popen] = []
    prev_stdout = None
    last = len(cmds) - 1
    for index, cmd in enumerate(cmds):
        kwargs: dict[str, Any] = {}
        if index == 0:
            kwargs["stdout"] = subprocess.PIPE
        else:
            kwargs["stdin"] = prev_stdout
        if index == last:
            kwargs["stdout"] = subprocess.PIPE if capture else subprocess.DEVNULL
            kwargs["stderr"] = subprocess.PIPE
        proc = _popen(cmd, **kwargs)
        procs.append(proc)
        prev_stdout = proc.stdout
    if prev_stdout is not None:
        prev_stdout.close()

    deadline = time.monotonic() + max(1, int(timeout))
    stdout = b""
    stderr = b""
    try:
        out, err = procs[-1].communicate(timeout=max(0.1, deadline - time.monotonic()))
        stdout = out or b""
        stderr = err or b""
    except subprocess.TimeoutExpired:
        for proc in procs:
            try:
                proc.kill()
            except OSError:
                pass
        return 124, "", f"timeout sau {timeout}s"

    for proc in procs[:-1]:
        try:
            proc.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:  # pragma: no cover
            try:
                proc.kill()
            except OSError:
                pass

    rc = 0
    for proc in procs:
        code = proc.returncode
        if code:
            rc = rc or code
            if not stderr:
                stderr = f"lệnh lỗi rc={code}: {' '.join(list(proc.args)[:6])}".encode()
    return (
        rc,
        stdout.decode("utf-8", "replace"),
        stderr.decode("utf-8", "replace"),
    )


# --------------------------------------------------------------------------
# tiện ích
# --------------------------------------------------------------------------
def _check_timeout(timeout: Any) -> int:
    """Chặn timeout <= 0 (R17: không được treo)."""
    try:
        value = int(timeout)
    except (TypeError, ValueError):
        raise RoffError(
            EXIT_INPUT, "invalid_timeout", error=f"timeout không hợp lệ: {timeout!r}"
        ) from None
    if value <= 0:
        raise RoffError(
            EXIT_INPUT, "invalid_timeout", error=f"timeout phải > 0 (nhận {value})"
        )
    return value


def _remote_abs(path: str) -> str:
    """Chuẩn hoá đường dẫn remote: bắt buộc absolute, không gửi ``~`` qua ssh."""
    text = str(path or "").strip()
    if not text:
        raise RoffError(EXIT_INPUT, "empty_path", error="đường dẫn remote rỗng")
    if text.startswith("~"):
        raise RoffError(
            EXIT_INPUT,
            "relative_remote_path",
            error=f"đường dẫn remote phải absolute (đã resolve '~'): {path!r}",
        )
    if not text.startswith("/"):
        raise RoffError(
            EXIT_INPUT,
            "relative_remote_path",
            error=f"đường dẫn remote phải absolute: {path!r}",
        )
    return text.rstrip("/") or "/"


def _local_abs(path: str) -> str:
    """Chuẩn hoá đường dẫn local (expand ``~``, thành absolute)."""
    return os.path.abspath(os.path.expanduser(str(path)))


def _suffix_kind(path: str) -> str:
    """Phân loại 1 file: ``"media"`` | ``"text"`` | ``"other"``."""
    lower = str(path).lower()
    if any(lower.endswith(suffix) for suffix in INCOMPRESSIBLE_SUFFIXES):
        return "media"
    if any(lower.endswith(suffix) for suffix in COMPRESSIBLE_SUFFIXES):
        return "text"
    return "other"


def _batch_bytes_by_kind(root: str) -> tuple[dict[str, int], list[tuple[str, int]]]:
    """Tổng byte theo kiểu nội dung + danh sách file mẫu (đường dẫn, size).

    File mẫu chỉ gồm file **kiểu text** (để đo tỉ lệ nén) — media thì đo
    cũng vô nghĩa (nén lại không giảm).
    """
    totals = {"media": 0, "text": 0, "other": 0}
    samples: list[tuple[str, int]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in DEFAULT_EXCLUDES]
        for name in filenames:
            full = os.path.join(dirpath, name)
            try:
                size = os.stat(full).st_size
            except OSError:
                continue
            kind = _suffix_kind(name)
            totals[kind] += int(size)
            if kind == "text":
                samples.append((full, int(size)))
    samples.sort(key=lambda item: item[1], reverse=True)
    return totals, samples


def _estimate_saving(samples: Sequence[tuple[str, int]]) -> float:
    """Ước lượng tỉ lệ tiết kiệm khi nén (mẫu nhỏ, có ngân sách byte).

    Nén thật vài file text bằng zlib (mức 6 — cùng mức gzip mặc định) để
    biết nén **có đáng** không: file text toàn ký tự ngẫu nhiên nén gần
    như không giảm ⇒ không đáng tốn CPU.
    """
    total = 0
    compressed = 0
    budget = COMPRESS_PROBE_BUDGET
    for path, size in samples[:COMPRESS_PROBE_FILES]:
        if total >= budget:
            break
        try:
            with open(path, "rb") as fh:
                data = fh.read(min(size, budget - total))
        except OSError:
            continue
        if not data:
            continue
        total += len(data)
        compressed += len(zlib.compress(data, 6))
    if total <= 0:
        return 0.0
    return 1.0 - (compressed / float(total))


def _dominant_decision(totals: dict[str, int]) -> tuple[bool, str]:
    """Quyết định nén theo **byte của kiểu nội dung chủ đạo**.

    Returns:
        ``(candidate, reason)`` — ``candidate=True`` nghĩa là *có thể* nén
        (còn phải qua bước đo "có đáng nén không").
    """
    total = sum(totals.values())
    if total <= 0:
        return False, "empty"
    text_ratio = totals["text"] / float(total)
    media_ratio = totals["media"] / float(total)
    if media_ratio >= COMPRESS_DOMINANT_RATIO:
        return False, "dominant_media"
    if text_ratio < COMPRESS_DOMINANT_RATIO:
        return False, "mixed"
    return True, "dominant_text"


def _compress_by_dominant(sizes: Sequence[tuple[str, int]]) -> bool:
    """Nén hay không theo byte chủ đạo — dùng khi **không đo được** tỉ lệ nén
    (vd pull: file nằm trên host, chưa tải về thì không đo được)."""
    totals = {"media": 0, "text": 0, "other": 0}
    for rel, size in sizes:
        totals[_suffix_kind(rel)] += int(size)
    candidate, _reason = _dominant_decision(totals)
    return candidate


def should_compress_batch(root: str) -> tuple[bool, dict[str, Any]]:
    """Quyết định nén cho **cả lô** (1 thư mục) theo kiểu nội dung chủ đạo.

    Không còn "all-or-nothing": 1 cây có cả ``.mp4`` lẫn ``.txt`` thì quyết
    định **một lần cho cả lô** theo byte của kiểu chủ đạo:

    * ≥ 80 % byte là **text** ⇒ có thể nén;
    * ≥ 80 % byte là **media** (đã nén) ⇒ **không** nén;
    * còn lại (trộn lẫn/không rõ) ⇒ **không** nén (an toàn).

    Thêm nữa, nén phải **thực sự đáng**: đo thử trên mẫu, nếu tiết kiệm <
    10 % thì **không** nén (tốn CPU mà không giảm byte).

    Returns:
        ``(compress, info)`` — ``info`` mô tả quyết định (để log/ghi lại).
    """
    totals, samples = _batch_bytes_by_kind(root)
    total_bytes = sum(totals.values())
    info: dict[str, Any] = {
        "total_bytes": total_bytes,
        "media_bytes": totals["media"],
        "text_bytes": totals["text"],
        "other_bytes": totals["other"],
    }
    candidate, reason = _dominant_decision(totals)
    if total_bytes > 0:
        info["text_ratio"] = round(totals["text"] / float(total_bytes), 4)
        info["media_ratio"] = round(totals["media"] / float(total_bytes), 4)
    if not candidate:
        info["reason"] = reason
        return False, info
    saving = _estimate_saving(samples)
    info["estimated_saving"] = round(saving, 4)
    if saving < COMPRESS_MIN_SAVING:
        info["reason"] = "not_worth_it"
        return False, info
    info["reason"] = "dominant_text"
    return True, info


def _local_files(root: str) -> list[str]:
    """Danh sách file (đường dẫn tương đối, ký hiệu ``/``) dưới ``root``."""
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in DEFAULT_EXCLUDES]
        for name in filenames:
            full = os.path.join(dirpath, name)
            out.append(os.path.relpath(full, root).replace(os.sep, "/"))
    return sorted(out)


def _sha256_file(path: str) -> str:
    """sha256 của 1 file (đọc theo khối 1 MB, không nuốt hết RAM)."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_kv(text: str) -> dict[str, str]:
    """Parse output ``KEY=value`` (bỏ qua dòng lạ, vd banner của bridge)."""
    out: dict[str, str] = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        out[key] = value.strip()
    return out


def _parse_transferred(out: str) -> int:
    """Số byte rsync thực sự chuyển (đọc từ ``--stats``)."""
    match = re.search(r"Total transferred file size:\s*([\d,]+)", out or "")
    if not match:
        return 0
    try:
        return int(match.group(1).replace(",", ""))
    except ValueError:
        return 0


def _first_line(text: str) -> str:
    """Dòng lỗi đầu tiên, bỏ phần "rsync: " thừa."""
    for raw in (text or "").splitlines():
        line = raw.strip()
        if line:
            return line[:400]
    return ""


def _is_missing_rsync(text: str) -> bool:
    """Có phải lỗi "không có rsync" không (R16: fallback tar-over-ssh)?"""
    lowered = (text or "").lower()
    if "command not found" in lowered:
        return True
    return "not found" in lowered and "rsync" in lowered


def _is_missing_dir(text: str) -> bool:
    """Có phải lỗi "thư mục đích chưa tồn tại" không?"""
    lowered = (text or "").lower()
    return "no such file or directory" in lowered and "mkdir" in lowered


# --------------------------------------------------------------------------
# lệnh rsync
# --------------------------------------------------------------------------
def _rsync_args(
    host: str,
    src_spec: str,
    dst_spec: str,
    excludes: Sequence[str],
    compress: bool,
    timeout: int,
    extra: Sequence[str] = (),
) -> list[str]:
    """Tham số rsync (luôn có ``--partial``, ``--no-owner``, ``--no-group``)."""
    args = [
        "rsync",
        "-a",
        "--no-owner",
        "--no-group",
        "--partial",
        "--stats",
        "--timeout=%d" % max(1, int(timeout)),
    ]
    if compress:
        args.append("-z")
    for pattern in excludes:
        args.append("--exclude=%s" % pattern)
    args.extend(extra)
    args += ["-e", _ssh_transport(host), src_spec, dst_spec]
    return args


# --------------------------------------------------------------------------
# transfer_slot (Q37)
# --------------------------------------------------------------------------
@contextmanager
def transfer_slot(timeout: float) -> Iterator[None]:
    """Giới hạn số luồng truyền **đồng thời toàn máy** (Q37).

    Dùng chung sổ slot với mọi phiên/agent/harness (``SlotLedger``, state dir
    riêng ``transfers``). Hết giờ chờ → ``RoffError(EXIT_NO_HOST)``.
    """
    ledger = SlotLedger(state_dir=os.path.join(_state_dir(), "transfers"))
    lease = ledger.acquire(TRANSFER_BUCKET, _max_parallel_transfers(), float(timeout))
    try:
        yield
    finally:
        try:
            ledger.release(lease)
        except Exception:  # pragma: no cover - release idempotent
            pass


# --------------------------------------------------------------------------
# push
# --------------------------------------------------------------------------
def host_has_rsync(host: str, timeout: int = RSYNC_PROBE_TIMEOUT) -> bool:
    """Host này có ``rsync`` không? (API-CONTRACT §15b.7)

    Dò **1 lần** bằng 1 lệnh ngắn (``command -v rsync``) rồi **cache** lại
    theo alias — đừng dò lại mỗi lần push (mỗi lần dò = 1 round-trip ssh).

    Args:
        host: alias host.
        timeout: giới hạn giây cho lệnh dò (ngắn — dò quá lâu thì coi như
            không có rsync, còn hơn kẹt push).

    Returns:
        ``True`` nếu host có ``rsync`` trong ``PATH``.
    """
    alias = str(host or "")
    if not alias:
        return False
    if alias in _RSYNC_CACHE:
        return _RSYNC_CACHE[alias]
    # Script in ra **marker** rõ ràng: stdout rỗng ⇒ không kết luận được
    # (probe lỗi) ⇒ coi như có rsync cho an toàn (fallback tar vẫn giữ được).
    script = (
        "command -v rsync >/dev/null 2>&1 && echo __RSYNC__ || echo __NO_RSYNC__"
    )
    try:
        _rc, out, _err = _run_ssh(alias, script, timeout)
    except Exception:  # pragma: no cover - ssh lỗi
        out = ""
    text = str(out or "").strip()
    if "__RSYNC__" in text and "__NO_RSYNC__" not in text:
        has = True
    elif "__NO_RSYNC__" in text:
        has = False
    else:
        has = True  # probe không cho câu trả lời ⇒ để rsync tự báo lỗi
    _RSYNC_CACHE[alias] = has
    return has


def clear_rsync_cache() -> None:
    """Xoá cache khả năng rsync (dùng khi test / sau khi cài thêm tool)."""
    _RSYNC_CACHE.clear()


def push(
    host: str,
    local_dir: str,
    remote_dir: str,
    excludes: list[str] | None = None,
    timeout: int = 3600,
) -> TransferResult:
    """Đẩy thư mục local lên host.

    ``rsync -a --no-owner --no-group --partial`` (mặc định); host không có
    rsync → fallback ``tar`` qua pipe ssh (R16). ``--partial`` để **resume**
    được khi đứt giữa chừng (kịch bản S6). File đã nén thì **không nén** lại.

    Args:
        host: alias host.
        local_dir: thư mục nguồn (nội dung được copy vào ``remote_dir``).
        remote_dir: thư mục đích **absolute** trên host.
        excludes: mẫu loại trừ, cộng thêm :data:`DEFAULT_EXCLUDES`.
        timeout: giới hạn giây.

    Returns:
        :class:`TransferResult` (``files`` rỗng: không có manifest phía local).
    """
    timeout = _check_timeout(timeout)
    src = _local_abs(local_dir)
    if not os.path.isdir(src):
        raise RoffError(
            EXIT_INPUT,
            "local_dir_not_found",
            error=f"không có thư mục local: {local_dir}",
            local_dir=local_dir,
        )
    dst = _remote_abs(remote_dir)
    patterns = list(DEFAULT_EXCLUDES) + [str(e) for e in (excludes or [])]
    # Quyết định nén **một lần cho cả lô** theo kiểu nội dung chủ đạo
    # (W3i) — không phải all-or-nothing theo từng file.
    compress, _info = should_compress_batch(src)

    with transfer_slot(timeout):
        # Dò rsync 1 lần (cached) — host không có rsync thì đừng chạy rsync
        # để rồi mới nhận ra (W3l).
        if host_has_rsync(host):
            result = _push_rsync(host, src, dst, patterns, compress, timeout)
            if result is None:  # rsync lỗi lạ → fallback tar-over-ssh
                result = _push_tar(host, src, dst, patterns, timeout)
        else:
            result = _push_tar(host, src, dst, patterns, timeout)
    return result


def _push_rsync(
    host: str,
    src: str,
    dst: str,
    excludes: Sequence[str],
    compress: bool,
    timeout: int,
) -> TransferResult | None:
    """Push bằng rsync; trả ``None`` nếu host không có rsync (để fallback)."""
    cmd = _prefix_nice() + _rsync_args(
        host,
        src.rstrip("/") + "/",
        f"{host}:{dst.rstrip('/')}/",
        excludes,
        compress,
        timeout,
    )
    rc, out, err = _run_cmd(cmd, timeout)
    if rc != 0 and _is_missing_rsync(err):
        return None
    if rc != 0 and _is_missing_dir(err):
        rc_mk, _, _ = _run_ssh(host, f"mkdir -p {shlex.quote(dst)}", min(timeout, 60))
        if rc_mk == 0:
            rc, out, err = _run_cmd(cmd, timeout)
    if rc != 0:
        return TransferResult(
            ok=False,
            bytes_transferred=_parse_transferred(out),
            error=_first_line(err) or _first_line(out) or f"rsync rc={rc}",
        )
    return TransferResult(ok=True, bytes_transferred=_parse_transferred(out))


def _push_tar(
    host: str,
    src: str,
    dst: str,
    excludes: Sequence[str],
    timeout: int,
) -> TransferResult:
    """Fallback: ``tar cf -`` ở local → pipe → ``tar xf -`` trên host."""
    tar_cmd = _prefix_nice() + ["tar", "cf", "-"]
    for pattern in excludes:
        tar_cmd.append(f"--exclude={pattern}")
    tar_cmd += ["-C", src, "."]
    ssh_cmd = _prefix_nice() + _ssh_argv(
        host, f"mkdir -p {shlex.quote(dst)} && tar xf - -C {shlex.quote(dst)}"
    )
    rc, out, err = _pipeline([tar_cmd, ssh_cmd], timeout)
    if rc != 0:
        return TransferResult(
            ok=False, error=_first_line(err) or _first_line(out) or f"tar rc={rc}"
        )
    return TransferResult(ok=True)


# --------------------------------------------------------------------------
# pull
# --------------------------------------------------------------------------
def _remote_file_list(
    host: str, remote_dir: str, timeout: int
) -> list[tuple[str, int]]:
    """Liệt kê file trên host (1 round-trip) → ``[(đường dẫn tương đối, size)]``."""
    script = (
        f"cd {shlex.quote(remote_dir)} 2>/dev/null || exit 3\n"
        "find . -type f -printf 'ROFF_FILE=%s\\t%P\\n' 2>/dev/null\n"
    )
    rc, out, err = _run_ssh(host, script, timeout)
    if rc != 0:
        return []
    files: list[tuple[str, int]] = []
    for raw in out.splitlines():
        line = raw.strip()
        if not line.startswith("ROFF_FILE="):
            continue
        value = line[len("ROFF_FILE="):]
        size_text, _, rel = value.partition("\t")
        rel = rel.strip()
        if not rel:
            continue
        try:
            size = int(size_text.strip())
        except ValueError:
            size = 0
        files.append((rel, size))
    files.sort()
    return files


def pull(
    host: str, remote_dir: str, local_dir: str, timeout: int = 3600
) -> TransferResult:
    """Kéo thư mục từ host về local, kèm manifest sha256 của file **mới**.

    File đã có sẵn ở đích (đúng nội dung) **không** tính vào manifest.

    Args:
        host: alias host.
        remote_dir: thư mục nguồn **absolute** trên host.
        local_dir: thư mục đích local.
        timeout: giới hạn giây.

    Returns:
        :class:`TransferResult` với ``files`` = manifest sha256 file mới.
    """
    timeout = _check_timeout(timeout)
    src = _remote_abs(remote_dir)
    dst = _local_abs(local_dir)
    os.makedirs(dst, exist_ok=True)

    with transfer_slot(timeout):
        remote_files = _remote_file_list(host, src, timeout)
        before = set(_local_files(dst))
        compress = _compress_by_dominant(
            [(rel, size) for rel, size in remote_files if rel not in before]
        )
        if host_has_rsync(host):
            result = _pull_rsync(host, src, dst, compress, timeout)
            if result is None:  # rsync lỗi lạ → fallback tar
                result = _pull_tar(host, src, dst, timeout)
        else:
            result = _pull_tar(host, src, dst, timeout)

    files: list[dict] = []
    total = 0
    for rel, _size in remote_files:
        if rel in before:
            continue
        path = os.path.join(dst, rel)
        if not os.path.isfile(path):
            continue
        size = os.path.getsize(path)
        files.append({"path": rel, "sha256": _sha256_file(path), "bytes": size})
        total += size
    result.files = files
    result.bytes_transferred = total
    return result


def _pull_rsync(
    host: str, src: str, dst: str, compress: bool, timeout: int
) -> TransferResult | None:
    """Pull bằng rsync; trả ``None`` nếu host không có rsync (để fallback)."""
    cmd = _prefix_nice() + _rsync_args(
        host,
        f"{host}:{src.rstrip('/')}/",
        dst.rstrip("/") + "/",
        (),
        compress,
        timeout,
    )
    rc, out, err = _run_cmd(cmd, timeout)
    if rc != 0 and _is_missing_rsync(err):
        return None
    if rc != 0:
        return TransferResult(
            ok=False,
            bytes_transferred=_parse_transferred(out),
            error=_first_line(err) or _first_line(out) or f"rsync rc={rc}",
        )
    return TransferResult(ok=True, bytes_transferred=_parse_transferred(out))


def _pull_tar(host: str, src: str, dst: str, timeout: int) -> TransferResult:
    """Fallback: ``tar cf -`` trên host → pipe → ``tar xf -`` ở local."""
    ssh_cmd = _prefix_nice() + _ssh_argv(host, f"tar cf - -C {shlex.quote(src)} .")
    tar_cmd = _prefix_nice() + ["tar", "xf", "-", "-C", dst]
    rc, out, err = _pipeline([ssh_cmd, tar_cmd], timeout)
    if rc != 0:
        return TransferResult(
            ok=False, error=_first_line(err) or _first_line(out) or f"tar rc={rc}"
        )
    return TransferResult(ok=True)


def pull_path(host: str, remote_path: str, local_path: str) -> TransferResult:
    """Kéo **một** file/dir bất kỳ từ host về local.

    Không có tham số ``timeout`` (hợp đồng API §9) → dùng
    :data:`DEFAULT_TIMEOUT`.
    """
    src = _remote_abs(remote_path)
    dst = _local_abs(local_path)

    script = (
        f"if [ -d {shlex.quote(src)} ]; then echo ROFF_KIND=dir; "
        f"else echo ROFF_KIND=file; fi\n"
    )
    rc, out, err = _run_ssh(host, script, min(DEFAULT_TIMEOUT, 60))
    kind = "file"
    if "ROFF_KIND=dir" in out:
        kind = "dir"
    if kind == "dir":
        return pull(host, src, dst, DEFAULT_TIMEOUT)

    existed = os.path.exists(dst)
    cmd = _prefix_nice() + _rsync_args(
        host, f"{host}:{src}", dst, (), False, DEFAULT_TIMEOUT
    )
    rc, out, err = _run_cmd(cmd, DEFAULT_TIMEOUT)
    if rc != 0:
        return TransferResult(
            ok=False,
            error=_first_line(err) or _first_line(out) or f"rsync rc={rc}",
        )
    files: list[dict] = []
    if not existed and os.path.isfile(dst):
        files.append(
            {
                "path": os.path.basename(dst),
                "sha256": _sha256_file(dst),
                "bytes": os.path.getsize(dst),
            }
        )
    return TransferResult(
        ok=True,
        bytes_transferred=sum(item["bytes"] for item in files),
        files=files,
    )


# --------------------------------------------------------------------------
# host → host (Q43)
# --------------------------------------------------------------------------
def stream_between(
    src_host: str,
    src_path: str,
    dst_host: str,
    dst_path: str,
    timeout: int = 3600,
) -> TransferResult:
    """Chuyển dữ liệu **host → host** qua pipe ở local, **không ghi đĩa local**.

    ``ssh A 'tar cf -' | ssh B 'tar xf -'``. Host↔host trực tiếp đã hỏng
    (cloudflared SSH banner timeout) nên đây là đường duy nhất còn lại.

    Args:
        src_host: alias host nguồn.
        src_path: thư mục nguồn **absolute** trên host nguồn.
        dst_host: alias host đích.
        dst_path: thư mục đích **absolute** trên host đích.
        timeout: giới hạn giây.

    Returns:
        :class:`TransferResult` (``files`` rỗng: không có manifest phía local,
        vì dữ liệu không bao giờ chạm đĩa local).
    """
    timeout = _check_timeout(timeout)
    src = _remote_abs(src_path)
    dst = _remote_abs(dst_path)

    cmds = [
        _prefix_nice() + _ssh_argv(src_host, f"tar cf - -C {shlex.quote(src)} ."),
        _prefix_nice()
        + _ssh_argv(
            dst_host,
            f"mkdir -p {shlex.quote(dst)} && tar xf - -C {shlex.quote(dst)}",
        ),
    ]
    with transfer_slot(timeout):
        rc, out, err = _pipeline(cmds, timeout, capture=False)
    if rc != 0:
        return TransferResult(
            ok=False, error=_first_line(err) or _first_line(out) or f"tar rc={rc}"
        )
    return TransferResult(ok=True)
