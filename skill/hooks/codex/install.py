#!/usr/bin/env python3
"""Cài hook ``PreToolUse`` của Codex CLI để chặn lệnh nặng chạy ở máy local.

Codex CLI dùng **cùng** giao thức hook với Claude Code: payload trên stdin có
``tool_name`` + ``tool_input.command``, và lệnh bị chặn bằng cách hook thoát với
exit 2 kèm lý do trên stderr. Nên **cùng một script**
``../claude-code/pretooluse.py`` dùng được cho cả hai.

Cách dùng:
    python3 <skill-dir>/hooks/codex/install.py            # cài
    python3 <skill-dir>/hooks/codex/install.py --dry-run  # chỉ xem
    python3 <skill-dir>/hooks/codex/install.py --uninstall

Hook đọc/ghi ``~/.codex/hooks.json`` (mặc định), **không** ghi đè hook khác
của người dùng, và sao lưu file gốc trước khi sửa.

> Lưu ý: Codex **bỏ qua** ``permissionDecision: "allow"``/``"ask"`` trong JSON
> ⇒ ở Codex phải dùng chế độ mặc định (exit 2 + stderr), **không** bật
> ``ROFF_HOOK_OUTPUT=json``.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

#: Marker để nhận ra entry của chính roff (dùng để uninstall).
MARKER = "remote-offload/pretooluse"


def codex_dir() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))


def hooks_path() -> Path:
    return codex_dir() / "hooks.json"


def skill_dir() -> Path:
    """Thư mục skill (chứa ``hooks/`` và ``roff.py``)."""
    return Path(__file__).resolve().parents[2]


def hook_entry() -> dict:
    """Entry PreToolUse cho codex — cùng script, chế độ exit 2 + stderr."""
    script = skill_dir() / "hooks" / "claude-code" / "pretooluse.py"
    return {
        "matcher": "Bash",
        "hooks": [{
            "type": "command",
            "command": f'python3 "{script}"',
        }],
    }


def _marker_cmd(entry: dict) -> str:
    return entry["hooks"][0]["command"]


def _is_ours(group: dict, marker: str) -> bool:
    return any(marker in (h.get("command") or "") for h in (group.get("hooks") or []))


def install(target: Path, entry: dict, dry_run: bool) -> dict:
    if target.exists():
        try:
            settings = json.loads(target.read_text() or "{}")
        except json.JSONDecodeError as exc:
            return {"ok": False,
                    "error": f"{target} không phải JSON hợp lệ: {exc}"}
    else:
        settings = {}

    hooks = settings.setdefault("hooks", {})
    groups = hooks.setdefault("PreToolUse", [])
    if any(_is_ours(g, _marker_cmd(entry)) for g in groups):
        return {"ok": True, "installed": False, "detail": "hook đã có sẵn",
                "settings": str(target)}

    groups.append(entry)
    if dry_run:
        return {"ok": True, "installed": False, "dry_run": True,
                "would_add": entry, "settings": str(target)}

    target.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if target.exists():
        backup = str(target) + ".bak"
        shutil.copy2(target, backup)
    target.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n")
    return {"ok": True, "installed": True, "settings": str(target),
            "backup": backup}


def uninstall(target: Path, entry: dict, dry_run: bool) -> dict:
    if not target.exists():
        return {"ok": True, "removed": False,
                "detail": f"{target} không tồn tại"}
    settings = json.loads(target.read_text() or "{}")
    groups = settings.get("hooks", {}).get("PreToolUse", []) or []
    kept = [g for g in groups if not _is_ours(g, _marker_cmd(entry))]
    removed = len(groups) - len(kept)
    if dry_run:
        return {"ok": True, "removed": removed, "dry_run": True}
    settings.setdefault("hooks", {})["PreToolUse"] = kept
    backup = None
    if target.exists():
        backup = str(target) + ".bak"
        shutil.copy2(target, backup)
    target.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n")
    return {"ok": True, "removed": removed, "settings": str(target),
            "backup": backup}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--hooks-file", default=None,
                    help="đường dẫn hooks.json (mặc định ~/.codex/hooks.json)")
    args = ap.parse_args()

    target = Path(args.hooks_file) if args.hooks_file else hooks_path()
    entry = hook_entry()

    if args.uninstall:
        result = uninstall(target, entry, args.dry_run)
    else:
        result = install(target, entry, args.dry_run)

    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 3


if __name__ == "__main__":
    sys.exit(main())
