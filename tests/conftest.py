"""Hạ tầng test dùng chung: ssh giả, rsync giả, tiện ích gọi CLI."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SKILL_DIR = REPO / "skill"
LAUNCHER = SKILL_DIR / "roff.py"
FAKE_DIR = Path(__file__).resolve().parent / "fake"

# Mọi test đều import được roffkit (package nằm trong skill/).
if str(SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(SKILL_DIR))


@pytest.fixture
def fake_ssh(tmp_path, monkeypatch):
    """Đặt ssh giả vào PATH. Trả về hàm cấu hình hành vi theo alias."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    spec_path = tmp_path / "ssh_spec.json"
    spec_path.write_text(json.dumps({"hosts": {}}))

    ssh_script = bindir / "ssh"
    ssh_script.write_text(
        "#!/bin/sh\n"
        f'exec {sys.executable} "{FAKE_DIR / "fake_ssh.py"}" "$@"\n'
    )
    ssh_script.chmod(0o755)

    rsync_script = bindir / "rsync"
    rsync_script.write_text(
        "#!/bin/sh\n"
        "# rsync giả: chép đệ quy nếu có, để test chỉ cần exit code + stdout\n"
        'for a in "$@"; do case "$a" in *:*) SRC="$a";; esac; done\n'
        "exit ${ROFF_FAKE_RSYNC_EXIT:-0}\n"
    )
    rsync_script.chmod(0o755)

    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.setenv("ROFF_FAKE_SSH", str(spec_path))

    state = {"hosts": {}}

    def configure(alias: str, **kwargs):
        state["hosts"][alias] = kwargs
        spec_path.write_text(json.dumps({"hosts": state["hosts"]}))
        return state["hosts"][alias]

    configure.state = state
    return configure


@pytest.fixture
def cli_env(monkeypatch, tmp_path):
    """Env cô lập cho CLI: state dir và config dir riêng, không đụng ~/.claude.

    **B27**: ``ROFF_CONFIG`` phải trỏ tới file **có thật** — CLI ``fail to``
    (exit 3, ``config_missing``) khi config bị chỉ định mà không tồn tại.
    Fixture này trước B27 trỏ tới file **chưa tạo** ⇒ mọi test viết sau này
    chạy lệnh nạp config dưới fixture này sẽ nhận exit 3 (quả mìn).

    Nội dung ``[fleet] include = *`` giữ **đúng** ngữ nghĩa cũ: trước B27,
    config thiếu ⇒ ``Config.load`` trả config mặc định ⇒ ``fleet_include =
    ["*"]`` ⇒ fleet = **mọi** host trong ssh config của test. Không có
    ``exclude`` ⇒ không đổi gì so với trước.
    """
    state = tmp_path / "state"
    cfgdir = tmp_path / "config"
    state.mkdir()
    cfgdir.mkdir()
    config = cfgdir / "config.ini"
    config.write_text("[fleet]\ninclude = *\n", encoding="utf-8")
    monkeypatch.setenv("ROFF_STATE_DIR", str(state))
    monkeypatch.setenv("ROFF_CONFIG", str(config))
    monkeypatch.setenv("ROFF_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    return {
        "state": state,
        "config": cfgdir,
        "config_file": config,
        "home": tmp_path / "home",
    }


def run_cli(args, env=None, timeout=60):
    """Chạy launcher, trả (rc, stdout, stderr)."""
    proc = subprocess.run(
        [sys.executable, str(LAUNCHER)] + list(args),
        capture_output=True, text=True, timeout=timeout,
        env=env,
    )
    return proc.returncode, proc.stdout, proc.stderr


def parse_json(stdout: str):
    """Parse JSON cuối cùng trong stdout. Fail loud nếu không phải 1 JSON object."""
    lines = [ln for ln in stdout.strip().splitlines() if ln.strip()]
    if not lines:
        raise AssertionError("stdout rỗng — lệnh phải in 1 JSON object")
    obj = json.loads(lines[-1])
    assert isinstance(obj, dict), "JSON phải là object"
    return obj


def write_config(path, body: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path
