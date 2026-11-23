"""B17 — kho input dùng chung bị **kẹt vĩnh viễn** (lock mồ côi + watcher quay
vòng mãi).

Sự cố đo thật trên fleet Muse 23/11/2026: job ``261123-141011-5884`` treo
**2 giờ 10 phút**, ledger đầy sự kiện::

    push_failed | shared_push_failed
    push_failed | shared_input_busy
    push_failed | shared_input_busy      ← lặp vô hạn, mỗi vòng ~65 s

Trên host còn lại thư mục ``<key>.lock`` **không rỗng** (mồ côi) mà
``<key>.complete`` thì **không có** ⇒ mọi lần đẩy sau đó đều thấy "lock
bận" + "chưa có marker" ⇒ không bao giờ đẩy được nữa.

Ba nguyên nhân (đã đọc code):

1. ``_release_lock()`` dùng ``rmdir`` (chỉ xoá thư mục **rỗng**) và nuốt lỗi
   ⇒ host flap là lệnh không chạy ⇒ lock bị bỏ lại, không ai kiểm tra lại;
2. ``LOCK_STALE_S = 1800`` > ``MARKER_WAIT_S = 60`` ⇒ nhánh "lock chết" không
   bao giờ kịp chạy trong 1 lần chờ ⇒ phải quay vòng ~30 lần (~30 phút);
3. watcher bắt lỗi đẩy ⇒ ghi ``push_failed`` ⇒ **quay vòng mãi** trên cùng
   host ⇒ không failover, job treo tới hết ``timeout``.

Test ở đây **không cần mạng**: dùng seam ``_run_shell`` (runner giả) và
ledger tạm (``ROFF_STATE_DIR``).
"""

from __future__ import annotations

import json
import os
import shlex
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill"))

from roffkit import job as job_mod  # noqa: E402
from roffkit import shared as shared_mod  # noqa: E402
from roffkit import slots as slots_mod  # noqa: E402
from roffkit import watcher as watcher_mod  # noqa: E402


# --------------------------------------------------------------------------
# runner giả: chỉ quan tâm lock (tồn tại / bị xoá / lệnh hỏng)
# --------------------------------------------------------------------------
class FakeHost:
    """Host giả cho kho input: lock, marker, tuổi lock, chủ lock.

    Mô phỏng đúng những gì :mod:`roffkit.shared` cần:

    * ``mkdir`` (chiếm lock) — fail nếu lock đã tồn tại;
    * ``rm -rf`` + ``test ! -e`` (nhả lock, có bước **kiểm tra lại**);
    * ``stat -c %Y`` (tuổi lock) — chỉ trả về khi path **thực sự tồn tại**;
    * ``cat <lock>/owner`` (chủ lock).
    """

    def __init__(self) -> None:
        self.locks: set[str] = set()
        self.files: dict[str, str] = {}
        self.scripts: list[str] = []
        self.owner_writes: list[tuple[str, str]] = []
        self.lock_mtime = time.time()
        self.fail_times = 0
        self.fail_error = "ssh: connect to host muse-a port 22: Connection refused"

    # --- seam cho shared._run_shell -------------------------------------
    def run_shell(self, host: str, script: str, timeout: float = 60.0):
        self.scripts.append(script)
        if self.fail_times > 0:
            self.fail_times -= 1
            return (255, "", self.fail_error)
        rc, out = self._exec(script)
        return (rc, out, "")

    def _exec(self, script: str) -> tuple[int, str]:
        out = ""
        for part in script.split("&&"):
            rc, text = self._exec_part(part.strip())
            if rc != 0:
                return rc, text
            out = text or out
        return 0, out

    def _exec_part(self, part: str) -> tuple[int, str]:
        if not part:
            return 0, ""
        tokens = shlex.split(part)
        head = tokens[0] if tokens else ""
        if head == "mkdir" and "-p" in tokens[:2]:
            return 0, ""
        if head == "mkdir" and len(tokens) > 1:
            if tokens[1] in self.locks:
                return 1, ""
            self.locks.add(tokens[1])
            return 0, ""
        if head == "rm" and len(tokens) > 2:
            self.locks.discard(tokens[2])
            self.files.pop(tokens[2], None)
            self.files.pop(tokens[2] + "/owner", None)
            return 0, ""
        if head == "test" and len(tokens) > 2:
            path = tokens[-1]
            exists = path in self.locks or path in self.files
            if tokens[1] == "!":
                return (0, "") if not exists else (1, "")
            if tokens[1] == "-e":
                return (0, "") if exists else (1, "")
            return 0, ""
        if head == "stat" and len(tokens) > 3:
            path = tokens[3]
            if path in self.locks or path in self.files:
                return 0, str(int(self.lock_mtime))
            return 1, ""
        if head == "cat" and len(tokens) > 1:
            return 0, self.files.get(tokens[1], "")
        if head == "printf" and len(tokens) >= 5 and tokens[-2] == ">":
            self.files[tokens[-1]] = tokens[2]
            if tokens[-1].endswith("/owner"):
                self.owner_writes.append((tokens[-1], tokens[2]))
            return 0, ""
        # touch / chmod / grep ⇒ coi như thành công
        return 0, ""


@pytest.fixture
def host(monkeypatch):
    """Host giả cho mọi lệnh ssh của ``shared``."""
    fake = FakeHost()
    monkeypatch.setattr(shared_mod, "_run_shell", fake.run_shell)
    monkeypatch.setattr(shared_mod, "_sleep", lambda seconds: None)
    return fake


@pytest.fixture
def pushes(monkeypatch):
    """Ghi lại mọi lần gọi ``transfer.push`` (đếm số lần đẩy thật)."""
    calls: list[tuple] = []

    class Result:
        ok = True
        bytes_transferred = 1
        files: list = []
        error = None

    def fake_push(host_name, local_dir, remote_dir, *args, **kwargs):
        calls.append((host_name, local_dir, remote_dir))
        return Result()

    monkeypatch.setattr(shared_mod.transfer_mod, "push", fake_push)
    return calls


def _source(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("x")
    return src


# --------------------------------------------------------------------------
# 1) LOCK_STALE_S phải nhỏ hơn MARKER_WAIT_S
# --------------------------------------------------------------------------
def test_lock_stale_threshold_is_below_marker_wait():
    """Ngưỡng stale **phải** nhỏ hơn thời gian chờ marker.

    Nếu không, nhánh "lock chết" không bao giờ kịp chạy trong 1 lần chờ ⇒
    phải quay vòng ~30 lần (≈ 30 phút) mới dọn được 1 lock mồ côi.
    """
    assert shared_mod.LOCK_STALE_S < shared_mod.MARKER_WAIT_S


def test_lock_stale_is_not_the_old_30_minutes():
    """Ngưỡng cũ 30 phút ⇒ mỗi lock mồ côi "ăn" 30 phút của fleet."""
    assert shared_mod.LOCK_STALE_S <= 60.0


# --------------------------------------------------------------------------
# 2) lock mồ côi bị dọn trong 1 lần chờ ⇒ đẩy được
# --------------------------------------------------------------------------
def test_stale_orphan_lock_is_cleaned_and_push_succeeds(host, pushes, tmp_path):
    """Lock mồ côi + không có marker ⇒ lần đẩy sau **dọn lock cũ và đẩy được**.

    Không được trả lỗi ``shared_input_busy`` (đó là hình thức kẹt vĩnh viễn).
    """
    src = _source(tmp_path)
    root = "/home/hatch/.roff"
    lock = shared_mod.shared_lock(root, str(src))
    host.locks.add(lock)                     # lock mồ côi của agent đã chết
    host.lock_mtime = time.time() - 45.0     # tuổi > LOCK_STALE_S (30 s)

    path = shared_mod.ensure_shared_input("muse-a", str(src), root)

    assert len(pushes) == 1, "phải đẩy được sau khi dọn lock cũ"
    assert lock not in host.locks, "lock cũ phải được dọn"
    assert path == f"{root}/inputs/{shared_mod.shared_key(str(src))}"


def test_stale_orphan_lock_writes_marker_for_next_time(host, pushes, tmp_path):
    """Sau khi dọn + đẩy ⇒ có ``.complete`` ⇒ lần sau **dùng lại**, không đẩy lại."""
    src = _source(tmp_path)
    root = "/home/hatch/.roff"
    lock = shared_mod.shared_lock(root, str(src))
    host.locks.add(lock)
    host.lock_mtime = time.time() - 45.0

    shared_mod.ensure_shared_input("muse-a", str(src), root)
    marker = shared_mod.shared_marker(root, str(src))
    assert marker in host.files, "phải ghi marker .complete"
    assert len(pushes) == 1

    shared_mod.ensure_shared_input("muse-a", str(src), root)
    assert len(pushes) == 1, "lần sau phải dùng lại bản copy, không đẩy lại"


def test_live_lock_of_running_process_is_not_stolen(host, pushes, tmp_path, monkeypatch):
    """Lock của tiến trình **đang sống** ⇒ không được xoá.

    Không xoá ⇒ lần đẩy phải báo ``shared_input_busy`` (có lý do rõ) thay vì
    đẩy trùng làm hỏng dữ liệu.
    """
    src = _source(tmp_path)
    root = "/home/hatch/.roff"
    lock = shared_mod.shared_lock(root, str(src))
    host.locks.add(lock)
    host.lock_mtime = time.time()                     # lock còn mới
    host.files[lock + "/owner"] = json.dumps(
        {"id": "abc", "pid": os.getpid(), "host": "may-cua-toi", "started": time.time()}
    )
    monkeypatch.setattr(shared_mod, "MARKER_WAIT_S", 0.05)

    with pytest.raises(Exception) as exc:
        shared_mod.ensure_shared_input("muse-a", str(src), root)

    assert exc.value.reason == "shared_input_busy"
    assert lock in host.locks, "lock của tiến trình còn sống không được xoá"
    assert not pushes, "không được đẩy trùng"


# --------------------------------------------------------------------------
# 3) nhả lock phải chắc chắn xong
# --------------------------------------------------------------------------
def test_release_lock_uses_rm_rf_and_verifies(host):
    """Phải dùng ``rm -rf`` (xoá được thư mục **không rỗng**) + **kiểm tra lại**.

    ``rmdir`` cũ chỉ xoá thư mục rỗng ⇒ luôn bỏ lại lock có file ``owner``.
    """
    lock = "/home/hatch/.roff/inputs/abc.lock"
    host.locks.add(lock)

    released = shared_mod._release_lock("muse-a", lock)

    assert released is True
    joined = " ".join(host.scripts)
    assert "rm -rf" in joined, f"phải dùng rm -rf: {host.scripts}"
    assert "rmdir" not in joined, f"rmdir không xoá được thư mục không rỗng: {host.scripts}"
    assert "test ! -e" in joined, f"phải kiểm tra lại lock đã biến mất: {host.scripts}"
    assert lock not in host.locks


def test_release_lock_retries_when_first_command_fails(host, monkeypatch):
    """Host flap ⇒ lệnh ssh hỏng lần đầu ⇒ phải **thử lại**, không bỏ sót lock."""
    lock = "/home/hatch/.roff/inputs/abc.lock"
    host.locks.add(lock)
    host.fail_times = 1          # lần đầu hỏng (host flap)

    released = shared_mod._release_lock("muse-a", lock)

    assert released is True, "phải thử lại và cuối cùng nhả được lock"
    assert lock not in host.locks
    assert len([s for s in host.scripts if "rm -rf" in s]) >= 2, "phải thử lại"


def test_release_lock_reports_when_lock_survives(host, capsys):
    """Lock vẫn còn sau mọi lần thử ⇒ phải **báo lại** (không im lặng)."""
    lock = "/home/hatch/.roff/inputs/abc.lock"
    host.locks.add(lock)

    # Host "chết" ⇒ mọi lệnh đều hỏng ⇒ không xoá được lock.
    host.fail_times = 99

    released = shared_mod._release_lock("muse-a", lock)

    assert released is False
    err = capsys.readouterr().err
    assert lock in err, f"phải báo lại lock còn lại: {err!r}"


def test_release_lock_is_idempotent(host):
    """Nhả 2 lần ⇒ lần 2 không lỗi (lock đã đi)."""
    lock = "/home/hatch/.roff/inputs/abc.lock"
    host.locks.add(lock)
    assert shared_mod._release_lock("muse-a", lock) is True
    assert shared_mod._release_lock("muse-a", lock) is True


def test_lock_records_its_owner(host, pushes, tmp_path):
    """Lock phải ghi **chủ** (pid + máy) để biết ai đang giữ."""
    src = _source(tmp_path)
    root = "/home/hatch/.roff"
    lock = shared_mod.shared_lock(root, str(src))

    shared_mod.ensure_shared_input("muse-a", str(src), root)

    assert host.owner_writes, "phải ghi file owner trong lock"
    path, raw = host.owner_writes[-1]
    assert path == lock + "/owner"
    owner = json.loads(raw)
    assert owner["pid"] == os.getpid()
    assert owner["host"]
    assert owner["id"]


def test_lock_heartbeat_touches_lock_while_pushing(host, monkeypatch):
    """Trong lúc đẩy (có thể kéo dài **vài giờ**) lock phải được "chạm".

    Không chạm ⇒ mtime đứng yên ⇒ sau ``LOCK_STALE_S`` người khác tưởng lock
    mồ côi và xoá ⇒ **đẩy trùng**.
    """
    beat = shared_mod._LockHeartbeat(
        "muse-a", "/home/hatch/.roff/inputs/abc.lock", "token-1", interval=0.05
    )
    try:
        beat.start()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not any(
            "touch" in s for s in host.scripts
        ):
            time.sleep(0.02)
    finally:
        beat.stop()

    assert any("touch" in s for s in host.scripts), "phải chạm lock khi đang đẩy"


# --------------------------------------------------------------------------
# 4) watcher: đẩy thất bại liên tiếp ⇒ failover hoặc lost (KHÔNG quay vòng mãi)
# --------------------------------------------------------------------------
class _Stop(Exception):
    """Dừng vòng lặp watcher trong test (bảo đảm test không treo)."""


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("ROFF_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ROFF_CONFIG", str(tmp_path / "none.ini"))


def _queued_job(tmp_path, ledger, **options):
    """Job ở state ``queued`` (đang xếp hàng) + ticket slot của nó."""
    job_id = "261123-141530-b17"
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
    payload = {
        "workdir": "/home/hatch/.roff/projects/demo",
        "layout_root": "/home/hatch/.roff",
        "slot_host": "muse-a",
        "slot_ticket": ticket,
        "slot_slots": 1,
        "retry": 1,
        "timeout": 600,
    }
    payload.update(options)
    job_mod.set_job_options(job_id, **payload)
    return job, ticket


def _failing_start(reason: str, calls: list):
    """Giả lập ``_start`` luôn hỏng với lý do ``reason``."""

    def _start(job):
        calls.append(job.id)
        job_mod.set_job_options(job.id, start_error=reason, start_error_detail=reason)
        return False

    return _start


def test_input_push_failed_is_infrastructure_reason():
    """Lý do mới là lỗi **hạ tầng** ⇒ được failover/retry."""
    assert "input_push_failed" in watcher_mod.INFRASTRUCTURE_REASONS
    assert (
        watcher_mod.should_retry(
            kind="input_push_failed", attempts=0, max_attempts=1, needs_retry=True
        )
        is True
    )
    # --side-effects ⇒ không retry (tôn trọng cờ của người dùng)
    assert (
        watcher_mod.should_retry(
            kind="input_push_failed", attempts=0, max_attempts=1, needs_retry=False
        )
        is False
    )


def test_watch_fails_over_after_repeated_push_failures(monkeypatch, tmp_path):
    """Đẩy hỏng liên tiếp N lần trên cùng host ⇒ **failover**, không lặp vô hạn."""
    ledger = slots_mod.SlotLedger(state_dir=str(tmp_path / "state"))
    job, _ticket = _queued_job(tmp_path, ledger)

    starts: list = []
    failovers: list = []

    monkeypatch.setattr(
        watcher_mod, "_start", _failing_start("input_push_failed", starts)
    )

    def fake_failover(job, reason):
        failovers.append(reason)
        job.host = "muse-b"
        job.state = "launched"
        return True

    monkeypatch.setattr(watcher_mod, "_failover", fake_failover)
    monkeypatch.setattr(
        watcher_mod, "remote_state", lambda *a, **k: {"reachable": False}
    )

    slept: list = []

    def fake_sleep(seconds):
        slept.append(seconds)
        if len(slept) >= 3:
            raise _Stop()

    monkeypatch.setattr(watcher_mod, "_sleep", fake_sleep)

    with pytest.raises(_Stop):
        watcher_mod.watch(job.id)

    assert failovers == ["input_push_failed"], "phải failover đúng 1 lần"
    assert len(starts) == watcher_mod.MAX_START_FAILURES, (
        "phải dừng sau N lần đẩy hỏng, không quay vòng vô hạn"
    )
    assert len(slept) < watcher_mod.MAX_START_FAILURES + 5, "không được lặp mãi"


def test_watch_marks_lost_when_host_is_pinned(monkeypatch, tmp_path):
    """Host **ghim** (``--host``) ⇒ không failover được ⇒ job ``lost`` + reason rõ."""
    ledger = slots_mod.SlotLedger(state_dir=str(tmp_path / "state"))
    job, _ticket = _queued_job(
        tmp_path,
        ledger,
        host_reasons=["host bị ép qua --host muse-a"],
    )

    starts: list = []
    failovers: list = []

    monkeypatch.setattr(
        watcher_mod, "_start", _failing_start("input_push_failed", starts)
    )
    monkeypatch.setattr(
        watcher_mod, "_failover", lambda job, reason: failovers.append(reason) or True
    )
    monkeypatch.setattr(watcher_mod, "_sleep", lambda seconds: None)

    watcher_mod.watch(job.id)

    assert failovers == [], "job ghim host không được failover sang máy khác"
    assert len(starts) == watcher_mod.MAX_START_FAILURES, "phải dừng sau N lần"
    saved = job_mod.load_job(job.id)
    assert saved.state == "lost", f"job phải kết thúc, không treo: {saved.state}"
    assert saved.reason == "input_push_failed", f"reason phải rõ: {saved.reason}"
    events = [e["event"] for e in job_mod.job_options(job.id).get("events", [])]
    assert "lost" in events


def test_watch_marks_lost_when_no_retry_allowed(monkeypatch, tmp_path):
    """``--side-effects`` ⇒ không failover ⇒ ``lost`` với reason rõ."""
    ledger = slots_mod.SlotLedger(state_dir=str(tmp_path / "state"))
    job, _ticket = _queued_job(tmp_path, ledger)
    job.needs_retry = False
    job_mod.save_job(job)

    starts: list = []
    failovers: list = []
    monkeypatch.setattr(
        watcher_mod, "_start", _failing_start("input_push_failed", starts)
    )
    monkeypatch.setattr(
        watcher_mod, "_failover", lambda job, reason: failovers.append(reason) or True
    )

    watcher_mod.watch(job.id)

    assert failovers == [], "--side-effects không được failover"
    saved = job_mod.load_job(job.id)
    assert saved.state == "lost"
    assert saved.reason == "input_push_failed"


def test_start_failure_counter_is_per_host(monkeypatch, tmp_path):
    """Bộ đếm lỗi tính **theo host** ⇒ đổi host thì bộ đếm về 0."""
    ledger = slots_mod.SlotLedger(state_dir=str(tmp_path / "state"))
    job, _ticket = _queued_job(tmp_path, ledger)

    first = watcher_mod._count_start_failure(job.id, "muse-a", "input_push_failed")
    second = watcher_mod._count_start_failure(job.id, "muse-a", "input_push_failed")
    other = watcher_mod._count_start_failure(job.id, "muse-b", "input_push_failed")

    assert (first, second) == (1, 2)
    assert other == 1, "đổi host thì bộ đếm phải về 0"

    watcher_mod._clear_start_failures(job.id)
    assert watcher_mod._count_start_failure(job.id, "muse-b", "x") == 1


def test_start_records_failure_reason_in_ledger(monkeypatch, tmp_path):
    """``_start`` hỏng ⇒ phải ghi **lý do** vào ledger (watcher đọc được)."""
    from roffkit import inputs as inputs_mod

    job, _ticket = _queued_job(
        tmp_path, slots_mod.SlotLedger(state_dir=str(tmp_path / "state"))
    )

    def boom(*a, **k):
        raise Exception("shared_input_busy")

    monkeypatch.setattr(inputs_mod, "prepare_inputs", boom)

    assert watcher_mod._start(job) is False
    options = job_mod.job_options(job.id)
    assert options.get("start_error") == "input_push_failed"


def test_pinned_host_detection():
    """Host ghim phải nhận ra được (qua ``host_reasons`` của cli.py)."""
    pinned = {"host_reasons": ["host bị ép qua --host muse-pmb333"]}
    assert watcher_mod._host_is_pinned(pinned, "muse-pmb333") is True
    assert watcher_mod._host_is_pinned(pinned, "muse-khac") is False
    assert watcher_mod._host_is_pinned({}, "muse-a") is False
    assert watcher_mod._host_is_pinned({"host_reasons": ["host còn slot"]}, "muse-a") is False


# --------------------------------------------------------------------------
# 5) input local biến mất ⇒ job kết thúc với reason đúng (không treo)
# --------------------------------------------------------------------------
def test_input_not_found_is_not_infrastructure_reason():
    """Lỗi **của job** (file local mất) không được coi là hạ tầng."""
    assert "input_not_found" not in watcher_mod.INFRASTRUCTURE_REASONS
    assert (
        watcher_mod.should_retry(
            kind="input_not_found", attempts=0, max_attempts=3, needs_retry=True
        )
        is False
    )


def test_watch_ends_when_local_input_disappeared(monkeypatch, tmp_path):
    """File input local bị xoá giữa chừng ⇒ watcher **kết thúc**, không treo.

    Không failover (máy khác cũng không có file đó) và không quay vòng vô hạn:
    lỗi này là lỗi của **job** ⇒ kết thúc ngay với reason ``input_not_found``.
    """
    missing = tmp_path / "da-bi-xoa.mp4"
    ledger = slots_mod.SlotLedger(state_dir=str(tmp_path / "state"))
    job, _ticket = _queued_job(
        tmp_path,
        ledger,
        input_specs=[{"kind": "path", "value": str(missing)}],
    )

    failovers: list = []
    monkeypatch.setattr(
        watcher_mod, "_failover", lambda job, reason: failovers.append(reason) or True
    )
    monkeypatch.setattr(watcher_mod, "_sleep", lambda seconds: None)

    watcher_mod.watch(job.id)

    saved = job_mod.load_job(job.id)
    assert saved.state == "lost", f"job phải kết thúc, không treo: {saved.state}"
    assert saved.reason == "input_not_found", f"reason phải đúng: {saved.reason}"
    assert not failovers, "lỗi của job không được failover"
    events = [e["event"] for e in job_mod.job_options(job.id).get("events", [])]
    assert events.count("prepare_input_failed") == 1, (
        f"phải kết thúc ngay, không quay vòng: {events}"
    )


def test_start_marks_job_lost_when_input_missing(monkeypatch, tmp_path):
    """``_start`` thấy input local không có ⇒ đánh dấu ``lost`` ngay."""
    ledger = slots_mod.SlotLedger(state_dir=str(tmp_path / "state"))
    job, _ticket = _queued_job(
        tmp_path,
        ledger,
        input_specs=[{"kind": "path", "value": str(tmp_path / "khong-co.mp4")}],
    )

    assert watcher_mod._start(job) is False
    saved = job_mod.load_job(job.id)
    assert saved.state == "lost"
    assert saved.reason == "input_not_found"
