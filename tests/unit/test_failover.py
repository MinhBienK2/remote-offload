"""Unit test cho failover của watcher — P5 (giữ workdir + input) và P6 (toolchain).

Ba lỗi đã xác minh trên fleet thật:

* **P5a** — workdir được dựng lại là ``{layout_root_mới}/projects/{project}``
  ⇒ **mất** hậu tố ``part-N``/``shard-M`` ⇒ các job của cùng lô rơi chung một
  thư mục và đè lên nhau.
* **P5b** — item của ``--each`` không được đẩy lại ⇒ job failover lên host mới
  chạy với path không tồn tại.
* **P6** — host mới không được ensure toolchain ⇒ host thiếu ``ffmpeg``/``node``
  làm job failover chết ngay.

Các test dùng **runner giả** cho phần filesystem trên host (marker + lock của
:mod:`roffkit.shared`) — không cần mạng thật.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from roffkit import job as job_mod  # noqa: E402
from roffkit import shared as shared_mod  # noqa: E402
from roffkit import toolchain as toolchain_mod  # noqa: E402
from roffkit import watcher as watcher_mod  # noqa: E402
from test_shared import FakeRemote  # noqa: E402

OLD_ROOT = "/old/root"
NEW_ROOT = "/new/root"
PROJECT = "demo"


# --------------------------------------------------------------------------
# hạ tầng test
# --------------------------------------------------------------------------
class FakeProfile:
    """Profile giả của host mới (những trường watcher/policy dùng)."""

    alias = "muse-new"
    reachable = True
    bridge_busy = False
    error = None
    detail = None
    tools: dict = {}
    mem_available_mb = 4096
    disk_free_mb = 100000
    internet = True
    layout_root = NEW_ROOT
    home = "/root"
    boot_id = "boot-2"
    instance_token = "tok-2"
    projects: list = []
    toolchains: list = []
    cores = 4
    load1 = 0.0


class FakeChoice:
    host = "muse-new"
    score = 1.0
    reasons: list = []
    rejected: dict = {}


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("ROFF_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ROFF_CONFIG", str(tmp_path / "none.ini"))


@pytest.fixture
def host(monkeypatch):
    """Host giả: chạy lệnh trên "máy" không có filesystem thật."""
    fake = FakeRemote()
    monkeypatch.setattr(shared_mod, "_run_shell", fake.run_shell)
    monkeypatch.setattr(watcher_mod, "cached_profiles", lambda *a, **k: [FakeProfile()])
    monkeypatch.setattr(watcher_mod, "select", lambda *a, **k: FakeChoice())

    class Result:
        ok = True
        bytes_transferred = 1
        files: list = []
        error = None

    monkeypatch.setattr(
        shared_mod.transfer_mod, "push", lambda *a, **k: Result()
    )
    return fake


@pytest.fixture
def launches(monkeypatch):
    """Ghi lại mỗi lần launch + trạng thái options trong ledger tại thời điểm đó."""
    calls: list[dict] = []

    def fake_launch(host_name, job, env=None, cpus=0, mem_mb=0):
        calls.append(
            {
                "host": host_name,
                "options": job_mod.job_options(job.id),
                "item": job.item,
            }
        )
        job.state = "launched"

    monkeypatch.setattr(watcher_mod, "launch", fake_launch)
    return calls


def make_job(tmp_path, workdir_suffix="", item=None, item_inside=False, **options):
    """Tạo job + options như ``roff do`` đã ghi (input trong kho dùng chung)."""
    jid = "261123-101530-ab12"
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    (src / "data.bin").write_text("x")

    job = job_mod.Job(
        id=jid,
        cmd="true",
        host="muse-old",
        project=PROJECT,
        out_remote=f"{OLD_ROOT}/jobs/{jid}/out",
    )
    job_mod.save_job(job)

    base = {
        "workdir": f"{OLD_ROOT}/projects/{PROJECT}{workdir_suffix}",
        "layout_root": OLD_ROOT,
        "input_dir": str(src),
        "input_local": str(src),
        "input_shared_key": shared_mod.shared_key(str(src)),
        "input_shared_rel": "",
        "input_remote": f"{OLD_ROOT}/inputs/{shared_mod.shared_key(str(src))}",
        "needs": [],
        "retry": 1,
    }
    if item is not None:
        base.update(
            {
                "item_local": str(item),
                "item_shared_key": shared_mod.shared_key(str(item)),
                "item_shared_rel": Path(str(item)).name,
                "item_inside_input": item_inside,
            }
        )
    base.update(options)
    job_mod.set_job_options(jid, **base)
    return job, src


# --------------------------------------------------------------------------
# P5a — giữ hậu tố workdir
# --------------------------------------------------------------------------
def test_failover_keeps_part_and_shard_suffix(host, launches, tmp_path):
    """``part-2/shard-1`` phải được giữ khi đổi host (không được gộp job)."""
    job, _src = make_job(tmp_path, workdir_suffix="/part-2/shard-1")

    ok = watcher_mod._failover(job, "host_restarted")

    assert ok is True
    options = job_mod.job_options(job.id)
    assert options["workdir"] == f"{NEW_ROOT}/projects/{PROJECT}/part-2/shard-1"


def test_failover_keeps_part_suffix(host, launches, tmp_path):
    """``part-0`` phải được giữ khi đổi host."""
    job, _src = make_job(tmp_path, workdir_suffix="/part-0")

    watcher_mod._failover(job, "host_restarted")

    options = job_mod.job_options(job.id)
    assert options["workdir"] == f"{NEW_ROOT}/projects/{PROJECT}/part-0"


def test_failover_without_suffix_keeps_plain_workdir(host, launches, tmp_path):
    """Không có hậu tố ⇒ workdir vẫn là ``{root}/projects/{project}``."""
    job, _src = make_job(tmp_path)

    watcher_mod._failover(job, "host_restarted")

    options = job_mod.job_options(job.id)
    assert options["workdir"] == f"{NEW_ROOT}/projects/{PROJECT}"


def test_failover_keeps_suffix_even_without_layout_root_in_ledger(host, launches,
                                                                   tmp_path):
    """Job cũ không có ``layout_root`` trong ledger ⇒ hậu tố vẫn phải giữ."""
    job, _src = make_job(tmp_path, workdir_suffix="/part-3")
    job_mod.set_job_options(job.id, layout_root="")

    watcher_mod._failover(job, "host_restarted")

    options = job_mod.job_options(job.id)
    assert options["workdir"] == f"{NEW_ROOT}/projects/{PROJECT}/part-3"


# --------------------------------------------------------------------------
# P5b — input và item phải theo host mới
# --------------------------------------------------------------------------
def test_failover_recomputes_input_remote_in_shared_store(host, launches, tmp_path):
    """``input_remote`` phải trỏ vào kho của host MỚI (không phải path host cũ)."""
    job, src = make_job(tmp_path)

    watcher_mod._failover(job, "host_restarted")

    options = job_mod.job_options(job.id)
    key = shared_mod.shared_key(str(src))
    assert options["input_remote"] == f"{NEW_ROOT}/inputs/{key}"
    assert options["input_shared_key"] == key


def test_failover_recomputes_item_inside_input(host, launches, tmp_path):
    """Item nằm trong input ⇒ ``$ROFF_ITEM`` trỏ vào bản copy của input (host mới)."""
    src = tmp_path / "src"
    src.mkdir()
    item = src / "clip.mp4"
    item.write_text("x")
    job, _src = make_job(tmp_path, item=item, item_inside=True)

    watcher_mod._failover(job, "host_restarted")

    key = shared_mod.shared_key(str(src))
    assert job.item == f"{NEW_ROOT}/inputs/{key}/clip.mp4"


def test_failover_recomputes_item_as_own_source(host, launches, tmp_path):
    """Item không nằm trong input nào ⇒ tự thành nguồn riêng trong kho host mới."""
    item = tmp_path / "items" / "clip.mp4"
    item.parent.mkdir()
    item.write_text("x")
    job, _src = make_job(tmp_path, item=item, item_inside=False)

    watcher_mod._failover(job, "host_restarted")

    key = shared_mod.shared_key(str(item))
    assert job.item == f"{NEW_ROOT}/inputs/{key}/clip.mp4"


def test_failover_writes_options_before_launch(host, launches, tmp_path):
    """``set_job_options`` phải chạy **trước** ``launch`` (launch đọc ledger)."""
    job, src = make_job(tmp_path)

    watcher_mod._failover(job, "host_restarted")

    assert len(launches) == 1
    seen = launches[0]["options"]
    assert seen["workdir"] == f"{NEW_ROOT}/projects/{PROJECT}"
    assert seen["input_remote"] == (
        f"{NEW_ROOT}/inputs/{shared_mod.shared_key(str(src))}")


def test_failover_does_not_launch_when_push_fails(host, launches, tmp_path, monkeypatch):
    """Đẩy lại input thất bại ⇒ KHÔNG launch (tuyệt đối không dùng path host cũ)."""
    job, _src = make_job(tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("mạng chết")

    monkeypatch.setattr(shared_mod, "ensure_shared_input", boom)

    ok = watcher_mod._failover(job, "host_restarted")

    assert ok is False
    assert not launches, "không được launch khi chưa có input trên host mới"
    events = [e["event"] for e in job_mod.job_options(job.id).get("events", [])]
    assert "failover_push_failed" in events


# --------------------------------------------------------------------------
# P6 — host mới phải được ensure toolchain
# --------------------------------------------------------------------------
def test_failover_ensures_tools_on_new_host(host, launches, tmp_path, monkeypatch):
    """Host mới phải được cài toolchain **trước** khi launch."""
    job, _src = make_job(tmp_path, needs=["ffmpeg"])
    calls: list[tuple] = []

    def fake_ensure(host_name, profile, tools):
        calls.append((host_name, list(tools)))
        return []

    monkeypatch.setattr(toolchain_mod, "ensure_tools", fake_ensure)

    watcher_mod._failover(job, "host_restarted")

    assert calls == [("muse-new", ["ffmpeg"])]
    assert len(launches) == 1


def test_failover_does_not_launch_when_ensure_tools_fails(host, launches, tmp_path,
                                                          monkeypatch):
    """Toolchain cài lỗi ⇒ KHÔNG launch (job chết ngay nếu thiếu tool)."""
    job, _src = make_job(tmp_path, needs=["ffmpeg"])

    def boom(*args, **kwargs):
        raise RuntimeError("setup script fail")

    monkeypatch.setattr(toolchain_mod, "ensure_tools", boom)

    ok = watcher_mod._failover(job, "host_restarted")

    assert ok is False
    assert not launches
    events = [e["event"] for e in job_mod.job_options(job.id).get("events", [])]
    assert "failover_ensure_tools_failed" in events


# --------------------------------------------------------------------------
# _start — job còn queued cũng phải dùng kho dùng chung
# --------------------------------------------------------------------------
def test_start_uses_shared_store(host, launches, tmp_path, monkeypatch):
    """Job ``queued``: input phải vào kho dùng chung và ``input_remote`` được ghi."""
    job, src = make_job(tmp_path)
    job.state = "queued"
    job_mod.save_job(job)

    monkeypatch.setattr(watcher_mod, "launch", lambda *a, **k: setattr(job, "state", "launched"))

    ok = watcher_mod._start(job)

    assert ok is True
    options = job_mod.job_options(job.id)
    key = shared_mod.shared_key(str(src))
    assert options["input_remote"] == f"{OLD_ROOT}/inputs/{key}"
