"""W5b — **B13**: 951 job *ma* trong ledger phải được nhận ra và dọn.

Đo thật trên máy người dùng (23/11/2026): ledger local
``~/.local/state/roff/jobs/`` có **997** file job, trong đó **951** là job của
phiên bản **cũ** (định dạng cũ: có ``remote``/``pid``/``submitted_at``, **không**
có khoá ``state``).

Hậu quả đo được:

* ``Job.from_dict`` lấy default của dataclass ⇒ ``state == "queued"`` ⇒
  ``list_jobs()`` trả ``{'queued': 959, 'exited': 37, 'launched': 1}``;
* ``roff wait <job-ma>`` **treo tới hết timeout** (exit 124);
* ``reconcile_slots`` coi 959 job ma là **còn sống** ⇒ giữ slot của ticket mà
  không ai theo dõi.

Cách sửa: dict **không có khoá ``state``** ⇒ ``state = "unknown"``, và
``unknown`` là trạng thái **terminal**.

Test chạy **không cần mạng**.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(_HERE))
_SKILL = os.path.join(_REPO, "skill")
_LAUNCHER = os.path.join(_SKILL, "roff.py")
if _SKILL not in sys.path:
    sys.path.insert(0, _SKILL)

from roffkit import job as job_mod  # noqa: E402
from roffkit import slots as slots_mod  # noqa: E402


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("ROFF_STATE_DIR", str(state))
    return state


def _write_legacy_job(state_dir, job_id: str = "261121-184232-a955") -> str:
    """Ghi 1 job **định dạng cũ** (không có khoá ``state``) — như thật."""
    directory = os.path.join(str(state_dir), "jobs")
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"{job_id}.json")
    payload = {
        "id": job_id,
        "host": "muse-tampt",
        "remote": f"/home/hatch/.roff/jobs/{job_id}",
        "pid": 20739,
        "cmd": "echo hi",
        "project": "demo",
        "submitted_at": 1790000000.0,
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    return path


def _write_job(state_dir, job_id: str, state: str, created_at: float | None = None) -> str:
    directory = os.path.join(str(state_dir), "jobs")
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"{job_id}.json")
    payload = {
        "id": job_id,
        "cmd": "true",
        "host": "muse-a",
        "project": "demo",
        "created_at": created_at if created_at is not None else time.time(),
        "state": state,
        "exit_code": 0 if state == "exited" else None,
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    return path


def _run_cli(args, env, timeout=60):
    proc = subprocess.run(
        [sys.executable, _LAUNCHER] + list(args),
        capture_output=True, text=True, timeout=timeout, env=env,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _parse(stdout: str) -> dict:
    lines = [ln for ln in stdout.strip().splitlines() if ln.strip()]
    assert lines, "CLI phải in 1 JSON object"
    return json.loads(lines[-1])


# --------------------------------------------------------------------------
# from_dict
# --------------------------------------------------------------------------
def test_job_without_state_key_is_unknown():
    """Dict không có khoá ``state`` ⇒ ``state == "unknown"`` (KHÔNG phải queued)."""
    job = job_mod.Job.from_dict({"id": "x", "host": "muse-a"})
    assert job.state == "unknown"
    assert job.state != "queued"


def test_job_with_state_key_keeps_state():
    job = job_mod.Job.from_dict({"id": "x", "state": "queued"})
    assert job.state == "queued"


def test_unknown_is_terminal():
    """``unknown`` phải là terminal: không giữ slot, wait trả về ngay."""
    assert "unknown" in job_mod.TERMINAL_STATES
    job = job_mod.Job.from_dict({"id": "x"})
    assert job.state in job_mod.TERMINAL_STATES


# --------------------------------------------------------------------------
# reconcile_slots — job ma không được giữ slot
# --------------------------------------------------------------------------
def test_legacy_job_does_not_hold_slot(tmp_path, _state):
    """Job ma (không có khoá state) không được giữ ticket trong hàng đợi."""
    state_dir = str(_state)
    _write_legacy_job(state_dir, "ghost-1")

    ledger = slots_mod.SlotLedger(state_dir=state_dir)
    ticket = ledger.request_slot("muse-a", 1, owner="ghost-1")
    assert ticket

    result = slots_mod.reconcile_slots(state_dir=state_dir)

    removed = {entry["ticket"] for entry in result["removed"]}
    assert ticket in removed, "ticket của job ma phải được thu hồi"
    assert ledger.queue_length() == 0


def test_live_job_keeps_its_slot(tmp_path, _state):
    """Job **đang chạy** (có state, không terminal) vẫn giữ ticket."""
    state_dir = str(_state)
    _write_job(state_dir, "live-1", "running")

    ledger = slots_mod.SlotLedger(state_dir=state_dir)
    ticket = ledger.request_slot("muse-a", 1, owner="live-1")

    slots_mod.reconcile_slots(state_dir=state_dir)
    assert ledger.queue_length() == 1, "job đang chạy không được mất slot"
    assert ticket


# --------------------------------------------------------------------------
# gc — dọn ledger
# --------------------------------------------------------------------------
def test_gc_removes_legacy_job_files(_state):
    state_dir = str(_state)
    _write_legacy_job(state_dir, "ghost-1")
    _write_legacy_job(state_dir, "ghost-2")

    result = job_mod.gc_jobs(state_dir=state_dir, max_age_days=30)

    assert result["removed_count"] == 2
    assert set(result["removed"]) == {"ghost-1", "ghost-2"}
    assert not list(Path(state_dir, "jobs").glob("ghost-*.json"))


def test_gc_removes_old_terminal_jobs(_state):
    state_dir = str(_state)
    old = time.time() - 40 * 86400
    _write_job(state_dir, "old-exited", "exited", created_at=old)
    _write_job(state_dir, "new-exited", "exited", created_at=time.time())

    result = job_mod.gc_jobs(state_dir=state_dir, max_age_days=30)

    assert result["removed"] == ["old-exited"]
    assert Path(state_dir, "jobs", "new-exited.json").exists()


def test_gc_keeps_running_and_queued_jobs(_state):
    state_dir = str(_state)
    _write_job(state_dir, "running-1", "running")
    _write_job(state_dir, "queued-1", "queued")

    result = job_mod.gc_jobs(state_dir=state_dir, max_age_days=30)

    assert result["removed"] == []
    assert Path(state_dir, "jobs", "running-1.json").exists()
    assert Path(state_dir, "jobs", "queued-1.json").exists()


def test_gc_keeps_unreadable_job_files(_state):
    """File JSON hỏng ⇒ KHÔNG xoá (không xoá thứ mình không hiểu)."""
    state_dir = str(_state)
    directory = Path(state_dir, "jobs")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "broken.json").write_text("{không phải json", encoding="utf-8")

    result = job_mod.gc_jobs(state_dir=state_dir, max_age_days=30)

    assert result["removed"] == []
    assert "broken" in result["skipped"]
    assert (directory / "broken.json").exists()


def test_gc_removes_sidecar_files(_state):
    """File phụ (``.heartbeat``/``.env``) của job bị xoá cũng phải đi."""
    state_dir = str(_state)
    _write_legacy_job(state_dir, "ghost-1")
    directory = Path(state_dir, "jobs")
    (directory / "ghost-1.heartbeat").write_text("x", encoding="utf-8")
    (directory / "ghost-1.env").write_text("SECRET=1", encoding="utf-8")

    job_mod.gc_jobs(state_dir=state_dir, max_age_days=30)

    assert not (directory / "ghost-1.heartbeat").exists()
    assert not (directory / "ghost-1.env").exists()


# --------------------------------------------------------------------------
# CLI — wait / jobs / gc
# --------------------------------------------------------------------------
def _cli_env(tmp_path, monkeypatch):
    state = tmp_path / "state"
    cfgdir = tmp_path / "config"
    state.mkdir(exist_ok=True)
    cfgdir.mkdir(exist_ok=True)
    # ssh config rỗng ⇒ fleet rỗng ⇒ ``roff gc`` không probe mạng.
    ssh_config = tmp_path / "ssh_config"
    ssh_config.write_text("", encoding="utf-8")
    # B27: ``ROFF_CONFIG`` phải trỏ tới file **có thật** — CLI fail to (exit 3)
    # khi config bị chỉ định mà không tồn tại. Trước B27 thì file thiếu chỉ
    # âm thầm nới rộng fleet thành MỌI host trong ~/.ssh/config.
    config = cfgdir / "config.ini"
    config.write_text("[fleet]\ninclude = *\n", encoding="utf-8")
    env = dict(os.environ)
    env["ROFF_STATE_DIR"] = str(state)
    env["ROFF_CONFIG"] = str(config)
    env["ROFF_SSH_CONFIG"] = str(ssh_config)
    return env, state


def test_wait_on_ghost_job_returns_immediately(tmp_path, monkeypatch):
    """``roff wait`` với job ma ⇒ trả **ngay** (không treo tới hết timeout)."""
    env, state = _cli_env(tmp_path, monkeypatch)
    _write_legacy_job(state, "ghost-1")

    started = time.time()
    rc, out, _err = _run_cli(["wait", "ghost-1", "--timeout", "3600"], env)
    elapsed = time.time() - started

    payload = _parse(out)
    assert elapsed < 10, "phải trả ngay, không treo"
    assert payload["ok"] is False
    assert payload["reason"] == "unknown_state"
    assert payload["state"] == "unknown"
    assert rc != 0


def test_jobs_shows_unknown_state(tmp_path, monkeypatch):
    """``roff jobs`` hiển thị job ma là ``unknown`` (không ẩn đi)."""
    env, state = _cli_env(tmp_path, monkeypatch)
    _write_legacy_job(state, "ghost-1")
    _write_job(state, "live-1", "running")

    rc, out, _err = _run_cli(["jobs"], env)
    payload = _parse(out)

    states = {item["job"]: item["state"] for item in payload["jobs"]}
    assert states["ghost-1"] == "unknown"
    assert states["live-1"] == "running"
    assert rc == 0


def test_gc_cli_reports_removed_jobs(tmp_path, monkeypatch):
    """``roff gc`` trả ``removed_jobs`` + ``freed_mb``."""
    env, state = _cli_env(tmp_path, monkeypatch)
    _write_legacy_job(state, "ghost-1")
    _write_legacy_job(state, "ghost-2")

    rc, out, _err = _run_cli(["gc"], env)
    payload = _parse(out)

    assert payload["removed_jobs_count"] == 2
    assert set(payload["removed_jobs"]) == {"ghost-1", "ghost-2"}
    assert "freed_mb" in payload
    assert rc == 0
