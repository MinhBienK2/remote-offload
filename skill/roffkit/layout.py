"""Layout trên host: chọn root bền vững, đường dẫn tuyệt đối, instance token.

Ba khái niệm (xem docs/API-CONTRACT.md §4):

``layout_root``
    Nơi chứa ``projects/``, ``jobs/``, ``toolchain/``, ``cache/`` trên host. Ưu tiên
    một mount riêng còn sống sau restart (``/home/hatch`` trên Muse); nếu không
    có thì rơi về ``$HOME/.roff`` và ``durable=False`` để watcher biết phải
    xử lý mất việc khác đi.

``instance_token``
    Chuỗi ngẫu nhiên 32 hex sinh **một lần**, lưu ở ``$HOME/.roff-instance-token``
    (cố ý đặt ở ``$HOME``: ``$HOME`` bị xoá khi host wipe ⇒ token đổi ⇒ phát
    hiện wipe).

``remote_path``
    Mọi đường dẫn gửi qua ssh phải **absolute**; ``~`` được resolve bằng
    ``$HOME`` thật của host. Không bao giờ gửi chuỗi ``~`` qua ssh.

Module này không in gì ra stdout.
"""

from __future__ import annotations

import os
import secrets
import shlex
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

try:  # tuỳ cách import: package `roffkit` hoặc module chạy rời
    from roffkit.errors import EXIT_INPUT, EXIT_UNREACHABLE, RoffError
except ImportError:  # pragma: no cover
    from .errors import EXIT_INPUT, EXIT_UNREACHABLE, RoffError

DURABLE_CANDIDATES = (
    "/home/hatch",
    "/mnt/data",
    "/data",
    "/workspace",
    "/scratch",
)
"""Các mount có thể bền vững, xét theo thứ tự."""

TOKEN_FILENAME = ".roff-instance-token"
"""Tên file token trong ``$HOME`` của host."""

TOKEN_BYTES = 16
"""16 byte → 32 ký tự hex."""


@runtime_checkable
class ShellRunner(Protocol):
    """Chạy một script shell trên host.

    Gọi được như ``runner(host, script)`` và trả về ``(rc, stdout, stderr)``.
    """

    def __call__(self, host: str, script: str) -> tuple[int, str, str]: ...


def _no_runner(host: str, script: str) -> tuple[int, str, str]:
    """Runner mặc định: chưa có lớp ssh trong roffkit nên báo rõ cách dùng."""
    raise NotImplementedError(
        "shell runner chưa được cài; truyền run=<callable (host, script) -> "
        "(rc, stdout, stderr)>"
    )


@dataclass
class Layout:
    """Layout đã chọn cho một host."""

    root: str
    """Absolute, ví dụ ``/home/hatch/.roff``."""
    durable: bool
    """True nếu nằm trên mount riêng (sống sót restart)."""
    home: str
    """Absolute ``$HOME`` của host (nơi đặt ``instance_token``)."""


# ------------------------------------------------------------------ helpers


def _norm_home(path: str) -> str:
    """Chuẩn hoá đường dẫn home: bỏ dấu "/" cuối, luôn absolute."""
    if not path:
        raise RoffError(EXIT_INPUT, error="home rỗng")
    if not path.startswith("/"):
        raise RoffError(EXIT_INPUT, error=f"home không phải absolute: {path!r}")
    return path.rstrip("/") or "/"


def _join(base: str, name: str) -> str:
    """Nối ``name`` vào ``base`` (base đã absolute, không có dấu "/" cuối)."""
    if base.endswith("/"):
        return base + name
    return base + "/" + name


def _fs_id(path: str) -> str | None:
    """Định danh filesystem của ``path`` (``st_dev``), ``None`` nếu lỗi."""
    try:
        return str(os.stat(path).st_dev)
    except OSError:
        return None


# --------------------------------------------------------------- remote_path


def remote_path(profile: dict, path: str) -> str:
    """Giải ``~`` và ``~/x`` thành ``<profile['home']>/x``.

    Đường dẫn khác giữ nguyên, bảo đảm absolute. Đường dẫn không absolute và
    không bắt đầu bằng ``~`` (hoặc ``~`` của user khác) → ``RoffError(EXIT_INPUT)``
    vì không thể đoán an toàn.
    """
    if not isinstance(profile, dict):
        raise RoffError(EXIT_INPUT, error="profile phải là dict")
    home = profile.get("home")
    if not isinstance(home, str) or not home:
        raise RoffError(EXIT_INPUT, error="profile thiếu 'home'")
    home = _norm_home(home)

    if path == "~":
        return home
    if path.startswith("~/"):
        rest = path[2:].lstrip("/")
        if not rest:
            return home
        return _join(home, rest)
    if path.startswith("~"):
        raise RoffError(
            EXIT_INPUT, error=f"không resolve được path (user khác?): {path!r}"
        )
    if not path.startswith("/"):
        raise RoffError(
            EXIT_INPUT,
            error=f"path phải absolute hoặc bắt đầu bằng '~': {path!r}",
        )
    return path


# ---------------------------------------------------------------- layout_for


def layout_for(host: str, home: str, probe_result: dict | None = None) -> Layout:
    """Chọn layout root cho một host.

    Root = durable candidate **đầu tiên** tồn tại, ghi được và **khác filesystem**
    với ``home`` → ``<candidate>/.roff`` (``durable=True``); ngược lại
    ``<home>/.roff`` (``durable=False``).

    ``probe_result`` (dict) cho phép override để test được mà không cần host
    thật::

        {"durable_candidates": ["/home/hatch"], "home_fs": "devA",
         "candidate_fs": {"/home/hatch": "devB"}}

    Với ``probe_result=None`` hàm tự kiểm tra filesystem local (dùng cho test
    và cho host = máy local).
    """
    home_norm = _norm_home(home)

    if probe_result is not None:
        candidates = probe_result.get("durable_candidates") or []
        home_fs = probe_result.get("home_fs")
        candidate_fs = probe_result.get("candidate_fs") or {}
        for raw_candidate in candidates:
            candidate = _norm_home(str(raw_candidate))
            if candidate == home_norm:
                continue
            fs = candidate_fs.get(candidate)
            if home_fs is not None and fs is not None and str(fs) == str(home_fs):
                # cùng filesystem với $HOME → không bền vững hơn $HOME/.roff
                continue
            return Layout(root=_join(candidate, ".roff"), durable=True, home=home_norm)
        return Layout(root=_join(home_norm, ".roff"), durable=False, home=home_norm)

    home_fs = _fs_id(home_norm)
    for candidate in DURABLE_CANDIDATES:
        candidate = _norm_home(candidate)
        if candidate == home_norm:
            continue
        if not os.path.isdir(candidate):
            continue
        if not os.access(candidate, os.W_OK):
            continue
        fs = _fs_id(candidate)
        if home_fs is not None and fs is not None and fs == home_fs:
            continue
        return Layout(root=_join(candidate, ".roff"), durable=True, home=home_norm)
    return Layout(root=_join(home_norm, ".roff"), durable=False, home=home_norm)


# ------------------------------------------------------------ instance_token


def _token_script(token_path: str) -> str:
    """Script POSIX sh: đọc token, chưa có thì tạo mới (32 hex, mode 600)."""
    quoted = shlex.quote(token_path)
    return (
        "set -eu\n"
        "umask 077\n"
        f"p={quoted}\n"
        'if [ -f "$p" ]; then cat "$p"; exit 0; fi\n'
        'tmp=$(mktemp "$p.XXXXXX")\n'
        f'tok=$(od -An -tx1 -N{TOKEN_BYTES} /dev/urandom | tr -d \' \\n\')\n'
        "printf '%s\\n' \"$tok\" > \"$tmp\"\n"
        'mv -f "$tmp" "$p"\n'
        'cat "$p"\n'
    )


def create_instance_token(path: str) -> str:
    """Sinh token 32 hex và ghi vào ``path`` với mode 600 (``os.open``).

    Đây là đường **cục bộ** (host = máy local, hoặc unit test). Trên host remote
    dùng :func:`instance_token` với shell runner — script POSIX ``mktemp``-style
    cho cùng ngữ nghĩa (mode 600 + đổi tên nguyên tử).

    Trả về token vừa ghi.
    """
    token = secrets.token_hex(TOKEN_BYTES)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(token + "\n")
    os.chmod(path, 0o600)
    return token


def instance_token(host: str, home: str, run: ShellRunner | None = None) -> str:
    """Đọc token từ ``<home>/.roff-instance-token``; chưa có thì tạo (32 hex) và
    ghi mode 600. Trả về token.

    Trong thực tế hàm này chạy script qua ssh nên cần một *shell runner* — một
    callable ``(host, script) -> (rc, stdout, stderr)``. Ở đây runner được
    truyền vào qua tham số ``run``; nếu không truyền thì hàm ném
    ``NotImplementedError`` với hướng dẫn rõ ràng (module ``sshx`` chưa tồn tại).
    """
    runner = run if run is not None else _no_runner
    home_norm = _norm_home(home)
    token_path = _join(home_norm, TOKEN_FILENAME)

    rc, stdout, stderr = runner(host, _token_script(token_path))
    if rc != 0:
        raise RoffError(
            EXIT_UNREACHABLE,
            detail=(stderr or stdout or f"rc={rc} khi đọc instance token"),
        )
    token = (stdout or "").strip()
    if not token:
        raise RoffError(
            EXIT_UNREACHABLE, detail="instance token rỗng sau khi đọc"
        )
    return token


# ------------------------------------------------------------- remote_mkdirs


def remote_mkdirs(host: str, path: str, run: ShellRunner | None = None) -> None:
    """``mkdir -p`` trên host qua shell runner.

    Path phải absolute. Lỗi remote → ``RoffError(EXIT_UNREACHABLE)`` kèm
    ``detail`` là text lỗi gốc.
    """
    runner = run if run is not None else _no_runner
    if not path.startswith("/"):
        raise RoffError(EXIT_INPUT, error=f"path phải absolute: {path!r}")
    script = "mkdir -p " + shlex.quote(path)
    rc, stdout, stderr = runner(host, script)
    if rc != 0:
        raise RoffError(
            EXIT_UNREACHABLE,
            detail=(stderr or stdout or f"rc={rc} khi mkdir {path}"),
        )
