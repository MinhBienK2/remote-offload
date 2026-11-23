#!/usr/bin/env python3
"""S16 — skill phải chạy được trên **codex**, không chỉ Claude Code.

Cùng kịch bản với S1 nhưng chạy `codex exec` thay vì `claude -p`, trong 1 dự án
tạm không liên quan gì tới remote-offload.

Đây là nghiệm thu ở tầng agent: tốn token, gọi dịch vụ ngoài ⇒ chạy bằng tay.

Dùng:
    python3 tests/acceptance/s16_codex_activation.py
    python3 tests/acceptance/s16_codex_activation.py --keep
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

#: Lệnh "nặng" mà nếu chạy **ngay trên máy local** thì S16 fail.
LOCAL_HEAVY = re.compile(
    r"(^|[;&|]\s*|\s)(pytest|npm\s+(test|ci|install)|pnpm\s+(test|install)|"
    r"yarn\s+test|vitest|jest|cargo\s+(build|test)|go\s+(build|test))\b",
    re.IGNORECASE,
)


def ran_locally(cmd: str) -> bool:
    """Lệnh có chạy ngay trên máy local không (bỏ phần `roff do --cmd '…'`)."""
    without_offload = re.sub(r"\broff\s+do\b.*", " ", cmd, flags=re.DOTALL)
    return bool(LOCAL_HEAVY.search(without_offload))

TEST_FILE = '''"""Test ngủ — chỉ để agent phải chạy 1 lệnh "nặng" về mặt hình thức."""
import time


def test_slow():
    time.sleep(2)
    assert True
'''

PROMPT = "Chạy toàn bộ test của dự án này bằng pytest và báo kết quả."


def build_project(root: Path) -> Path:
    proj = root / "s16-project"
    (proj / "tests").mkdir(parents=True, exist_ok=True)
    (proj / "tests" / "test_slow.py").write_text(TEST_FILE, encoding="utf-8")
    (proj / "README.md").write_text(
        "# Dự án thử nghiệm S16\n\nKhông liên quan gì tới remote-offload.\n",
        encoding="utf-8",
    )
    return proj


def run_session(proj: Path, model: str | None, timeout: int) -> tuple[list[dict], str]:
    argv = [
        "codex",
        "exec",
        "--json",
        "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
    ]
    if model:
        argv += ["--model", model]
    argv += [PROMPT]
    proc = subprocess.run(
        argv, cwd=str(proj), capture_output=True, text=True, timeout=timeout
    )
    events: list[dict] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return events, proc.stderr


def commands_from(events: list[dict]) -> list[str]:
    """Rút lệnh shell mà codex đã chạy (nhiều shape khác nhau giữa các bản)."""
    out: list[str] = []
    for ev in events:
        # codex dùng ThreadItem/ItemCompleted + payload kiểu function_call
        for key in ("item", "payload", "call", "arguments"):
            node = ev.get(key)
            if not isinstance(node, dict):
                continue
            for name in ("command", "cmd", "arguments"):
                val = node.get(name)
                if isinstance(val, str):
                    out.append(val)
                elif isinstance(val, list):
                    out.extend(str(v) for v in val)
            if node.get("type") in (
                "function_call",
                "local_shell_call",
                "exec_command",
                "command_execution",
            ):
                args = node.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        args = None
                if isinstance(args, dict):
                    for name in ("command", "cmd"):
                        if isinstance(args.get(name), str):
                            out.append(args[name])
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=None)
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    root = Path(tempfile.mkdtemp(prefix="roff-s16-"))
    proj = build_project(root)
    print(f"[S16] dự án thử: {proj}")
    try:
        events, err = run_session(proj, args.model, args.timeout)
    except subprocess.TimeoutExpired:
        print("[S16] FAIL: codex quá thời gian")
        return 2
    finally:
        if not args.keep:
            shutil.rmtree(root, ignore_errors=True)
        else:
            print(f"[S16] giữ lại: {root}")

    cmds = commands_from(events)
    print(f"[S16] số event: {len(events)} | số lệnh tìm thấy: {len(cmds)}")
    for c in cmds:
        print(f"  $ {c[:200]}")

    used_roff = any("roff" in c for c in cmds)
    ran_local_heavy = any(ran_locally(c) for c in cmds)
    print(f"[S16] dùng roff: {used_roff} | chạy nặng ngay local: {ran_local_heavy}")
    if err.strip():
        print("[S16] stderr (500 ký tự cuối):", err.strip()[-500:])
    ok = used_roff and not ran_local_heavy
    print("[S16] KẾT QUẢ:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
