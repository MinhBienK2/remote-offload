"""Toolchain trên host: cài tool còn thiếu idempotent (script setup + stamp).

Tách khỏi :mod:`roffkit.cli` vì **watcher** cũng phải đảm bảo toolchain trên
host mới khi failover: host thiếu ``ffmpeg``/``node`` thì job chết ngay lúc
launch. ``cli`` import ``watcher`` (một chiều) nên phần toolchain phải nằm ở
module riêng — nếu để trong ``cli`` thì ``watcher`` phải import ngược lại ⇒
import vòng.

Cài idempotent theo stamp ``(hash script, boot_id, instance_token)``: host
restart hay bị wipe thì stamp hỏng ⇒ cài lại.

Module này không in ra stdout (quy tắc chung của roff).
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import tempfile
import time
from typing import Any, Sequence

from . import layout as layout_mod
from . import sshx
from . import transfer as transfer_mod
from .errors import EXIT_JOB_FAILED, EXIT_UNREACHABLE, RoffError

__all__ = ["ensure_tools"]


def _shell_runner(host: str, script: str) -> tuple[int, str, str]:
    """Bọc :func:`roffkit.sshx.run_shell` thành protocol của :mod:`layout`.

    ``sshx.run_shell`` trả về đối tượng :class:`RunResult` nên cần bọc lại
    thành bộ ba ``(rc, stdout, stderr)``.
    """
    result = sshx.run_shell(host, script, timeout=60)
    return (int(result.rc), result.stdout or "", result.stderr or "")


def _layout_root_of(profile: Any) -> str:
    """Layout root trên host (từ profile; fallback ``$HOME/.roff``)."""
    root = getattr(profile, "layout_root", None)
    if root:
        return str(root)
    home = getattr(profile, "home", None) or "/root"
    return f"{str(home).rstrip('/')}/.roff"


def _setup_script_for(tool: str) -> str | None:
    """Đường dẫn script setup của ``tool`` (nếu có) trong ``skill/setups``."""
    base = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "setups")
    candidates = (
        os.path.join(base, f"{tool}.sh"),
        os.path.join(base, f"{tool}.bash"),
        os.path.join(base, tool, "setup.sh"),
        os.path.join(base, tool, "install.sh"),
    )
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def _sha256_file(path: str) -> str:
    """sha256 của 1 file (đọc theo khối 1 MB)."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_remote_stamp(host: str, stamp_path: str) -> dict:
    """Đọc stamp toolchain trên host ({} nếu chưa có / không đọc được)."""
    script = "cat " + shlex.quote(stamp_path) + " 2>/dev/null || true"
    rc, out, _err = _shell_runner(host, script)
    if rc != 0 or not out.strip():
        return {}
    try:
        data = json.loads(out)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _run_setup(host: str, profile: Any, script_path: str, name: str) -> dict:
    """Chạy script setup idempotent + ghi stamp theo (hash, boot_id, token).

    Trả ``{"host", "name", "ensured", "skipped", "detail"}``.
    """
    layout_root = _layout_root_of(profile)
    remote_dir = f"{layout_root}/toolchain/{name}"
    stamp_path = f"{remote_dir}/stamp.json"

    script_hash = _sha256_file(script_path)
    boot_id = str(getattr(profile, "boot_id", None) or "")
    token = ""
    home = getattr(profile, "home", None)
    if home:
        try:
            token = layout_mod.instance_token(host, str(home), run=_shell_runner)
        except RoffError:
            token = str(getattr(profile, "instance_token", None) or "")

    stamp = _read_remote_stamp(host, stamp_path)
    if (
        stamp.get("script_sha256") == script_hash
        and str(stamp.get("boot_id") or "") == boot_id
        and str(stamp.get("instance_token") or "") == token
    ):
        return {
            "host": host,
            "name": name,
            "ensured": False,
            "skipped": True,
            "detail": "đã cài (stamp khớp: hash script + boot_id + instance_token)",
        }

    # 1) đẩy script lên host
    staging = tempfile.mkdtemp(prefix="roff-setup-")
    try:
        target = os.path.join(staging, "setup.sh")
        try:
            os.link(os.path.abspath(script_path), target)
        except OSError:
            shutil.copy2(os.path.abspath(script_path), target)
        os.chmod(target, 0o755)
        transfer_mod.push(host, staging, remote_dir)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    # 2) chạy script
    rc, out, err = _shell_runner(
        host, "sh " + shlex.quote(f"{remote_dir}/setup.sh") + " 2>&1"
    )
    detail = (out or err or "").strip()
    if rc != 0:
        # Setup hỏng ⇒ **job fail** (exit 1), KHÔNG phải "không với được host"
        # (255) — xem W3m. Chỉ rc==255 (ssh hỏng) mới là 255.
        code = EXIT_UNREACHABLE if int(rc) == 255 else EXIT_JOB_FAILED
        raise RoffError(
            code,
            "setup_failed",
            error=f"script setup '{name}' thất bại trên host {host}",
            detail=detail or f"rc={rc}",
            host=host,
            name=name,
        )

    # 3) ghi stamp (hash script + boot_id + instance_token)
    stamp_payload = {
        "name": name,
        "script_sha256": script_hash,
        "boot_id": boot_id,
        "instance_token": token,
        "installed_at": time.time(),
    }
    script = (
        "mkdir -p " + shlex.quote(remote_dir) + " && printf '%s' "
        + shlex.quote(json.dumps(stamp_payload, ensure_ascii=False))
        + " > " + shlex.quote(stamp_path)
    )
    _shell_runner(host, script)

    return {
        "host": host,
        "name": name,
        "ensured": True,
        "skipped": False,
        "detail": detail[-2000:] or None,
    }


def ensure_tools(host: str, profile: Any, tools: Sequence[str]) -> list[dict]:
    """Cài các tool còn thiếu (có script setup) trên host; trả kết quả từng tool.

    Tool đã có trên host (theo profile) thì bỏ qua; tool không có script setup
    thì policy đã loại host đó rồi nên cũng bỏ qua.
    """
    have = dict(getattr(profile, "tools", None) or {})
    out: list[dict] = []
    for tool in tools or []:
        if tool in have:
            continue
        script_path = _setup_script_for(tool)
        if not script_path:
            # Không có script setup ⇒ policy đã loại host này rồi.
            continue
        out.append(_run_setup(host, profile, script_path, tool))
        have[tool] = f"{tool} (vừa cài)"
    return out
