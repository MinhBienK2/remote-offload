"""Unit test cho B23 — job chạy xong nhưng **artifact không về** phải **báo lỗi**.

Sự cố (đo thật 23/11/2026): 1 trong 3 shard của kịch bản S11 **mất file** trong
khi ``roff wait`` trả **rc 0** cho cả 3 shard. Nguyên nhân: pull hỏng (fleet
Muse restart/flap) bị watcher **nuốt** ⇒ ledger báo "thành công" ⇒ agent tin là
có artifact rồi đọc thư mục output **rỗng**, không có cảnh báo nào.

Hợp đồng khoá lại ở đây:

* watcher **thử lại** pull (mặc định 3 lần) — pull hỏng do tunnel flap thì
  kéo lại được, artifact vẫn nằm trên host;
* pull hỏng **hẳn** ⇒ ``reason="pull_failed"``, ``pulled=false``,
  ``pull_error`` khác rỗng, event ``pull_failed`` trong ledger;
* ``roff wait`` ⇒ exit **74** (``EXIT_PULL_FAILED``), ``ok=false``,
  ``reason="pull_failed"``, ``exit=0`` (job **remote** vẫn thành công);
* ``roff pull`` chạy lại được ⇒ ``pulled=true`` + manifest mới.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[2]
SKILL = REPO / "skill"
LAUNCHER = SKILL / "roff.py"

if str(SKILL) not in sys.path:
    sys.path.insert(0, str(SKILL))

from roffkit import cli as cli_mod  # noqa: E402
from roffkit import job as job_mod  # noqa: E402
from roffkit import watcher as watcher_mod  # noqa: E402
from roffkit.errors import EXIT_PULL_FAILED, RoffError  # noqa: E402


# --------------------------------------------------------------------------
# hạ tầng test
# --------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Ledger riêng + thử pull nhanh (không ngủ 5 s giữa các lần thử)."""
    monkeypatch.setenv("ROFF_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ROFF_CONFIG", str(tmp_path / "none.ini"))
    monkeypatch.setenv("ROFF_NO_WATCHER", "1")
    monkeypatch.setenv("ROFF_PULL_RETRIES", "3")
    monkeypatch.setenv("ROFF_PULL_RETRY_DELAY_S", "0")


def make_job(job_id="job-pull-fail", **kwargs):
    """Tạo 1 job thật trong ledger local (để options + job field cùng tồn tại)."""
    base = dict(
        id=job_id,
        cmd="echo hello",
        host="muse-a",
        project="proj",
        created_at=1_700_000_000.0,
        out_remote=f"/home/hatch/.roff/jobs/{job_id}/out",
        out_local=f"/tmp/roff-out-{job_id}",
        state="running",
    )
    base.update(kwargs)
    job = job_mod.Job(**base)
    job_mod.save_job(job)
    return job


#: Trạng thái remote trả về cho watcher: job chạy xong, exit 0.
EXITED_ZERO = {
    "state": "exited",
    "exit": 0,
    "reachable": True,
    "boot_id": "boot-1",
    "instance_token": "tok-1",
    "workdir_exists": True,
}


def _ok_result(files=None):
    """Kết quả pull thành công (giống ``transfer.TransferResult``)."""
    return SimpleNamespace(
        ok=True,
        files=list(files if files is not None else [{"path": "a.txt", "sha256": "x"}]),
        bytes_transferred=3,
        error=None,
    )


def _wait(job_id, timeout=5):
    """Gọi ``cli._cmd_wait`` (không qua subprocess) → (payload, exit code)."""
    args = argparse.Namespace(job_id=job_id, timeout=timeout)
    return cli_mod._cmd_wait(args)


def _run_cli(args, timeout=60):
    """Chạy CLI thật (tiến trình con) → (rc, stdout, stderr)."""
    proc = subprocess.run(
        [sys.executable, str(LAUNCHER)] + [str(a) for a in args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=dict(os.environ),
    )
    return proc.returncode, proc.stdout, proc.stderr


# --------------------------------------------------------------------------
# watcher — pull hỏng hẳn ⇒ phải ghi nhận, không được nuốt lỗi
# --------------------------------------------------------------------------
def test_watch_pull_failed_records_reason_and_event(monkeypatch):
    """Pull hỏng cả 3 lần thử ⇒ job vẫn ``exited``/exit 0 nhưng có lý do.

    ``reason="pull_failed"``, ``pulled=false``, ``pull_error`` khác rỗng và
    event ``pull_failed`` (kèm chi tiết) phải có trong ledger.
    """
    job = make_job(job_id="job-pull-fail")
    calls = []

    def fake_pull(host, remote, local):
        calls.append((host, remote, local))
        raise RuntimeError("tunnel đứt giữa chừng")

    monkeypatch.setattr(
        watcher_mod, "remote_state", lambda host, job, timeout=30: dict(EXITED_ZERO)
    )
    monkeypatch.setattr(watcher_mod, "pull", fake_pull)

    watcher_mod.watch(job.id)

    assert len(calls) == 3, f"phải thử pull lại (ROFF_PULL_RETRIES=3), thấy {len(calls)}"

    saved = job_mod.load_job(job.id)
    assert saved.state == "exited"
    assert saved.exit_code == 0, "job remote chạy xong ⇒ exit 0 (không phải job fail)"
    assert saved.reason == "pull_failed", "phải ghi rõ lý do pull_failed"

    options = job_mod.job_options(job.id)
    assert options.get("pulled") is False, "phải ghi pulled=false"
    assert str(options.get("pull_error") or ""), "phải ghi pull_error khác rỗng"

    events = watcher_mod.job_events(job.id)
    failed = [event for event in events if event.get("event") == "pull_failed"]
    assert failed, "phải có event pull_failed trong ledger"
    assert str(failed[-1].get("detail") or ""), "event pull_failed phải kèm chi tiết"


def test_watch_pull_retry_succeeds_after_one_failure(monkeypatch):
    """Pull hỏng **1** lần rồi thành công ⇒ không có event ``pull_failed``.

    Cơ chế thử lại phải hoạt động: job exit 0, ``pulled=true``.
    """
    job = make_job(job_id="job-pull-retry")
    calls = []

    def fake_pull(host, remote, local):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("tunnel flap tạm thời")
        return _ok_result()

    monkeypatch.setattr(
        watcher_mod, "remote_state", lambda host, job, timeout=30: dict(EXITED_ZERO)
    )
    monkeypatch.setattr(watcher_mod, "pull", fake_pull)

    watcher_mod.watch(job.id)

    assert len(calls) == 2, f"phải thử lại đúng 1 lần, thấy {len(calls)}"

    saved = job_mod.load_job(job.id)
    assert saved.state == "exited"
    assert saved.exit_code == 0
    assert not saved.reason, f"không được có lý do khi pull thành công: {saved.reason!r}"

    options = job_mod.job_options(job.id)
    assert options.get("pulled") is True
    assert not str(options.get("pull_error") or "")

    events = watcher_mod.job_events(job.id)
    assert not [
        event for event in events if event.get("event") == "pull_failed"
    ], "pull thành công ở lần thử sau ⇒ không được ghi event pull_failed"


def test_watch_pull_retries_are_configurable(monkeypatch):
    """Số lần thử pull lấy từ biến môi trường ``ROFF_PULL_RETRIES``."""
    monkeypatch.setenv("ROFF_PULL_RETRIES", "5")
    job = make_job(job_id="job-pull-cfg")
    calls = []

    def fake_pull(host, remote, local):
        calls.append(1)
        raise RuntimeError("tunnel đứt")

    monkeypatch.setattr(
        watcher_mod, "remote_state", lambda host, job, timeout=30: dict(EXITED_ZERO)
    )
    monkeypatch.setattr(watcher_mod, "pull", fake_pull)

    watcher_mod.watch(job.id)

    assert len(calls) == 5, f"phải thử 5 lần (ROFF_PULL_RETRIES=5), thấy {len(calls)}"


# --------------------------------------------------------------------------
# cli._cmd_wait — exit 74 khi job xong nhưng artifact chưa về
# --------------------------------------------------------------------------
def test_cmd_wait_pull_failed_is_exit_74():
    """Job exit 0 + ``pulled=false`` ⇒ exit 74, ``ok=false``, ``reason=pull_failed``."""
    job = make_job(job_id="job-wait-fail", state="exited", exit_code=0)
    job_mod.set_job_options(job.id, pulled=False, pull_error="rsync: không ghi được")

    payload, code = _wait(job.id)

    assert code == EXIT_PULL_FAILED == 74
    assert payload["ok"] is False
    assert payload["reason"] == "pull_failed"
    assert payload["exit"] == 0, "job remote chạy xong ⇒ exit 0 (đừng báo job fail)"
    assert payload["pulled"] is False
    assert str(payload["pull_error"] or ""), "phải kèm chi tiết lỗi"


def test_cmd_wait_pull_ok_is_exit_0():
    """Job exit 0 + ``pulled=true`` ⇒ exit 0 (không đổi hành vi cũ)."""
    job = make_job(job_id="job-wait-ok", state="exited", exit_code=0)
    job_mod.set_job_options(job.id, pulled=True, pull_error="", manifest=[])

    payload, code = _wait(job.id)

    assert code == 0
    assert payload["ok"] is True
    assert payload["exit"] == 0
    assert payload["pulled"] is True


def test_cmd_wait_job_failed_is_exit_1():
    """Job exit ≠ 0 ⇒ exit 1 (không đổi hành vi cũ)."""
    job = make_job(job_id="job-wait-bad", state="exited", exit_code=3)
    job_mod.set_job_options(job.id, pulled=True, pull_error="")

    payload, code = _wait(job.id)

    assert code == 1
    assert payload["exit"] == 3
    assert payload["reason"] == "job_failed"


def test_cmd_wait_legacy_job_without_pull_info_is_exit_0():
    """Job **cũ** (ledger không có khoá ``pulled``) ⇒ exit 0 như trước.

    Không có thông tin pull ⇒ **không** được đoán là hỏng.
    """
    job = make_job(job_id="job-wait-legacy", state="exited", exit_code=0)

    payload, code = _wait(job.id)

    assert code == 0
    assert payload["ok"] is True


def test_cli_process_exit_code_is_74(tmp_path):
    """Exit code **thật** của tiến trình `roff wait` phải là 74 (hợp đồng CLI)."""
    job = make_job(job_id="job-wait-proc", state="exited", exit_code=0)
    job_mod.set_job_options(job.id, pulled=False, pull_error="rsync: không ghi được")

    rc, out, _err = _run_cli(["wait", job.id, "--timeout", "5"])

    assert rc == 74, f"exit code thật phải là 74, thấy {rc}"
    payload = _only_json(out)
    assert payload["ok"] is False
    assert payload["reason"] == "pull_failed"
    assert payload["exit"] == 0


def _only_json(stdout: str) -> dict:
    """Parse stdout thành đúng 1 JSON object (fail loud nếu dư text)."""
    import json

    text = stdout.strip()
    assert text, "stdout rỗng — lệnh phải in 1 JSON object"
    obj = json.loads(text)
    assert isinstance(obj, dict)
    return obj


# --------------------------------------------------------------------------
# roff pull — chạy lại được sau khi pull hỏng
# --------------------------------------------------------------------------
def test_cmd_pull_after_failure_sets_pulled_true(monkeypatch, tmp_path):
    """Pull lại thành công ⇒ ``pulled=true`` + manifest mới + gỡ lý do lỗi."""
    job = make_job(job_id="job-pull-again", state="exited", exit_code=0)
    job.reason = "pull_failed"
    job_mod.save_job(job)
    job_mod.set_job_options(job.id, pulled=False, pull_error="rsync: không ghi được")

    monkeypatch.setattr(cli_mod.transfer_mod, "pull", lambda *a, **k: _ok_result())
    monkeypatch.setattr(
        cli_mod.transfer_mod,
        "pull_path",
        lambda *a, **k: SimpleNamespace(ok=True, files=[], bytes_transferred=0),
    )

    args = argparse.Namespace(job_id=job.id, local=str(tmp_path / "out"))
    payload, code = cli_mod._cmd_pull(args)

    assert code == 0
    assert payload["ok"] is True

    options = job_mod.job_options(job.id)
    assert options.get("pulled") is True
    assert not str(options.get("pull_error") or "")
    assert options.get("manifest"), "phải cập nhật manifest"

    saved = job_mod.load_job(job.id)
    assert not saved.reason, "kéo lại xong ⇒ không còn lý do pull_failed"

    # `roff wait` phải trở lại exit 0 (đường cứu đã dùng).
    _payload, code_again = _wait(job.id)
    assert code_again == 0


def test_cmd_pull_failure_reports_error(monkeypatch, tmp_path):
    """Pull hỏng ⇒ báo lỗi rõ ràng (exit ≠ 0, ``ok=false``, có ``detail``)."""
    job = make_job(job_id="job-pull-bad", state="exited", exit_code=0)

    monkeypatch.setattr(
        cli_mod.transfer_mod,
        "pull",
        lambda *a, **k: SimpleNamespace(
            ok=False, files=[], bytes_transferred=0, error="rsync: Permission denied"
        ),
    )

    args = argparse.Namespace(job_id=job.id, local=str(tmp_path / "out"))
    with pytest.raises(RoffError) as excinfo:
        cli_mod._cmd_pull(args)

    assert excinfo.value.code != 0
    assert excinfo.value.reason == "pull_failed"
    payload = excinfo.value.payload()
    assert payload["ok"] is False
    assert str(payload.get("detail") or ""), "lỗi phải kèm detail"

    options = job_mod.job_options(job.id)
    assert options.get("pulled") is False
    assert str(options.get("pull_error") or ""), "phải ghi pull_error vào ledger"
