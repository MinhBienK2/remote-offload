"""B27 — ``ROFF_CONFIG`` trỏ tới file **không tồn tại** ⇒ **fail to** (exit 3).

Lỗi gốc (đo thật trên máy người dùng): ``~/.config/roff/config.ini`` có

    [fleet]
    include = muse-*
    exclude = colab-comfy

nhưng khi ``ROFF_CONFIG`` trỏ tới một file **không tồn tại** thì
:meth:`roffkit.config.Config.load` im lặng trả config rỗng ⇒
``fleet_include = ["*"]`` ⇒ fleet lấy **MỌI** host trong ``~/.ssh/config``
(``exclude`` của người dùng bị bỏ qua im lặng). Một subagent khác đã vô tình
chạy job thật lên host ``comfy`` (máy Colab GPU) vì lý do đúng này.

Hợp đồng B27:

* config **được chỉ định** mà **không tồn tại** ⇒ ``exit 3`` +
  ``reason="config_missing"`` (không được im lặng nới rộng fleet);
* **không** chỉ định config + không có ``~/.config/roff/config.ini`` ⇒ vẫn
  chạy, fleet = mọi host (hành vi "cài xong chạy được ngay" — **giữ nguyên**);
* fleet **không giới hạn** phải **thấy được bằng máy**: ``fleet_unrestricted``
  + ``fleet_warning`` trong JSON của ``roff hosts`` / ``roff do``.

Test chạy CLI như **tiến trình con** (subprocess) — không mock — và dùng
``ssh`` giả trong ``tests/fake/`` nên **không** đụng fleet thật.
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

from roffkit import transfer as transfer_mod  # noqa: E402
from roffkit import watcher as watcher_mod  # noqa: E402
from roffkit.config import Config  # noqa: E402
from roffkit.errors import EXIT_CONFIG, RoffError  # noqa: E402

#: 4 máy "muse-*" + 1 máy Colab — giống hệt tình huống thật của người dùng.
SSH_HOSTS = ("muse-a", "muse-b", "muse-c", "muse-d", "colab-comfy")

#: Output probe tối thiểu (giống ``tests/unit/test_cli.py``) — host giả.
PROBE_OUTPUT = "\n".join(
    [
        "ROFF_HOSTNAME=htch-runtime",
        "ROFF_BOOT_ID=boot-1",
        "ROFF_HOME=/root",
        "ROFF_CORES=4",
        "ROFF_UPTIME_S=100000",
        "ROFF_LOAD1=0.10",
        "ROFF_MEM_TOTAL_KB=8388608",
        "ROFF_MEM_AVAILABLE_KB=921600",
        "ROFF_LAYOUT_ROOT=/home/hatch/.roff",
        "ROFF_DURABLE=1",
        "ROFF_DISK_FREE_MB=102400",
        "ROFF_INSTANCE_TOKEN=tok-1",
        "ROFF_CGROUP_LIMIT_MB=",
        "ROFF_CGROUP_USED_MB=",
        "ROFF_TOOL=ffmpeg=/usr/bin/ffmpeg",
        "ROFF_TOOL=node=/usr/bin/node",
        "ROFF_TOOL=python3=/usr/bin/python3",
        "ROFF_INTERNET_CODE=200",
        "ROFF_INTERNET_RC=0",
        "ROFF_INTERNET_ERR=",
        "",
    ]
)

LAUNCH_OUTPUT = "\n".join(
    [
        "ROFF_PID=4242",
        "ROFF_BOOT_ID=boot-1",
        "ROFF_INSTANCE_TOKEN=tok-1",
        "",
    ]
)


# --------------------------------------------------------------------------
# hạ tầng test
# --------------------------------------------------------------------------
@pytest.fixture
def home(tmp_path):
    """HOME cô lập — **không** có ``~/.config/roff/config.ini``."""
    path = tmp_path / "home"
    path.mkdir()
    return path


def _ssh_config(tmp_path, hosts=SSH_HOSTS) -> Path:
    """File ssh config giả với ``len(hosts)`` alias."""
    path = tmp_path / "ssh_config"
    lines = []
    for index, alias in enumerate(hosts):
        lines.append(f"Host {alias}\n  HostName 127.0.0.1\n  Port {2200 + index}\n")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _env(tmp_path, home, ssh_config, config=None) -> dict:
    """Env cô lập cho tiến trình con.

    ``config=None`` ⇒ **không** có ``ROFF_CONFIG`` (đường mặc định).
    """
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["ROFF_STATE_DIR"] = str(state)
    env["ROFF_SSH_CONFIG"] = str(ssh_config)
    env["ROFF_NO_WATCHER"] = "1"
    env.pop("ROFF_CONFIG", None)
    if config is not None:
        env["ROFF_CONFIG"] = str(config)
    return env


def run(args, env, timeout=60):
    """Chạy launcher như tiến trình con; trả (rc, stdout, stderr)."""
    proc = subprocess.run(
        [sys.executable, str(LAUNCHER)] + [str(a) for a in args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    return proc.returncode, proc.stdout, proc.stderr


def one_json(stdout: str) -> dict:
    """Parse stdout thành **đúng 1** JSON object; fail loud nếu không phải."""
    lines = [line for line in stdout.splitlines() if line.strip()]
    assert len(lines) == 1, f"phải in đúng 1 JSON object, thấy {len(lines)}:\n{stdout}"
    obj = json.loads(lines[0])
    assert isinstance(obj, dict), "JSON phải là object"
    return obj


def _good_host(fake_ssh, alias: str) -> None:
    """Cấu hình host giả trả lời đúng cho probe + launch."""
    fake_ssh(
        alias,
        responses=[
            {"match": "nproc", "stdout": PROBE_OUTPUT},
            {"match": "launch.sh", "stdout": LAUNCH_OUTPUT},
        ],
    )


# --------------------------------------------------------------------------
# 1. ROFF_CONFIG trỏ tới file không tồn tại ⇒ exit 3
# --------------------------------------------------------------------------
def test_roff_config_missing_exits_3(tmp_path, home, fake_ssh):
    """Config bị chỉ định mà không tồn tại ⇒ exit 3 + ``config_missing``."""
    missing = tmp_path / "khong-co-that-abc123" / "config.ini"
    env = _env(tmp_path, home, _ssh_config(tmp_path), config=missing)

    rc, out, _err = run(["hosts"], env)

    assert rc == EXIT_CONFIG, f"phải exit 3, thấy {rc}: {out}"
    payload = one_json(out)
    assert payload["ok"] is False
    assert payload["exit_code"] == EXIT_CONFIG
    assert payload["reason"] == "config_missing"
    # ``detail`` phải nói rõ **đường dẫn** (người dùng cần biết sửa ở đâu)
    assert str(missing) in payload["detail"]
    assert str(missing) in payload["error"]
    # Không được probe/gửi gì đi (không có payload host nào)
    assert "hosts" not in payload


def test_roff_do_with_missing_config_exits_3_and_creates_no_job(
    tmp_path, home, fake_ssh
):
    """``roff do`` với config thiếu ⇒ exit 3 **trước khi** tạo job."""
    missing = tmp_path / "khong-co-that-abc123" / "config.ini"
    env = _env(tmp_path, home, _ssh_config(tmp_path), config=missing)

    rc, out, _err = run(["do", "--cmd", "echo hi", "--name", "proj"], env)

    assert rc == EXIT_CONFIG, f"phải exit 3, thấy {rc}: {out}"
    payload = one_json(out)
    assert payload["reason"] == "config_missing"
    assert "job" not in payload
    # Ledger phải **rỗng** — không được tạo job rồi mới chết
    jobs_dir = Path(env["ROFF_STATE_DIR"]) / "jobs"
    leftovers = list(jobs_dir.glob("*")) if jobs_dir.is_dir() else []
    assert leftovers == [], f"đã tạo job dù config lỗi: {leftovers}"


# --------------------------------------------------------------------------
# 2. ROFF_CONFIG trỏ tới file có thật ⇒ chạy bình thường
# --------------------------------------------------------------------------
def test_roff_config_existing_keeps_include_and_exclude(tmp_path, home, fake_ssh):
    """Config có thật ⇒ fleet đúng 4 máy ``muse-*`` (không phải 5)."""
    config = tmp_path / "config.ini"
    config.write_text(
        "[fleet]\ninclude = muse-*\nexclude = colab-comfy\n", encoding="utf-8"
    )
    env = _env(tmp_path, home, _ssh_config(tmp_path), config=config)

    rc, out, _err = run(["hosts"], env)

    assert rc == 0, out
    payload = one_json(out)
    aliases = [host["alias"] for host in payload["hosts"]]
    assert payload["count"] == 4, aliases
    assert aliases == ["muse-a", "muse-b", "muse-c", "muse-d"]
    assert "colab-comfy" not in aliases


# --------------------------------------------------------------------------
# 3. Không có ROFF_CONFIG + không có ~/.config/roff/config.ini ⇒ vẫn chạy
# --------------------------------------------------------------------------
def test_no_config_anywhere_keeps_default_all_hosts(tmp_path, home, fake_ssh):
    """Không config ⇒ vẫn chạy, fleet = **mọi** host (hành vi mặc định)."""
    env = _env(tmp_path, home, _ssh_config(tmp_path))
    assert "ROFF_CONFIG" not in env
    assert not (home / ".config" / "roff" / "config.ini").exists()

    rc, out, _err = run(["hosts"], env)

    assert rc == 0, out
    payload = one_json(out)
    aliases = [host["alias"] for host in payload["hosts"]]
    assert payload["count"] == 5, aliases
    assert "colab-comfy" in aliases


# --------------------------------------------------------------------------
# 4. Cờ máy đọc được: fleet_unrestricted / fleet_warning
# --------------------------------------------------------------------------
def test_hosts_reports_fleet_unrestricted_when_config_does_not_limit(
    tmp_path, home, fake_ssh
):
    """Config có nhưng thiếu ``[fleet] include`` ⇒ phải báo "fleet đang mở"."""
    config = tmp_path / "config.ini"
    config.write_text("[local]\nnice = 19\n", encoding="utf-8")
    env = _env(tmp_path, home, _ssh_config(tmp_path), config=config)

    rc, out, _err = run(["hosts"], env)

    assert rc == 0, out
    payload = one_json(out)
    assert payload["fleet_unrestricted"] is True
    warning = payload["fleet_warning"]
    assert isinstance(warning, str) and warning
    assert "không giới hạn fleet" in warning


def test_hosts_reports_fleet_unrestricted_for_include_star(
    tmp_path, home, fake_ssh
):
    """``include = *`` ⇒ cũng là "không giới hạn" (dù có ``exclude``)."""
    config = tmp_path / "config.ini"
    config.write_text("[fleet]\ninclude = *\nexclude = colab-comfy\n", encoding="utf-8")
    env = _env(tmp_path, home, _ssh_config(tmp_path), config=config)

    rc, out, _err = run(["hosts"], env)

    assert rc == 0, out
    payload = one_json(out)
    assert payload["fleet_unrestricted"] is True
    assert payload["fleet_warning"]


def test_hosts_has_no_fleet_flags_when_fleet_is_limited(tmp_path, home, fake_ssh):
    """Fleet **có** giới hạn ⇒ không có 2 cờ (chọn cách "bỏ qua khi giới hạn")."""
    config = tmp_path / "config.ini"
    config.write_text("[fleet]\ninclude = muse-*\n", encoding="utf-8")
    env = _env(tmp_path, home, _ssh_config(tmp_path), config=config)

    rc, out, _err = run(["hosts"], env)

    assert rc == 0, out
    payload = one_json(out)
    assert "fleet_unrestricted" not in payload
    assert "fleet_warning" not in payload


def test_do_reports_fleet_unrestricted(tmp_path, home, fake_ssh):
    """JSON của ``roff do`` cũng phải có cờ khi fleet không giới hạn."""
    _good_host(fake_ssh, "muse-a")
    config = tmp_path / "config.ini"
    config.write_text("[fleet]\ninclude = muse-*\n", encoding="utf-8")
    env = _env(tmp_path, home, _ssh_config(tmp_path, hosts=("muse-a",)), config=config)

    rc, out, _err = run(["do", "--cmd", "echo hi", "--name", "proj"], env)

    assert rc == 0, out
    payload = one_json(out)
    assert payload["host"] == "muse-a"
    assert "fleet_unrestricted" not in payload
    assert "fleet_warning" not in payload


# --------------------------------------------------------------------------
# 5. doctor: vẫn chạy được và vẫn báo lỗi config
# --------------------------------------------------------------------------
def test_doctor_reports_missing_config_without_crashing(tmp_path, home, fake_ssh):
    """Config thiếu ⇒ doctor vẫn chạy, vẫn **báo** lỗi config."""
    missing = tmp_path / "khong-co-that-abc123" / "config.ini"
    env = _env(tmp_path, home, _ssh_config(tmp_path), config=missing)

    rc, out, _err = run(["doctor"], env)

    assert rc in (0, 1), out
    payload = one_json(out)
    check = [c for c in payload["checks"] if c["name"] == "config"][0]
    assert check["ok"] is False
    assert str(missing) in check["detail"]
    assert check.get("config_state") == "missing"
    # Lệnh không được crash (không có traceback trên stderr)
    assert "Traceback" not in _err


def test_doctor_distinguishes_unconfigured_from_unrestricted(
    tmp_path, home, fake_ssh
):
    """Hai mức độ khác nhau: "chưa cấu hình" vs "đang mở"."""
    ssh_config = _ssh_config(tmp_path)

    # (a) không có config ⇒ "chưa cấu hình"
    env = _env(tmp_path, home, ssh_config)
    rc, out, _err = run(["doctor"], env)
    assert rc in (0, 1), out
    check = [c for c in one_json(out)["checks"] if c["name"] == "config"][0]
    assert check["ok"] is False
    assert check.get("config_state") == "unconfigured"
    assert "chưa cấu hình" in check["detail"]

    # (b) có config nhưng không giới hạn fleet ⇒ "đang mở"
    config = tmp_path / "config.ini"
    config.write_text("[local]\nnice = 19\n", encoding="utf-8")
    env = _env(tmp_path, home, ssh_config, config=config)
    rc, out, _err = run(["doctor"], env)
    assert rc in (0, 1), out
    check = [c for c in one_json(out)["checks"] if c["name"] == "config"][0]
    assert check["ok"] is False
    assert check.get("config_state") == "unrestricted"
    assert "đang mở" in check["detail"]


def test_doctor_reports_ok_when_fleet_is_limited(tmp_path, home, fake_ssh):
    """Config giới hạn fleet ⇒ check ``config`` phải ``ok``."""
    config = tmp_path / "config.ini"
    config.write_text("[fleet]\ninclude = muse-*\n", encoding="utf-8")
    env = _env(tmp_path, home, _ssh_config(tmp_path), config=config)

    rc, out, _err = run(["doctor"], env)

    assert rc in (0, 1), out
    check = [c for c in one_json(out)["checks"] if c["name"] == "config"][0]
    assert check["ok"] is True
    assert check.get("config_state") == "ok"
    assert check.get("fleet_unrestricted") is False


# --------------------------------------------------------------------------
# 6. Tầng hàm: strict=True / strict=False
# --------------------------------------------------------------------------
def test_config_load_strict_raises_config_missing(tmp_path):
    """``strict=True`` + path tường minh không tồn tại ⇒ RoffError(3)."""
    missing = tmp_path / "khong-co.ini"
    with pytest.raises(RoffError) as exc:
        Config.load(str(missing), strict=True)
    assert exc.value.code == EXIT_CONFIG
    payload = exc.value.payload()
    assert payload["reason"] == "config_missing"
    assert str(missing) in payload["detail"]
    assert str(missing) in payload["error"]


def test_config_load_non_strict_keeps_old_behaviour(tmp_path):
    """``strict=False`` (mặc định) ⇒ giữ hành vi cũ (config mặc định)."""
    cfg = Config.load(str(tmp_path / "khong-co.ini"))
    assert cfg.path is None
    assert cfg.fleet_include == ["*"]


def test_config_load_default_path_missing_is_not_an_error(tmp_path, monkeypatch):
    """``path=None`` (đường mặc định) không tồn tại ⇒ **không** phải lỗi."""
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = Config.load(strict=True)
    assert cfg.path is None
    assert cfg.fleet_include == ["*"]


def test_config_fleet_unrestricted_flag(tmp_path):
    """``include`` hiệu lực là ``["*"]`` ⇒ fleet không giới hạn."""
    config = tmp_path / "config.ini"
    config.write_text("[fleet]\ninclude = muse-*\n", encoding="utf-8")
    assert Config.load(str(config)).fleet_unrestricted() is False

    config.write_text("[local]\nnice = 19\n", encoding="utf-8")
    assert Config.load(str(config)).fleet_unrestricted() is True

    config.write_text("[fleet]\ninclude = *\n", encoding="utf-8")
    assert Config.load(str(config)).fleet_unrestricted() is True


def test_watcher_and_transfer_keep_non_strict_config(tmp_path, monkeypatch):
    """Watcher/transfer đọc config **sau khi** job đã ghim host ⇒ không được chết.

    Ném lỗi ở đó ⇒ watcher chết ⇒ job mất im lặng (tệ hơn dùng config mặc định).
    """
    monkeypatch.setenv("ROFF_CONFIG", str(tmp_path / "khong-co.ini"))
    assert watcher_mod._config() is not None
    assert transfer_mod._config() is not None
