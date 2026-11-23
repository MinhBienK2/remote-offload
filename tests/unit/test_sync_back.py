"""Unit test cho B26 — ``--sync-back`` phải **thực sự** kéo file về project.

Sự cố (đo thật 23/11/2026, fleet thật): ``--sync-back`` được **quảng cáo**
trong ``skill/SKILL.md`` ("pull specific paths back into the working tree") và
trong ``docs/SPEC-CLI.md`` §1, nhưng **không mã nào thực sự kéo** các path đó
về local:

* ``cli._cmd_do()`` chỉ *tính* đường dẫn remote rồi lưu vào ledger;
* ``watcher._failover()`` chỉ *remap* đường dẫn đó khi job chạy lại ở máy khác;
* chỗ duy nhất gọi ``transfer.pull_path()`` là ``cli._cmd_pull()`` — tức là
  **thủ công**, khi người dùng tự gọi ``roff pull``.

⇒ ``roff do --sync-back X`` là lệnh **rỗng (no-op)**: job báo thành công,
``pulled: true``, nhưng file **không** về project ⇒ agent tin là đã có file.

Hợp đồng khoá lại ở đây:

* ánh xạ path đúng: gốc project local = ``os.getcwd()`` lúc gọi ``roff do``;
  path tương đối ⇒ local ``<project_root>/<p>``, remote ``<workdir>/<p>``;
  path **trong** input source ⇒ remote nằm trong **kho dùng chung** của host;
  path **ngoài** project **và** ngoài mọi input ⇒ **từ chối** lúc gửi job
  (exit 2, ``sync_back_outside_project``);
* watcher kéo về **thật** sau khi job exit 0 (mỗi cặp remote → local);
* sync-back hỏng ⇒ **không nuốt lỗi** (bài học B23): ``sync_back_error`` khác
  rỗng + event ``sync_back_failed`` + ``roff wait`` trả **74** với
  ``reason="sync_back_failed"``, ``ok=false``, ``exit=0``; ``pulled`` **giữ
  nguyên** (artifact chính đã về thì đã về);
* ``roff pull`` thành công ⇒ **xoá** ``sync_back_error`` ⇒ ``roff wait`` trở
  lại exit 0.
"""

from __future__ import annotations

import argparse
import json
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
from roffkit import probe as probe_mod  # noqa: E402
from roffkit import shared as shared_mod  # noqa: E402
from roffkit import sshx  # noqa: E402
from roffkit import transfer as transfer_mod  # noqa: E402
from roffkit import watcher as watcher_mod  # noqa: E402
from roffkit.errors import EXIT_PULL_FAILED, RoffError  # noqa: E402

#: Layout root của host giả (khớp với profile trong :func:`_patch_seams`).
LAYOUT_ROOT = "/home/hatch/.roff"

#: Workdir của job (project tên ``proj``).
WORKDIR = f"{LAYOUT_ROOT}/projects/proj"


# --------------------------------------------------------------------------
# hạ tầng test
# --------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Ledger riêng + không spawn watcher (test không cần tiến trình nền)."""
    monkeypatch.setenv("ROFF_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ROFF_CONFIG", str(tmp_path / "none.ini"))
    monkeypatch.setenv("ROFF_NO_WATCHER", "1")
    monkeypatch.setenv("ROFF_PULL_RETRIES", "1")
    monkeypatch.setenv("ROFF_PULL_RETRY_DELAY_S", "0")


def _args(cmd: str = "echo hi", sync_back=None, inputs=None, name: str = "proj"):
    """``argparse.Namespace`` giống hệt ``roff do``."""
    return argparse.Namespace(
        cmd=cmd,
        inputs=list(inputs or []),
        out=None,
        need=[],
        cpus=0,
        mem=None,
        disk=None,
        each=None,
        shards=1,
        after=None,
        name=name,
        sync_back=list(sync_back or []),
        checkpoint=None,
        env=[],
        retry=1,
        side_effects=False,
        timeout=600,
        priority="normal",
        prefer_same_host=True,
        host=None,
    )


def _patch_seams(monkeypatch, tmp_path):
    """Bịt mọi đường ra ssh/subprocess của :func:`cli_mod._cmd_do`."""
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    config = tmp_path / "config.ini"
    config.write_text("[fleet]\ninclude = muse-*\n", encoding="utf-8")
    monkeypatch.setenv("ROFF_STATE_DIR", str(state))
    monkeypatch.setenv("ROFF_CONFIG", str(config))

    profile = probe_mod.Profile(
        alias="muse-a",
        reachable=True,
        cores=4,
        mem_available_mb=4096,
        disk_free_mb=100000,
        layout_root=LAYOUT_ROOT,
        home="/root",
        boot_id="boot-1",
        instance_token="tok-1",
        internet=True,
        tools={"python3": "/usr/bin/python3"},
    )
    monkeypatch.setattr(cli_mod, "_fleet_aliases", lambda cfg: ["muse-a"])
    monkeypatch.setattr(probe_mod, "cached_profiles", lambda aliases, cfg: [profile])
    monkeypatch.setattr(
        sshx,
        "run_shell",
        lambda host, script, timeout=60: sshx.RunResult(rc=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(
        job_mod,
        "_run_ssh",
        lambda *a, **k: (
            0,
            "ROFF_PID=1\nROFF_BOOT_ID=boot-1\nROFF_INSTANCE_TOKEN=tok-1\n",
            "",
        ),
    )
    return profile


def make_job(job_id="job-sync-back", **kwargs):
    """Tạo 1 job thật trong ledger local."""
    base = dict(
        id=job_id,
        cmd="echo hello",
        host="muse-a",
        project="proj",
        created_at=1_700_000_000.0,
        out_remote=f"{LAYOUT_ROOT}/jobs/{job_id}/out",
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
    return cli_mod._cmd_wait(argparse.Namespace(job_id=job_id, timeout=timeout))


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


def _only_json(stdout: str) -> dict:
    """Parse stdout thành đúng 1 JSON object (fail loud nếu dư text)."""
    text = stdout.strip()
    assert text, "stdout rỗng — lệnh phải in 1 JSON object"
    obj = json.loads(text)
    assert isinstance(obj, dict)
    return obj


# --------------------------------------------------------------------------
# 1) ánh xạ path: tương đối ⇒ <project_root>/<p>  +  <workdir>/<p>
# --------------------------------------------------------------------------
def test_sync_back_relative_maps_local_and_remote(monkeypatch, tmp_path):
    """Path **tương đối** ⇒ local = ``<project_root>/<p>``, remote = ``<workdir>/<p>``.

    Đây là ánh xạ **có ý nghĩa**: cwd của job trên host **chính là** workdir
    (``$ROFF_WORK``) ⇒ file job tạo ra nằm đúng ở ``<workdir>/<p>``.
    """
    _patch_seams(monkeypatch, tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.chdir(project)

    payload, code = cli_mod._cmd_do(_args(sync_back=["report.md"]))

    assert code == 0
    jid = payload["job"]
    options = job_mod.job_options(jid)
    assert options["project_root"] == str(project)
    assert options["sync_back_local"] == [str(project / "report.md")]
    assert options["sync_back_remote"] == [f"{WORKDIR}/report.md"], (
        f"remote phải là <workdir>/report.md, thấy {options['sync_back_remote']}"
    )


def test_sync_back_absolute_inside_project_is_kept(monkeypatch, tmp_path):
    """Path **tuyệt đối** nhưng nằm trong project ⇒ giữ nguyên, remote theo workdir."""
    _patch_seams(monkeypatch, tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.chdir(project)

    payload, code = cli_mod._cmd_do(_args(sync_back=[str(project / "out" / "r.md")]))

    assert code == 0
    options = job_mod.job_options(payload["job"])
    assert options["sync_back_local"] == [str(project / "out" / "r.md")]
    assert options["sync_back_remote"] == [f"{WORKDIR}/out/r.md"]


# --------------------------------------------------------------------------
# 2) path nằm trong input source ⇒ remote nằm trong kho dùng chung
# --------------------------------------------------------------------------
def test_sync_back_inside_input_uses_shared_store(monkeypatch, tmp_path):
    """Path **trong** input source ⇒ remote nằm trong kho dùng chung của host.

    Input không còn được copy vào workdir (W3f) ⇒ bản copy của nó nằm ở
    ``<layout_root>/inputs/<key>/…``.
    """
    _patch_seams(monkeypatch, tmp_path)
    project = tmp_path / "proj"
    (project / "src").mkdir(parents=True)
    (project / "src" / "in.txt").write_text("x", encoding="utf-8")
    monkeypatch.chdir(project)
    # Đẩy input lên host là giả (không mạng thật).
    monkeypatch.setattr(
        transfer_mod,
        "push",
        lambda *a, **k: SimpleNamespace(
            ok=True, bytes_transferred=1, files=[], error=None
        ),
    )

    payload, code = cli_mod._cmd_do(
        _args(sync_back=["src/in.txt"], inputs=[str(project / "src")])
    )

    assert code == 0
    options = job_mod.job_options(payload["job"])
    key = shared_mod.shared_key(str(project / "src"))
    assert options["sync_back_local"] == [str(project / "src" / "in.txt")]
    assert options["sync_back_remote"] == [
        f"{LAYOUT_ROOT}/inputs/{key}/in.txt"
    ], f"remote phải nằm trong kho dùng chung, thấy {options['sync_back_remote']}"


# --------------------------------------------------------------------------
# 3) path ngoài project và ngoài mọi input ⇒ từ chối lúc gửi job (exit 2)
# --------------------------------------------------------------------------
def test_sync_back_outside_project_is_rejected(monkeypatch, tmp_path):
    """Path ngoài project **và** ngoài mọi input ⇒ ``roff do`` **exit 2**.

    Không có nơi nào trên host để tìm file đó ⇒ phải từ chối ngay, đừng im
    lặng làm cái gì đó vô nghĩa (B26).
    """
    _patch_seams(monkeypatch, tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.chdir(project)

    with pytest.raises(RoffError) as excinfo:
        cli_mod._cmd_do(_args(sync_back=["/etc/passwd"]))

    assert excinfo.value.code == 2
    assert excinfo.value.reason == "sync_back_outside_project"
    payload = excinfo.value.payload()
    assert payload["ok"] is False
    assert str(payload.get("error") or ""), "phải nói rõ path nào nằm ngoài project"
    assert "/etc/passwd" in str(payload["error"])


def test_sync_back_outside_project_cli_exit_2(monkeypatch, tmp_path):
    """Exit code **thật** của tiến trình ``roff do`` phải là 2 + reason."""
    _patch_seams(monkeypatch, tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.chdir(project)

    rc, out, _err = _run_cli(
        ["do", "--cmd", "echo hi", "--sync-back", "/etc/passwd"]
    )

    assert rc == 2, f"exit code thật phải là 2, thấy {rc}"
    payload = _only_json(out)
    assert payload["ok"] is False
    assert payload["reason"] == "sync_back_outside_project"


# --------------------------------------------------------------------------
# 4) watcher: job exit 0 ⇒ kéo về **thật** mỗi cặp remote → local
# --------------------------------------------------------------------------
def test_watch_pulls_sync_back_paths(monkeypatch, tmp_path):
    """Job exit 0 ⇒ watcher gọi ``pull_path`` cho **từng** cặp remote → local."""
    job = make_job(job_id="job-sync-ok")
    job_mod.set_job_options(
        job.id,
        out_local=None,
        sync_back=["report.md"],
        sync_back_local=[str(tmp_path / "proj" / "report.md")],
        sync_back_remote=[f"{WORKDIR}/report.md"],
    )
    calls: list[tuple] = []

    def fake_pull_path(host, remote, local):
        calls.append((host, remote, local))
        return _ok_result()

    monkeypatch.setattr(
        watcher_mod, "remote_state", lambda host, job, timeout=30: dict(EXITED_ZERO)
    )
    monkeypatch.setattr(watcher_mod, "pull", lambda *a, **k: _ok_result())
    monkeypatch.setattr(watcher_mod, "pull_path", fake_pull_path)

    watcher_mod.watch(job.id)

    assert calls == [
        ("muse-a", f"{WORKDIR}/report.md", str(tmp_path / "proj" / "report.md"))
    ], f"phải kéo đúng cặp remote → local, thấy {calls}"

    options = job_mod.job_options(job.id)
    assert not str(options.get("sync_back_error") or ""), "không được ghi lỗi"
    events = watcher_mod.job_events(job.id)
    assert [
        event for event in events if event.get("event") == "synced_back"
    ], "phải có event synced_back"


def test_watch_sync_back_success_keeps_wait_exit_0(monkeypatch, tmp_path):
    """Sync-back thành công ⇒ không có ``sync_back_error`` ⇒ ``roff wait`` trả 0."""
    job = make_job(job_id="job-sync-ok2")
    job_mod.set_job_options(
        job.id,
        out_local=None,
        sync_back_local=[str(tmp_path / "proj" / "report.md")],
        sync_back_remote=[f"{WORKDIR}/report.md"],
    )

    monkeypatch.setattr(
        watcher_mod, "remote_state", lambda host, job, timeout=30: dict(EXITED_ZERO)
    )
    monkeypatch.setattr(watcher_mod, "pull", lambda *a, **k: _ok_result())
    monkeypatch.setattr(watcher_mod, "pull_path", lambda *a, **k: _ok_result())

    watcher_mod.watch(job.id)

    saved = job_mod.load_job(job.id)
    assert saved.state == "exited"
    assert saved.exit_code == 0
    assert not saved.reason, f"không được có lý do: {saved.reason!r}"
    options = job_mod.job_options(job.id)
    assert not str(options.get("sync_back_error") or "")

    payload, code = _wait(job.id)
    assert code == 0
    assert payload["ok"] is True


# --------------------------------------------------------------------------
# 5) watcher: sync-back hỏng ⇒ báo lỗi (không nuốt), `roff wait` trả 74
# --------------------------------------------------------------------------
def test_watch_sync_back_failed_records_error(monkeypatch, tmp_path):
    """Sync-back hỏng ⇒ ``sync_back_error`` khác rỗng + event ``sync_back_failed``."""
    job = make_job(job_id="job-sync-bad")
    job_mod.set_job_options(
        job.id,
        out_local=None,
        sync_back_local=[str(tmp_path / "proj" / "report.md")],
        sync_back_remote=[f"{WORKDIR}/report.md"],
    )

    monkeypatch.setattr(
        watcher_mod, "remote_state", lambda host, job, timeout=30: dict(EXITED_ZERO)
    )
    monkeypatch.setattr(watcher_mod, "pull", lambda *a, **k: _ok_result())
    monkeypatch.setattr(
        watcher_mod,
        "pull_path",
        lambda *a, **k: SimpleNamespace(
            ok=False, files=[], bytes_transferred=0, error="rsync: không tìm thấy"
        ),
    )

    watcher_mod.watch(job.id)

    options = job_mod.job_options(job.id)
    assert str(options.get("sync_back_error") or ""), "phải ghi sync_back_error"
    events = watcher_mod.job_events(job.id)
    failed = [
        event for event in events if event.get("event") == "sync_back_failed"
    ]
    assert failed, "phải có event sync_back_failed"
    assert str(failed[-1].get("detail") or ""), "event phải kèm chi tiết lỗi"


def test_watch_sync_back_failed_wait_is_exit_74(monkeypatch, tmp_path):
    """Sync-back hỏng ⇒ ``roff wait`` trả 74, ``reason=sync_back_failed``.

    ``pulled`` **giữ nguyên** (``true``): artifact chính đã về local ⇒ không
    được nói dối rằng artifact chưa về (B23).
    """
    job = make_job(job_id="job-sync-bad2")
    job_mod.set_job_options(
        job.id,
        out_local=None,
        pulled=True,
        pull_error="",
        sync_back_local=[str(tmp_path / "proj" / "report.md")],
        sync_back_remote=[f"{WORKDIR}/report.md"],
    )

    monkeypatch.setattr(
        watcher_mod, "remote_state", lambda host, job, timeout=30: dict(EXITED_ZERO)
    )
    monkeypatch.setattr(watcher_mod, "pull", lambda *a, **k: _ok_result())
    monkeypatch.setattr(
        watcher_mod,
        "pull_path",
        lambda *a, **k: SimpleNamespace(
            ok=False, files=[], bytes_transferred=0, error="rsync: không tìm thấy"
        ),
    )

    watcher_mod.watch(job.id)

    saved = job_mod.load_job(job.id)
    assert saved.state == "exited"
    assert saved.exit_code == 0, "job remote chạy xong ⇒ exit 0"
    assert saved.reason == "sync_back_failed"

    payload, code = _wait(job.id)
    assert code == EXIT_PULL_FAILED == 74
    assert payload["ok"] is False
    assert payload["reason"] == "sync_back_failed"
    assert payload["exit"] == 0
    assert payload["pulled"] is True, "artifact chính đã về ⇒ pulled giữ nguyên"
    assert str(payload.get("sync_back_error") or ""), "phải khoá lỗi trong payload"


def test_cli_wait_sync_back_failed_is_exit_74(tmp_path):
    """Exit code **thật** của tiến trình ``roff wait`` phải là 74."""
    job = make_job(job_id="job-sync-proc", state="exited", exit_code=0)
    job_mod.set_job_options(
        job.id,
        pulled=True,
        pull_error="",
        sync_back_error="/tmp/proj/report.md: rsync: không tìm thấy",
    )

    rc, out, _err = _run_cli(["wait", job.id, "--timeout", "5"])

    assert rc == 74, f"exit code thật phải là 74, thấy {rc}"
    payload = _only_json(out)
    assert payload["ok"] is False
    assert payload["reason"] == "sync_back_failed"
    assert payload["exit"] == 0
    assert payload["pulled"] is True


def test_cli_status_shows_sync_back_error(tmp_path):
    """``roff status`` phải hiện ``sync_back_error`` (vẫn exit 0)."""
    job = make_job(job_id="job-sync-status", state="exited", exit_code=0)
    job_mod.set_job_options(job.id, sync_back_error="rsync: không tìm thấy")

    args = argparse.Namespace(job_id=job.id)
    payload, code = cli_mod._cmd_status(args)

    assert code == 0
    assert str(payload.get("sync_back_error") or "") == "rsync: không tìm thấy"


# --------------------------------------------------------------------------
# 6) `roff pull` thành công ⇒ xoá `sync_back_error` ⇒ `roff wait` trở lại 0
# --------------------------------------------------------------------------
def test_cmd_pull_clears_sync_back_error(monkeypatch, tmp_path):
    """``roff pull`` thành công ⇒ xoá lỗi ⇒ ``roff wait`` trở lại exit 0."""
    job = make_job(job_id="job-sync-pull", state="exited", exit_code=0)
    job.reason = "sync_back_failed"
    job_mod.save_job(job)
    job_mod.set_job_options(
        job.id,
        pulled=True,
        pull_error="",
        sync_back_error="/tmp/proj/report.md: rsync: không tìm thấy",
        sync_back_local=[str(tmp_path / "proj" / "report.md")],
        sync_back_remote=[f"{WORKDIR}/report.md"],
    )

    pulled: list[tuple] = []

    def fake_pull_path(host, remote, local):
        pulled.append((host, remote, local))
        return _ok_result()

    monkeypatch.setattr(cli_mod.transfer_mod, "pull", lambda *a, **k: _ok_result())
    monkeypatch.setattr(cli_mod.transfer_mod, "pull_path", fake_pull_path)

    args = argparse.Namespace(job_id=job.id, local=str(tmp_path / "out"))
    payload, code = cli_mod._cmd_pull(args)

    assert code == 0
    assert payload["ok"] is True
    assert pulled, "phải kéo lại path sync-back"
    assert pulled[0][1] == f"{WORKDIR}/report.md"

    options = job_mod.job_options(job.id)
    assert not str(options.get("sync_back_error") or ""), "phải xoá lỗi sync-back"
    saved = job_mod.load_job(job.id)
    assert not saved.reason, "kéo lại xong ⇒ không còn lý do sync_back_failed"

    _payload, code_again = _wait(job.id)
    assert code_again == 0, "roff wait phải trở lại exit 0"


def test_cmd_pull_sync_back_failure_is_reported(monkeypatch, tmp_path):
    """``roff pull`` mà path sync-back **không** kéo về được ⇒ phải báo lỗi."""
    job = make_job(job_id="job-sync-pull-bad", state="exited", exit_code=0)
    job_mod.set_job_options(
        job.id,
        sync_back_local=[str(tmp_path / "proj" / "report.md")],
        sync_back_remote=[f"{WORKDIR}/report.md"],
    )

    monkeypatch.setattr(cli_mod.transfer_mod, "pull", lambda *a, **k: _ok_result())
    monkeypatch.setattr(
        cli_mod.transfer_mod,
        "pull_path",
        lambda *a, **k: SimpleNamespace(
            ok=False, files=[], bytes_transferred=0, error="rsync: không tìm thấy"
        ),
    )

    args = argparse.Namespace(job_id=job.id, local=str(tmp_path / "out"))
    with pytest.raises(RoffError) as excinfo:
        cli_mod._cmd_pull(args)

    assert excinfo.value.reason == "sync_back_failed"
    options = job_mod.job_options(job.id)
    assert str(options.get("sync_back_error") or ""), "phải ghi lỗi vào ledger"


# --------------------------------------------------------------------------
# 7) failover: remote đổi theo host mới, đích local **không** đổi
# --------------------------------------------------------------------------
def test_failover_remaps_sync_back_remote_but_not_local(monkeypatch, tmp_path):
    """Failover ⇒ ``sync_back_remote`` theo host mới, ``sync_back_local`` giữ nguyên."""
    jid = "job-sync-failover"
    old_root = "/home/old/.roff"
    new_root = "/home/new/.roff"
    job = job_mod.Job(id=jid, cmd="true", host="muse-old", project="proj")
    job.out_remote = f"{old_root}/jobs/{jid}/out"
    job_mod.save_job(job)
    local_dest = str(tmp_path / "proj" / "report.md")
    job_mod.set_job_options(
        jid,
        workdir=f"{old_root}/projects/proj",
        layout_root=old_root,
        needs=[],
        sync_back_local=[local_dest],
        sync_back_remote=[f"{old_root}/projects/proj/report.md"],
    )

    class P:
        alias = "muse-new"
        reachable = True
        bridge_busy = False
        error = None
        detail = None
        tools: dict = {}
        mem_available_mb = 4096
        disk_free_mb = 100000
        internet = True
        layout_root = new_root
        projects: list = []
        toolchains: list = []

    class Choice:
        host = "muse-new"
        score = 1.0
        reasons: list = []
        rejected: dict = {}

    monkeypatch.setattr(watcher_mod, "cached_profiles", lambda *a, **k: [P()])
    monkeypatch.setattr(watcher_mod, "select", lambda *a, **k: Choice())
    monkeypatch.setattr(
        watcher_mod, "launch", lambda *a, **k: setattr(job, "state", "launched")
    )

    assert watcher_mod._failover(job, "host_restarted") is True

    options = job_mod.job_options(jid)
    assert options["sync_back_remote"] == [f"{new_root}/projects/proj/report.md"], (
        f"remote phải theo host mới: {options['sync_back_remote']}"
    )
    assert options["sync_back_local"] == [local_dest], "đích local không được đổi"


def test_failover_remaps_shared_store_sync_back(monkeypatch, tmp_path):
    """Path sync-back trong **kho dùng chung** ⇒ remap sang kho của host mới.

    Kho dùng chung có **cùng key trên mọi host** (key = sha256 của đường dẫn
    local) ⇒ chỉ cần đổi gốc layout root (giống input).
    """
    jid = "job-sync-failover-shared"
    old_root = "/home/old/.roff"
    new_root = "/home/new/.roff"
    src = tmp_path / "src"
    src.mkdir()
    (src / "in.txt").write_text("x", encoding="utf-8")
    key = shared_mod.shared_key(str(src))

    job = job_mod.Job(id=jid, cmd="true", host="muse-old", project="proj")
    job.out_remote = f"{old_root}/jobs/{jid}/out"
    job_mod.save_job(job)
    job_mod.set_job_options(
        jid,
        workdir=f"{old_root}/projects/proj",
        layout_root=old_root,
        needs=[],
        input_local=str(src),
        input_shared_key=key,
        input_sources=[{"local": str(src), "key": key, "rel": ""}],
        sync_back_local=[str(src / "in.txt")],
        sync_back_remote=[f"{old_root}/inputs/{key}/in.txt"],
    )

    class P:
        alias = "muse-new"
        reachable = True
        bridge_busy = False
        error = None
        detail = None
        tools: dict = {}
        mem_available_mb = 4096
        disk_free_mb = 100000
        internet = True
        layout_root = new_root
        projects: list = []
        toolchains: list = []

    class Choice:
        host = "muse-new"
        score = 1.0
        reasons: list = []
        rejected: dict = {}

    monkeypatch.setattr(watcher_mod, "cached_profiles", lambda *a, **k: [P()])
    monkeypatch.setattr(watcher_mod, "select", lambda *a, **k: Choice())
    monkeypatch.setattr(
        watcher_mod, "launch", lambda *a, **k: setattr(job, "state", "launched")
    )
    monkeypatch.setattr(
        shared_mod,
        "ensure_shared_input",
        lambda host, local, layout_root, **kwargs: shared_mod.shared_remote_path(
            layout_root, shared_mod.shared_key(local), shared_mod.shared_rel(local)
        ),
    )

    assert watcher_mod._failover(job, "host_restarted") is True

    options = job_mod.job_options(jid)
    assert options["sync_back_remote"] == [f"{new_root}/inputs/{key}/in.txt"], (
        f"path trong kho dùng chung phải theo kho của host mới: "
        f"{options['sync_back_remote']}"
    )
    assert options["sync_back_local"] == [str(src / "in.txt")], "đích local không đổi"
