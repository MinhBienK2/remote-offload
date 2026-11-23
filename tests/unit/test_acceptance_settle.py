"""Unit test cho `tests/acceptance/_common.py::wait_state_settled`.

Đây là bộ đo trạng thái mà **cả** bộ nghiệm thu dùng để chờ job trên host.
Nó phải giữ đúng **nguyên tắc** (xem `docs/REPORT.md` §2):

* ``exited``/``cancelled`` ⇒ kết luận ngay;
* ``lost`` ⇒ **chưa** kết luận từ một lần thấy — phải thấy **liên tiếp**
  ``LOST_CONFIRMATIONS`` lần (verdict của `remote_state()` có thể **sai**:
  đọc `kill -0 $PID` đúng lúc process vừa chết mà file `exit` chưa ghi);
* host chết giữa chờ ⇒ **skip** (hạ tầng), không `fail`.

Không cần fleet — monkeypatch `roffkit.job.remote_state`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
ACCEPTANCE = REPO / "tests" / "acceptance"
SKILL = REPO / "skill"
for p in (str(SKILL), str(ACCEPTANCE)):
    if p not in sys.path:
        sys.path.insert(0, p)

import _common as common  # noqa: E402


def _install(monkeypatch, seq):
    """Cài `remote_state` trả lần lượt các phần tử của `seq` (lặp lại cuối)."""
    calls: list[str] = []

    def fake_remote_state(alias, job, timeout=30):
        index = min(len(calls), len(seq) - 1)
        calls.append(str(alias))
        return dict(seq[index])

    from roffkit import job as job_mod

    monkeypatch.setattr(job_mod, "remote_state", fake_remote_state)
    return calls


def test_exited_is_concluded_immediately(monkeypatch):
    calls = _install(monkeypatch, [{"state": "running"}, {"state": "exited", "exit": 0}])
    state = common.wait_state_settled("muse-x", object(), timeout=30, poll=0)
    assert state["state"] == "exited"
    assert len(calls) == 2, "phải dừng ngay khi thấy exited"


def test_cancelled_is_concluded_immediately(monkeypatch):
    _install(monkeypatch, [{"state": "cancelled"}])
    state = common.wait_state_settled("muse-x", object(), timeout=30, poll=0)
    assert state["state"] == "cancelled"


def test_single_lost_is_not_concluded(monkeypatch):
    """Một lần thấy `lost` ⇒ **chưa** kết luận (verdict có thể sai)."""
    _install(monkeypatch, [{"state": "lost", "reason": "vanished"},
                            {"state": "exited", "exit": 0}])
    state = common.wait_state_settled("muse-x", object(), timeout=30, poll=0)
    assert state["state"] == "exited", (
        "job đã xong bình thường ở lần hỏi thứ hai ⇒ không được kết luận 'lost'")


def test_repeated_lost_is_concluded(monkeypatch):
    """Thấy `lost` liên tiếp đủ số lần ⇒ kết luận `lost`."""
    seq = [{"state": "lost", "reason": "vanished"}]
    _install(monkeypatch, seq)
    state = common.wait_state_settled("muse-x", object(), timeout=30, poll=0)
    assert state["state"] == "lost"
    assert state["reason"] == "vanished"


def test_lost_counter_resets_after_running(monkeypatch):
    """`lost` xen kẽ với `running` ⇒ phải thấy liên tiếp mới kết luận."""
    seq = [
        {"state": "lost"},
        {"state": "running"},
        {"state": "lost"},
        {"state": "running"},
        {"state": "exited", "exit": 0},
    ]
    _install(monkeypatch, seq)
    state = common.wait_state_settled("muse-x", object(), timeout=30, poll=0)
    assert state["state"] == "exited"


def test_timeout_returns_last_state(monkeypatch):
    """Hết giờ ⇒ trả trạng thái cuối, không ném."""
    _install(monkeypatch, [{"state": "running"}])
    state = common.wait_state_settled("muse-x", object(), timeout=0, poll=0)
    assert state["state"] == "running"


def test_host_down_skips(monkeypatch):
    """Host chết giữa chờ ⇒ `skip` (hạ tầng), không trả về trạng thái."""
    _install(monkeypatch, [{}])  # remote_state không trả gì ⇒ host không với được
    monkeypatch.setattr(common, "_host_alive", lambda alias: False)
    with pytest.raises(pytest.skip.Exception):
        common.wait_state_settled("muse-x", object(), timeout=30, poll=0)


def test_lost_confirmations_constant_is_at_least_two():
    """Một lần xác nhận là **không đủ** — phải ≥ 2 (có thể sai ngay cả lần 2)."""
    assert common.LOST_CONFIRMATIONS >= 2
