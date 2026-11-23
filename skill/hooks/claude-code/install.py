#!/usr/bin/env python3
"""Cài hook PreToolUse của Claude Code để chặn lệnh nặng chạy ở máy local.

Cách dùng:
    python3 <skill-dir>/hooks/claude-code/install.py            # cài
    python3 <skill-dir>/hooks/claude-code/install.py --dry-run  # chỉ xem
    python3 <skill-dir>/hooks/claude-code/install.py --uninstall

Hook đọc ~/.claude/settings.json, thêm/mở khoá "hooks.PreToolUse" với matcher "Bash".
Không ghi đè các hook khác của người dùng. Ghi lại file backup trước khi sửa.
"""
import argparse
import json
import os
import shutil
import sys
from pathlib import Path

MARKER = "remote-offload/pretooluse"

#: Wrapper `roff` đặt trong PATH. Không dùng symlink: ``__file__`` của script
#: chạy qua symlink là đường dẫn symlink, không phải thư mục thật của skill.
WRAPPER = """#!/bin/sh
# Wrapper 'roff' — do remote-offload tạo. Gọi launcher thật của skill.
exec {python} "{launcher}" "$@"
"""


def default_bin_dir() -> Path:
    return Path(os.environ.get("ROFF_BIN_DIR") or (Path.home() / ".local" / "bin"))


def install_wrapper(skill_dir: Path, bin_dir: Path) -> dict:
    """Đặt lệnh ``roff`` vào PATH.

    Không có ``roff`` trên PATH thì agent đọc SKILL.md rồi chạy ``roff do …``
    sẽ nhận *command not found* — và phản ứng tự nhiên của một agent là chạy
    lệnh đó ở máy local, tức đúng điều mà skill sinh ra để tránh.
    """
    launcher = (skill_dir / "roff.py").resolve()
    path = bin_dir / "roff"
    body = WRAPPER.format(python=shutil.which("python3") or "python3",
                          launcher=launcher)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)
    return {
        "installed": True,
        "path": str(path),
        "on_path": _on_path(path.parent),
        "hint": (f"thêm {path.parent} vào PATH" if not _on_path(path.parent)
                 else f"{path} đã sẵn sàng"),
    }


def _on_path(directory: Path) -> bool:
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if entry and Path(entry).resolve() == directory.resolve():
            return True
    return False


def uninstall_wrapper(bin_dir: Path) -> dict:
    path = bin_dir / "roff"
    if not path.exists():
        return {"removed": False}
    try:
        path.unlink()
    except OSError:
        return {"removed": False}
    return {"removed": True, "path": str(path)}


def settings_path() -> Path:
    return Path(os.environ.get("CLAUDE_SETTINGS_PATH",
                               Path.home() / ".claude" / "settings.json"))


def hook_entry(skill_dir: Path) -> dict:
    return {
        "matcher": "Bash",
        "hooks": [{
            "type": "command",
            "command": (f'python3 "{skill_dir / "hooks" / "claude-code" / "pretooluse.py"}"'),
        }],
    }


def already_installed(settings: dict, marker_cmd: str) -> bool:
    for group in settings.get("hooks", {}).get("PreToolUse", []) or []:
        for h in group.get("hooks", []) or []:
            if marker_cmd in (h.get("command") or ""):
                return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--settings", default=None)
    ap.add_argument("--bin-dir", default=None,
                    help="nơi đặt lệnh 'roff' (mặc định ~/.local/bin)")
    ap.add_argument("--no-bin", action="store_true",
                    help="không tạo lệnh 'roff' trong PATH")
    args = ap.parse_args()

    skill_dir = Path(__file__).resolve().parents[2]
    bin_dir = Path(args.bin_dir) if args.bin_dir else default_bin_dir()
    target = Path(args.settings) if args.settings else settings_path()
    entry = hook_entry(skill_dir)
    marker_cmd = entry["hooks"][0]["command"]

    if args.uninstall:
        if not target.exists():
            print(json.dumps({"ok": True, "removed": False,
                              "detail": "settings.json không tồn tại"}))
            return 0
        settings = json.loads(target.read_text() or "{}")
        groups = settings.get("hooks", {}).get("PreToolUse", []) or []
        kept = []
        removed = 0
        for g in groups:
            hs = [h for h in (g.get("hooks") or []) if marker_cmd not in (h.get("command") or "")]
            removed += len(g.get("hooks") or []) - len(hs)
            if hs:
                g = dict(g, hooks=hs)
                kept.append(g)
        settings.setdefault("hooks", {})["PreToolUse"] = kept
        if not target.exists() or args.dry_run:
            print(json.dumps({"ok": True, "removed": removed, "dry_run": args.dry_run}))
            return 0
        shutil.copy2(target, str(target) + ".bak")
        target.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n")
        bin_info = ({"removed": False, "detail": "bỏ qua (--no-bin)"}
                    if args.no_bin else uninstall_wrapper(bin_dir))
        print(json.dumps({"ok": True, "removed": removed, "settings": str(target),
                          "bin": bin_info}))
        return 0

    if target.exists():
        try:
            settings = json.loads(target.read_text() or "{}")
        except json.JSONDecodeError as exc:
            print(json.dumps({"ok": False,
                              "error": f"settings.json không phải JSON hợp lệ: {exc}"}))
            return 3
    else:
        settings = {}

    if already_installed(settings, marker_cmd):
        bin_info = ({"installed": False, "detail": "bỏ qua (--no-bin)"}
                    if args.no_bin else install_wrapper(skill_dir, bin_dir))
        print(json.dumps({"ok": True, "installed": False,
                          "detail": "hook đã có sẵn", "settings": str(target),
                          "bin": bin_info}, ensure_ascii=False))
        return 0

    settings.setdefault("hooks", {}).setdefault("PreToolUse", []).append(entry)

    if args.dry_run:
        print(json.dumps({"ok": True, "installed": False, "dry_run": True,
                          "would_add": entry, "settings": str(target),
                          "would_install_bin": (None if args.no_bin
                                                else str(bin_dir / "roff"))},
                         ensure_ascii=False))
        return 0

    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        shutil.copy2(target, str(target) + ".bak")
    target.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n")
    bin_info = ({"installed": False, "detail": "bỏ qua (--no-bin)"}
                if args.no_bin else install_wrapper(skill_dir, bin_dir))
    print(json.dumps({"ok": True, "installed": True, "settings": str(target),
                      "backup": str(target) + ".bak" if target.exists() else None,
                      "bin": bin_info}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
