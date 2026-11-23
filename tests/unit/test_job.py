"""Unit test cho ``roffkit.job``.

Không cần mạng thật: seam ``_run_ssh`` được monkeypatch để trả về output giả
(các dòng ``ROFF_*``).

Nguyên tắc được kiểm tra:
  * job id dạng ``YYMMDD-HHMMSS-xxxx``;
  * ``launch`` ghi pid + boot_id + instance_token vào meta.json **lúc launch**;
  * secret (``--env``) **không** lọt vào argv (bug R7) và file env mode 600;
  * ``remote_state`` gom MỌI thứ trong **một** round-trip ssh;
  * phân biệt host_restarted / host_wiped / vanished / oom_killed;
  * ``cancel`` kill theo **pid đã ghi nhận**, không pkill theo pattern (bug R19).
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill"))

from roffkit import job as job_mod  # noqa: E402
from roffkit.job import (  # noqa: E402
    JOB_STATES,
    Job,
    cancel,
    heartbeat_path,
    launch,
    ledger_dir,
    list_jobs,
    load_job,
    new_job_id,
    remote_state,
    save_job,
)


# --------------------------------------------------------------------------
# giả lập
# --------------------------------------------------------------------------
class FakeSsh:
    """Giả lập ``_run_ssh(alias, script, timeout, stdin=None)``."""

    def __init__(self, stdout="", rc=0, stderr=""):
        self.stdout = stdout
        self.rc = rc
        self.stderr = stderr
        self.calls: list[dict] = []

    def __call__(self, alias, script, timeout=30, stdin=None):
        self.calls.append(
            {"alias": alias, "script": script, "timeout": timeout, "stdin": stdin}
        )
        return (self.rc, self.stdout, self.stderr)

    @property
    def last(self) -> dict:
        return self.calls[-1]


def tar_members(data: bytes) -> dict[str, tuple[int, bytes]]:
    """Đọc member của tar → {tên: (mode, nội dung)}."""
    out: dict[str, tuple[int, bytes]] = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tf:
        for member in tf.getmembers():
            payload = tf.extractfile(member)
            out[member.name] = (member.mode, payload.read() if payload else b"")
    return out


def make_job(**kwargs) -> Job:
    """Job mẫu (đã có attempt để so sánh boot_id/token)."""
    base = dict(
        id="261123-101530-ab12",
        cmd="echo hello",
        host="muse-a",
        project="proj",
        created_at=1_700_000_000.0,
        out_remote="/home/hatch/.roff/jobs/261123-101530-ab12/out",
    )
    base.update(kwargs)
    return Job(**base)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("ROFF_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ROFF_CONFIG", str(tmp_path / "none.ini"))


# --------------------------------------------------------------------------
# id + ledger
# --------------------------------------------------------------------------
def test_new_job_id_format():
    jid = new_job_id()
    assert re.fullmatch(r"\d{6}-\d{6}-[0-9a-f]{4}", jid), jid


def test_new_job_id_unique():
    assert len({new_job_id() for _ in range(50)}) > 1


def test_ledger_dir_under_state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ROFF_STATE_DIR", str(tmp_path / "state"))
    assert ledger_dir() == str(tmp_path / "state" / "jobs")


def test_save_and_load_job_roundtrip():
    j = make_job(cpus=2, mem_mb=512, shard=0, shards=3, item="a.mp4")
    j.attempts = [{"host": "muse-a", "boot_id": "b1", "instance_token": "t1"}]
    save_job(j)

    loaded = load_job(j.id)
    assert loaded.cmd == j.cmd
    assert loaded.host == j.host
    assert loaded.project == j.project
    assert loaded.out_remote == j.out_remote
    assert loaded.cpus == 2
    assert loaded.mem_mb == 512
    assert loaded.shard == 0 and loaded.shards == 3
    assert loaded.item == "a.mp4"
    assert loaded.attempts == j.attempts


def test_load_job_missing_raises_exit_input():
    with pytest.raises(Exception) as exc:
        load_job("261123-000000-ffff")
    assert exc.value.code == 2  # EXIT_INPUT


def test_list_jobs_filters_by_state():
    a = make_job(id="job-a", state="running")
    b = make_job(id="job-b", state="exited")
    save_job(a)
    save_job(b)

    assert {j.id for j in list_jobs()} == {"job-a", "job-b"}
    assert {j.id for j in list_jobs(state="exited")} == {"job-b"}
    assert list_jobs(state="lost") == []


def test_job_states_contain_terminal_and_running():
    assert "exited" in JOB_STATES
    assert "lost" in JOB_STATES
    assert "cancelled" in JOB_STATES
    assert "running" in JOB_STATES


def test_heartbeat_path_inside_ledger_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ROFF_STATE_DIR", str(tmp_path / "state"))
    path = heartbeat_path("abc")
    assert path.startswith(str(tmp_path / "state"))
    assert path.endswith("abc.heartbeat")


def test_job_options_roundtrip():
    j = make_job()
    save_job(j)
    job_mod.set_job_options(j.id, retry=2, input_dir="/tmp/x")
    opts = job_mod.job_options(j.id)
    assert opts["retry"] == 2
    assert opts["input_dir"] == "/tmp/x"
    # save_job không được xoá khối tuỳ chọn
    save_job(j)
    assert job_mod.job_options(j.id)["retry"] == 2


def test_job_env_file_is_secret_safe(tmp_path):
    j = make_job()
    save_job(j)
    path = job_mod.save_job_env(j.id, {"TOKEN": "s3cr3t-value"})
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    assert job_mod.load_job_env(j.id) == {"TOKEN": "s3cr3t-value"}


# --------------------------------------------------------------------------
# launch
# --------------------------------------------------------------------------
LAUNCH_STDOUT = (
    "ROFF_PID=4242\n"
    "ROFF_BOOT_ID=boot-xyz\n"
    "ROFF_INSTANCE_TOKEN=tok-abc\n"
)


def test_launch_writes_pid_boot_token_to_meta_json(monkeypatch):
    fake = FakeSsh(stdout=LAUNCH_STDOUT)
    monkeypatch.setattr(job_mod, "_run_ssh", fake)

    j = make_job()
    launch("muse-a", j, env={"TOKEN": "s3cr3t-value"}, cpus=2, mem_mb=700)

    members = tar_members(fake.last["stdin"])
    assert "env" in members
    assert "cmd" in members
    assert "run.sh" in members
    assert "meta.json" in members

    meta = json.loads(members["meta.json"][1].decode())
    assert meta["job_id"] == j.id
    assert meta["boot_id"] == "__ROFF_BOOT_ID__"  # placeholder, host điền sau
    assert meta["instance_token"] == "__ROFF_INSTANCE_TOKEN__"

    # launch phải ghi lại pid + boot_id + instance_token vào ledger local
    attempt = j.attempts[-1]
    assert attempt["pid"] == 4242
    assert attempt["boot_id"] == "boot-xyz"
    assert attempt["instance_token"] == "tok-abc"
    assert attempt["host"] == "muse-a"

    saved = load_job(j.id)
    assert saved.attempts[-1]["pid"] == 4242


def test_launch_env_file_mode_600_and_secret_not_in_argv(monkeypatch):
    fake = FakeSsh(stdout=LAUNCH_STDOUT)
    monkeypatch.setattr(job_mod, "_run_ssh", fake)

    j = make_job()
    launch("muse-a", j, env={"TOKEN": "s3cr3t-value-xyz"})

    members = tar_members(fake.last["stdin"])
    mode, payload = members["env"]
    assert mode == 0o600
    assert b"s3cr3t-value-xyz" in payload
    # secret KHÔNG được nằm trong argv (thấy được qua ps)
    assert "s3cr3t-value-xyz" not in fake.last["script"]
    assert "s3cr3t-value-xyz" not in str(fake.calls[0]["alias"])


def test_launch_requires_out_remote(monkeypatch):
    monkeypatch.setattr(job_mod, "_run_ssh", FakeSsh(stdout=LAUNCH_STDOUT))
    j = make_job(out_remote=None)
    with pytest.raises(Exception) as exc:
        launch("muse-a", j)
    assert exc.value.code == 2  # EXIT_INPUT


def test_launch_workdir_missing_exits_97(monkeypatch):
    fake = FakeSsh(stdout="ROFF_ERR=workdir_missing\n", rc=97)
    monkeypatch.setattr(job_mod, "_run_ssh", fake)
    j = make_job()
    with pytest.raises(Exception) as exc:
        launch("muse-a", j)
    assert exc.value.code == 97  # EXIT_NO_WORKDIR


def test_launch_unreachable_reports_detail(monkeypatch):
    fake = FakeSsh(rc=255, stderr="ssh: connect to host muse-a port 2222: Connection refused")
    monkeypatch.setattr(job_mod, "_run_ssh", fake)
    j = make_job()
    with pytest.raises(Exception) as exc:
        launch("muse-a", j)
    assert exc.value.code == 255  # EXIT_UNREACHABLE
    assert "Connection refused" in str(exc.value.fields.get("detail", ""))


# --------------------------------------------------------------------------
# remote_state
# --------------------------------------------------------------------------
def _state_output(
    exit_file="0",
    exit_code="",
    boot_id="boot-xyz",
    token="tok-abc",
    workdir="1",
    pid="4242",
    alive="1",
    oom="0",
) -> str:
    return (
        f"ROFF_EXIT_FILE={exit_file}\n"
        f"ROFF_EXIT_CODE={exit_code}\n"
        f"ROFF_BOOT_ID={boot_id}\n"
        f"ROFF_INSTANCE_TOKEN={token}\n"
        f"ROFF_WORKDIR_EXISTS={workdir}\n"
        f"ROFF_PID={pid}\n"
        f"ROFF_PID_ALIVE={alive}\n"
        f"ROFF_OOM={oom}\n"
    )


def _job_with_attempt(**kwargs) -> Job:
    j = make_job(**kwargs)
    j.attempts = [
        {
            "host": "muse-a",
            "pid": 4242,
            "boot_id": "boot-xyz",
            "instance_token": "tok-abc",
            "started": 1.0,
        }
    ]
    return j


def test_remote_state_running(monkeypatch):
    monkeypatch.setattr(job_mod, "_run_ssh", FakeSsh(stdout=_state_output()))
    st = remote_state("muse-a", _job_with_attempt())
    assert st["state"] == "running"
    assert st["exit"] is None
    assert st["reachable"] is True
    assert st["boot_id"] == "boot-xyz"
    assert st["instance_token"] == "tok-abc"
    assert st["workdir_exists"] is True


def test_remote_state_exited_reads_exit_code(monkeypatch):
    out = _state_output(exit_file="1", exit_code="3", alive="0")
    monkeypatch.setattr(job_mod, "_run_ssh", FakeSsh(stdout=out))
    st = remote_state("muse-a", _job_with_attempt())
    assert st["state"] == "exited"
    assert st["exit"] == 3


def test_remote_state_lost_host_restarted(monkeypatch):
    out = _state_output(boot_id="boot-NEW", alive="0")
    monkeypatch.setattr(job_mod, "_run_ssh", FakeSsh(stdout=out))
    st = remote_state("muse-a", _job_with_attempt())
    assert st["state"] == "lost"
    assert st["reason"] == "host_restarted"


def test_remote_state_lost_host_wiped(monkeypatch):
    out = _state_output(token="tok-NEW", alive="0")
    monkeypatch.setattr(job_mod, "_run_ssh", FakeSsh(stdout=out))
    st = remote_state("muse-a", _job_with_attempt())
    assert st["state"] == "lost"
    assert st["reason"] == "host_wiped"


def test_remote_state_lost_vanished(monkeypatch):
    out = _state_output(alive="0")
    monkeypatch.setattr(job_mod, "_run_ssh", FakeSsh(stdout=out))
    st = remote_state("muse-a", _job_with_attempt())
    assert st["state"] == "lost"
    assert st["reason"] == "vanished"


def test_remote_state_lost_oom_killed(monkeypatch):
    out = _state_output(alive="0", oom="1")
    monkeypatch.setattr(job_mod, "_run_ssh", FakeSsh(stdout=out))
    st = remote_state("muse-a", _job_with_attempt())
    assert st["state"] == "lost"
    assert st["reason"] == "oom_killed"


def test_remote_state_unreachable_is_not_lost(monkeypatch):
    fake = FakeSsh(rc=255, stderr="ssh: connect to host muse-a port 2222: Connection refused")
    monkeypatch.setattr(job_mod, "_run_ssh", fake)
    st = remote_state("muse-a", _job_with_attempt())
    assert st["reachable"] is False
    assert st["state"] != "lost"
    assert "Connection refused" in (st.get("detail") or "")


def test_remote_state_uses_one_round_trip(monkeypatch):
    fake = FakeSsh(stdout=_state_output())
    monkeypatch.setattr(job_mod, "_run_ssh", fake)
    remote_state("muse-a", _job_with_attempt())
    assert len(fake.calls) == 1


# --------------------------------------------------------------------------
# cancel
# --------------------------------------------------------------------------
def test_cancel_kills_recorded_pid_group_only(monkeypatch):
    fake = FakeSsh(stdout="ROFF_PID=4242\nROFF_KILLED=1\nROFF_PID_ALIVE=0\n")
    monkeypatch.setattr(job_mod, "_run_ssh", fake)

    j = _job_with_attempt()
    res = cancel("muse-a", j)

    script = fake.last["script"]
    assert "pkill" not in script, "không được pkill theo pattern"
    assert "kill -TERM -4242" in script
    assert "kill -KILL -4242" in script
    assert res["killed"] is True
    assert res["pid"] == 4242


def test_cancel_marks_job_cancelled(monkeypatch):
    fake = FakeSsh(stdout="ROFF_PID=4242\nROFF_KILLED=1\nROFF_PID_ALIVE=0\n")
    monkeypatch.setattr(job_mod, "_run_ssh", fake)
    j = _job_with_attempt()
    save_job(j)

    cancel("muse-a", j)

    assert j.state == "cancelled"
    assert load_job(j.id).state == "cancelled"


# --------------------------------------------------------------------------
# chạy thật script remote (mô phỏng host bằng /bin/sh local)
# --------------------------------------------------------------------------
def test_launch_scripts_run_for_real_locally(tmp_path, monkeypatch):
    """Script launch/run/state phải chạy thật được (không chỉ "syntax đúng").

    Mô phỏng host bằng ``/bin/sh`` local: bootstrap (mkdir + tar + launch.sh)
    được chạy thật, job được launch thật, exit file + meta.json phải đúng.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    workdir = tmp_path / "proj"
    workdir.mkdir()
    jid = "261123-101530-ab12"
    job = make_job(
        id=jid,
        cmd="echo hello-remote; exit 0",
        out_remote=str(tmp_path / "jobs" / jid / "out"),
    )
    save_job(job)
    job_mod.set_job_options(job.id, workdir=str(workdir))

    def local_shell(alias, script, timeout=30, stdin=None):
        proc = subprocess.run(
            ["/bin/sh", "-c", script],
            input=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        return (
            proc.returncode,
            proc.stdout.decode("utf-8", "replace"),
            proc.stderr.decode("utf-8", "replace"),
        )

    monkeypatch.setattr(job_mod, "_run_ssh", local_shell)

    launch("muse-a", job, env={"MY_SECRET": "s3cr3t-value"}, cpus=2, mem_mb=700)

    job_dir = tmp_path / "jobs" / jid
    # env file mode 600, secret nằm trong file chứ không nằm trong argv
    env_file = job_dir / "env"
    assert env_file.is_file()
    assert oct(env_file.stat().st_mode & 0o777) == "0o600"
    assert "s3cr3t-value" in env_file.read_text()

    # job đã chạy xong → có exit file + log
    deadline = time.time() + 10
    while time.time() < deadline and not (job_dir / "exit").is_file():
        time.sleep(0.1)
    assert (job_dir / "exit").is_file(), "job không tự ghi exit file"
    assert (job_dir / "exit").read_text().strip() == "0"
    assert "hello-remote" in (job_dir / "log").read_text()

    # meta.json đã điền pid/boot_id/instance_token (không còn placeholder)
    meta = json.loads((job_dir / "meta.json").read_text())
    assert isinstance(meta["pid"], int) and meta["pid"] > 0
    assert meta["boot_id"] and meta["boot_id"] != "__ROFF_BOOT_ID__"
    assert meta["instance_token"]
    assert meta["instance_token"] != "__ROFF_INSTANCE_TOKEN__"

    # remote_state đọc được exit + boot/token khớp lúc launch
    st = remote_state("muse-a", job)
    assert st["state"] == "exited"
    assert st["exit"] == 0
    assert st["boot_id"] == meta["boot_id"]
    assert st["instance_token"] == meta["instance_token"]
