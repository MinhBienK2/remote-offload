"""``roff doctor`` — chẩn đoán sức khoẻ hệ thống (W5a).

Module này **chỉ báo, không sửa gì**: không ghi file, không đổi quyền, không
kill tiến trình. Mỗi check trả về ``{"name", "ok", "detail"}``; check lỗi
**không** làm hỏng lệnh (bọc try/except ⇒ ``ok: false`` + ``detail``).

Mặc định **không** probe mạng: đọc cache ``probes.json`` + kiểm tra local ⇒
chạy trong < 1 s. ``--probe`` mới probe thật (chậm vài giây, giống
``roff hosts``).

Các check:

``config``
    file ``~/.config/roff/config.ini`` có tồn tại và parse được không.
``wrapper``
    lệnh ``roff`` có trong ``PATH`` (``~/.local/bin/roff``) và chạy được không.
``hook``
    hook ``remote-offload`` đã đăng ký trong ``~/.claude/settings.json`` chưa.
``fleet``
    bao nhiêu host trong fleet còn sống (đọc cache, hoặc probe thật với
    ``--probe``).
``state_perms``
    mọi file state phải 0600, mọi thư mục state 0700.
``orphan_jobs``
    job trong ledger mà watcher đã chết (dùng **đúng** cách kiểm tra mà
    watcher/reconcile dùng: :func:`roffkit.slots.pid_alive`).
``disk_local``
    dung lượng ``/`` còn trống (:func:`shutil.disk_usage`).

Module không in gì ra stdout (chỉ :mod:`roffkit.cli` in JSON).
"""

from __future__ import annotations

import os
import shutil
import stat
import time
from pathlib import Path
from typing import Any, Callable

try:  # pragma: no cover - hai cách import đều phải chạy được
    from roffkit import job as job_mod
    from roffkit import perm
    from roffkit import probe as probe_mod
    from roffkit import slots as slots_mod
    from roffkit.config import DEFAULT_CONFIG_PATH, Config
    from roffkit.errors import EXIT_JOB_FAILED, EXIT_OK
except ImportError:  # pragma: no cover
    from . import job as job_mod
    from . import perm
    from . import probe as probe_mod
    from . import slots as slots_mod
    from .config import DEFAULT_CONFIG_PATH, Config
    from .errors import EXIT_JOB_FAILED, EXIT_OK

__all__ = ["run_checks", "CHECK_NAMES"]

#: Tên các check (thứ tự cố định trong payload).
CHECK_NAMES: tuple[str, ...] = (
    "config",
    "wrapper",
    "hook",
    "fleet",
    "state_perms",
    "orphan_jobs",
    "disk_local",
)

#: Ngưỡng "đã dùng" của ``/`` ⇒ coi là có vấn đề.
DISK_USED_PCT_LIMIT = 90.0


def _check(name: str, ok: bool, detail: str, **extra: Any) -> dict:
    """Dựng 1 check ({name, ok, detail} + khoá phụ tuỳ ý)."""
    out: dict[str, Any] = {"name": name, "ok": bool(ok), "detail": detail}
    out.update(extra)
    return out


# --------------------------------------------------------------------------
# từng check
# --------------------------------------------------------------------------
def check_config() -> dict:
    """Config INI có tồn tại và parse được không; fleet có bị giới hạn không.

    **B27** — phân biệt 2 mức độ (cả hai đều là "vấn đề"):

    * ``config_state="unconfigured"`` — **chưa cấu hình**: không có file config
      (và ``ROFF_CONFIG`` cũng không trỏ tới file nào) ⇒ fleet lấy MỌI host;
    * ``config_state="unrestricted"`` — **đang mở**: file config **có** nhưng
      không giới hạn fleet (thiếu ``[fleet] include``, hoặc ``include = *``)
      ⇒ fleet lấy MỌI host trong ``~/.ssh/config``.

    Ngoài ra, ``ROFF_CONFIG`` trỏ tới file không tồn tại ⇒
    ``config_state="missing"`` (lỗi cấu hình, không phải "chưa cấu hình").
    """
    explicit = bool(os.environ.get("ROFF_CONFIG"))
    path = os.path.expanduser(
        os.environ.get("ROFF_CONFIG") or DEFAULT_CONFIG_PATH
    )
    if not os.path.isfile(path):
        if explicit:
            return _check(
                "config",
                False,
                f"thiếu config: ROFF_CONFIG trỏ tới file không tồn tại ({path}) — "
                "nếu bỏ qua, fleet sẽ lấy MỌI host trong ~/.ssh/config",
                config_state="missing",
                config_path=path,
                fleet_unrestricted=True,
            )
        return _check(
            "config",
            False,
            f"chưa cấu hình: không có config {path} — "
            "fleet sẽ lấy MỌI host trong ~/.ssh/config",
            config_state="unconfigured",
            config_path=path,
            fleet_unrestricted=True,
        )
    try:
        cfg = Config.load(path)
    except Exception as exc:
        return _check(
            "config",
            False,
            f"config lỗi: {exc}",
            config_state="invalid",
            config_path=path,
        )
    if cfg.fleet_unrestricted():
        return _check(
            "config",
            False,
            f"đang mở: config {path} không giới hạn fleet "
            "([fleet] include = * hoặc thiếu include) — "
            "fleet sẽ lấy MỌI host trong ~/.ssh/config",
            config_state="unrestricted",
            config_path=path,
            fleet_unrestricted=True,
        )
    return _check(
        "config",
        True,
        path,
        config_state="ok",
        config_path=path,
        fleet_unrestricted=False,
    )


def check_wrapper() -> dict:
    """Lệnh ``roff`` trong PATH có chạy được không."""
    bin_dir = os.environ.get("ROFF_BIN_DIR") or str(Path.home() / ".local" / "bin")
    path = Path(bin_dir) / "roff"
    if not path.exists():
        return _check("wrapper", False, f"không có lệnh roff: {path}")
    if not os.access(path, os.X_OK):
        return _check("wrapper", False, f"{path} không có quyền chạy")
    return _check("wrapper", True, str(path))


def check_hook() -> dict:
    """Hook PreToolUse của remote-offload đã đăng ký chưa."""
    path = os.environ.get("CLAUDE_SETTINGS_PATH") or str(
        Path.home() / ".claude" / "settings.json"
    )
    target = Path(path)
    if not target.is_file():
        return _check("hook", False, f"không có {path}")
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return _check("hook", False, f"không đọc được {path}: {exc}")
    if "remote-offload" not in text and "pretooluse" not in text:
        return _check(
            "hook",
            False,
            f"chưa đăng ký hook remote-offload trong {path} "
            "(chạy skill/hooks/claude-code/install.py)",
        )
    return _check("hook", True, f"đã đăng ký trong {path}")


def check_fleet(
    cfg: "Config", aliases: list[str], probe_now: bool = False,
    state_dir: str | None = None,
) -> dict:
    """Bao nhiêu host còn sống (đọc cache; ``--probe`` mới probe thật)."""
    alive: list[str] = []
    dead: list[str] = []
    unknown: list[str] = []
    if probe_now:
        for profile in probe_mod.probe_many(aliases, timeout=30):
            alias = str(getattr(profile, "alias", "") or "")
            if not alias:
                continue
            (alive if getattr(profile, "reachable", False) else dead).append(alias)
    else:
        # Đọc cache thô: mặc định **không** probe mạng. Entry quá hạn TTL
        # ⇒ coi như "không biết" (không phải "chết") — cache cũ không được
        # báo thành host chết.
        ttl = float(getattr(cfg, "probe_cache_s", 60) or 60)
        now = time.time()
        entries = probe_mod.cache_entries(state_dir)
        for alias in aliases:
            entry = entries.get(alias)
            profile = entry.get("profile") if isinstance(entry, dict) else None
            ts = entry.get("ts") if isinstance(entry, dict) else None
            fresh = isinstance(ts, (int, float)) and (now - float(ts)) < ttl
            if not fresh:
                unknown.append(alias)
                continue
            reachable = bool(
                isinstance(profile, dict) and profile.get("reachable")
            )
            (alive if reachable else dead).append(alias)
    total = len(alive) + len(dead) + len(unknown)
    detail = f"{len(alive)}/{total} host sống"
    if unknown:
        detail += f" ({len(unknown)} cache cũ — dùng --probe để đo thật)"
    if not total:
        return _check("fleet", False, "fleet rỗng — không có alias nào trong ~/.ssh/config")
    if dead:
        return _check(
            "fleet",
            False,
            f"{detail} — chết: {', '.join(dead)}",
            hosts=alive,
            dead_hosts=dead,
            unknown_hosts=unknown,
        )
    if not alive and unknown:
        # **Không** host nào chắc chắn sống, nhưng cũng **không** host nào
        # chắc chắn chết (cache quá hạn) ⇒ "0/N host sống" đọc ra như đã
        # biết cả fleet chết ⇒ nói rõ là **chưa biết**.
        return _check(
            "fleet",
            True,
            f"chưa xác định được host nào — {len(unknown)} cache quá hạn, "
            "dùng --probe để đo thật",
            hosts=alive,
            dead_hosts=dead,
            unknown_hosts=unknown,
        )
    return _check(
        "fleet", True, detail, hosts=alive, dead_hosts=dead, unknown_hosts=unknown
    )


def check_state_perms(state_dir: str) -> dict:
    """Mọi file state 0600, mọi thư mục state 0700."""
    root = Path(state_dir)
    if not root.is_dir():
        return _check("state_perms", True, f"{root} chưa tồn tại (chưa chạy lệnh nào)")
    bad_files: list[str] = []
    bad_dirs: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        try:
            mode = os.stat(dirpath).st_mode & 0o777
        except OSError:
            continue
        if mode != perm.DIR_MODE:
            bad_dirs.append(dirpath)
        for name in filenames:
            full = os.path.join(dirpath, name)
            try:
                st = os.lstat(full)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue  # socket/symlink: không ép quyền
            if (st.st_mode & 0o777) != perm.FILE_MODE:
                bad_files.append(full)
    total_bad = len(bad_files) + len(bad_dirs)
    if total_bad:
        sample = (bad_files + bad_dirs)[:5]
        return _check(
            "state_perms",
            False,
            f"{total_bad} mục state sai quyền (file 0600, thư mục 0700): "
            + ", ".join(sample),
            bad_files=bad_files[:50],
            bad_dirs=bad_dirs[:50],
        )
    return _check("state_perms", True, "mọi file state đã 0600, thư mục 0700")


def check_orphan_jobs(state_dir: str) -> dict:
    """Job trong ledger mà watcher đã chết (slot sẽ kẹt vĩnh viễn)."""
    try:
        jobs = job_mod.list_jobs(state_dir=state_dir)
    except Exception as exc:
        return _check("orphan_jobs", False, f"không đọc được ledger: {exc}")
    orphans: list[str] = []
    for job in jobs:
        if job.state in job_mod.TERMINAL_STATES:
            continue
        try:
            options = job_mod.job_options(job.id, state_dir=state_dir)
        except Exception:  # pragma: no cover - ledger hỏng
            continue
        try:
            pid = int(options.get("watcher_pid") or 0)
            started = float(options.get("watcher_started") or 0.0)
        except (TypeError, ValueError):
            continue
        if not pid:
            continue
        if not slots_mod.pid_alive(pid, started):
            orphans.append(job.id)
    if orphans:
        return _check(
            "orphan_jobs",
            False,
            f"{len(orphans)} job mồ côi (watcher đã chết)",
            jobs=orphans[:50],
            count=len(orphans),
        )
    return _check("orphan_jobs", True, "không có job mồ côi")


def check_disk_local(path: str = "/") -> dict:
    """Dung lượng ``/`` còn trống."""
    try:
        usage = shutil.disk_usage(path)
    except OSError as exc:
        return _check("disk_local", False, f"không đọc được dung lượng {path}: {exc}")
    total = usage.total or 1
    used_pct = (usage.total - usage.free) * 100.0 / total
    free_gb = usage.free / (1024.0 ** 3)
    detail = f"{path} còn {free_gb:.1f} GB ({used_pct:.0f}% đã dùng)"
    return _check(
        "disk_local",
        used_pct < DISK_USED_PCT_LIMIT,
        detail,
        free_mb=int(usage.free / (1024 * 1024)),
        used_pct=round(used_pct, 1),
    )


# --------------------------------------------------------------------------
# API công khai
# --------------------------------------------------------------------------
def run_checks(
    probe_now: bool = False,
    state_dir: str | None = None,
) -> dict:
    """Chạy mọi check, trả payload JSON của ``roff doctor``.

    Args:
        probe_now: ``True`` ⇒ probe thật host (chậm); mặc định đọc cache.
        state_dir: thư mục state (mặc định ``$ROFF_STATE_DIR``).

    Returns:
        ``{"ok": bool, "exit_code": int, "problems": int, "checks": [...]}``.
    """
    base = state_dir or os.environ.get("ROFF_STATE_DIR") or str(
        Path.home() / ".local" / "state" / "roff"
    )

    checks: list[dict] = []

    def add(name: str, fn: Callable[[], dict]) -> None:
        try:
            checks.append(fn())
        except Exception as exc:  # check lỗi không được làm hỏng lệnh
            checks.append(_check(name, False, f"lỗi khi kiểm tra: {exc}"))

    add("config", check_config)
    add("wrapper", check_wrapper)
    add("hook", check_hook)

    def _fleet() -> dict:
        # Cùng **đường dẫn** với ``check_config`` (``ROFF_CONFIG`` nếu có, else
        # mặc định) ⇒ check ``config`` và check ``fleet`` nói về **cùng** một
        # config. ``strict=False``: doctor **chỉ báo**, không được chết vì
        # config thiếu (B27).
        cfg = Config.load(os.environ.get("ROFF_CONFIG") or None, strict=False)
        try:
            from roffkit import fleet as fleet_mod
        except ImportError:  # pragma: no cover - chạy module lẻ
            from . import fleet as fleet_mod
        aliases = fleet_mod.env_fleet_aliases(cfg)
        return check_fleet(cfg, aliases, probe_now=probe_now, state_dir=base)

    add("fleet", _fleet)
    add("state_perms", lambda: check_state_perms(base))
    add("orphan_jobs", lambda: check_orphan_jobs(base))
    add("disk_local", check_disk_local)

    problems = sum(1 for item in checks if not item.get("ok"))
    code = EXIT_OK if problems == 0 else EXIT_JOB_FAILED
    return {
        "ok": problems == 0,
        "exit_code": code,
        "problems": problems,
        "checks": checks,
    }
