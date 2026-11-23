#!/usr/bin/env python3
"""S1 — skill phải **tự bật** ở skill khác, dự án khác, phiên mới.

Kịch bản: dựng 1 dự án tạm (chỉ có 1 file pytest ngủ 2 s), mở 1 phiên
Claude Code **mới** ở thư mục đó, yêu cầu "chạy test", rồi đọc log tool
calls để xem agent có **tự** gọi `roff do` hay không.

Đây là nghiệm thu ở tầng **agent**, không phải unit test: nó tốn token và
gọi dịch vụ ngoài, nên chạy bằng tay, không để trong `pytest tests/acceptance`.

Dùng:
    python3 tests/acceptance/s1_skill_activation.py            # dùng model mặc định
    python3 tests/acceptance/s1_skill_activation.py --model sonnet
    python3 tests/acceptance/s1_skill_activation.py --keep      # giữ thư mục dự án
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[2] / "skill"
HOOK = SKILL_DIR / "hooks" / "claude-code" / "pretooluse.py"

TEST_FILE = '''"""Test ngủ — chỉ để agent phải chạy 1 lệnh "nặng" về mặt hình thức."""
import time


def test_slow():
    time.sleep(2)
    assert True
'''

PROMPT = (
    "Chạy toàn bộ test của dự án này bằng pytest và báo kết quả."
)


def build_project(root: Path) -> Path:
    proj = root / "s1-project"
    (proj / "tests").mkdir(parents=True, exist_ok=True)
    (proj / "tests" / "test_slow.py").write_text(TEST_FILE, encoding="utf-8")
    (proj / "README.md").write_text(
        "# Dự án thử nghiệm S1\n\nDự án tối giản, không liên quan gì tới "
        "remote-offload.\n",
        encoding="utf-8",
    )
    return proj


def run_session(proj: Path, model: str | None, timeout: int,
                hook_settings: Path | None = None,
                hook_off: bool = False) -> list[dict]:
    argv = [
        "claude",
        "-p",
        PROMPT,
        "--output-format",
        "stream-json",
        "--verbose",
        "--permission-mode",
        "bypassPermissions",
        "--allow-dangerously-skip-permissions",
        "--no-session-persistence",
    ]
    if model:
        argv += ["--model", model]
    if hook_settings is not None:
        argv += ["--settings", str(hook_settings)]
    env = dict(os.environ)
    if hook_off:
        # Tắt hook **toàn cục** (kể cả hook đã cài trong ~/.claude/settings.json)
        # ⇒ đo được "skill tự bật được không mà không cần hook".
        env["ROFF_HOOK_OFF"] = "1"
    # Cùng state như phiên thật để skill/hook nạp đúng như bình thường.
    proc = subprocess.run(
        argv,
        cwd=str(proj),
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    events: list[dict] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return events


#: Lệnh "nặng" mà nếu chạy **ngay trên máy local** thì S1 fail.
LOCAL_HEAVY = re.compile(
    r"(^|[;&|]\s*|\s)(pytest|npm\s+(test|ci|install)|pnpm\s+(test|install)|"
    r"yarn\s+test|vitest|jest|cargo\s+(build|test)|go\s+(build|test))\b",
    re.IGNORECASE,
)


def ran_locally(cmd: str) -> bool:
    """Lệnh có **chạy ngay trên máy local** không?

    Phần `roff do --cmd '…'` là lệnh được **gửi đi**, không phải lệnh chạy ở
    đây ⇒ phải bỏ nó ra trước khi so khớp.
    """
    without_offload = re.sub(r"\broff\s+do\b.*", " ", cmd, flags=re.DOTALL)
    return bool(LOCAL_HEAVY.search(without_offload))


def tool_calls(events: list[dict]) -> list[dict]:
    out: list[dict] = []
    for ev in events:
        msg = ev.get("message")
        if not isinstance(msg, dict):
            continue
        for block in msg.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                out.append(block)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=None)
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--with-hook", action="store_true",
                    help="cài hook PreToolUse (qua --settings tạm) cho phiên thử")
    ap.add_argument("--hook-off", action="store_true",
                    help="TẮT hook (kể cả hook đã cài trong ~/.claude/settings.json) "
                         "⇒ đo được 'skill tự bật được không mà không cần hook'")
    args = ap.parse_args()

    root = Path(tempfile.mkdtemp(prefix="roff-s1-"))
    proj = build_project(root)
    print(f"[S1] dự án thử: {proj}")

    hook_settings = None
    if args.with_hook:
        hook_settings = root / "settings-with-hook.json"
        hook_settings.write_text(json.dumps({
            "hooks": {"PreToolUse": [{
                "matcher": "Bash",
                "hooks": [{"type": "command",
                           "command": f'python3 "{HOOK}"'}],
            }]}
        }, indent=2), encoding="utf-8")
        print(f"[S1] hook: {hook_settings}")
    if args.hook_off:
        print("[S1] hook: TẮT (ROFF_HOOK_OFF=1)")

    try:
        events = run_session(proj, args.model, args.timeout, hook_settings,
                             hook_off=args.hook_off)
    except subprocess.TimeoutExpired:
        print("[S1] FAIL: phiên Claude Code quá thời gian")
        return 2
    finally:
        if not args.keep:
            shutil.rmtree(root, ignore_errors=True)
        else:
            print(f"[S1] giữ lại: {root}")

    calls = tool_calls(events)
    print(f"[S1] số tool call: {len(calls)}")

    used_roff = False       # agent đã dùng skill (qua Bash hoặc qua tool Skill)
    submitted = False       # agent đã **gửi job** (roff do)
    ran_local_heavy = False
    details: list[str] = []
    for block in calls:
        name = block.get("name")
        inp = block.get("input") or {}
        if name == "Skill":
            # Agent gọi skill bằng tool Skill ⇒ cũng là "dùng roff".
            skill_name = str(inp.get("skill") or "")
            details.append(f"  Skill: {skill_name}")
            if "roff" in skill_name.lower() or "offload" in skill_name.lower():
                used_roff = True
            continue
        if name != "Bash":
            details.append(f"  {name}: {str(inp)[:120]}")
            continue
        cmd = str(inp.get("command") or "")
        details.append(f"  Bash: {cmd[:200]}")
        if re.search(r"\broff\b", cmd):
            used_roff = True
        if re.search(r"\broff\s+do\b", cmd):
            submitted = True
        if ran_locally(cmd):
            ran_local_heavy = True

    print("[S1] tool calls:")
    for line in details:
        print(line)

    ok = used_roff and not ran_local_heavy
    print(f"[S1] dùng roff: {used_roff} | đã gửi job (roff do): {submitted} | "
          f"chạy nặng ngay local: {ran_local_heavy}")
    if used_roff and not submitted:
        print("[S1] LƯU Ý: agent nhắc tới roff nhưng không gửi job nào")
    print("[S1] KẾT QUẢ:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
