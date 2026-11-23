"""Test cho sshx.py — lớp ssh trung tâm.

Bảo đảm các bất biến quan trọng:
  * BatchMode=yes luôn có (host cần mật khẩu phải fail, không treo)
  * nice/ionice luôn có (máy local phải nhẹ)
  * ControlMaster dùng chung socket theo alias
  * phân loại lỗi ssh đúng: "Permission denied" ≠ host chết
  * timeout không treo
"""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill"))

from roffkit import fleet  # noqa: E402
from roffkit import sshx  # noqa: E402


def test_batchmode_always_present():
    argv = sshx.ssh_base("muse-a")
    joined = " ".join(argv)
    assert "BatchMode=yes" in joined, joined
    assert "-o" in argv and "ControlMaster=auto" in joined


def test_nice_and_ionice_prefix():
    """Tiền tố phải có **cả hai** và đúng giá trị.

    **Không** chốt thứ tự: sau B34 cả 5 module dùng chung **một** hàm
    (``roffkit.config.local_nice_prefix``) ⇒ thứ tự `nice`/`ionice` là chi
    tiết cài đặt, không phải hợp đồng (`nice` và `ionice` đặt hai thuộc
    tính độc lập, cả hai đều được kế thừa qua fork+exec).
    """
    prefix = sshx._prefix()
    assert "nice" in prefix
    assert "ionice" in prefix
    assert prefix[prefix.index("nice") + 1] == "-n"
    assert prefix[prefix.index("ionice") + 1].startswith("-c")


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


# --------------------------------------------------------------------------
# B25 — roff và ssh phải đọc CÙNG một file ssh-config
#
# Sự cố: ``fleet`` fingerprint theo ``$HOME/.ssh/config`` (Python expanduser
# theo ``$HOME``) còn ssh mặc định đọc ``~/.ssh/config`` theo **passwd entry**
# (``getpwuid``). Hai file khác nhau ⇒ ssh đọc file A, roff fingerprint file B
# ⇒ đổi config không đổi fingerprint ⇒ tái dùng master cũ ⇒ nói chuyện với
# **máy cũ** (bug B14 mà B25 chặn).
# --------------------------------------------------------------------------
def _home_with_config(tmp_path: Path, text: str) -> Path:
    """Dựng ``$HOME`` tạm có ``.ssh/config`` ⇒ trả về path của file config."""
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True, exist_ok=True)
    cfg = home / ".ssh" / "config"
    cfg.write_text(text, encoding="utf-8")
    return cfg


def test_config_args_use_default_config_when_it_exists(monkeypatch, tmp_path):
    """Config mặc định tồn tại ⇒ ``-F`` trỏ đúng file đó."""
    cfg = _home_with_config(tmp_path, "Host muse-a\n  HostName 10.0.0.1\n")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("ROFF_SSH_CONFIG", raising=False)

    assert sshx.ssh_config_args() == ["-F", str(cfg)]
    assert sshx.ssh_config_path() == str(cfg)


def test_config_args_use_env_replacement(monkeypatch, tmp_path):
    """``$ROFF_SSH_CONFIG`` được set ⇒ ``-F`` trỏ đúng file thay thế."""
    cfg = tmp_path / "ssh_config"
    cfg.write_text("Host muse-a\n  HostName 10.0.0.1\n", encoding="utf-8")
    monkeypatch.setenv("ROFF_SSH_CONFIG", str(cfg))

    assert sshx.ssh_config_args() == ["-F", str(cfg)]
    assert sshx.ssh_config_path() == str(cfg)


def test_config_args_empty_when_no_config_file(monkeypatch, tmp_path):
    """Không có file config nào ⇒ không truyền ``-F`` (ssh tự lo)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("ROFF_SSH_CONFIG", raising=False)

    assert sshx.ssh_config_args() == []
    assert sshx.ssh_config_path() == ""


def test_fleet_and_sshx_point_to_same_config_file(monkeypatch, tmp_path):
    """Hai vế của B25 phải chỉ **cùng một file** (không lệch nhau)."""
    cfg = _home_with_config(tmp_path, "Host muse-a\n  HostName 10.0.0.1\n")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("ROFF_SSH_CONFIG", raising=False)

    assert fleet.ssh_config_file() == str(cfg)
    assert sshx.ssh_config_path() == str(cfg)


def test_ssh_argv_reads_config_under_temp_home(monkeypatch, tmp_path):
    """Regression quan trọng nhất: ``$HOME`` trỏ sang thư mục tạm ⇒ argv ssh
    phải có ``-F`` trỏ vào config **trong** thư mục tạm (tức ssh đọc đúng file
    mà roff fingerprint), không phải config của passwd home."""
    cfg = _home_with_config(tmp_path, "Host muse-a\n  HostName 10.9.9.9\n  Port 2222\n")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ROFF_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("ROFF_SSH_CONFIG", raising=False)

    argv = sshx.ssh_base("muse-a")

    assert "-F" in argv, f"argv phải có -F: {argv}"
    assert argv[argv.index("-F") + 1] == str(cfg)


def test_real_ssh_resolves_config_under_temp_home(monkeypatch, tmp_path):
    """Bằng chứng bằng ssh thật: ``ssh -G`` với file mà roff truyền phải ra
    đúng HostName/Port trong config của ``$HOME`` tạm.

    Không có ``-F`` thì ssh đọc config của **passwd home** ⇒ kết quả khác.
    """
    ssh_bin = shutil.which("ssh")
    if not ssh_bin:
        pytest.skip("máy này không có ssh")
    _home_with_config(tmp_path, "Host muse-a\n  HostName 10.9.9.9\n  Port 2222\n")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ROFF_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("ROFF_SSH_CONFIG", raising=False)

    argv = sshx.ssh_base("muse-a")
    f_path = argv[argv.index("-F") + 1]

    def _resolve(*extra: str) -> dict[str, str]:
        proc = subprocess.run([ssh_bin, "-F", f_path, "-G", "muse-a", *extra],
                              capture_output=True, text=True, timeout=30)
        out = {}
        for line in proc.stdout.splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2:
                out[parts[0].lower()] = parts[1].strip()
        return out

    resolved = _resolve()
    assert resolved.get("hostname") == "10.9.9.9", resolved
    assert resolved.get("port") == "2222", resolved

    # Đối chiếu: KHÔNG có ``-F`` ⇒ ssh đọc config của **passwd home** ⇒ kết quả
    # khác hẳn ⇒ đó chính là lệch lạc mà B25 chặn.
    proc = subprocess.run([ssh_bin, "-G", "muse-a"],
                          capture_output=True, text=True, timeout=30)
    without = {}
    for line in proc.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            without[parts[0].lower()] = parts[1].strip()
    assert without.get("hostname") != "10.9.9.9", without


def test_env_replacement_missing_file_still_passes_F(monkeypatch, tmp_path):
    """``$ROFF_SSH_CONFIG`` trỏ tới file **không tồn tại** ⇒ vẫn truyền
    ``-F`` ⇒ ssh chết với "Can't open user config file" (thất bại rõ ràng)
    chứ không lặng lẽ rơi về ``~/.ssh/config`` (đó là lớp bug B25)."""
    missing = tmp_path / "khong-co.conf"
    monkeypatch.setenv("ROFF_SSH_CONFIG", str(missing))

    assert sshx.ssh_config_args() == ["-F", str(missing)]
    assert fleet.ssh_config_file() == str(missing)
