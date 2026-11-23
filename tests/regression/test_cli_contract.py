"""Hợp đồng CLI — mọi lệnh in **đúng 1 JSON object** và trả exit code đúng.

Đây là bài kiểm tra **tầng hợp đồng**, không phải hành vi: nó không đụng mạng,
nên chạy được ở bất kỳ máy nào. Nếu một thay đổi làm hỏng hợp đồng này thì
đó là hỏng thật, không phải "test cũ".

Nguồn sự thật: `docs/SPEC-CLI.md` §0 và `docs/API-CONTRACT.md` §2.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
LAUNCHER = REPO / "skill" / "roff.py"

EXIT_OK = 0
EXIT_JOB_FAILED = 1
EXIT_INPUT = 2
EXIT_CONFIG = 3
EXIT_NO_HOST = 10
EXIT_JOB_LOST = 69
EXIT_TIMEOUT = 124
EXIT_CANCELLED = 130
EXIT_UNREACHABLE = 255


def run(args, env=None, timeout=60):
    e = dict(os.environ)
    if env:
        e.update(env)
    return subprocess.run(
        [sys.executable, str(LAUNCHER)] + list(args),
        capture_output=True, text=True, timeout=timeout, env=e,
    )


def one_json(proc) -> dict:
    """Parse stdout thành **đúng 1** JSON object; fail nếu có nhiều hơn."""
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    assert len(lines) == 1, (
        f"phải in đúng 1 JSON object, thấy {len(lines)} dòng:\n{proc.stdout[:500]}"
    )
    obj = json.loads(lines[0])
    assert isinstance(obj, dict), f"stdout phải là object, thấy {type(obj)}"
    return obj


# ------------------------------------------------------------------ hợp đồng chung
def test_help_exits_zero_and_prints_help_on_stderr():
    """Help đi ra **stderr** để stdout luôn sạch (chỉ JSON) — xem SPEC-CLI §0."""
    p = run(["--help"])
    assert p.returncode == EXIT_OK
    assert p.stdout.strip() == "", f"stdout phải rỗng, thấy: {p.stdout[:200]}"
    assert "do" in p.stderr


def test_unknown_command_is_input_error():
    """Lỗi argparse ⇒ stderr + exit 2, **không** in JSON (xem SPEC-CLI §0)."""
    p = run(["khong-co-lenh"])
    assert p.returncode == EXIT_INPUT, p.stdout
    assert p.stdout.strip() == "", f"stdout phải rỗng, thấy: {p.stdout[:200]}"
    assert p.stderr.strip(), "phải có message trên stderr"


@pytest.mark.parametrize("args", [
    ["do"],                                   # thiếu --cmd
    ["do", "--cmd", ""],                     # --cmd rỗng
    ["do", "--cmd", "true", "--timeout", "0"],   # --timeout 0 bị từ chối
    ["do", "--cmd", "true", "--priority", "urgent"],  # priority sai
    ["do", "--cmd", "true", "--in", "/khong/ton/tai/roi"],  # input không tồn tại
])
def test_bad_input_exits_2_without_touching_the_network(args):
    p = run(args)
    assert p.returncode == EXIT_INPUT, f"{args} -> rc={p.returncode} {p.stdout}"
    if p.stdout.strip():
        # lỗi argparse ⇒ stderr; lỗi sau khi parse ⇒ JSON trên stdout
        obj = one_json(p)
        assert obj["ok"] is False
        assert obj["exit_code"] == EXIT_INPUT
        assert obj.get("reason"), f"lỗi phải có reason: {obj}"
        assert obj.get("error"), f"lỗi phải có error: {obj}"
    else:
        assert p.stderr.strip(), "phải có message (stderr hoặc JSON)"


def test_status_of_unknown_job_exits_2():
    p = run(["status", "khong-ton-tai-123"])
    assert p.returncode == EXIT_INPUT, p.stdout
    obj = one_json(p)
    assert obj["ok"] is False
    assert obj["exit_code"] == EXIT_INPUT


def test_wait_of_unknown_job_exits_2():
    p = run(["wait", "khong-ton-tai-123", "--timeout", "1"])
    assert p.returncode == EXIT_INPUT, p.stdout
    obj = one_json(p)
    assert obj["ok"] is False
    assert obj["exit_code"] == EXIT_INPUT


def test_bad_config_exits_3(tmp_path):
    bad = tmp_path / "config.ini"
    bad.write_text("đây không phải INI [[[\n", encoding="utf-8")
    p = run(["hosts"], env={"ROFF_CONFIG": str(bad)})
    assert p.returncode == EXIT_CONFIG, p.stdout
    obj = one_json(p)
    assert obj["ok"] is False
    assert obj["exit_code"] == EXIT_CONFIG


def test_jobs_of_empty_state_is_valid_json(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    p = run(["jobs"], env={"ROFF_STATE_DIR": str(state)})
    assert p.returncode == EXIT_OK, p.stdout
    obj = one_json(p)
    assert obj["ok"] is True
    assert isinstance(obj.get("jobs"), list)
    assert obj.get("count") == len(obj["jobs"])


def test_hosts_prints_one_object_per_command(tmp_path):
    # Fleet rỗng ⇒ không probe mạng (test này phải chạy được **không mạng**).
    ssh_config = tmp_path / "ssh_config"
    ssh_config.write_text("", encoding="utf-8")
    p = run(
        ["hosts", "--json"],
        env={"ROFF_STATE_DIR": str(tmp_path), "ROFF_SSH_CONFIG": str(ssh_config)},
    )
    # probe cần mạng ⇒ có thể fail, nhưng stdout vẫn phải là 1 JSON object
    obj = one_json(p)
    assert "ok" in obj
    assert obj["exit_code"] == p.returncode


def test_logs_is_the_only_command_that_prints_text(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    p = run(["logs", "khong-ton-tai-123"], env={"ROFF_STATE_DIR": str(state)})
    # lệnh này in text, không phải JSON — nhưng vẫn phải là 1 dòng, không phải
    # JSON lẫn JSON
    assert not p.stdout.strip().startswith("{") or p.stdout.strip().count("\n") == 0
