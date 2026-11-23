"""B27b — fixture ``cli_env`` (tests/conftest.py) phải cô lập **và** dùng được.

Sau B27, ``ROFF_CONFIG`` trỏ tới file **không tồn tại** ⇒ CLI fail to
(exit 3, ``config_missing``). Fixture ``cli_env`` trước đó trỏ ``ROFF_CONFIG``
tới ``<cfgdir>/config.ini`` **mà không tạo file đó** ⇒ mọi test viết sau này
chạy lệnh nạp config dưới fixture này sẽ nhận exit 3 (quả mìn).

Ba điều phải được khoá lại ở đây:

1. **Cô lập**: ``cli_env`` không được đọc ``~/.config/roff/config.ini`` thật
   của người dùng (fixture tồn tại chính là để không đụng config thật).
2. **Ngữ nghĩa không đổi**: trước B27, config thiếu ⇒ ``fleet_include =
   ["*"]`` ⇒ fleet = **mọi** host trong ssh config của test. Fixture mới
   phải cho **đúng** ngữ nghĩa đó.
3. **Không còn quả mìn**: lệnh nạp config dưới fixture phải chạy được (exit 0),
   không phải exit 3.

Test chạy CLI như **tiến trình con** (subprocess) + ``ssh`` giả trong
``tests/fake/`` ⇒ không đụng fleet thật.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SKILL = REPO / "skill"
LAUNCHER = SKILL / "roff.py"

if str(SKILL) not in sys.path:
    sys.path.insert(0, str(SKILL))

from roffkit.config import Config  # noqa: E402

#: Host giả trong ssh config của test (không phải máy thật của người dùng).
FAKE_HOSTS = ("muse-a", "muse-b", "colab-comfy")


def _ssh_config(tmp_path, hosts=FAKE_HOSTS) -> Path:
    """File ssh config giả với ``len(hosts)`` alias."""
    path = tmp_path / "ssh_config"
    lines = []
    for index, alias in enumerate(hosts):
        lines.append(f"Host {alias}\n  HostName 127.0.0.1\n  Port {2200 + index}\n")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _run(args, env, timeout=60):
    """Chạy launcher như tiến trình con; trả (rc, stdout)."""
    proc = subprocess.run(
        [sys.executable, str(LAUNCHER)] + [str(a) for a in args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    return proc.returncode, proc.stdout


def _fleet_aliases(stdout: str) -> list[str]:
    """Alias trong payload JSON của ``roff hosts``."""
    lines = [line for line in stdout.splitlines() if line.strip()]
    assert len(lines) == 1, f"phải in đúng 1 JSON object:\n{stdout}"
    payload = json.loads(lines[0])
    assert isinstance(payload, dict)
    return [host["alias"] for host in payload.get("hosts") or []]


# --------------------------------------------------------------------------
# 1. Cô lập: không đụng config thật của người dùng
# --------------------------------------------------------------------------
def test_cli_env_points_to_a_real_config_file(cli_env):
    """``ROFF_CONFIG`` phải trỏ tới file **có thật** (không phải file chưa tạo)."""
    config_path = Path(os.environ["ROFF_CONFIG"])
    assert config_path.is_file(), f"config của fixture phải tồn tại: {config_path}"
    assert config_path == cli_env["config_file"]
    # Vẫn nằm trong thư mục tạm của test — không đụng config thật.
    assert cli_env["config"].parent in config_path.parents


def test_cli_env_ignores_users_real_config(cli_env, fake_ssh, tmp_path):
    """Nếu roff lỡ đọc ``~/.config/roff/config.ini`` thật ⇒ fleet bị thu hẹp.

    Ta dựng một HOME giả có config **giới hạn** fleet; nếu CLI đọc nhầm
    file đó thì fleet chỉ còn 1 host. Fleet phải là **mọi** host ⇒ CLI đã
    đọc đúng config của fixture.
    """
    fake_home = tmp_path / "fake-home"
    (fake_home / ".config" / "roff").mkdir(parents=True)
    (fake_home / ".config" / "roff" / "config.ini").write_text(
        "[fleet]\ninclude = chi-mot-mot-host\n", encoding="utf-8"
    )

    env = dict(os.environ)
    env["HOME"] = str(fake_home)
    env["ROFF_SSH_CONFIG"] = str(_ssh_config(tmp_path))

    rc, out = _run(["hosts"], env)

    assert rc == 0, out
    aliases = _fleet_aliases(out)
    assert aliases == list(FAKE_HOSTS), (
        f"CLI đã đọc config mặc định trong HOME thay vì config của fixture: {aliases}"
    )


# --------------------------------------------------------------------------
# 2. Ngữ nghĩa fleet không đổi so với trước B27
# --------------------------------------------------------------------------
def test_cli_env_config_has_same_fleet_semantics_as_missing_config(cli_env):
    """Config của fixture ≡ config mặc định (trước B27: config thiếu).

    Trước B27: config thiếu ⇒ ``Config.load`` trả ``fleet_include = ["*"]``,
    ``fleet_exclude = []``. Fixture mới phải cho **đúng** hai giá trị đó.
    """
    cfg = Config.load(os.environ["ROFF_CONFIG"])
    assert cfg.fleet_include == ["*"]
    assert cfg.fleet_exclude == []
    # Không có exclude ⇒ fleet = mọi host (đúng như trước B27)
    assert cfg.fleet_unrestricted() is True


def test_cli_env_fleet_selects_every_host(cli_env, fake_ssh, tmp_path):
    """Dưới fixture, fleet = **mọi** host trong ssh config (không đổi so với cũ)."""
    env = dict(os.environ)
    env["ROFF_SSH_CONFIG"] = str(_ssh_config(tmp_path))

    rc, out = _run(["hosts"], env)

    assert rc == 0, out
    aliases = _fleet_aliases(out)
    assert aliases == list(FAKE_HOSTS), aliases


# --------------------------------------------------------------------------
# 3. Không còn quả mìn exit 3
# --------------------------------------------------------------------------
def test_cli_env_does_not_fail_to_load_config(cli_env, fake_ssh, tmp_path):
    """Lệnh nạp config dưới fixture phải chạy được (không phải exit 3)."""
    env = dict(os.environ)
    env["ROFF_SSH_CONFIG"] = str(_ssh_config(tmp_path))

    rc, out = _run(["hosts"], env)

    assert rc == 0, f"fixture lại tạo quả mìn exit 3: {out}"
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload.get("reason") != "config_missing"


@pytest.mark.parametrize(
    "command",
    [
        ["hosts"],
        ["doctor"],
    ],
)
def test_cli_env_commands_that_read_config_all_work(cli_env, fake_ssh, tmp_path,
                                                    command):
    """Mọi lệnh đọc config dưới fixture đều phải không chết vì config."""
    env = dict(os.environ)
    env["ROFF_SSH_CONFIG"] = str(_ssh_config(tmp_path))

    rc, out = _run(command, env)

    assert rc in (0, 1), f"{command} -> rc={rc}: {out}"
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload.get("reason") != "config_missing", payload
