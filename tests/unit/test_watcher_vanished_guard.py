"""Unit test — lớp bảo vệ verdict ``vanished`` của watcher (P1 + P2).

Bối cảnh (đo thật 24/11/2026 trên fleet Muse, kịch bản S49):

    poll đầu tiên trả về::

        {'state': 'lost', 'exit': None, 'pid': 8155, 'pid_alive': False,
         'reachable': True, 'reason': 'vanished'}

nhưng mốc thời gian trên host cho thấy job **chạy thành công** (``exit=0``,
nice 19) — chỉ là poll đọc trúng khoảng giữa lúc process **vừa chết**
(``kill -0`` thất bại) và lúc file ``exit`` **chưa kịp** ghi.

⇒ ``vanished`` **không** được dùng để failover **ngay**. Hai lớp bảo vệ
(tách biệt nhau):

* **P1 — xác nhận lần 2**: hỏi lại host 1 lần nữa sau
  :data:`watcher.VANISH_CONFIRM_DELAY_S` giây; lần 2 vẫn thấy process
  không còn mới failover; lần 2 thấy ``exited`` ⇒ job xong bình thường.
* **P2 — khoảng miễn sau launch**: trong :data:`watcher.VANISH_GRACE_S` giây
  kể từ ``attempt["started"]`` ⇒ "không còn process" **chưa chắc chắn** ⇒
  ghi event ``unverified``, chờ thử lại, **không** failover.

Verdict mạnh hơn (``host_restarted`` / ``host_wiped`` / ``oom_killed``)
**không** qua 2 lớp này ⇒ failover ngay (không làm chậm).

Test chạy **không cần mạng**: seam ``remote_state`` + ``_sleep`` của
:mod:`roffkit.watcher`.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill"))

from roffkit import job as job_mod  # noqa: E402
from roffkit import watcher as watcher_mod  # noqa: E402


class StopLoop(Exception):
    """Dùng để thoát vòng lặp ``watch()`` trong test."""


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("ROFF_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ROFF_CONFIG", str(tmp_path / "none.ini"))


def make_job(**kwargs) -> job_mod.Job:
    """Job đã launch trên host (có attempt với ``boot_id`` + token)."""
    base = dict(
        id="261124-204202-ab12",
        cmd="echo hello",
        host="muse-a",
        project="proj",
        created_at=1_700_000_000.0,
        state="running",
        out_remote="/home/hatch/.roff/jobs/261124-204202-ab12/out",
        out_local="/tmp/out-261124-204202-ab12",
    )
    base.update(kwargs)
    return job_mod.Job(**base)


def make_attempt(started: float | None = None, **kw) -> dict:
    """Attempt của job (giống hệt cái ``launch()`` ghi vào ledger)."""
    attempt = {
        "attempt": 0,
        "host": "muse-a",
        "pid": 8155,
        "boot_id": "boot-1",
        "instance_token": "tok-1",
        "started": time.time() - 600.0 if started is None else started,
        "failover": False,
    }
    attempt.update(kw)
    return attempt


def state_vanished(**kw) -> dict:
    """Trạng thái host đúng lúc race (pid có, ``kill -0`` thất bại)."""
    state = {
        "state": "lost",
        "exit": None,
        "reachable": True,
        "boot_id": "boot-1",
        "instance_token": "tok-1",
        "workdir_exists": True,
        "pid": 8155,
        "pid_alive": False,
        "oom": False,
        "reason": "vanished",
        "error": None,
        "detail": None,
    }
    state.update(kw)
    return state


def state_exited(exit_code: int = 0, **kw) -> dict:
    """Trạng thái host khi job **đã xong** (có file ``exit``)."""
    state = {
        "state": "exited",
        "exit": exit_code,
        "reachable": True,
        "boot_id": "boot-1",
        "instance_token": "tok-1",
        "workdir_exists": True,
        "pid": 8155,
        "pid_alive": False,
        "oom": False,
        "reason": None,
        "error": None,
        "detail": None,
    }
    state.update(kw)
    return state


def state_running(**kw) -> dict:
    """Trạng thái host khi job vẫn còn sống."""
    state = {
        "state": "running",
        "exit": None,
        "reachable": True,
        "boot_id": "boot-1",
        "instance_token": "tok-1",
        "workdir_exists": True,
        "pid": 8155,
        "pid_alive": True,
        "oom": False,
        "reason": None,
        "error": None,
        "detail": None,
    }
    state.update(kw)
    return state


class Harness:
    """Bộ giả lập watcher: đếm lượt hỏi host, ghi lại failover + pull + event.

    ``_failover`` giả **dừng** vòng lặp ngay (ném :class:`StopLoop`) — chỉ cần
    quan tâm lần failover **đầu tiên**.
    """

    def __init__(self, monkeypatch, job, states, *, sleep_limit: int = 6):
        self.job = job
        self.states = list(states)
        self.state_calls: list[dict] = []
        self.failovers: list[str] = []
        self.failover_at: list[int] = []
        self.pulls: list[tuple] = []
        self.slept: list[float] = []
        self.events: list[dict] = []
        self._sleep_limit = sleep_limit

        origin_record = watcher_mod._record_event

        def record(job_id, event, **fields):
            self.events.append({"event": event, **fields})
            origin_record(job_id, event, **fields)

        monkeypatch.setattr(watcher_mod, "load_job", lambda job_id: job)
        monkeypatch.setattr(watcher_mod, "save_job", lambda job: None)
        monkeypatch.setattr(watcher_mod, "remote_state", self._remote_state)
        monkeypatch.setattr(watcher_mod, "_failover", self._failover)
        monkeypatch.setattr(watcher_mod, "_sleep", self._sleep)
        monkeypatch.setattr(watcher_mod, "pull", self._pull)
        monkeypatch.setattr(watcher_mod, "_pull_sync_back", lambda job, opt: True)
        monkeypatch.setattr(watcher_mod, "_record_event", record)

    # -- seam -------------------------------------------------------------
    def _remote_state(self, host, job, timeout=30):
        index = min(len(self.state_calls), len(self.states) - 1)
        self.state_calls.append(self.states[index])
        return dict(self.states[index])

    def _failover(self, job, reason):
        self.failovers.append(reason)
        self.failover_at.append(len(self.state_calls))
        job.host = "muse-b"
        job.state = "launched"
        raise StopLoop()

    def _pull(self, *args, **kwargs):
        self.pulls.append(args)
        return SimpleNamespace(ok=True, files=[], bytes_transferred=0)

    def _sleep(self, seconds):
        self.slept.append(seconds)
        if len(self.slept) >= self._sleep_limit:
            raise StopLoop()

    # -- tiện ích ---------------------------------------------------------
    def event_names(self) -> list[str]:
        return [e["event"] for e in self.events]

    def confirmed(self) -> bool:
        """Watcher đã bước xác nhận lần 2 (P1) hay chưa?"""
        return "vanish_confirm" in self.event_names()


def _watch(harness, job) -> None:
    """Chạy ``watch()`` tới khi vòng lặp kết thúc (bình thường hoặc StopLoop)."""
    with pytest.raises(StopLoop):
        watcher_mod.watch(job.id)


# --------------------------------------------------------------------------
# 1) ``vanished`` lần đầu ⇒ KHÔNG failover ngay (phải hỏi lại)
# --------------------------------------------------------------------------
def test_first_vanished_does_not_failover_immediately(monkeypatch):
    """Lần đầu thấy ``vanished`` ⇒ watcher phải **hỏi lại** host, chưa failover."""
    job = make_job()
    job.attempts = [make_attempt()]
    harness = Harness(monkeypatch, job, [state_vanished(), state_running()])

    _watch(harness, job)

    assert harness.failovers == [], "vanished lần đầu KHÔNG được failover ngay"
    assert len(harness.state_calls) >= 2, "phải hỏi lại host lần 2"
    assert harness.confirmed(), "phải có bước xác nhận lần 2 (P1)"


def test_failover_never_happens_before_second_query(monkeypatch):
    """Failover (nếu có) phải xảy ra **sau** lần hỏi thứ 2, không phải lần 1."""
    job = make_job()
    job.attempts = [make_attempt()]
    harness = Harness(monkeypatch, job, [state_vanished(), state_vanished()])

    _watch(harness, job)

    assert harness.failover_at == [2], (
        "failover phải xảy ra ở lần hỏi thứ 2, không phải lần 1"
    )


# --------------------------------------------------------------------------
# 2) Lần hỏi thứ 2 vẫn ``vanished`` ⇒ CÓ failover
# --------------------------------------------------------------------------
def test_second_vanished_confirms_and_fails_over(monkeypatch):
    """Hỏi lại vẫn thấy process không còn ⇒ lần này mới failover."""
    job = make_job()
    job.attempts = [make_attempt()]
    harness = Harness(monkeypatch, job, [state_vanished(), state_vanished()])

    _watch(harness, job)

    assert harness.failovers == ["vanished"]
    assert len(harness.state_calls) == 2, "chỉ hỏi lại **một** lần"


def test_second_vanished_confirm_waits_before_second_query(monkeypatch):
    """Bước xác nhận phải **chờ** một khoảng ngắn trước khi hỏi lại host."""
    job = make_job()
    job.attempts = [make_attempt()]
    harness = Harness(monkeypatch, job, [state_vanished(), state_vanished()])

    _watch(harness, job)

    assert watcher_mod.VANISH_CONFIRM_DELAY_S in harness.slept


# --------------------------------------------------------------------------
# 3) Lần hỏi thứ 2 trả ``exited`` ⇒ KHÔNG failover, kết thúc bình thường
# --------------------------------------------------------------------------
def test_second_query_exited_means_job_finished_no_failover(monkeypatch):
    """Lần 2 thấy ``exited`` ⇒ job đã xong ⇒ kéo artifact, **không** failover."""
    job = make_job()
    job.attempts = [make_attempt()]
    harness = Harness(monkeypatch, job, [state_vanished(), state_exited(0)])

    watcher_mod.watch(job.id)

    assert harness.failovers == [], "job đã xong ⇒ KHÔNG failover"
    assert harness.pulls, "phải kéo artifact về local"
    assert job.state == "exited"
    assert job.exit_code == 0
    assert "finished" in harness.event_names()


def test_second_query_exited_nonzero_also_no_failover(monkeypatch):
    """Lần 2 thấy ``exited`` với exit != 0 ⇒ job tự fail ⇒ KHÔNG failover."""
    job = make_job()
    job.attempts = [make_attempt()]
    harness = Harness(monkeypatch, job, [state_vanished(), state_exited(3)])

    watcher_mod.watch(job.id)

    assert harness.failovers == []
    assert job.state == "exited"
    assert job.exit_code == 3


# --------------------------------------------------------------------------
# 4) Trong khoảng miễn kể từ ``attempt["started"]`` ⇒ KHÔNG failover
# --------------------------------------------------------------------------
def test_vanished_inside_grace_window_does_not_failover(monkeypatch):
    """Mới launch ⇒ "không còn process" **chưa chắc** ⇒ chờ, không failover."""
    job = make_job()
    job.attempts = [make_attempt(started=time.time() - 1.0)]
    harness = Harness(monkeypatch, job, [state_vanished()])

    _watch(harness, job)

    assert harness.failovers == [], "trong khoảng miễn KHÔNG được failover"
    assert not harness.confirmed(), "trong khoảng miễn không được hỏi lại host"
    assert job.state == "running"


def test_vanished_inside_grace_window_records_unverified(monkeypatch):
    """Trong khoảng miễn ⇒ ghi event ``unverified`` (quy tắc vàng)."""
    job = make_job()
    job.attempts = [make_attempt(started=time.time() - 1.0)]
    harness = Harness(monkeypatch, job, [state_vanished()])

    _watch(harness, job)

    assert harness.failovers == []
    assert "unverified" in harness.event_names(), harness.event_names()


def test_grace_window_expires_then_vanished_is_confirmed(monkeypatch):
    """Hết khoảng miễn ⇒ P1 (hỏi lại lần 2) mới áp dụng."""
    job = make_job()
    job.attempts = [
        make_attempt(started=time.time() - (watcher_mod.VANISH_GRACE_S + 10))
    ]
    harness = Harness(monkeypatch, job, [state_vanished(), state_vanished()])

    _watch(harness, job)

    assert harness.failovers == ["vanished"]
    assert len(harness.state_calls) == 2


# --------------------------------------------------------------------------
# 5) Verdict mạnh hơn ⇒ failover NGAY, không hỏi lại (không làm chậm)
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "reason,extra",
    [
        ("host_restarted", {"boot_id": "boot-2"}),
        ("host_wiped", {"instance_token": "tok-2"}),
        ("oom_killed", {"oom": True}),
    ],
)
def test_strong_verdicts_fail_over_without_second_query(monkeypatch, reason, extra):
    """``host_restarted``/``host_wiped``/``oom_killed`` ⇒ failover ngay."""
    job = make_job()
    job.attempts = [make_attempt()]
    harness = Harness(monkeypatch, job, [state_vanished(reason=reason, **extra)])

    _watch(harness, job)

    assert harness.failovers == [reason]
    assert len(harness.state_calls) == 1, (
        "verdict mạnh không được hỏi lại host (không làm chậm)"
    )


def test_strong_verdict_inside_grace_window_also_fails_over(monkeypatch):
    """Khoảng miễn chỉ bảo vệ verdict ``vanished`` — verdict mạnh vẫn ngay."""
    job = make_job()
    job.attempts = [make_attempt(started=time.time() - 1.0)]
    harness = Harness(
        monkeypatch,
        job,
        [state_vanished(reason="host_restarted", boot_id="boot-2")],
    )

    _watch(harness, job)

    assert harness.failovers == ["host_restarted"]
    assert len(harness.state_calls) == 1


# --------------------------------------------------------------------------
# Lớp bảo vệ phải **độc lập**
# --------------------------------------------------------------------------
def test_p2_alone_blocks_failover(monkeypatch):
    """P2 (khoảng miễn) chặn failover **dù** lần 2 cũng thấy ``vanished``."""
    job = make_job()
    job.attempts = [make_attempt(started=time.time() - 1.0)]
    harness = Harness(monkeypatch, job, [state_vanished(), state_vanished()])

    _watch(harness, job)

    assert harness.failovers == []
    assert not harness.confirmed()


def test_p1_alone_blocks_failover_on_first_vanished(monkeypatch):
    """P1 (hỏi lại lần 2) chặn failover **dù** ngoài khoảng miễn."""
    job = make_job()
    job.attempts = [
        make_attempt(started=time.time() - (watcher_mod.VANISH_GRACE_S + 10))
    ]
    harness = Harness(monkeypatch, job, [state_vanished(), state_running()])

    _watch(harness, job)

    assert harness.failovers == []
    assert len(harness.state_calls) >= 2


# --------------------------------------------------------------------------
# Hỏi lại KHÔNG được ⇒ chưa chắc (quy tắc vàng) ⇒ không failover
# --------------------------------------------------------------------------
def test_second_query_unreachable_does_not_failover(monkeypatch):
    """Hỏi lại lần 2 không với được host ⇒ KHÔNG failover (quy tắc vàng)."""
    job = make_job()
    job.attempts = [make_attempt()]
    harness = Harness(
        monkeypatch,
        job,
        [
            state_vanished(),
            {
                "state": "unreachable",
                "exit": None,
                "reachable": False,
                "boot_id": None,
                "instance_token": None,
                "workdir_exists": False,
                "pid": None,
                "pid_alive": False,
                "oom": False,
                "reason": None,
                "error": "ssh_unreachable",
                "detail": "Connection refused",
            },
        ],
    )

    _watch(harness, job)

    assert harness.failovers == [], "hỏi lại không được ⇒ KHÔNG failover"
