"""Test cho sshx.py — lớp ssh trung tâm.

Bảo đảm các bất biến quan trọng:
  * BatchMode=yes luôn có (host cần mật khẩu phải fail, không treo)
  * nice/ionice luôn có (máy local phải nhẹ)
  * ControlMaster dùng chung socket theo alias
  * phân loại lỗi ssh đúng: "Permission denied" ≠ host chết
  * timeout không treo
"""
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill"))

from roffkit import sshx  # noqa: E402


def test_batchmode_always_present():
    argv = sshx.ssh_base("muse-a")
    joined = " ".join(argv)
    assert "BatchMode=yes" in joined, joined
    assert "-o" in argv and "ControlMaster=auto" in joined


def test_nice_and_ionice_prefix():
    prefix = sshx._prefix()
    assert "nice" in prefix
    assert "ionice" in prefix
    assert prefix.index("ionice") < prefix.index("nice")


def test_control_path_is_per_alias_and_safe():
    a = sshx.control_path("muse-a")
    b = sshx.control_path("muse-b")
    assert a != b
    assert "muse-a" in a.name
    # alias lạ không được phá path
    weird = sshx.control_path("../../etc/passwd")
    assert weird.parent == sshx.cm_dir()


@pytest.mark.parametrize("stderr,expected", [
    ("root@host: Permission denied (publickey).", "auth_refused"),
    ("Host key verification failed.", "auth_refused"),
    ("kex_exchange_identification: read: Connection reset by peer", "host_unreachable"),
    ("ssh: connect to host 127.0.0.1 port 2227: Connection refused", "host_unreachable"),
    ("HTTP 429 Too Many Requests", "bridge_busy"),
    ("ssh: connect to host x port 22: Connection timed out", "timeout"),
    ("ssh: Could not resolve hostname foo", "host_unreachable"),
])
def test_classify_ssh_error(stderr, expected):
    assert sshx.classify_ssh_error(stderr, 255) == expected


def test_permission_denied_is_not_host_death():
    """Key bị từ chối là lỗi phía người dùng, không phải host chết — báo sai thì
    agent đi tìm host khác trong khi vấn đề là key."""
    reason = sshx.classify_ssh_error("Permission denied (publickey).", 255)
    assert reason == "auth_refused"


def test_run_shell_timeout_does_not_hang(monkeypatch):
    class FakeProc:
        returncode = 255
        stdout = ""
        stderr = "timeout"

    def fake_run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 60))

    monkeypatch.setattr(sshx.subprocess, "run", fake_run)
    result = sshx.run_shell("muse-a", "echo hi", timeout=0.1)
    assert result.rc == 255
    assert "timeout" in result.stderr


def test_run_shell_check_raises_host_error(monkeypatch):
    class FakeProc:
        returncode = 255
        stdout = ""
        stderr = "Permission denied (publickey)."

    monkeypatch.setattr(sshx.subprocess, "run", lambda cmd, **kw: FakeProc())
    with pytest.raises(sshx.HostError) as exc:
        sshx.run_shell("muse-a", "true", check=True)
    assert exc.value.reason == "auth_refused"
    assert exc.value.code == 255
    assert "detail" in exc.value.payload()


def test_state_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("ROFF_STATE_DIR", str(tmp_path / "st"))
    assert sshx.state_dir() == tmp_path / "st"
