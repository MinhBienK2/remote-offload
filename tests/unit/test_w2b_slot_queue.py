"""Hồi quy W2b — hết slot là "đang bận", **không** phải lỗi.

Sự cố thật: ``roff do`` gọi ``ledger.acquire(host, slots, 900 s)``. Host hết
slot ⇒ lệnh **block tới 15 phút** rồi ném exit 10 ⇒ agent bị treo và **không
có job nào được tạo**.

Hợp đồng mới:

* ``roff do`` không bao giờ chờ slot (thử 1 lần) ⇒ host bận ⇒ job ở state
  ``queued``, **exit 0**;
* watcher xin slot **sau** (mỗi vòng poll), giữ nguyên thứ tự FIFO;
* mọi nhánh kết thúc đều trả slot (không rò slot).

Test chạy **không cần mạng**: dùng ``SlotLedger`` với ``state_dir`` tạm và
ssh giả (fixture ``fake_ssh`` của ``tests/conftest.py``).
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
from roffkit import slots as slots_mod  # noqa: E402
from roffkit import watcher as watcher_mod  # noqa: E402

PROBE_OUTPUT = "\n".join(
    [
        "ROFF_HOSTNAME=htch-runtime",
        "ROFF_BOOT_ID=boot-1",
        "ROFF_HOME=/root",
        "ROFF_CORES=4",
        "ROFF_MEM_TOTAL_KB=8388608",
        "ROFF_MEM_AVAILABLE_KB=921600",
        "ROFF_LAYOUT_ROOT=/home/hatch/.roff",
        "ROFF_DURABLE=1",
        "ROFF_DISK_FREE_MB=102400",
        "ROFF_INSTANCE_TOKEN=tok-1",
        "ROFF_CGROUP_LIMIT_MB=",
        "ROFF_CGROUP_USED_MB=",
        "ROFF_TOOL=python3=/usr/bin/python3",
        "ROFF_INTERNET_CODE=200",
        "ROFF_INTERNET_RC=0",
        "ROFF_INTERNET_ERR=",
        "",
    ]
)


# --------------------------------------------------------------------------
# request_slot / try_acquire / cancel_slot
# --------------------------------------------------------------------------
def test_request_slot_does_not_take_a_slot(tmp_path):
    """``request_slot`` chỉ đăng ký vào hàng đợi, **không** chiếm slot."""
    ledger = slots_mod.SlotLedger(state_dir=str(tmp_path))
    ticket = ledger.request_slot("h1", 1)
    assert ticket
    assert ledger.counts() == {}, "request_slot không được chiếm slot"
    assert ledger.queue_length() == 1


def test_try_acquire_keeps_fifo_order(tmp_path):
    """2 ticket cùng host, capacity 1 ⇒ ticket 1 được, ticket 2 phải chờ."""
    ledger = slots_mod.SlotLedger(state_dir=str(tmp_path))
    first = ledger.request_slot("h1", 1)
    second = ledger.request_slot("h1", 1)

    lease = ledger.try_acquire(first)
    assert lease is not None and lease.ticket == first
    assert ledger.try_acquire(second) is None, "ticket 2 chưa tới lượt"
    assert ledger.queue_length() == 1, "ticket 2 phải **giữ nguyên** trong hàng đợi"

    ledger.release(lease)
    lease2 = ledger.try_acquire(second)
    assert lease2 is not None and lease2.ticket == second
    assert ledger.queue_length() == 0


def test_try_acquire_non_head_returns_none(tmp_path):
    """Ticket không phải đầu hàng ⇒ None (kể cả host còn capacity)."""
    ledger = slots_mod.SlotLedger(state_dir=str(tmp_path))
    ledger.request_slot("h1", 2)
    second = ledger.request_slot("h1", 2)
    assert ledger.try_acquire(second) is None
    assert ledger.queue_length() == 2


def test_try_acquire_unknown_ticket_returns_none(tmp_path):
    """Ticket không tồn tại ⇒ None, không ném lỗi."""
    ledger = slots_mod.SlotLedger(state_dir=str(tmp_path))
    assert ledger.try_acquire("khong-co") is None


def test_cancel_slot_removes_ticket_from_queue(tmp_path):
    """``cancel_slot`` ⇒ ticket biến mất khỏi hàng đợi."""
    ledger = slots_mod.SlotLedger(state_dir=str(tmp_path))
    ticket = ledger.request_slot("h1", 1)
    assert ledger.queue_length() == 1
    ledger.cancel_slot(ticket)
    assert ledger.queue_length() == 0
    assert ledger.try_acquire(ticket) is None


def test_cancel_slot_releases_lease(tmp_path):
    """Ticket đã có slot ⇒ ``cancel_slot`` trả slot luôn (không rò slot)."""
    ledger = slots_mod.SlotLedger(state_dir=str(tmp_path))
    ticket = ledger.request_slot("h1", 1)
    lease = ledger.try_acquire(ticket)
    assert lease is not None
    assert ledger.counts() == {"h1": 1}
    ledger.cancel_slot(ticket)
    assert ledger.counts() == {}


def test_acquire_still_raises_when_out_of_slots(tmp_path):
    """``acquire`` giữ hành vi cũ: hết giờ ⇒ ``RoffError(EXIT_NO_HOST)``."""
    from roffkit.errors import EXIT_NO_HOST, RoffError

    ledger = slots_mod.SlotLedger(state_dir=str(tmp_path))
    ledger.request_slot("h1", 1)
    holder = ledger.request_slot("h1", 1)
    ledger.try_acquire(holder)

    with pytest.raises(RoffError) as excinfo:
        ledger.acquire("h1", 1, timeout=0.2)
    assert excinfo.value.code == EXIT_NO_HOST


def test_acquire_timeout_zero_tries_once(tmp_path):
    """``acquire(timeout=0)`` = thử 1 lần rồi bỏ (không block)."""
    from roffkit.errors import EXIT_NO_HOST, RoffError

    ledger = slots_mod.SlotLedger(state_dir=str(tmp_path))
    first = ledger.request_slot("h1", 1)
    ledger.try_acquire(first)

    with pytest.raises(RoffError) as excinfo:
        ledger.acquire("h1", 1, timeout=0)
    assert excinfo.value.code == EXIT_NO_HOST
    assert ledger.queue_length() == 0, "ticket hết giờ phải được gỡ khỏi hàng đợi"


# --------------------------------------------------------------------------
# watcher: job queued xin slot sau
# --------------------------------------------------------------------------
class _Stop(Exception):
    """Dừng vòng lặp watcher trong test."""


def _queued_job(tmp_path, ledger):
    """Tạo job ở state ``queued`` (đang xếp hàng) + ticket của nó."""
    job_id = "261123-101530-ab12"
    job = job_mod.Job(
        id=job_id,
        cmd="true",
        host="muse-a",
        project="demo",
        state="queued",
        out_remote="/home/hatch/.roff/jobs/%s/out" % job_id,
    )
    job_mod.save_job(job)
    ticket = ledger.request_slot("muse-a", 1)
    job_mod.set_job_options(
        job_id,
        workdir="/home/hatch/.roff/projects/demo",
        layout_root="/home/hatch/.roff",
        slot_host="muse-a",
        slot_ticket=ticket,
        slot_slots=1,
    )
    return job, ticket


def test_watcher_keeps_job_queued_while_host_is_busy(monkeypatch, tmp_path):
    """Host bận ⇒ job **vẫn** ``queued`` (không launch, không lost)."""
    monkeypatch.setenv("ROFF_STATE_DIR", str(tmp_path / "state"))
    ledger = slots_mod.SlotLedger(state_dir=str(tmp_path / "state"))

    # 1 job khác đang giữ slot của host ⇒ host đầy (ticket đầu hàng)
    holder = ledger.request_slot("muse-a", 1)
    lease = ledger.try_acquire(holder)
    assert lease is not None

    # job mới xếp hàng sau ⇒ không được cấp slot
    job, ticket = _queued_job(tmp_path, ledger)

    launched: list = []
    monkeypatch.setattr(watcher_mod, "launch", lambda *a, **k: launched.append(a))
    monkeypatch.setattr(watcher_mod, "_start", lambda job: True)

    def _boom(_seconds):
        raise _Stop()

    monkeypatch.setattr(watcher_mod, "_sleep", _boom)

    with pytest.raises(_Stop):
        watcher_mod.watch(job.id)

    assert not launched, "không được launch khi chưa có slot"
    assert job_mod.load_job(job.id).state == "queued", "host bận KHÔNG phải lỗi"
    assert ledger.queue_length() == 1, "ticket phải giữ nguyên trong hàng đợi"


def test_watcher_acquires_slot_then_launches(monkeypatch, tmp_path):
    """Khi host rảnh ⇒ watcher lấy slot rồi launch."""
    monkeypatch.setenv("ROFF_STATE_DIR", str(tmp_path / "state"))
    ledger = slots_mod.SlotLedger(state_dir=str(tmp_path / "state"))
    job, ticket = _queued_job(tmp_path, ledger)

    started: list = []

    def _fake_start(job):
        """Giả lập ``_start``: launch thành công ⇒ job chuyển sang ``launched``."""
        started.append(job.id)
        job.state = "launched"
        job_mod.save_job(job)
        return True

    monkeypatch.setattr(watcher_mod, "_start", _fake_start)
    # Sau khi launch, watcher hỏi trạng thái remote — giả lập "không với được"
    # để không cần mạng thật.
    monkeypatch.setattr(
        watcher_mod, "remote_state", lambda *a, **k: {"reachable": False}
    )

    def _boom(_seconds):
        raise _Stop()

    monkeypatch.setattr(watcher_mod, "_sleep", _boom)

    with pytest.raises(_Stop):
        watcher_mod.watch(job.id)

    assert started == [job.id], "phải start job sau khi được slot"
    assert ledger.counts().get("muse-a") == 1, "slot phải được giữ khi job chạy"


def test_watcher_keeps_fifo_position_across_polls(monkeypatch, tmp_path):
    """Ticket giữ nguyên vị trí hàng đợi ⇒ mỗi vòng poll KHÔNG tạo ticket mới."""
    monkeypatch.setenv("ROFF_STATE_DIR", str(tmp_path / "state"))
    ledger = slots_mod.SlotLedger(state_dir=str(tmp_path / "state"))

    holder = ledger.request_slot("muse-a", 1)
    ledger.try_acquire(holder)
    job, ticket = _queued_job(tmp_path, ledger)

    options = job_mod.job_options(job.id)
    for _ in range(3):
        assert watcher_mod._acquire_slot(job, options) is None
        options = job_mod.job_options(job.id)

    assert job_mod.job_options(job.id)["slot_ticket"] == ticket
    assert ledger.queue_length() == 1, "phải giữ đúng 1 ticket (không tạo thêm)"


# --------------------------------------------------------------------------
# roff do: host bận ⇒ job queued, exit 0
# --------------------------------------------------------------------------
def _cli_env(tmp_path, monkeypatch=None):
    """Env cô lập cho CLI (state riêng, config riêng, ssh config riêng).

    Nếu có ``monkeypatch`` thì env của **tiến trình test** cũng được đặt ⇒
    job tạo trong test nằm đúng ledger mà CLI con đọc.
    """
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    config = tmp_path / "config.ini"
    config.write_text("[fleet]\ninclude = muse-*\n")
    ssh_config = tmp_path / "ssh_config"
    ssh_config.write_text("Host muse-a\n  HostName 127.0.0.1\n  Port 2222\n")
    env = dict(os.environ)
    env.update(
        {
            "ROFF_STATE_DIR": str(state),
            "ROFF_CONFIG": str(config),
            "ROFF_SSH_CONFIG": str(ssh_config),
            "ROFF_NO_WATCHER": "1",
        }
    )
    if monkeypatch is not None:
        for key in ("ROFF_STATE_DIR", "ROFF_CONFIG", "ROFF_SSH_CONFIG"):
            monkeypatch.setenv(key, env[key])
    return env


def test_do_when_host_is_busy_creates_queued_job(fake_ssh, tmp_path):
    """Host hết slot ⇒ ``roff do`` **không** exit 10, tạo job state ``queued``."""
    fake_ssh(
        "muse-a",
        responses=[{"match": "nproc", "stdout": PROBE_OUTPUT}],
    )
    env = _cli_env(tmp_path)

    # job khác đang giữ slot của muse-a (capacity mặc định = 1)
    ledger = slots_mod.SlotLedger(state_dir=env["ROFF_STATE_DIR"])
    holder = ledger.request_slot("muse-a", 1)
    assert ledger.try_acquire(holder) is not None

    rc, out, _err = run_cli(["do", "--cmd", "echo hi", "--name", "proj"], env=env)
    assert rc == 0, "host bận KHÔNG được exit 10"
    payload = parse_json(out)
    assert payload["ok"] is True
    assert payload["exit_code"] == 0
    assert payload["state"] == "queued"
    assert payload["host"] == "muse-a"
    assert payload["job"]
    assert payload["next"] and "wait" in payload["next"][0]

    data = json.loads(
        (tmp_path / "state" / "jobs" / f"{payload['job']}.json").read_text()
    )
    assert data["state"] == "queued", "job phải được tạo ở state queued"


def test_do_with_forced_busy_host_is_queued(fake_ssh, tmp_path):
    """``--host`` với host hết slot ⇒ cũng ``queued`` + exit 0 (không exit 10)."""
    fake_ssh(
        "muse-a",
        responses=[{"match": "nproc", "stdout": PROBE_OUTPUT}],
    )
    env = _cli_env(tmp_path)

    ledger = slots_mod.SlotLedger(state_dir=env["ROFF_STATE_DIR"])
    holder = ledger.request_slot("muse-a", 1)
    assert ledger.try_acquire(holder) is not None

    rc, out, _err = run_cli(
        ["do", "--cmd", "echo hi", "--host", "muse-a", "--name", "proj"], env=env
    )
    assert rc == 0
    payload = parse_json(out)
    assert payload["state"] == "queued"
    assert payload["host"] == "muse-a"


def test_do_never_blocks_waiting_for_slot(fake_ssh, tmp_path):
    """Lệnh không được treo chờ slot (timeout cũ = 900 s)."""
    fake_ssh(
        "muse-a",
        responses=[{"match": "nproc", "stdout": PROBE_OUTPUT}],
    )
    env = _cli_env(tmp_path)
    env["ROFF_SLOT_WAIT_S"] = "900"

    ledger = slots_mod.SlotLedger(state_dir=env["ROFF_STATE_DIR"])
    holder = ledger.request_slot("muse-a", 1)
    ledger.try_acquire(holder)

    rc, out, _err = run_cli(["do", "--cmd", "echo hi", "--name", "proj"], env=env)
    assert rc == 0
    assert parse_json(out)["state"] == "queued"


# --------------------------------------------------------------------------
# roff wait / roff cancel trên job queued
# --------------------------------------------------------------------------
def test_wait_on_queued_job_timeout_is_124(fake_ssh, tmp_path, monkeypatch):
    """Job còn ``queued`` khi hết giờ ⇒ exit 124, ``state:"queued"``."""
    env = _cli_env(tmp_path, monkeypatch)
    job_id = "job-queued"
    job = job_mod.Job(
        id=job_id, cmd="true", host="muse-a", project="demo", state="queued"
    )
    job_mod.save_job(job)

    rc, out, _err = run_cli(["wait", job_id, "--timeout", "1"], env=env)
    assert rc == 124
    payload = parse_json(out)
    assert payload["ok"] is True, "timeout KHÔNG phải lỗi"
    assert payload["exit_code"] == 124
    assert payload["state"] == "queued"


def test_cancel_queued_job_cancels_ticket(fake_ssh, tmp_path, monkeypatch):
    """``roff cancel`` trên job ``queued`` ⇒ state cancelled + bỏ ticket."""
    env = _cli_env(tmp_path, monkeypatch)
    ledger = slots_mod.SlotLedger(state_dir=env["ROFF_STATE_DIR"])
    job_id = "job-queued2"
    job = job_mod.Job(
        id=job_id, cmd="true", host="muse-a", project="demo", state="queued"
    )
    job_mod.save_job(job)
    ticket = ledger.request_slot("muse-a", 1)
    job_mod.set_job_options(job_id, slot_ticket=ticket, slot_host="muse-a")

    rc, out, _err = run_cli(["cancel", job_id], env=env)
    assert rc == 0
    payload = parse_json(out)
    assert payload["state"] == "cancelled"
    assert job_mod.load_job(job_id).state == "cancelled"
    assert ledger.queue_length() == 0, "ticket phải bị bỏ khỏi hàng đợi"


def test_cancel_does_not_overwrite_terminal_state(fake_ssh, tmp_path, monkeypatch):
    """Job đã ``exited`` ⇒ ``cancel`` KHÔNG đổi state."""
    env = _cli_env(tmp_path, monkeypatch)
    job_id = "job-done"
    job = job_mod.Job(
        id=job_id,
        cmd="true",
        host="muse-a",
        project="demo",
        state="exited",
        exit_code=0,
    )
    job_mod.save_job(job)

    rc, out, _err = run_cli(["cancel", job_id], env=env)
    payload = parse_json(out)
    assert payload["state"] == "exited", "trạng thái terminal không được ghi đè"
    assert job_mod.load_job(job_id).state == "exited"
    assert job_mod.load_job(job_id).exit_code == 0
    assert rc == 0
