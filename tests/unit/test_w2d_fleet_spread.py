"""Hồi quy W2d — ``roff do`` không được chết chỉ vì thiếu host giữa lô.

Sự cố: fleet có nhiều host, một host chết giữa lô ⇒ lệnh exit 10 ⇒ các job đã
tạo trước đó bị **bỏ rơi** (không ai theo dõi, agent không biết job id để
``roff wait``).

Hợp đồng mới:

* chỉ fail khi **toàn bộ** fleet không dùng được (exit 10, reason ``no_host``);
* không còn host *chưa dùng* nào dùng được ⇒ **dùng lại** host đã dùng
  (ít tải nhất) ⇒ job xếp hàng (FIFO) thay vì chết lệnh;
* job đã tạo không bị bỏ rơi: payload lỗi của ``RoffError`` có khoá ``jobs``.

Test chạy **không cần mạng**: dùng ssh giả (fixture ``fake_ssh``).
"""

from __future__ import annotations

import json
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(_HERE))
_SKILL = os.path.join(_REPO, "skill")
if _SKILL not in sys.path:
    sys.path.insert(0, _SKILL)

from conftest import parse_json, run_cli  # noqa: E402

from roffkit import job as job_mod  # noqa: E402


def _probe_out(
    hostname: str,
    boot_id: str,
    token: str,
    cores: int = 4,
    mem_mb: int = 900,
) -> str:
    """Output probe giả (mỗi host 1 cặp boot_id/token riêng ⇒ khác máy vật lý)."""
    return "\n".join(
        [
            f"ROFF_HOSTNAME={hostname}",
            f"ROFF_BOOT_ID={boot_id}",
            "ROFF_HOME=/root",
            f"ROFF_CORES={cores}",
            "ROFF_MEM_TOTAL_KB=8388608",
            f"ROFF_MEM_AVAILABLE_KB={mem_mb * 1024}",
            "ROFF_LAYOUT_ROOT=/home/hatch/.roff",
            "ROFF_DURABLE=1",
            "ROFF_DISK_FREE_MB=102400",
            f"ROFF_INSTANCE_TOKEN={token}",
            "ROFF_CGROUP_LIMIT_MB=",
            "ROFF_CGROUP_USED_MB=",
            "ROFF_TOOL=python3=/usr/bin/python3",
            "ROFF_INTERNET_CODE=200",
            "ROFF_INTERNET_RC=0",
            "ROFF_INTERNET_ERR=",
            "",
        ]
    )


def _cli_env(tmp_path, monkeypatch, aliases: list[str]) -> dict:
    """Env cô lãi cho CLI: state/config/ssh-config riêng + không spawn watcher.

    Env của **tiến trình test** cũng được đặt ⇒ job tạo trong test nằm đúng
    ledger mà CLI con đọc.
    """
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    config = tmp_path / "config.ini"
    config.write_text("[fleet]\ninclude = muse-*\n")
    ssh_config = tmp_path / "ssh_config"
    body = []
    for alias in aliases:
        body.append(f"Host {alias}\n  HostName 127.0.0.1\n  Port 2222\n")
    ssh_config.write_text("\n".join(body))
    env = dict(os.environ)
    env.update(
        {
            "ROFF_STATE_DIR": str(state),
            "ROFF_CONFIG": str(config),
            "ROFF_SSH_CONFIG": str(ssh_config),
            "ROFF_NO_WATCHER": "1",
        }
    )
    for key in ("ROFF_STATE_DIR", "ROFF_CONFIG", "ROFF_SSH_CONFIG"):
        monkeypatch.setenv(key, env[key])
    return env


def _job_files(tmp_path) -> list[str]:
    """Danh sách file job trong ledger của test."""
    jobs_dir = tmp_path / "state" / "jobs"
    if not jobs_dir.is_dir():
        return []
    return sorted(p.name for p in jobs_dir.glob("*.json"))


def _states(tmp_path) -> list[str]:
    """State của mọi job trong ledger."""
    out = []
    for name in _job_files(tmp_path):
        data = json.loads((tmp_path / "state" / "jobs" / name).read_text())
        out.append(data.get("state"))
    return out


def _items(tmp_path, count: int = 3) -> str:
    """Tạo ``count`` file item cho ``--each`` ⇒ trả glob."""
    folder = tmp_path / "items"
    folder.mkdir(exist_ok=True)
    for index in range(count):
        (folder / f"item-{index}.txt").write_text(f"item {index}")
    return str(folder / "*.txt")


# --------------------------------------------------------------------------
# fleet có host chết giữa lô ⇒ KHÔNG exit 10
# --------------------------------------------------------------------------
def test_do_with_dead_host_in_fleet_creates_all_jobs(fake_ssh, tmp_path, monkeypatch):
    """1 host sống + 1 host chết, 3 item ⇒ exit 0 và tạo đủ 3 job."""
    fake_ssh(
        "muse-alive",
        responses=[
            {"match": "nproc", "stdout": _probe_out("htch-a", "boot-a", "tok-a", cores=8)},
        ],
    )
    fake_ssh(
        "muse-dead",
        ssh_error="ssh: connect to host muse-dead port 22: Connection refused",
    )
    env = _cli_env(tmp_path, monkeypatch, ["muse-alive", "muse-dead"])

    rc, out, _err = run_cli(
        ["do", "--cmd", "echo hi", "--name", "proj", "--each", _items(tmp_path, 3)],
        env=env,
    )

    assert rc == 0, f"host chết giữa lô KHÔNG được làm lệnh chết: {out}"
    payload = parse_json(out)
    assert payload["ok"] is True
    assert payload.get("count") == 3, "phải tạo đủ 3 job"
    assert len(_job_files(tmp_path)) == 3
    # Job 1 chạy, job 2-3 xếp hàng (host hết slot) ⇒ KHÔNG job nào bị mất
    assert "launched" in _states(tmp_path)
    assert "lost" not in _states(tmp_path)


def test_do_reuses_used_host_when_fleet_is_small(fake_ssh, tmp_path, monkeypatch):
    """Fleet 1 host sống + N item ⇒ host được **dùng lại**, không chết lệnh."""
    fake_ssh(
        "muse-alive",
        responses=[
            {"match": "nproc", "stdout": _probe_out("htch-a", "boot-a", "tok-a")},
        ],
    )
    env = _cli_env(tmp_path, monkeypatch, ["muse-alive"])

    rc, out, _err = run_cli(
        ["do", "--cmd", "echo hi", "--name", "proj", "--each", _items(tmp_path, 4)],
        env=env,
    )

    assert rc == 0, out
    payload = parse_json(out)
    assert payload["ok"] is True
    assert payload.get("count") == 4
    assert len(_job_files(tmp_path)) == 4
    assert _states(tmp_path).count("queued") == 3, "3 job sau phải xếp hàng"


# --------------------------------------------------------------------------
# cả fleet chết ⇒ exit 10 + payload có khoá "jobs"
# --------------------------------------------------------------------------
def test_do_with_whole_fleet_unreachable_exits_10(fake_ssh, tmp_path, monkeypatch):
    """Cả fleet chết ⇒ exit 10, reason ``no_host``, payload có khoá ``jobs``."""
    fake_ssh(
        "muse-dead1",
        ssh_error="ssh: connect to host muse-dead1 port 22: Connection refused",
    )
    fake_ssh(
        "muse-dead2",
        ssh_error="ssh: connect to host muse-dead2 port 22: Connection timed out",
    )
    env = _cli_env(tmp_path, monkeypatch, ["muse-dead1", "muse-dead2"])

    rc, out, _err = run_cli(["do", "--cmd", "echo hi", "--name", "proj"], env=env)

    assert rc == 10, f"cả fleet chết ⇒ exit 10: {out}"
    payload = parse_json(out)
    assert payload["ok"] is False
    assert payload["reason"] == "no_host"
    assert "không với được host" in payload["error"]
    rejected = payload.get("rejected") or {}
    assert rejected, "payload phải nói lý do loại từng host"
    assert any("không với được host" in why for why in rejected.values())
    assert "jobs" in payload, "payload lỗi phải có khoá jobs (agent còn wait được)"
    assert payload["jobs"] == []


# --------------------------------------------------------------------------
# lỗi giữa lô ⇒ job đã tạo không bị bỏ rơi
# --------------------------------------------------------------------------
def test_mid_batch_failure_reports_created_jobs(fake_ssh, tmp_path, monkeypatch):
    """Job 1 tạo xong, job 2 launch fail ⇒ payload có ``jobs`` + không mất job."""
    fake_ssh(
        "muse-a",
        responses=[
            {"match": "nproc", "stdout": _probe_out("htch-a", "boot-a", "tok-a", cores=8)},
            {"match": "launch.sh", "stdout": "ROFF_PID=111\n"},
        ],
    )
    fake_ssh(
        "muse-b",
        responses=[
            {"match": "nproc", "stdout": _probe_out("htch-b", "boot-b", "tok-b", cores=4)},
            {"match": "launch.sh", "exit": 1, "stderr": "launch failed on host"},
        ],
    )
    env = _cli_env(tmp_path, monkeypatch, ["muse-a", "muse-b"])

    rc, out, _err = run_cli(
        ["do", "--cmd", "echo hi", "--name", "proj", "--each", _items(tmp_path, 2)],
        env=env,
    )

    assert rc != 0, "launch fail ⇒ lệnh phải báo lỗi"
    payload = parse_json(out)
    assert payload["ok"] is False
    assert "jobs" in payload, "payload lỗi phải kèm job id đã tạo"
    assert len(payload["jobs"]) >= 1
    for entry in payload["jobs"]:
        assert entry.get("job"), "mỗi job phải có id"
        assert entry.get("host"), "mỗi job phải kèm host"

    # Job đã tạo không bị bỏ rơi ở state "queued" ⇒ phải có trạng thái rõ ràng
    states = _states(tmp_path)
    assert states, "phải có job trong ledger"
    assert "queued" not in states, "job đã tạo mà không watcher không được mồ côi"


def test_failed_job_is_marked_lost_not_orphaned(fake_ssh, tmp_path, monkeypatch):
    """Job launch fail ⇒ state ``lost`` + reason rõ ràng (không mồ côi)."""
    fake_ssh(
        "muse-a",
        responses=[
            {"match": "nproc", "stdout": _probe_out("htch-a", "boot-a", "tok-a", cores=8)},
            {"match": "launch.sh", "stdout": "ROFF_PID=111\n"},
        ],
    )
    fake_ssh(
        "muse-b",
        responses=[
            {"match": "nproc", "stdout": _probe_out("htch-b", "boot-b", "tok-b", cores=4)},
            {"match": "launch.sh", "exit": 1, "stderr": "launch failed on host"},
        ],
    )
    env = _cli_env(tmp_path, monkeypatch, ["muse-a", "muse-b"])

    rc, _out, _err = run_cli(
        ["do", "--cmd", "echo hi", "--name", "proj", "--each", _items(tmp_path, 2)],
        env=env,
    )
    assert rc != 0

    jobs = [job_mod.load_job(p.stem) for p in (tmp_path / "state" / "jobs").glob("*.json")]
    lost = [job for job in jobs if job.state == "lost"]
    assert lost, "job launch fail phải được đánh dấu lost"
    assert lost[0].reason == "launch_failed"


@pytest.mark.parametrize("count", [2, 3])
def test_do_with_mixed_fleet_never_exits_10(fake_ssh, tmp_path, monkeypatch, count):
    """Nhiều host, 1 chết ⇒ **không** bao giờ exit 10 khi còn host dùng được."""
    fake_ssh(
        "muse-alive",
        responses=[
            {"match": "nproc", "stdout": _probe_out("htch-a", "boot-a", "tok-a")},
        ],
    )
    fake_ssh(
        "muse-dead",
        ssh_error="ssh: connect to host muse-dead port 22: Connection refused",
    )
    env = _cli_env(tmp_path, monkeypatch, ["muse-alive", "muse-dead"])

    rc, out, _err = run_cli(
        ["do", "--cmd", "echo hi", "--name", "proj", "--each", _items(tmp_path, count)],
        env=env,
    )

    assert rc == 0, out
    assert len(_job_files(tmp_path)) == count
