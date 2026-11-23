"""Hồi quy W3a — ticket/lease mồ côi **không** được kẹt host vĩnh viễn.

Sự cố thật: ticket xếp hàng của 1 job đã chết (agent bị kill giữa lúc chờ
slot) nằm ở **đầu hàng đợi** ⇒ host đó không bao giờ cấp được slot cho ai nữa
⇒ mọi job sau đều kẹt, `roff do` trả exit 10 vô ích.

Cơ chế mới (W3a):

* ticket/lease mang **owner** (job id) + **holder_pid/holder_started**;
* reconcile đọc ledger job local ⇒ owner không còn job ⇒ dọn ticket;
* holder (watcher) đã chết ⇒ thu hồi lease + đánh dấu job ``lost``;
* quá hạn (TTL) ⇒ dọn.

Test chạy **không cần mạng**: chỉ dùng ``SlotLedger`` + ``job`` ledger tạm.
"""

from __future__ import annotations

import os
import sys
import time

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(_HERE))
_SKILL = os.path.join(_REPO, "skill")
if _SKILL not in sys.path:
    sys.path.insert(0, _SKILL)

from roffkit import job as job_mod  # noqa: E402
from roffkit import slots as slots_mod  # noqa: E402
from roffkit import watcher as watcher_mod  # noqa: E402


def _make_job(job_id: str, state: str = "queued", state_dir: str = "", **options) -> None:
    """Tạo job trong ledger local (không cần host thật).

    ``state_dir`` phải **trùng** state dir của sổ slot — nếu không, reconcile
    đọc ledger khác và dọn oan (P4).
    """
    job = job_mod.Job(
        id=job_id,
        cmd="true",
        host="muse-a",
        project="demo",
        state=state,
        out_remote=f"/home/hatch/.roff/jobs/{job_id}/out",
    )
    job_mod.save_job(job, state_dir=state_dir or None)
    if options:
        job_mod.set_job_options(job_id, state_dir=state_dir or None, **options)


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    """Ledger job + sổ slot nằm trong thư mục tạm."""
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("ROFF_STATE_DIR", str(state))
    return state


# --------------------------------------------------------------------------
# owner / holder
# --------------------------------------------------------------------------
def test_request_slot_records_owner_and_time(tmp_path):
    """Ticket phải lưu **owner** (job id) + thời điểm yêu cầu."""
    ledger = slots_mod.SlotLedger(state_dir=str(tmp_path))
    ticket = ledger.request_slot("muse-a", 1, owner="job-1")
    assert ticket

    state = ledger._read_state()
    entry = next(e for e in state["queue"] if e["ticket"] == ticket)
    assert entry["owner"] == "job-1"
    assert entry["requested_at"] > 0


def test_try_acquire_inherits_owner_from_queue(tmp_path):
    """Lease nhận lại owner/holder của ticket (không mất dấu vết job)."""
    ledger = slots_mod.SlotLedger(state_dir=str(tmp_path))
    ticket = ledger.request_slot("muse-a", 1, owner="job-1")
    lease = ledger.try_acquire(ticket)
    assert lease is not None
    assert lease.owner == "job-1"


def test_set_holder_updates_lease_and_queue(tmp_path):
    """``set_holder`` ghi pid của watcher lên lease **và** ticket đang xếp hàng."""
    ledger = slots_mod.SlotLedger(state_dir=str(tmp_path))
    ticket = ledger.request_slot("muse-a", 1, owner="job-1")
    assert ledger.set_holder(ticket, os.getpid(), 12345.0) is True

    state = ledger._read_state()
    entry = next(e for e in state["queue"] if e["ticket"] == ticket)
    assert entry["holder_pid"] == os.getpid()
    assert entry["holder_started"] == 12345.0

    lease = ledger.try_acquire(ticket)
    assert lease is not None
    assert lease.holder_pid == os.getpid()


def test_cancel_slot_releases_lease_and_ticket(tmp_path):
    """Huỷ ticket đã có lease ⇒ trả slot **và** gỡ ticket (không rò slot)."""
    ledger = slots_mod.SlotLedger(state_dir=str(tmp_path))
    ticket = ledger.request_slot("muse-a", 1, owner="job-1")
    lease = ledger.try_acquire(ticket)
    assert lease is not None
    ledger.cancel_slot(ticket)
    assert ledger.counts() == {}
    assert ledger.queue_length() == 0
    assert ledger.lease_for(ticket) is None


# --------------------------------------------------------------------------
# reconcile
# --------------------------------------------------------------------------
def test_reconcile_removes_ticket_of_dead_job(tmp_path):
    """Job chết (không còn trong ledger) ⇒ ticket của nó bị dọn."""
    ledger = slots_mod.SlotLedger(state_dir=str(tmp_path))
    ticket = ledger.request_slot("muse-a", 1, owner="job-gone")
    assert ledger.queue_length() == 1

    result = slots_mod.reconcile_slots(state_dir=str(tmp_path))

    removed = result["removed"]
    assert any(item["ticket"] == ticket for item in removed)
    assert removed[0]["reason"] == "owner_dead"
    assert ledger.queue_length() == 0


def test_reconcile_keeps_ticket_of_queued_job(tmp_path):
    """Job **đang xếp hàng** ⇒ ticket của nó KHÔNG được dọn."""
    ledger = slots_mod.SlotLedger(state_dir=str(tmp_path))
    _make_job("job-alive", state="queued", state_dir=str(tmp_path))
    ticket = ledger.request_slot("muse-a", 1, owner="job-alive")

    slots_mod.reconcile_slots(state_dir=str(tmp_path))

    assert ledger.queue_length() == 1, "ticket của job còn sống phải được giữ"
    assert ledger.position(ticket) == 1


def test_reconcile_keeps_ticket_of_running_job(tmp_path):
    """Job đang chạy (``running``) ⇒ ticket/lease của nó không bị dọn."""
    ledger = slots_mod.SlotLedger(state_dir=str(tmp_path))
    _make_job("job-running", state="running", state_dir=str(tmp_path))
    ticket = ledger.request_slot("muse-a", 1, owner="job-running")
    ledger.try_acquire(ticket)

    slots_mod.reconcile_slots(state_dir=str(tmp_path))

    assert ledger.counts().get("muse-a") == 1
    assert ledger.queue_length() == 0


def test_reconcile_removes_expired_ticket(tmp_path):
    """Ticket quá hạn (TTL) ⇒ dọn, kể cả owner còn sống."""
    ledger = slots_mod.SlotLedger(state_dir=str(tmp_path))
    _make_job("job-old", state="queued", state_dir=str(tmp_path))
    ticket = ledger.request_slot("muse-a", 1, owner="job-old")

    # Giả lập ticket đã cũ: sửa trực tiếp file sổ slot.
    path = ledger.state_path
    import json

    with open(path, "r", encoding="utf-8") as fh:
        state = json.load(fh)
    for entry in state["queue"]:
        if entry["ticket"] == ticket:
            entry["requested_at"] = time.time() - 10 * 24 * 3600
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(state, fh)

    result = slots_mod.reconcile_slots(state_dir=str(tmp_path), max_age_s=3600)

    assert any(item["ticket"] == ticket for item in result["removed"])
    assert ledger.queue_length() == 0


def test_reconcile_reclaims_lease_of_dead_watcher(tmp_path):
    """Watcher chết (holder pid không còn) ⇒ lease bị thu hồi."""
    ledger = slots_mod.SlotLedger(state_dir=str(tmp_path))
    _make_job("job-watcher-dead", state="launched", state_dir=str(tmp_path))
    ticket = ledger.request_slot("muse-a", 1, owner="job-watcher-dead")
    ledger.try_acquire(ticket)
    # Holder = pid không tồn tại (999999) ⇒ coi như watcher đã chết.
    ledger.set_holder(ticket, 999999, 1.0)

    result = slots_mod.reconcile_slots(state_dir=str(tmp_path))

    assert any(item["ticket"] == ticket for item in result["removed"])
    assert ledger.counts().get("muse-a") in (None, 0)


def test_reconcile_marks_owner_job_lost_when_holder_died(tmp_path):
    """Watcher chết ⇒ job chủ bị đánh dấu ``lost`` (reason ``watcher_died``)."""
    ledger = slots_mod.SlotLedger(state_dir=str(tmp_path))
    _make_job("job-lost", state="launched", state_dir=str(tmp_path))
    ticket = ledger.request_slot("muse-a", 1, owner="job-lost")
    ledger.try_acquire(ticket)
    ledger.set_holder(ticket, 999999, 1.0)

    result = slots_mod.reconcile_slots(state_dir=str(tmp_path))

    assert "job-lost" in list(result["lost_jobs"])
    job = job_mod.load_job("job-lost", state_dir=str(tmp_path))
    assert job.state == "lost"
    assert job.reason == "watcher_died"


def test_reconcile_is_idempotent(tmp_path):
    """Gọi reconcile 2 lần ⇒ kết quả giống nhau, không dọn nhầm."""
    ledger = slots_mod.SlotLedger(state_dir=str(tmp_path))
    _make_job("job-ok", state="queued", state_dir=str(tmp_path))
    ledger.request_slot("muse-a", 1, owner="job-ok")
    ledger.request_slot("muse-a", 1, owner="job-gone")

    first = slots_mod.reconcile_slots(state_dir=str(tmp_path))
    second = slots_mod.reconcile_slots(state_dir=str(tmp_path))

    assert len(first["removed"]) == 1
    assert second["removed"] == []


def test_reconcile_does_not_touch_excluded_owner(tmp_path):
    """Job đang tự reconcile ⇒ không tự dọn ticket của mình."""
    ledger = slots_mod.SlotLedger(state_dir=str(tmp_path))
    _make_job("job-self", state="queued", state_dir=str(tmp_path))
    ledger.request_slot("muse-a", 1, owner="job-self")

    result = slots_mod.reconcile_slots(state_dir=str(tmp_path), exclude_owners=["job-self"])

    assert result["removed"] == []
    assert ledger.queue_length() == 1


def test_reconcile_keeps_queue_when_ledger_broken(tmp_path, monkeypatch):
    """Ledger job đọc lỗi ⇒ **không** dọn ticket (tránh giết job đang chờ)."""

    def _boom():
        raise RuntimeError("ledger hỏng")

    monkeypatch.setattr(job_mod, "list_jobs", _boom)
    ledger = slots_mod.SlotLedger(state_dir=str(tmp_path))
    ticket = ledger.request_slot("muse-a", 1, owner="job-x")

    result = slots_mod.reconcile_slots(state_dir=str(tmp_path))

    assert result["removed"] == []
    assert ledger.queue_length() == 1, "hàng đợi phải giữ nguyên"
    assert ledger.position(ticket) == 1


def test_reclaim_orphans_none_owners_skips_owner_rule(tmp_path):
    """``alive_owners=None`` ⇒ không dọn theo owner (chỉ holder/TTL)."""
    ledger = slots_mod.SlotLedger(state_dir=str(tmp_path))
    ledger.request_slot("muse-a", 1, owner="job-x")

    removed = ledger.reclaim_orphans(None, max_age_s=24 * 3600)

    assert removed == []
    assert ledger.queue_length() == 1


# --------------------------------------------------------------------------
# P3 — capacity theo config (đổi `slots` trong config phải có tác dụng)
# --------------------------------------------------------------------------
def test_capacity_follows_config_when_increased(tmp_path):
    """Sửa config tăng ``slots`` ⇒ capacity tăng theo (không bị ghim)."""
    ledger = slots_mod.SlotLedger(state_dir=str(tmp_path))
    ledger.request_slot("muse-a", 1)
    assert ledger._capacity(ledger._read_state(), "muse-a") == 1

    ledger.request_slot("muse-a", 3)
    assert ledger._capacity(ledger._read_state(), "muse-a") == 3


def test_capacity_never_drops_below_leases_held(tmp_path):
    """Không được xuống dưới số lease đang giữ (job đang chạy không rơi slot)."""
    ledger = slots_mod.SlotLedger(state_dir=str(tmp_path))
    first = ledger.request_slot("muse-a", 2)
    ledger.try_acquire(first)
    second = ledger.request_slot("muse-a", 2)
    ledger.try_acquire(second)
    assert ledger.counts()["muse-a"] == 2

    ledger.request_slot("muse-a", 1)

    assert ledger._capacity(ledger._read_state(), "muse-a") == 2
    assert ledger.counts()["muse-a"] == 2


def test_three_jobs_run_in_parallel_when_capacity_is_three(tmp_path):
    """Config ``slots = 3`` ⇒ 3 job chạy song song được."""
    ledger = slots_mod.SlotLedger(state_dir=str(tmp_path))
    tickets = [ledger.request_slot("muse-a", 3) for _ in range(3)]
    leases = [ledger.try_acquire(ticket) for ticket in tickets]

    assert all(lease is not None for lease in leases)
    assert ledger.counts()["muse-a"] == 3


# --------------------------------------------------------------------------
# P4 — reconcile phải đọc ledger **cùng** state dir với sổ slot
# --------------------------------------------------------------------------
def test_reconcile_uses_given_state_dir_not_global(tmp_path, monkeypatch):
    """``reconcile_slots(state_dir=A)`` phải đọc ledger trong A, không phải B.

    Nếu đọc nhầm ledger thật của người dùng ⇒ mọi ticket trong A bị dọn oan
    (owner không có trong ledger ⇒ ``owner_dead``).
    """
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv("ROFF_STATE_DIR", str(other))
    # Job "thật" của người dùng nằm ở state dir TOÀN CỤC (other) — không liên
    # quan tới sổ slot ở tmp_path.
    _make_job("job-global", state="queued", state_dir=str(other))

    ledger = slots_mod.SlotLedger(state_dir=str(tmp_path))
    ticket = ledger.request_slot("muse-a", 1, owner="job-gone")

    result = slots_mod.reconcile_slots(state_dir=str(tmp_path))

    assert any(item["ticket"] == ticket for item in result["removed"])
    assert ledger.queue_length() == 0


def test_reconcile_keeps_ticket_when_job_is_in_given_state_dir(tmp_path, monkeypatch):
    """Job sống nằm trong state dir **được truyền** ⇒ ticket được giữ."""
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv("ROFF_STATE_DIR", str(other))

    ledger = slots_mod.SlotLedger(state_dir=str(tmp_path))
    _make_job("job-here", state="queued", state_dir=str(tmp_path))
    ledger.request_slot("muse-a", 1, owner="job-here")

    result = slots_mod.reconcile_slots(state_dir=str(tmp_path))

    assert result["removed"] == []
    assert ledger.queue_length() == 1


# --------------------------------------------------------------------------
# W3a — nhánh kết thúc của watcher vẫn phải trả slot (không rò slot)
# --------------------------------------------------------------------------
def test_watcher_releases_slot_when_job_finishes(monkeypatch, tmp_path):
    """Job kết thúc ⇒ watcher trả slot + bỏ ticket (bảo vệ hành vi W2)."""
    monkeypatch.setenv("ROFF_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "state").mkdir(exist_ok=True)
    ledger = slots_mod.SlotLedger(state_dir=str(tmp_path / "state"))

    job = job_mod.Job(
        id="job-finish",
        cmd="true",
        host="muse-a",
        project="demo",
        state="launched",
        out_remote="/home/hatch/.roff/jobs/job-finish/out",
    )
    job_mod.save_job(job, state_dir=str(tmp_path / "state"))
    ticket = ledger.request_slot("muse-a", 1, owner=job.id)
    lease = ledger.try_acquire(ticket)
    assert lease is not None
    job_mod.set_job_options(
        job.id,
        state_dir=str(tmp_path / "state"),
        slot_ticket=ticket,
        slot_host="muse-a",
    )

    watcher_mod._release_slot(job.id)

    assert ledger.counts().get("muse-a") in (None, 0), "slot phải được trả"
    assert ledger.queue_length() == 0, "ticket phải được bỏ khỏi hàng đợi"


def test_watcher_cancel_branch_also_releases(monkeypatch, tmp_path):
    """Nhánh ``cancelled`` cũng phải trả slot (không rò slot)."""
    monkeypatch.setenv("ROFF_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "state").mkdir(exist_ok=True)
    ledger = slots_mod.SlotLedger(state_dir=str(tmp_path / "state"))

    job = job_mod.Job(
        id="job-cancel",
        cmd="true",
        host="muse-a",
        project="demo",
        state="launched",
        out_remote="/home/hatch/.roff/jobs/job-cancel/out",
    )
    job_mod.save_job(job, state_dir=str(tmp_path / "state"))
    ticket = ledger.request_slot("muse-a", 1, owner=job.id)
    ledger.try_acquire(ticket)
    job_mod.set_job_options(
        job.id,
        state_dir=str(tmp_path / "state"),
        slot_ticket=ticket,
        slot_host="muse-a",
    )

    watcher_mod._release_slot(job.id)

    assert ledger.counts().get("muse-a") in (None, 0)
