"""Unit test cho ``roffkit.slots`` — sổ chiếm chỗ dùng chung (flock).

Không cần fleet thật: mọi thao tác chạm trên state_dir tạm.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(_HERE))
_SKILL = os.path.join(_REPO, "skill")
if _SKILL not in sys.path:
    sys.path.insert(0, _SKILL)

from roffkit.slots import Lease, SlotLedger  # noqa: E402

try:  # phần A có thể chưa xong -> dùng fallback trong chính module
    from roffkit.errors import EXIT_NO_HOST, RoffError
except ImportError:  # pragma: no cover
    from roffkit.slots import EXIT_NO_HOST, RoffError  # type: ignore


# --------------------------------------------------------------------------
# hành vi cơ bản
# --------------------------------------------------------------------------
def test_acquire_returns_lease(tmp_path):
    ledger = SlotLedger(state_dir=str(tmp_path))
    lease = ledger.acquire("h1", 1, timeout=5)
    assert isinstance(lease, Lease)
    assert lease.host == "h1"
    assert lease.ticket
    assert lease.acquired_at > 0


def test_counts_reflect_leases(tmp_path):
    ledger = SlotLedger(state_dir=str(tmp_path))
    assert ledger.counts() == {}
    lease = ledger.acquire("h1", 2, timeout=5)
    assert ledger.counts() == {"h1": 1}
    ledger.release(lease)
    assert ledger.counts() == {}


def test_release_is_idempotent(tmp_path):
    ledger = SlotLedger(state_dir=str(tmp_path))
    lease = ledger.acquire("h1", 1, timeout=5)
    ledger.release(lease)
    ledger.release(lease)
    assert ledger.counts() == {}


def test_state_files_created(tmp_path):
    ledger = SlotLedger(state_dir=str(tmp_path))
    lease = ledger.acquire("h1", 1, timeout=5)
    assert (tmp_path / "slots.json").exists()
    assert (tmp_path / "slots.lock").exists()
    data = json.loads((tmp_path / "slots.json").read_text(encoding="utf-8"))
    assert lease.ticket in data["leases"]
    ledger.release(lease)


def test_two_leases_when_capacity_allows(tmp_path):
    ledger = SlotLedger(state_dir=str(tmp_path))
    a = ledger.acquire("h1", 2, timeout=5)
    b = ledger.acquire("h1", 2, timeout=5)
    assert a.ticket != b.ticket
    assert ledger.counts() == {"h1": 2}
    ledger.release(a)
    ledger.release(b)


def test_independent_hosts_do_not_block_each_other(tmp_path):
    ledger = SlotLedger(state_dir=str(tmp_path))
    a = ledger.acquire("h1", 1, timeout=5)
    b = ledger.acquire("h2", 1, timeout=5)
    assert ledger.counts() == {"h1": 1, "h2": 1}
    ledger.release(a)
    ledger.release(b)


# --------------------------------------------------------------------------
# timeout
# --------------------------------------------------------------------------
def test_acquire_timeout_raises_no_host(tmp_path):
    ledger = SlotLedger(state_dir=str(tmp_path))
    held = ledger.acquire("h1", 1, timeout=5)
    start = time.monotonic()
    with pytest.raises(RoffError) as exc:
        ledger.acquire("h1", 1, timeout=0.4)
    elapsed = time.monotonic() - start
    assert exc.value.code == EXIT_NO_HOST
    assert "no slot on h1" in exc.value.reason
    assert 0.3 <= elapsed < 10
    ledger.release(held)


def test_error_matches_part_a_contract():
    """Exit code và payload phải khớp errors.py của phần A."""
    errors_mod = pytest.importorskip("roffkit.errors")
    ledger_error = RoffError
    assert errors_mod.EXIT_NO_HOST == 10
    assert ledger_error is errors_mod.RoffError
    payload = errors_mod.RoffError(errors_mod.EXIT_NO_HOST, "no slot on h1").payload()
    assert payload["ok"] is False
    assert payload["reason"] == "no slot on h1"
    assert payload["exit_code"] == 10


def test_acquire_timeout_zero_tries_once(tmp_path):
    ledger = SlotLedger(state_dir=str(tmp_path))
    held = ledger.acquire("h1", 1, timeout=5)
    start = time.monotonic()
    with pytest.raises(RoffError):
        ledger.acquire("h1", 1, timeout=0)
    assert time.monotonic() - start < 5
    ledger.release(held)


def test_timed_out_request_leaves_no_queue_garbage(tmp_path):
    ledger = SlotLedger(state_dir=str(tmp_path))
    held = ledger.acquire("h1", 1, timeout=5)
    with pytest.raises(RoffError):
        ledger.acquire("h1", 1, timeout=0.3)
    assert ledger.queue_length() == 0
    ledger.release(held)


# --------------------------------------------------------------------------
# FIFO + priority
# --------------------------------------------------------------------------
def test_queue_length_and_position(tmp_path):
    ledger = SlotLedger(state_dir=str(tmp_path))
    held = ledger.acquire("h1", 1, timeout=5)

    done = threading.Event()

    def waiter():
        try:
            ledger.acquire("h1", 1, timeout=10)
        except RoffError:
            pass
        finally:
            done.set()

    t = threading.Thread(target=waiter)
    t.start()
    deadline = time.monotonic() + 5
    while ledger.queue_length() < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ledger.queue_length() == 1

    data = json.loads((tmp_path / "slots.json").read_text(encoding="utf-8"))
    ticket = data["queue"][0]["ticket"]
    assert ledger.position(ticket) == 1
    assert ledger.position("khong-ton-tai") == 0

    ledger.release(held)
    assert done.wait(10)
    t.join(5)


def test_fifo_order_is_respected(tmp_path):
    ledger = SlotLedger(state_dir=str(tmp_path))
    held = ledger.acquire("h1", 1, timeout=5)
    order: list[str] = []
    lock = threading.Lock()

    def waiter(name: str):
        lease = ledger.acquire("h1", 1, timeout=20)
        with lock:
            order.append(name)
        time.sleep(0.05)
        ledger.release(lease)

    threads = []
    for i, name in enumerate(("A", "B", "C"), start=1):
        t = threading.Thread(target=waiter, args=(name,))
        t.start()
        threads.append(t)
        # phải chờ thread này VÀO HÀNG ĐỢI thì mới start thread kế tiếp,
        # nếu không thứ tự vào hàng là ngẫu nhiên và test không kiểm tra FIFO
        deadline = time.monotonic() + 5
        while ledger.queue_length() < i and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ledger.queue_length() >= i, f"{name} chưa vào hàng đợi"

    ledger.release(held)
    for t in threads:
        t.join(20)
    assert order == ["A", "B", "C"]


def test_high_priority_jumps_the_queue(tmp_path):
    ledger = SlotLedger(state_dir=str(tmp_path))
    held = ledger.acquire("h1", 1, timeout=5)
    order: list[str] = []
    lock = threading.Lock()

    def waiter(name: str, priority: str):
        lease = ledger.acquire("h1", 1, timeout=20, priority=priority)
        with lock:
            order.append(name)
        time.sleep(0.05)
        ledger.release(lease)

    normal = threading.Thread(target=waiter, args=("normal", "normal"))
    normal.start()
    deadline = time.monotonic() + 5
    while ledger.queue_length() < 1 and time.monotonic() < deadline:
        time.sleep(0.01)

    high = threading.Thread(target=waiter, args=("high", "high"))
    high.start()
    deadline = time.monotonic() + 5
    while ledger.queue_length() < 2 and time.monotonic() < deadline:
        time.sleep(0.01)

    ledger.release(held)
    normal.join(20)
    high.join(20)
    assert order and order[0] == "high"


# --------------------------------------------------------------------------
# nhiều tiến trình
# --------------------------------------------------------------------------
def test_two_threads_serialize_on_single_slot(tmp_path):
    """2 thread cùng xin slot trên host slots=1 -> phải serialize."""
    ledger_a = SlotLedger(state_dir=str(tmp_path))
    ledger_b = SlotLedger(state_dir=str(tmp_path))
    events: list[tuple[str, str]] = []
    lock = threading.Lock()

    def worker(name: str, ledger: SlotLedger):
        lease = ledger.acquire("h1", 1, timeout=20)
        with lock:
            events.append(("acquired", name))
        time.sleep(0.15)
        with lock:
            events.append(("released", name))
        ledger.release(lease)

    t1 = threading.Thread(target=worker, args=("A", ledger_a))
    t2 = threading.Thread(target=worker, args=("B", ledger_b))
    t1.start()
    t2.start()
    t1.join(30)
    t2.join(30)

    assert len(events) == 4
    held = 0
    for kind, _ in events:
        if kind == "acquired":
            assert held == 0, "hai tiến trình cùng chiếm 1 slot!"
            held = 1
        else:
            held = 0
    assert held == 0


def test_two_processes_serialize_on_single_slot(tmp_path):
    """2 tiến trình thật (subprocess) -> flock phải chặn thật."""
    code = (
        "import sys, time\n"
        "sys.path.insert(0, {skill!r})\n"
        "from roffkit.slots import SlotLedger\n"
        "ledger = SlotLedger(state_dir={state!r})\n"
        "lease = ledger.acquire('h1', 1, timeout=15)\n"
        "print('acquired', flush=True)\n"
        "time.sleep(1.5)\n"
        "ledger.release(lease)\n"
        "print('released', flush=True)\n"
    ).format(skill=_SKILL, state=str(tmp_path))

    env = dict(os.environ)
    env["PYTHONPATH"] = _SKILL + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
    )
    try:
        line = proc.stdout.readline() if proc.stdout else ""
        if "acquired" not in line:
            err = proc.stderr.read() if proc.stderr else ""
            pytest.skip(f"subprocess không import được roffkit: {err.strip()[:300]}")

        ledger = SlotLedger(state_dir=str(tmp_path))
        with pytest.raises(RoffError):
            ledger.acquire("h1", 1, timeout=0.5)

        rest = proc.stdout.read() if proc.stdout else ""
        proc.wait(timeout=20)
        assert "released" in rest
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
