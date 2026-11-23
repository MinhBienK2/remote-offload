"""Unit test cho ``roffkit.watcher``.

Quy tắc vàng (API-CONTRACT §11): **mất kết nối KHÔNG đồng nghĩa job chết**.

Bốn tổ hợp bắt buộc của :func:`failover_decision`:
  1. ``boot_id`` khác              → ``host_restarted``  (failover được)
  2. ``instance_token`` khác       → ``host_wiped``      (failover được, cần re-push)
  3. boot_id + token GIỐNG, job không còn process → ``vanished`` (failover được)
  4. không kết nối được, chưa xác minh được gì   → ``None`` (KHÔNG failover)

Ngoài ra: retry **chỉ** khi lỗi thuộc hạ tầng; **không bao giờ** retry khi job
tự fail (bug R20); ``wait_for`` poll ledger **cục bộ**, không ssh.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill"))

from roffkit import job as job_mod  # noqa: E402
from roffkit import watcher as watcher_mod  # noqa: E402
from roffkit.watcher import (  # noqa: E402
    failover_decision,
    should_retry,
    spawn_watcher,
    validate_timeout,
    wait_for,
    watch,
)


class StopLoop(Exception):
    """Dùng để thoát vòng lặp watch() trong test."""


def make_job(**kwargs) -> job_mod.Job:
    base = dict(
        id="261123-101530-ab12",
        cmd="echo hello",
        host="muse-a",
        project="proj",
        created_at=1_700_000_000.0,
        out_remote="/home/hatch/.roff/jobs/261123-101530-ab12/out",
    )
    base.update(kwargs)
    return job_mod.Job(**base)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("ROFF_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ROFF_CONFIG", str(tmp_path / "none.ini"))


# --------------------------------------------------------------------------
# failover_decision — 4 tổ hợp bắt buộc
# --------------------------------------------------------------------------
BEFORE = {
    "reachable": True,
    "boot_id": "boot-1",
    "instance_token": "tok-1",
    "process_alive": True,
}


def test_failover_decision_host_restarted():
    now = dict(BEFORE, boot_id="boot-2")
    assert failover_decision(BEFORE, now) == "host_restarted"


def test_failover_decision_host_wiped():
    now = dict(BEFORE, instance_token="tok-2")
    assert failover_decision(BEFORE, now) == "host_wiped"


def test_failover_decision_vanished():
    now = dict(BEFORE, process_alive=False)
    assert failover_decision(BEFORE, now) == "vanished"


def test_failover_decision_unreachable_does_not_failover():
    now = {"reachable": False}
    assert failover_decision(BEFORE, now) is None


def test_failover_decision_same_everything_means_alive():
    assert failover_decision(BEFORE, dict(BEFORE)) is None


def test_failover_decision_unreachable_beats_changed_ids():
    """Chưa kết nối được thì KHÔNG được failover, kể cả khi id có vẻ đổi."""
    now = {"reachable": False, "boot_id": "boot-2", "instance_token": "tok-2"}
    assert failover_decision(BEFORE, now) is None


# --------------------------------------------------------------------------
# watch(): không retry job tự fail (bug R20)
# --------------------------------------------------------------------------
def test_watch_does_not_retry_failed_job(monkeypatch):
    j = make_job(state="running")
    j.attempts = [{"host": "muse-a", "boot_id": "b", "instance_token": "t"}]
    failovers = []

    monkeypatch.setattr(watcher_mod, "load_job", lambda job_id: j)
    monkeypatch.setattr(watcher_mod, "save_job", lambda job: None)
    monkeypatch.setattr(
        watcher_mod,
        "remote_state",
        lambda host, job, timeout=30: {
            "state": "exited",
            "exit": 2,
            "reachable": True,
            "boot_id": "b",
            "instance_token": "t",
            "workdir_exists": True,
        },
    )
    monkeypatch.setattr(watcher_mod, "_failover", lambda job, reason: failovers.append(reason) or True)

    watch(j.id)

    assert failovers == [], "job tự fail thì KHÔNG được retry"
    assert j.state == "exited"
    assert j.exit_code == 2


def test_watch_exited_zero_pulls_artifacts(monkeypatch):
    j = make_job(state="running", out_local="/tmp/out")
    j.attempts = [{"host": "muse-a", "boot_id": "b", "instance_token": "t"}]
    pulled = []

    monkeypatch.setattr(watcher_mod, "load_job", lambda job_id: j)
    monkeypatch.setattr(watcher_mod, "save_job", lambda job: None)
    monkeypatch.setattr(
        watcher_mod,
        "remote_state",
        lambda host, job, timeout=30: {
            "state": "exited",
            "exit": 0,
            "reachable": True,
            "boot_id": "b",
            "instance_token": "t",
            "workdir_exists": True,
        },
    )
    monkeypatch.setattr(watcher_mod, "pull", lambda *a, **k: pulled.append(a) or None)

    watch(j.id)

    assert j.state == "exited"
    assert j.exit_code == 0
    assert pulled, "phải kéo artifact về local"


# --------------------------------------------------------------------------
# watch(): mất kết nối KHÔNG failover (bug R13 / kịch bản S7)
# --------------------------------------------------------------------------
def test_watch_does_not_failover_when_unreachable(monkeypatch):
    j = make_job(state="running")
    j.attempts = [{"host": "muse-a", "boot_id": "b", "instance_token": "t"}]
    failovers = []
    slept = []

    monkeypatch.setattr(watcher_mod, "load_job", lambda job_id: j)
    monkeypatch.setattr(watcher_mod, "save_job", lambda job: None)
    monkeypatch.setattr(
        watcher_mod,
        "remote_state",
        lambda host, job, timeout=30: {
            "state": "unknown",
            "exit": None,
            "reachable": False,
            "error": "ssh_unreachable",
            "detail": "Connection refused",
            "boot_id": None,
            "instance_token": None,
            "workdir_exists": False,
        },
    )
    monkeypatch.setattr(watcher_mod, "_failover", lambda job, reason: failovers.append(reason) or True)

    def fake_sleep(seconds):
        slept.append(seconds)
        if len(slept) >= 3:
            raise StopLoop()

    monkeypatch.setattr(watcher_mod, "_sleep", fake_sleep)

    with pytest.raises(StopLoop):
        watch(j.id)

    assert failovers == [], "mất kết nối KHÔNG được failover"
    assert j.state == "running"
    assert slept, "watcher phải chờ rồi thử lại"


def test_watch_failovers_when_host_restarted(monkeypatch):
    j = make_job(state="running")
    j.attempts = [{"host": "muse-a", "boot_id": "b", "instance_token": "t"}]
    calls = []

    monkeypatch.setattr(watcher_mod, "load_job", lambda job_id: j)
    monkeypatch.setattr(watcher_mod, "save_job", lambda job: None)

    states = [
        {
            "state": "lost",
            "reason": "host_restarted",
            "exit": None,
            "reachable": True,
            "boot_id": "b2",
            "instance_token": "t",
            "workdir_exists": False,
        },
        {
            "state": "exited",
            "exit": 0,
            "reachable": True,
            "boot_id": "b2",
            "instance_token": "t",
            "workdir_exists": True,
        },
    ]

    def fake_remote_state(host, job, timeout=30):
        return states[min(len(calls), len(states) - 1)]

    def fake_failover(job, reason):
        calls.append(reason)
        job.host = "muse-b"
        job.state = "launched"
        return True

    monkeypatch.setattr(watcher_mod, "remote_state", fake_remote_state)
    monkeypatch.setattr(watcher_mod, "_failover", fake_failover)
    monkeypatch.setattr(watcher_mod, "pull", lambda *a, **k: None)

    watch(j.id)

    assert calls == ["host_restarted"]
    assert j.state == "exited"


def test_watch_does_not_failover_when_side_effects(monkeypatch):
    """--side-effects ⇒ không retry (kịch bản S9)."""
    j = make_job(state="running", needs_retry=False)
    j.attempts = [{"host": "muse-a", "boot_id": "b", "instance_token": "t"}]
    failovers = []

    monkeypatch.setattr(watcher_mod, "load_job", lambda job_id: j)
    monkeypatch.setattr(watcher_mod, "save_job", lambda job: None)
    monkeypatch.setattr(
        watcher_mod,
        "remote_state",
        lambda host, job, timeout=30: {
            "state": "lost",
            "reason": "host_restarted",
            "exit": None,
            "reachable": True,
            "boot_id": "b2",
            "instance_token": "t",
            "workdir_exists": False,
        },
    )
    monkeypatch.setattr(watcher_mod, "_failover", lambda job, reason: failovers.append(reason) or True)

    watch(j.id)

    assert failovers == []
    assert j.state == "lost"
    assert j.reason == "host_restarted"


# --------------------------------------------------------------------------
# wait_for(): poll ledger CỤC BỘ, không ssh
# --------------------------------------------------------------------------
def test_wait_for_polls_local_ledger_only(monkeypatch):
    states = ["running", "running", "exited"]
    calls = {"n": 0}

    def fake_load_job(job_id):
        idx = min(calls["n"], len(states) - 1)
        calls["n"] += 1
        return make_job(state=states[idx])

    def boom(*a, **k):
        raise AssertionError("wait_for không được gọi ssh")

    monkeypatch.setattr(watcher_mod, "load_job", fake_load_job)
    monkeypatch.setattr(watcher_mod, "remote_state", boom)
    monkeypatch.setattr(watcher_mod, "_sleep", lambda s: None)

    result = wait_for("job-1", timeout=5)

    assert result.state == "exited"
    assert calls["n"] == 3


def test_wait_for_timeout_raises_exit_timeout(monkeypatch):
    monkeypatch.setattr(watcher_mod, "load_job", lambda job_id: make_job(state="running"))
    monkeypatch.setattr(watcher_mod, "_sleep", lambda s: None)

    with pytest.raises(Exception) as exc:
        wait_for("job-1", timeout=0.2)
    assert exc.value.code == 124  # EXIT_TIMEOUT


def test_wait_for_rejects_zero_timeout(monkeypatch):
    with pytest.raises(Exception) as exc:
        wait_for("job-1", timeout=0)
    assert exc.value.code == 2  # EXIT_INPUT


# --------------------------------------------------------------------------
# should_retry / validate_timeout (hợp đồng API §15b.6)
# --------------------------------------------------------------------------
def test_should_retry_never_retries_job_failure():
    assert (
        should_retry(
            kind="job_failed", attempts=0, max_attempts=3, needs_retry=True
        )
        is False
    )
    assert (
        should_retry(
            kind="job_failed", attempts=9, max_attempts=99, needs_retry=True
        )
        is False
    )


def test_should_retry_allows_infrastructure_failures():
    for kind in ("host_restarted", "host_wiped", "oom_killed", "vanished"):
        assert (
            should_retry(kind=kind, attempts=0, max_attempts=3, needs_retry=True)
            is True
        )


def test_should_retry_respects_needs_retry_and_budget():
    assert (
        should_retry(
            kind="host_restarted", attempts=0, max_attempts=3, needs_retry=False
        )
        is False
    )
    assert (
        should_retry(
            kind="host_restarted", attempts=3, max_attempts=3, needs_retry=True
        )
        is False
    )
    assert (
        should_retry(
            kind="host_restarted", attempts=2, max_attempts=3, needs_retry=True
        )
        is True
    )


def test_should_retry_ignores_unknown_kinds():
    assert (
        should_retry(kind="lỗi lạ", attempts=0, max_attempts=3, needs_retry=True)
        is False
    )


def test_validate_timeout_rejects_zero_negative_and_none():
    for bad in (0, -1, None, "abc"):
        with pytest.raises(Exception) as exc:
            validate_timeout(bad)
        assert exc.value.code == 2  # EXIT_INPUT


def test_validate_timeout_returns_positive_float():
    assert validate_timeout(30) == 30.0
    assert validate_timeout("12.5") == 12.5


# --------------------------------------------------------------------------
# spawn_watcher()
# --------------------------------------------------------------------------
def test_spawn_watcher_runs_detached(monkeypatch):
    seen = {}

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            seen["cmd"] = list(cmd)
            seen["kwargs"] = kwargs

    monkeypatch.setattr(watcher_mod, "_popen", lambda cmd, **kw: FakePopen(cmd, **kw))

    spawn_watcher("job-1")

    joined = " ".join(seen["cmd"])
    assert "job-1" in joined
    assert "__watch" in joined
    assert seen["kwargs"].get("stdout") is not None
    assert seen["kwargs"].get("stdin") is not None
