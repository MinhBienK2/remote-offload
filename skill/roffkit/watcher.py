"""Vòng lặp giám sát job: chờ, phát hiện lỗi hạ tầng, failover, retry.

Quy tắc vàng (API-CONTRACT §11):

    **Mất kết nối KHÔNG đồng nghĩa job chết.**

Chỉ failover khi **chắc chắn** job đã mất:

===========================  =====================  ==================
Quan sát                    Kết luận               Hành động
===========================  =====================  ==================
``boot_id`` khác lúc launch  host restart           failover được
``instance_token`` khác      host bị wipe           failover được (re-push)
boot+token giống, hết proc   job tự biến mất        failover được
không kết nối được           chỉ mất tunnel         **KHÔNG** failover
===========================  =====================  ==================

Ngoài ra:

* **không bao giờ** retry khi job tự fail (exit != 0 do job) — bug R20;
* ``wait_for()`` poll **ledger cục bộ**, không ssh (0 round-trip);
* watcher là **tiến trình nền**, sống sót khi agent chết (kịch bản S10);
* verdict ``vanished`` **phải qua 2 lớp bảo vệ** trước khi failover —
  ``vanished`` là kết luận **mạnh** nhưng **không** phải bằng chứng tuyệt
  đối (đọc trạng thái đúng lúc process vừa chết ⇒ verdict sai, đo thật
  24/11/2026): **(P1)** hỏi lại host lần 2, **(P2)** không kết luận trong
  khoảng miễn ngay sau launch.

Module này **không in ra stdout** (quy tắc chung của roff).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from typing import Any

try:  # phần A — fallback tối giểu để test chạy được khi A chưa merge
    from .errors import (
        EXIT_INPUT,
        EXIT_JOB_FAILED,
        EXIT_NO_WORKDIR,
        EXIT_TIMEOUT,
        EXIT_UNREACHABLE,
        REASON_OOM,
        REASON_PULL_FAILED,
        REASON_RESTARTED,
        REASON_SYNC_BACK_FAILED,
        REASON_VANISHED,
        REASON_WIPED,
        RoffError,
    )
except ImportError:  # pragma: no cover - module chạy đứng riêng (không có package)
    from typing import Any as _Any

    EXIT_INPUT = 2
    EXIT_JOB_FAILED = 1
    EXIT_NO_WORKDIR = 97
    EXIT_TIMEOUT = 124
    EXIT_UNREACHABLE = 255
    REASON_OOM = "oom_killed"
    REASON_PULL_FAILED = "pull_failed"
    REASON_RESTARTED = "host_restarted"
    REASON_SYNC_BACK_FAILED = "sync_back_failed"
    REASON_VANISHED = "vanished"
    REASON_WIPED = "host_wiped"

    class RoffError(Exception):  # type: ignore[no-redef]
        """Bản sao tối thiểu của ``roffkit.errors.RoffError``."""

        def __init__(self, code: int, reason: str = "", **fields: _Any) -> None:
            super().__init__(reason or f"exit {code}")
            self.code = code
            self.reason = reason
            self.fields = fields

        def payload(self) -> dict:
            out: dict[str, _Any] = {"ok": False, "exit_code": self.code}
            if self.reason:
                out["reason"] = self.reason
            out.update(self.fields)
            return out


try:
    from .config import Config, local_nice_prefix
except ImportError:  # pragma: no cover - module chạy đứng riêng (không có package)
    from dataclasses import dataclass as _dataclass

    def local_nice_prefix() -> list[str]:  # type: ignore[no-redef]
        """Fallback tối thiểu — xem :func:`roffkit.config.local_nice_prefix`.

        Module chạy **đứng riêng** ⇒ không có ``config.py`` ⇒ không có
        chính sách nice/ionice ⇒ không bọc.
        """
        return []

    @_dataclass
    class Config:  # type: ignore[no-redef]
        """Config tối thiểu (chỉ dùng khi phần A chưa có)."""

        path: str | None = None
        probe_cache_s: int = 60


try:
    from .fleet import env_fleet_aliases
except ImportError:  # pragma: no cover - module chạy đứng riêng (không có package)
    def env_fleet_aliases(cfg: Any) -> list[str]:  # type: ignore[misc]
        """Bản sao tối giểu (chưa có phần A)."""
        return []


try:
    from .probe import cached_profiles
except ImportError:  # pragma: no cover - module chạy đứng riêng (không có package)
    def cached_profiles(aliases: list[str], cfg: Any) -> list[Any]:  # type: ignore[misc]
        """Bản sao tối thiểu (chưa có phần B)."""
        return []


try:
    from .policy import Need, select
except ImportError:  # pragma: no cover - module chạy đứng riêng (không có package)
    from dataclasses import dataclass as _dataclass
    from dataclasses import field as _field

    @_dataclass
    class Need:  # type: ignore[no-redef]
        """Bản sao tối thiểu của ``roffkit.policy.Need``."""

        tools: list[str] = _field(default_factory=list)
        mem_mb: int = 0
        disk_mb: int = 0
        cpus: int = 0
        internet: bool = False
        project: str = ""

    def select(*args: Any, **kwargs: Any) -> Any:  # type: ignore[misc]
        """Bản sao tối thiểu (chưa có phần B) → không chọn được host."""
        return None


from .job import (  # noqa: E402  (sau fallback để giữ tương thích)
    TERMINAL_STATES,
    Job,
    heartbeat_path,
    job_options,
    launch,
    load_job,
    load_job_env,
    remote_job_dir,
    remote_state,
    save_job,
    save_job_env,
    set_job_options,
)
from . import inputs as inputs_mod  # noqa: E402
from . import perm  # noqa: E402
from . import layout as layout_mod  # noqa: E402
from . import shared as shared_mod  # noqa: E402
from . import slots as slots_mod  # noqa: E402
from . import toolchain as toolchain_mod  # noqa: E402
from .transfer import pull  # noqa: E402
from .transfer import pull_path  # noqa: E402


__all__ = [
    "watch",
    "spawn_watcher",
    "wait_for",
    "failover_decision",
    "should_retry",
    "validate_timeout",
    "job_events",
    "record_event",
    "REASON_INPUT_PUSH_FAILED",
    "VANISH_CONFIRM_DELAY_S",
    "VANISH_GRACE_S",
]


#: Reason khi **không đẩy được input** lên host (kho input bị kẹt, host flap).
#:
#: Là lỗi **hạ tầng** của host đó (không phải lỗi của job) ⇒ được failover
#: sang host khác, hoặc job bị đánh dấu ``lost`` với lý do này. Trước đây
#: watcher quay vòng **mãi** trên cùng host ⇒ job treo tới hết ``timeout``
#: (2 giờ 10 phút) — đã xảy ra thật trên fleet Muse 23/11/2026.
REASON_INPUT_PUSH_FAILED = "input_push_failed"

#: Reason khi không tạo được thư mục làm việc trên host.
REASON_WORKDIR_MISSING = "workdir_missing"

#: Reason khi launch thất bại (không phải workdir / input).
REASON_LAUNCH_FAILED = "launch_failed"

#: Các lỗi **hạ tầng** → được phép failover/retry.
INFRASTRUCTURE_REASONS: tuple[str, ...] = (
    REASON_RESTARTED,
    REASON_WIPED,
    REASON_OOM,
    REASON_VANISHED,
    REASON_INPUT_PUSH_FAILED,
)

#: Lý do "job tự fail" — KHÔNG BAO GIỜ được retry (bug R20).
JOB_FAILED = "job_failed"

#: Số lần `_start` thất bại **liên tiếp** trên cùng host ⇒ coi host đó hỏng
#: ⇒ failover (nếu được phép) hoặc đánh dấu ``lost`` — **không** quay vòng
#: vô hạn (B17).
MAX_START_FAILURES = 3

#: Dấu hiệu host bị **ghim** (``--host``) trong ``host_reasons`` (cli.py ghi).
#: Job ghim KHÔNG được failover sang host khác (người dùng đã ép host) ⇒ khi
#: host đó hỏng thì job phải được đánh dấu ``lost`` với lý do rõ ràng.
PINNED_HOST_MARK = "--host"

#: Reason của lỗi kho input dùng chung ⇒ coi là lỗi **hạ tầng** của host.
_SHARED_PUSH_REASONS = ("shared_push_failed", "shared_input_busy")


# --------------------------------------------------------------------------
# hằng số
# --------------------------------------------------------------------------
#: Chu kỳ poll trạng thái job trên host (giây) — DESIGN §5.1.
POLL_INTERVAL = 5.0

#: Chu kỳ poll ledger cục bộ (giây) — chỉ đọc file, rẻ.
LOCAL_POLL_INTERVAL = 0.5

#: Timeout cho 1 lần hỏi trạng thái remote (giây).
REMOTE_TIMEOUT = 30

#: Số lần failover mặc định (Q25).
DEFAULT_RETRY = 1

#: Timeout mặc định của 1 job (giây) + phần đệm cho watcher.
DEFAULT_JOB_TIMEOUT = 7200

#: Ngưỡng timeout chờ cho watcher (giây).
WATCH_TIMEOUT = DEFAULT_JOB_TIMEOUT + 600

#: Chu kỳ reconcile sổ slot (số vòng poll) — dọn ticket/lease mồ côi (W3a).
RECONCILE_EVERY = 10

#: Số lần **thử** kéo artifact khi pull hỏng (mặc định).
#:
#: Pull hỏng **không** phải lỗi của job: reverse tunnel của fleet hay flap
#: (Muse restart 23/11/2026) ⇒ artifact vẫn nằm trên host, kéo lại được.
#: Cấu hình qua biến môi trường ``ROFF_PULL_RETRIES``.
DEFAULT_PULL_RETRIES = 3

#: Giãn cách giữa 2 lần thử kéo artifact (giây, mặc định).
#:
#: Cấu hình qua biến môi trường ``ROFF_PULL_RETRY_DELAY_S``.
DEFAULT_PULL_RETRY_DELAY_S = 5.0

#: Khoảng chờ (giây) **trước** khi hỏi lại host lần 2 để xác nhận ``vanished``.
#:
#: ``vanished`` là kết luận **mạnh** (host còn sống, ``boot_id`` +
#: ``instance_token`` không đổi, nhưng không còn process) — nhưng nó **KHÔNG**
#: phải bằng chứng tuyệt đối: đọc trạng thái đúng lúc process **vừa chết**
#: (``kill -0`` thất bại trong khi file ``exit`` **chưa kịp** ghi) thì verdict
#: **sai** (đo thật 24/11/2026 trên fleet Muse: job chạy thành công, exit 0,
#: nice 19 — nhưng poll báo ``vanished``).
#:
#: ⇒ Hỏi lại host **một lần nữa** sau khoảng ngắn; lần hỏi thứ hai vẫn thấy
#: vậy mới coi là mất thật. Lần hỏi thứ hai thấy ``exited`` ⇒ job đã xong
#: bình thường ⇒ xử lý như ``exited`` (kéo artifact, ghi ``finished``).
VANISH_CONFIRM_DELAY_S = 4.0

#: Khoảng miễn (giây) kể từ lúc launch mà **KHÔNG** được kết luận ``vanished``.
#:
#: Độ trễ ssh trên host Muse đo được 4–6 s ⇒ job **vừa launch** có thể chưa
#: hiện process trên host ⇒ thấy "không còn process" ngay sau launch **không**
#: đủ bằng chứng. Trong khoảng này, thấy process không còn ⇒ coi là **chưa
#: chắc chắn** (đúng quy tắc vàng: ghi event ``unverified`` rồi chờ thử lại),
#: **không** failover.
VANISH_GRACE_S = 30.0


# --------------------------------------------------------------------------
# tiện ích nhỏ
# --------------------------------------------------------------------------
def _config() -> Any | None:
    """Config hiện tại, hoặc ``None`` nếu không đọc được.

    **B27** — ``strict=False`` (cố ý): watcher đọc config **sau khi** job đã
    được *ghim host* trong ledger. Nếu ở đây ném lỗi (``config_missing``)
    thì watcher **chết** ⇒ job mất **im lặng** — tệ hơn hẳn việc dùng
    cấu hình mặc định. ``roff do`` (điểm vào của người dùng) mới là chỗ
    phải **fail to** khi ``ROFF_CONFIG`` trỏ tới file không tồn tại.
    """
    try:
        return Config.load(os.environ.get("ROFF_CONFIG") or None, strict=False)
    except Exception:
        return None


def _sleep(seconds: float) -> None:
    """Ngủ (seam cho test)."""
    time.sleep(seconds)


def _popen(cmd: list[str], **kwargs: Any) -> subprocess.Popen:
    """Mở tiến trình con (seam cho test)."""
    return subprocess.Popen(cmd, **kwargs)


def _retry_limit(options: dict) -> int:
    """Số lần failover được phép (``--retry``)."""
    try:
        return max(0, int(options.get("retry", DEFAULT_RETRY)))
    except (TypeError, ValueError):
        return DEFAULT_RETRY


def _watch_timeout(options: dict) -> float:
    """Trần thời gian watcher sống (giây)."""
    try:
        timeout = float(options.get("timeout", DEFAULT_JOB_TIMEOUT))
    except (TypeError, ValueError):
        timeout = float(DEFAULT_JOB_TIMEOUT)
    return max(60.0, timeout + 600.0)


def _failover_count(job: Job) -> int:
    """Số lần failover đã thực hiện."""
    return sum(1 for a in (job.attempts or []) if isinstance(a, dict) and a.get("failover"))


def _pull_retries() -> int:
    """Số lần thử kéo artifact (``ROFF_PULL_RETRIES``, mặc định 3).

    Giá trị sai ⇒ bỏ qua, dùng mặc định (không được làm watcher hỏng vì 1
    biến môi trường viết tay sai).
    """
    raw = os.environ.get("ROFF_PULL_RETRIES")
    if raw is None or str(raw).strip() == "":
        return DEFAULT_PULL_RETRIES
    try:
        return max(1, int(str(raw).strip()))
    except (TypeError, ValueError):
        return DEFAULT_PULL_RETRIES


def _pull_retry_delay() -> float:
    """Giãn cách giữa 2 lần thử kéo artifact (``ROFF_PULL_RETRY_DELAY_S``).

    Mặc định 5 s — đủ lâu cho reverse tunnel của host dựng lại.
    """
    raw = os.environ.get("ROFF_PULL_RETRY_DELAY_S")
    if raw is None or str(raw).strip() == "":
        return DEFAULT_PULL_RETRY_DELAY_S
    try:
        return max(0.0, float(str(raw).strip()))
    except (TypeError, ValueError):
        return DEFAULT_PULL_RETRY_DELAY_S


def _record_event(job_id: str, event: str, **fields: Any) -> None:
    """Ghi 1 sự kiện vào ledger (agent đọc được, vd ``disconnected``)."""
    try:
        options = job_options(job_id)
    except Exception:  # pragma: no cover
        return
    events = options.get("events")
    events = list(events) if isinstance(events, list) else []
    entry: dict[str, Any] = {"event": event, "ts": time.time()}
    entry.update(fields)
    events.append(entry)
    set_job_options(job_id, events=events[-200:])


def record_event(job_id: str, event: str, **fields: Any) -> None:
    """Ghi 1 sự kiện vào ledger (dùng cho cả CLI, không chỉ watcher).

    Bọc :func:`_record_event` để ``cli.py`` không phải gọi hàm ``_`` (private)
    của module khác.
    """
    _record_event(job_id, event, **fields)


def job_events(job_id: str) -> list[dict]:
    """Danh sách sự kiện đã ghi của 1 job."""
    events = job_options(job_id).get("events")
    return list(events) if isinstance(events, list) else []


def _touch_heartbeat(job_id: str) -> None:
    """Chạm file heartbeat (để ``roff status`` thấy job còn sống)."""
    path = heartbeat_path(job_id)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        perm.secure_dir(os.path.dirname(path))
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(str(time.time()))
        perm.secure_file(path)
    except OSError:  # pragma: no cover
        pass


# --------------------------------------------------------------------------
# quyết định failover — trái tim của watcher
# --------------------------------------------------------------------------
def failover_decision(profile_before: dict, profile_now: dict) -> str | None:
    """Trả reason nếu **chắc chắn** job đã mất; ``None`` nếu chưa chắc.

    Quy tắc (theo đúng thứ tự):

    1. không kết nối được → ``None`` (chỉ mất tunnel, **KHÔNG failover**);
    2. ``boot_id`` khác → ``host_restarted``;
    3. ``instance_token`` khác → ``host_wiped``;
    4. ``boot_id`` + token giống nhưng job không còn process → ``vanished``;
    5. còn lại → ``None`` (job vẫn sống).

    Args:
        profile_before: trạng thái lúc launch (``boot_id``, ``instance_token``).
        profile_now: trạng thái hiện tại (``reachable``, ``boot_id``,
            ``instance_token``, ``process_alive``).

    Returns:
        ``"host_restarted"`` | ``"host_wiped"`` | ``"vanished"`` | ``None``.
    """
    before = profile_before or {}
    now = profile_now or {}
    if not now.get("reachable", True):
        return None

    boot_before = before.get("boot_id")
    boot_now = now.get("boot_id")
    if boot_before and boot_now and boot_before != boot_now:
        return REASON_RESTARTED

    token_before = before.get("instance_token")
    token_now = now.get("instance_token")
    if token_before and token_now and token_before != token_now:
        return REASON_WIPED

    if now.get("process_alive") is False:
        return REASON_VANISHED

    return None


def should_retry(kind: str, attempts: int, max_attempts: int, needs_retry: bool) -> bool:
    """Job có được chạy lại không?

    * ``kind == "job_failed"`` → **LUÔN False** (bug R20: job tự fail KHÔNG
      BAO GIỜ được retry — lỗi do code của job, chạy lại cũng fail).
    * lỗi **hạ tầng** (``host_restarted`` / ``host_wiped`` / ``oom_killed`` /
      ``vanished``) → ``True`` khi ``needs_retry`` **và** còn lượt retry
      (``attempts < max_attempts``).
    * mọi kind khác → ``False`` (chỉ retry lỗi hạ tầng đã biết).

    Args:
        kind: loại lỗi (``"job_failed"`` hoặc reason hạ tầng).
        attempts: số lần đã thử (số failover đã làm).
        max_attempts: số lần retry tối đa còn được phép.
        needs_retry: ``False`` khi job có ``--side-effects``.
    """
    if kind == JOB_FAILED:
        return False
    if kind not in INFRASTRUCTURE_REASONS:
        return False
    if not needs_retry:
        return False
    try:
        return int(attempts) < int(max_attempts)
    except (TypeError, ValueError):
        return False


def validate_timeout(timeout: Any) -> float:
    """Kiểm tra tham số timeout.

    ``None``, âm, ``0`` hay không phải số → ``RoffError(EXIT_INPUT)`` (bug R17:
    ``--timeout 0`` phải bị từ chối, không được treo).

    Args:
        timeout: giá trị cần kiểm tra.

    Returns:
        timeout dạng ``float > 0``.
    """
    if timeout is None:
        raise RoffError(
            EXIT_INPUT, "invalid_timeout", error="timeout không được để None"
        )
    try:
        value = float(timeout)
    except (TypeError, ValueError):
        raise RoffError(
            EXIT_INPUT,
            "invalid_timeout",
            error=f"timeout không hợp lệ: {timeout!r}",
        ) from None
    if value <= 0:
        raise RoffError(
            EXIT_INPUT, "invalid_timeout", error=f"timeout phải > 0 (nhận {timeout})"
        )
    return value


def _loss_reason(job: Job, state: dict) -> str | None:
    """Reason job chắc chắn mất, theo :func:`failover_decision`.

    OOM là nguyên nhân **cụ thể** hơn "vanished": ``failover_decision`` chỉ
    biết boot_id/token nên luôn kết luận ``vanished`` khi process chết mà
    máy không restart. Nếu không ưu tiên OOM ở đây thì ``reason=oom_killed``
    **không bao giờ** tới được người đọc (vi phạm exit 69 của S5/R12).
    """
    if state.get("reason") == REASON_OOM or state.get("oom"):
        return REASON_OOM
    attempt = job.last_attempt() or {}
    before = {
        "reachable": True,
        "boot_id": attempt.get("boot_id"),
        "instance_token": attempt.get("instance_token"),
    }
    now = {
        "reachable": bool(state.get("reachable", True)),
        "boot_id": state.get("boot_id"),
        "instance_token": state.get("instance_token"),
        "process_alive": bool(state.get("pid_alive", False)),
    }
    decision = failover_decision(before, now)
    if decision:
        return decision
    return state.get("reason")


def _vanish_grace_remaining(job: Job) -> float:
    """Số giây còn lại của **khoảng miễn** ``vanished`` (tính từ lúc launch).

    Khoảng miễn tính từ ``attempt["started"]`` của attempt **hiện tại** ⇒
    sau mỗi lần failover lại có khoảng miễn mới (attempt mới có ``started``
    mới).

    Trả ``0.0`` khi attempt không ghi ``started`` (job định dạng cũ) ⇒ **không**
    có khoảng miễn, nhưng vẫn phải qua bước xác nhận lần 2 (P1).

    Args:
        job: job đang theo dõi.

    Returns:
        Số giây còn lại (``0.0`` = hết khoảng miễn).
    """
    attempt = job.last_attempt() or {}
    try:
        started = float(attempt.get("started") or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if started <= 0.0:
        return 0.0
    elapsed = time.time() - started
    if elapsed < 0.0:
        # Đồng hồ lệch ⇒ coi như vừa launch (an toàn: chờ, đừng failover).
        return VANISH_GRACE_S
    return max(0.0, VANISH_GRACE_S - elapsed)


def _confirm_vanished(job: Job, job_id: str, state: dict) -> dict:
    """Hỏi lại host **lần 2** để xác nhận ``vanished`` (P1).

    ``vanished`` **không** phải bằng chứng tuyệt đối: đọc trạng thái đúng lúc
    process vừa chết (``kill -0`` thất bại trong khi file ``exit`` chưa kịp
    ghi) thì verdict **sai** ⇒ failover một job **còn sống** (làm lại job, tốn
    băng thông, có thể chạy trùng hiệu ứng phụ).

    ⇒ Chỉ coi là mất thật khi lần hỏi thứ hai **vẫn** thấy process không còn
    (và không có exit file). Lần hỏi thứ hai thấy ``exited`` ⇒ job đã xong
    bình thường ⇒ xử lý như ``exited`` (**không** failover).

    Verdict mạnh hơn (``host_restarted`` / ``host_wiped`` / ``oom_killed``)
    **không** đi qua hàm này ⇒ không làm chậm những case đó.

    Args:
        job: job đang theo dõi.
        job_id: id job (để ghi event).
        state: trạng thái lần 1 (đã kết luận ``vanished``).

    Returns:
        dict trạng thái lần 2. Hỏi lại **không được** ⇒ dict
        ``reachable=False`` ⇒ coi như **chưa chắc** (quy tắc vàng: mất kết
        nối KHÔNG đồng nghĩa job chết).
    """
    _record_event(
        job_id,
        "vanish_confirm",
        host=job.host,
        delay_s=VANISH_CONFIRM_DELAY_S,
        pid=state.get("pid"),
    )
    _sleep(VANISH_CONFIRM_DELAY_S)
    try:
        again = remote_state(job.host, job, timeout=REMOTE_TIMEOUT)
    except Exception as exc:  # pragma: no cover - lỗi mạng
        _record_event(job_id, "vanish_confirm_failed", host=job.host, detail=str(exc))
        return {"reachable": False, "state": "unreachable", "reason": None}
    if not isinstance(again, dict):
        return {"reachable": False, "state": "unreachable", "reason": None}
    return again


def _finish_exited(job: Job, job_id: str, code: Any) -> None:
    """Job đã **xong** trên host: kéo artifact → ``exited`` → kết thúc.

    Tách riêng khỏi vòng lặp :func:`watch` để dùng lại được khi bước xác nhận
    lần 2 (P1) phát hiện job **đã xong** thay vì coi là ``vanished`` — cùng
    một đường đi ⇒ cùng một hợp đồng (kéo artifact **trước** khi đánh dấu
    terminal, rồi ghi event ``finished``).

    Args:
        job: job vừa xong.
        job_id: id job.
        code: exit code trên host (``None`` nếu không đọc được).
    """
    if code == 0:
        # KÉO ARTIFACT **TRƯỚC** KHI ĐÁNH DẤU TERMINAL.
        # Nếu đánh dấu trước, `roff wait` sẽ báo "exited" lúc thư mục
        # --out còn trống → agent đọc artifact ra rỗng. Lỗi này đã xảy
        # ra thật khi nghiệm thu S3 (23/11/2026).
        pulled_ok = _pull_artifacts(job)
        if not pulled_ok:
            # Pull hỏng hẳn (đã hết số lần thử) ⇒ job **remote** vẫn
            # chạy xong tốt (exit 0) nhưng artifact **chưa về** local.
            # Ghi `reason=pull_failed` để `roff wait` báo lỗi (exit 74)
            # thay vì báo thành công rồi để agent đọc thư mục rỗng (B23).
            job.reason = REASON_PULL_FAILED
        # KÉO PATH `--sync-back` VỀ PROJECT LOCAL (B26).
        # Trước đây không có mã nào làm việc này ⇒ `--sync-back` là
        # lệnh rỗng: job báo thành công, `pulled: true`, nhưng file
        # không về project ⇒ agent tin là đã có file.
        try:
            sync_options = job_options(job_id)
        except Exception:  # pragma: no cover - ledger hỏng
            sync_options = {}
        if not _pull_sync_back(job, sync_options):
            # Path sync-back **không** về local ⇒ báo lỗi (exit 74,
            # `reason=sync_back_failed`) — KHÔNG nuốt (bài học B23).
            # `pulled` giữ nguyên: artifact chính đã về thì đã về.
            if pulled_ok:
                job.reason = REASON_SYNC_BACK_FAILED
    job.exit_code = code
    if not _save_state(job, "exited"):
        _release_slot(job_id)
        return
    _record_event(job_id, "finished", exit=code, host=job.host)
    _release_slot(job_id)


def _save_state(job: Job, state: str) -> bool:
    """Ghi ``state`` vào ledger mà **không** ghi đè trạng thái terminal đã có.

    Race thật: ``roff cancel`` ghi ``cancelled`` vào ledger trong lúc watcher
    đang chờ ``remote_state`` (30 s). Watcher giữ bản job cũ trong RAM rồi ghi
    đè ⇒ job đã huỷ bị hồi sinh, tệ hơn: watcher thấy process đã chết ⇒
    ``vanished`` ⇒ **failover** ⇒ job bị huỷ chạy lại trên máy khác
    (vi phạm ``--side-effects``).

    Trả ``False`` nếu job đã ở trạng thái terminal (không ghi gì).
    """
    try:
        current = load_job(job.id)
    except Exception:
        current = None
    if current is not None and current.state in TERMINAL_STATES:
        return False
    job.state = state
    job.heartbeat = time.time()
    save_job(job)
    return True


def _mark_running(job: Job) -> bool:
    """Ghi ``state=running`` an toàn (xem :func:`_save_state`)."""
    return _save_state(job, "running")


# --------------------------------------------------------------------------
# start / failover
# --------------------------------------------------------------------------
def _layout_root_of_job(job: Job, options: dict) -> str:
    """Layout root của host (từ options, suy ra từ ``out_remote`` nếu thiếu)."""
    root = str(options.get("layout_root") or "")
    if root:
        return root
    try:
        return os.path.dirname(remote_job_dir(job))
    except Exception:  # pragma: no cover - job hỏng ⇒ không có layout root
        return ""


def _input_sources(options: dict) -> list[dict]:
    """Các input **local** phải có trong kho dùng chung của host.

    Job mới ghi ``input_sources`` (mọi input kiểu path); job cũ chỉ có
    ``input_local``/``input_dir`` ⇒ suy ra từ đó.
    """
    out: list[dict] = []
    for source in options.get("input_sources") or []:
        if isinstance(source, dict) and source.get("local"):
            out.append(source)
    if out:
        return out
    local = str(options.get("input_local") or options.get("input_dir") or "")
    kind = str(options.get("input_kind") or "path")
    if local and kind == "path":
        out.append({"local": local})
    return out


def _input_specs(options: dict) -> list[dict]:
    """Danh sách **mọi** input của job (kể cả job cũ chỉ có ``input_sources``).

    ``input_specs`` là nguồn sự thật (ghi lúc ``roff do``): nhờ đó watcher
    dựng lại được input không phải path (git:/http:) khi failover — input đó
    **không** nằm trong kho dùng chung nên không "đẩy lại" được.
    """
    specs = options.get("input_specs")
    if isinstance(specs, list):
        out = [
            {"kind": str(spec.get("kind")), "value": str(spec.get("value") or "")}
            for spec in specs
            if isinstance(spec, dict) and spec.get("kind")
        ]
        if out:
            return out
    out = []
    for source in _input_sources(options):
        local = str(source.get("local") or "")
        if local:
            out.append({"kind": "path", "value": local})
    return out


def _primary_input_local(options: dict) -> str:
    """Đường dẫn local của input **chính** (``$ROFF_IN``) nếu nó là path."""
    if str(options.get("input_kind") or "path") != "path":
        return ""
    return str(options.get("input_local") or options.get("input_dir") or "")


def _shell_runner(host: str, script: str) -> tuple[int, str, str]:
    """Chạy 1 script sh trên host (dùng seam của :mod:`roffkit.shared`)."""
    return shared_mod.run_shell(host, script)


def _release_slot(job_id: str) -> None:
    """Trả slot của 1 job (và bỏ ticket nếu job còn xếp hàng) — idempotent.

    Rò slot làm cả fleet bị khoá vĩnh viễn ⇒ mọi nhánh kết thúc của watcher
    đều phải gọi hàm này.
    """
    try:
        options = job_options(job_id)
    except Exception:  # pragma: no cover
        return
    ticket = str(options.get("slot_ticket") or "")
    if not ticket:
        return
    try:
        slots_mod.SlotLedger().cancel_slot(ticket)
    except Exception:  # pragma: no cover - release idempotent
        pass
    try:
        set_job_options(job_id, slot_ticket="", slot_released=True)
    except Exception:  # pragma: no cover
        pass


def _watcher_identity() -> tuple[int, float]:
    """(pid, starttime) của tiến trình watcher đang chạy."""
    pid = os.getpid()
    return pid, slots_mod.proc_start_time(pid)


def _set_holder(ticket: str, pid: int = 0, started: float = 0.0) -> None:
    """Ghi watcher làm **chủ** của ticket (lease hoặc ticket đang xếp hàng).

    Nhờ vậy reconcile biết ticket nào đã mồ côi (watcher chết) ⇒ thu hồi
    được slot thay vì kẹt host vĩnh viễn (W3a).
    """
    if not ticket:
        return
    if not pid:
        pid, started = _watcher_identity()
    try:
        slots_mod.SlotLedger().set_holder(ticket, pid, started)
    except Exception:  # pragma: no cover - sổ hỏng
        pass


def _register_watcher(job_id: str) -> None:
    """Ghi pid + starttime của watcher vào ledger job (W3a).

    Không có pid này thì không biết watcher còn sống hay không ⇒ slot của job
    bị bỏ rơi không bao giờ được thu hồi.
    """
    pid, started = _watcher_identity()
    try:
        set_job_options(
            job_id,
            watcher_pid=pid,
            watcher_started=started,
            watcher_started_at=time.time(),
        )
    except Exception:  # pragma: no cover
        pass
    try:
        ticket = str(job_options(job_id).get("slot_ticket") or "")
    except Exception:  # pragma: no cover
        ticket = ""
    _set_holder(ticket, pid, started)


def _reconcile_slots(job_id: str) -> None:
    """Dọn ticket/lease mồ côi của sổ slot (W3a).

    Không bao giờ đụng tới job của chính watcher này.
    """
    try:
        result = slots_mod.reconcile_slots(exclude_owners=[job_id])
    except Exception:  # pragma: no cover - sổ hỏng
        return
    removed = list(result.get("removed") or [])
    if removed:
        _record_event(
            job_id,
            "slot_reconcile",
            removed=len(removed),
            lost_jobs=list(result.get("lost_jobs") or []),
        )


def _acquire_slot(job: Job, options: dict) -> Any | None:
    """Xin slot cho job đang **xếp hàng** (timeout 0, mỗi vòng poll).

    Giữ **đúng 1 ticket** trong hàng đợi từ lúc ``roff do`` tạo job ⇒ thứ tự
    chờ (FIFO) không bị phá bởi việc mỗi vòng poll tạo ticket mới.

    Returns:
        ``Lease`` khi được cấp slot, ``None`` khi host vẫn đang bận.
    """
    host = str(options.get("slot_host") or job.host or "")
    if not host:
        return None
    try:
        slots = int(options.get("slot_slots") or 1) or 1
    except (TypeError, ValueError):
        slots = 1
    priority = str(options.get("priority") or "normal")
    ticket = str(options.get("slot_ticket") or "")
    try:
        ledger = slots_mod.SlotLedger()
    except Exception:  # pragma: no cover - sổ hỏng
        return None
    try:
        if ticket:
            if ledger.position(ticket) > 0:
                # Ticket vẫn còn trong hàng đợi ⇒ giữ nguyên vị trí (FIFO).
                lease = ledger.try_acquire(ticket)
                if lease is not None:
                    _set_holder(ticket)
                return lease
            held = ledger.lease_for(ticket)
            if held is not None:
                # Ticket đã được cấp slot rồi (lần trước launch lỗi) ⇒ giữ
                # nguyên lease, đừng đăng ký ticket mới (rò slot!).
                _set_holder(ticket)
                return held
        # Ticket không còn trong hàng đợi (bị xoá / state dọn) ⇒ đăng ký lại
        # (với **owner** = job id ⇒ ticket mồ côi nhận ra và dọn được).
        ticket = ledger.request_slot(host, slots, priority, owner=job.id)
        lease = ledger.try_acquire(ticket, owner=job.id)
        _set_holder(ticket)
        set_job_options(job.id, slot_ticket=ticket, slot_host=host)
        return lease
    except Exception as exc:  # pragma: no cover - sổ hỏng
        _record_event(job.id, "slot_request_failed", host=host, detail=str(exc))
        return None


class _StartFailure(Exception):
    """Lỗi khi khởi chạy job (nội bộ) — mang **lỗi gốc** + cách ghi ledger.

    * ``exc`` — lỗi gốc (giữ nguyên ``code``/``detail`` để ``roff do`` báo lại
      nguyên văn khi đi đường dẫn **nội tuyến**);
    * ``reason``/``event`` — lý do + tên event ghi vào ledger;
    * ``job_level`` — lỗi của **job** (không phải hạ tầng) ⇒ job kết thúc
      luôn ở state ``lost`` (không failover, không quay vòng).
    """

    def __init__(
        self,
        exc: Exception,
        reason: str,
        event: str,
        job_level: bool = False,
        code: int = 0,
        retryable: bool = False,
        detail: str = "",
    ) -> None:
        super().__init__(str(exc))
        self.exc = exc
        self.reason = reason
        self.event = event
        self.job_level = job_level
        self.code = int(code or 0)
        self.detail = detail or str(exc)
        """Chi tiết ghi vào ledger (``start_error_detail``)."""
        self.retryable = retryable
        """Lỗi **chuẩn bị input** do host chết ⇒ đường dẫn nội tuyến thử
        host khác (W3c). Lỗi **launch** thì KHÔNG retry (host còn sống, chỉ
        job hỏng) — đúng như hành vi cũ của ``roff do``."""

    def record(self, job: Job) -> None:
        """Ghi lỗi vào ledger (đúng như ``_start`` cũ làm)."""
        if self.job_level:
            _fail_job(job, self.reason, self.event, self.detail, self.code)
        else:
            _fail_start(job, self.reason, self.event, self.detail, self.code)

    def as_roff_error(self, job: Job) -> RoffError:
        """Lỗi ``RoffError`` tương đương lỗi gốc (để ``roff do`` báo ra)."""
        code = self.code or EXIT_JOB_FAILED
        fields: dict[str, Any] = {
            "error": f"khởi chạy job thất bại trên host {job.host}: {self.reason}",
            "job": job.id,
            "host": job.host,
        }
        if isinstance(self.exc, RoffError):
            code = self.exc.code or code
            original = str(self.exc.fields.get("error") or "")
            if original:
                fields["error"] = original
            detail = str(self.exc.fields.get("detail") or "")
            if detail:
                fields["detail"] = detail
        return RoffError(code, self.reason, **fields)


def _start(job: Job) -> bool:
    """Chuẩn bị input trên host rồi launch job (lần đầu hoặc sau khi xếp hàng).

    Input **không phải path** (``git:``/``http:``) cũng được dựng lại ở đây
    — job vừa được xếp hàng chưa từng chạm vào host nào.

    Khi thất bại ⇒ ghi **lý do** vào ledger (``start_error``) để watcher biết
    đây là lỗi **hạ tầng** (``input_push_failed``) hay lỗi của job ⇒ failover
    hay đánh dấu ``lost`` (B17).
    """
    try:
        _start_now(job)
    except _StartFailure as failure:
        failure.record(job)
        return False
    return True


def start_now(job_id: str) -> None:
    """Khởi chạy job **ngay trong tiến trình này** (không spawn watcher).

    Dùng khi **không có watcher nào** sẽ khởi chạy job — chế độ
    ``ROFF_NO_WATCHER=1`` (chỉ dùng cho test) — lúc đó ``roff do`` phải tự
    đẩy input + launch, và phải **báo lỗi ngay** (không nuốt) để giữ đúng
    hợp đồng exit-code của ``roff do``.

    Raises:
        RoffError: lỗi đẩy input / launch (code = exit code của lỗi gốc).
    """
    job = load_job(job_id)
    try:
        _start_now(job)
    except _StartFailure as failure:
        failure.record(job)
        raise failure.as_roff_error(job) from failure.exc


def _start_now(job: Job) -> None:
    """Đẩy input + tạo thư mục + launch job trên host (ném lỗi nếu hỏng)."""
    options = job_options(job.id)
    workdir = str(options.get("workdir") or "")
    layout_root = _layout_root_of_job(job, options)
    if not workdir or not layout_root:
        raise _StartFailure(
            RoffError(
                EXIT_JOB_FAILED,
                REASON_LAUNCH_FAILED,
                error="thiếu workdir hoặc layout_root trong ledger",
                job=job.id,
                host=job.host,
            ),
            REASON_LAUNCH_FAILED,
            "push_failed",
            detail="thiếu workdir hoặc layout_root trong ledger",
        )

    # --- 1) chuẩn bị mọi input trên host này -----------------------------
    specs = _input_specs(options)
    has_internet = bool(options.get("host_internet", False))
    try:
        prepared, primary_remote, primary_kind, env_extra = inputs_mod.prepare_inputs(
            specs,
            job.host,
            workdir,
            layout_root,
            has_internet=has_internet,
        )
    except Exception as exc:
        reason = _push_failure_reason(exc)
        # Lỗi của **job** (vd file local đã mất) ⇒ kết thúc luôn, không
        # failover (máy khác cũng không có file đó), không quay vòng.
        raise _StartFailure(
            exc,
            reason,
            "prepare_input_failed",
            job_level=_is_job_level_error(exc),
            code=_code_of(exc),
            retryable=not _is_job_level_error(exc),
        )

    if env_extra:
        try:
            saved = load_job_env(job.id)
            saved.update(env_extra)
            save_job_env(job.id, saved)
        except Exception:  # pragma: no cover
            pass

    options["input_remote"] = primary_remote
    options["input_kind"] = primary_kind
    path_sources = [
        {key: value for key, value in entry.items() if key != "value"}
        for entry in prepared
        if entry.get("kind") == "path"
    ]
    if path_sources:
        options["input_sources"] = path_sources
        first = path_sources[0]
        options["input_dir"] = first.get("local") or ""
        options["input_local"] = first.get("local") or ""
        options["input_shared_key"] = first.get("key") or ""
        options["input_shared_rel"] = first.get("rel") or ""

    # --- 2) item của --each (trong kho dùng chung của host này) -----------
    item_local = str(options.get("item_local") or "")
    if item_local:
        source_locals = [str(entry.get("local") or "") for entry in path_sources]
        try:
            remote, key, rel, inside = inputs_mod.resolve_item_path(
                job.host, item_local, layout_root, source_locals
            )
        except Exception as exc:
            reason = _push_failure_reason(exc)
            raise _StartFailure(
                exc,
                reason,
                "push_failed",
                job_level=_is_job_level_error(exc),
                code=_code_of(exc),
            )
        job.item = remote
        options["item_shared_key"] = key
        options["item_shared_rel"] = rel
        options["item_inside_input"] = inside

    # Ghi lại ledger TRƯỚC khi launch — launch() đọc options từ ledger để
    # dựng launch.sh ($ROFF_IN).
    set_job_options(job.id, **options)

    # --- 3) tạo thư mục làm việc trên host (launch cần workdir có sẵn) -----
    try:
        layout_mod.remote_mkdirs(job.host, workdir, run=_shell_runner)
        out_remote = str(job.out_remote or "")
        if out_remote:
            layout_mod.remote_mkdirs(
                job.host, os.path.dirname(out_remote), run=_shell_runner
            )
    except Exception as exc:
        raise _StartFailure(
            exc,
            REASON_WORKDIR_MISSING,
            "workdir_missing",
            code=_code_of(exc),
        )

    # Thư mục checkpoint phải có sẵn trên host (W3n) — job **xếp hàng** được
    # launch ở đây nên không ai tạo hộ được.
    checkpoint = str(options.get("checkpoint") or "")
    env = load_job_env(job.id)
    if checkpoint:
        try:
            layout_mod.remote_mkdirs(job.host, checkpoint, run=_shell_runner)
        except Exception as exc:
            raise _StartFailure(
                exc,
                REASON_WORKDIR_MISSING,
                "checkpoint_dir_failed",
                code=_code_of(exc),
            )
        env["ROFF_CHECKPOINT"] = checkpoint

    try:
        launch(
            job.host,
            job,
            env=env,
            cpus=job.cpus,
            mem_mb=job.mem_mb,
        )
    except RoffError as exc:
        if exc.code == EXIT_NO_WORKDIR:
            raise _StartFailure(
                exc, "workdir_missing", "workdir_missing",
                job_level=True, code=exc.code,
            )
        if exc.code == EXIT_INPUT:
            # job cấu hình sai (thiếu out_remote/project…) → không retry mãi
            raise _StartFailure(
                exc, "invalid_job", "invalid_job", job_level=True, code=exc.code
            )
        raise _StartFailure(
            exc,
            REASON_LAUNCH_FAILED,
            "launch_failed",
            code=_code_of(exc),
        )
    job.state = "launched"
    save_job(job)
    # (B36/C10) Đã launch thật ⇒ watcher **không** start lại lần nữa (vòng
    # poll kế tiếp sẽ chỉ theo dõi trạng thái remote). Có checkpoint ⇒
    # failover sau này chạy lại với ``$ROFF_RESUME=1``.
    try:
        set_job_options(
            job.id,
            deferred_start=False,
            resume=bool(options.get("checkpoint")),
        )
    except Exception:  # pragma: no cover - ledger hỏng
        pass
    return True


def _fail_start(
    job: Job, reason: str, event: str, detail: str = "", code: int = 0
) -> None:
    """Ghi lỗi `_start` vào ledger (event + ``start_error``).

    ``start_error`` là thứ watcher đọc để biết đây là lỗi **hạ tầng**
    (``input_push_failed``) hay lỗi của job ⇒ failover hay đánh dấu ``lost``.

    ``code`` (nếu có) = **exit code** của lỗi gốc (vd 255 = host không với
    được, 2 = input sai). Nhờ vậy đường dẫn **nội tuyến** (xem
    :func:`start_now`) báo lại đúng exit code cho ``roff do`` — không phải
    đoán lại từ chuỗi reason.
    """
    _record_event(job.id, event, host=job.host, detail=detail or reason)
    try:
        set_job_options(
            job.id,
            start_error=reason,
            start_error_detail=detail or reason,
            start_error_code=int(code or 0),
        )
    except Exception:  # pragma: no cover - ledger hỏng
        pass


def _fail_job(
    job: Job, reason: str, event: str, detail: str = "", code: int = 0
) -> bool:
    """Kết thúc job ngay với ``reason`` rõ ràng (lỗi của **job**, không phải hạ tầng).

    Lỗi loại này **không** được failover (failover cũng hỏng — vd file local
    đã mất thì máy khác cũng không có) và **không** được quay vòng vô hạn ⇒
    job kết thúc luôn ở state ``lost`` với ``reason`` đúng.
    """
    _fail_start(job, reason, event, detail, code)
    job.state = "lost"
    job.reason = reason
    save_job(job)
    return False


def _code_of(exc: Exception) -> int:
    """Exit code của lỗi (0 nếu không phải :class:`RoffError`)."""
    return _exit_code(exc)


def _push_failure_reason(exc: Exception) -> str:
    """Lý do đúng của lỗi khi **đẩy input** lên host.

    * lỗi kho input dùng chung (``shared_push_failed`` / ``shared_input_busy``)
      ⇒ lỗi **hạ tầng** của host đó ⇒ được failover;
    * host không với được (``EXIT_UNREACHABLE``) ⇒ cũng là lỗi hạ tầng ⇒
      được failover sang máy khác;
    * lỗi **của job** (vd ``input_not_found`` — file local đã mất) ⇒ KHÔNG
      failover (máy khác cũng không có file đó) và KHÔNG quay vòng vô hạn.
    """
    reason = str(getattr(exc, "reason", "") or "")
    if reason in _SHARED_PUSH_REASONS:
        return REASON_INPUT_PUSH_FAILED
    if _is_job_level_error(exc):
        return reason or "input_not_found"
    if _is_unreachable(exc):
        return REASON_INPUT_PUSH_FAILED
    if reason:
        return reason
    return REASON_INPUT_PUSH_FAILED


def _is_job_level_error(exc: Exception) -> bool:
    """Lỗi này là lỗi của **job** (không phải hạ tầng) không?

    ``EXIT_INPUT`` (2) = input sai / không có ⇒ failover **cũng hỏng** (file
    local đã mất thì máy khác cũng không có) ⇒ job phải kết thúc ngay với
    ``reason`` rõ ràng, không quay vòng.
    """
    return _exit_code(exc) == EXIT_INPUT


def _is_unreachable(exc: Exception) -> bool:
    """Lỗi này là "không với được host" không (``EXIT_UNREACHABLE``)?"""
    return _exit_code(exc) == EXIT_UNREACHABLE


def _exit_code(exc: Exception) -> int:
    """Exit code của lỗi (``0`` nếu không phải :class:`RoffError`)."""
    try:
        return int(getattr(exc, "code", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _host_is_pinned(options: dict, host: str) -> bool:
    """Job này có bị **ghim** vào 1 host không (``--host <alias>``)?

    Job ghim **không được** failover sang host khác (người dùng đã ép host) ⇒
    khi host đó hỏng, job phải được đánh dấu ``lost`` với lý do rõ ràng
    (``input_push_failed``), không quay vòng tới hết ``timeout``.
    """
    if options.get("host_pinned"):
        return True
    reasons = options.get("host_reasons") or []
    if not isinstance(reasons, (list, tuple)):
        return False
    for reason in reasons:
        text = str(reason or "")
        if PINNED_HOST_MARK in text and (not host or host in text):
            return True
    return False


def _count_start_failure(job_id: str, host: str, reason: str) -> int:
    """Đếm số lần `_start` thất bại **liên tiếp** trên cùng host.

    Bộ đếm nằm trong ledger ⇒ watcher được gia hạn (spawn lại) vẫn nhớ ⇒
    không quay vòng vô hạn qua nhiều đời watcher.
    """
    try:
        options = job_options(job_id)
    except Exception:  # pragma: no cover - ledger hỏng
        options = {}
    failures = 0
    if str(options.get("start_fail_host") or "") == host:
        try:
            failures = int(options.get("start_failures") or 0)
        except (TypeError, ValueError):
            failures = 0
    failures += 1
    try:
        set_job_options(
            job_id,
            start_failures=failures,
            start_fail_host=host,
            start_fail_reason=reason,
        )
    except Exception:  # pragma: no cover - ledger hỏng
        pass
    return failures


def _clear_start_failures(job_id: str) -> None:
    """Xoá bộ đếm lỗi (`_start` thành công ⇒ ổn)."""
    try:
        set_job_options(
            job_id, start_failures=0, start_fail_host="", start_fail_reason=""
        )
    except Exception:  # pragma: no cover - ledger hỏng
        pass


def _claim_slot_on_new_host(job: Job) -> None:
    """Xin slot cho host **MỚI** sau failover (job đã chạy ở đó).

    Job vừa failover không giữ slot nào ⇒ host mới có thể bị vượt quá số slot
    cho phép. Xin slot (best-effort): host mới đã đầy thì job **vẫn chạy**
    (không giết job đang chạy) — nhưng sổ slot sẽ phản ánh đúng hơn.
    """
    try:
        set_job_options(job.id, slot_host=job.host, slot_ticket="")
    except Exception:  # pragma: no cover - ledger hỏng
        return
    try:
        lease = _acquire_slot(job, job_options(job.id))
    except Exception:  # pragma: no cover - sổ hỏng
        return
    if lease is not None:
        try:
            set_job_options(job.id, slot_ticket=lease.ticket, slot_host=lease.host)
        except Exception:  # pragma: no cover - ledger hỏng
            pass


def _handle_start_failure(
    job: Job, job_id: str, host: str, retry_limit: int
) -> bool:
    """Xử lý `_start` thất bại nhiều lần liên tiếp trên cùng host.

    Sau :data:`MAX_START_FAILURES` lần liên tiếp:

    * lỗi **hạ tầng** (``input_push_failed``) + host **không ghim** + được
      phép retry ⇒ **failover** sang host khác;
    * còn lại (host ghim, hết lượt retry, ``--side-effects``, không còn host)
      ⇒ đánh dấu job ``lost`` với ``reason`` rõ ràng.

    Trả ``True`` nếu job đã kết thúc (``lost``) ⇒ watcher thoát.
    """
    try:
        options = job_options(job_id)
    except Exception:  # pragma: no cover - ledger hỏng
        options = {}
    reason = str(options.get("start_error") or "") or REASON_INPUT_PUSH_FAILED
    failures = _count_start_failure(job_id, host, reason)
    _record_event(
        job_id, "start_failed", host=host, reason=reason, failures=failures
    )
    if failures < MAX_START_FAILURES:
        return False

    pinned = _host_is_pinned(options, host)
    if reason == REASON_INPUT_PUSH_FAILED and not pinned:
        if should_retry(reason, _failover_count(job), retry_limit, job.needs_retry):
            _record_event(job_id, "input_push_failed", host=host, failures=failures)
            # Job **chưa chạy** ở host cũ ⇒ trả slot của host cũ (nếu không,
            # host cũ bị khoá slot tới khi job kết thúc).
            _release_slot(job_id)
            if _failover(job, reason):
                _claim_slot_on_new_host(job)
                _clear_start_failures(job_id)
                return False
            if job.reason and job.reason != reason:
                # Failover thất bại vì lý do riêng ⇒ giữ lý do đó.
                reason = job.reason
    elif pinned:
        _record_event(job_id, "input_push_failed_pinned", host=host, failures=failures)

    job.reason = reason
    if not _save_state(job, "lost"):
        _release_slot(job_id)
        return True
    _record_event(job_id, "lost", reason=reason, host=host)
    _release_slot(job_id)
    return True


def _workdir_suffix(old_workdir: str, old_root: str, project: str) -> str:
    """Hậu tố của workdir (``part-0``, ``part-2/shard-1``) so với gốc cũ.

    Phải **giữ** hậu tố này khi đổi host: bỏ đi thì các job của cùng một lô
    rơi chung vào 1 thư mục và đè lên nhau (mất dữ liệu).
    """
    if not old_workdir or not project:
        return ""
    base = ""
    if old_root:
        base = f"{str(old_root).rstrip('/')}/projects/{project}"
    else:
        # Không có layout_root trong ledger ⇒ dò ngược từ workdir cũ.
        marker = f"/projects/{project}"
        index = old_workdir.find(marker)
        if index == -1:
            return ""
        base = old_workdir[: index + len(marker)]
    if old_workdir == base:
        return ""
    prefix = base.rstrip("/") + "/"
    if old_workdir.startswith(prefix):
        return old_workdir[len(prefix):]
    return ""


def _failover(job: Job, reason: str) -> bool:
    """Chuyển job sang host khác: chọn host mới → push lại input → launch lại.

    Args:
        job: job đang mất.
        reason: reason đã xác minh (``host_restarted``/``host_wiped``/…).

    Returns:
        ``True`` nếu đã launch lại được, ``False`` nếu không còn host nào.
    """
    options = job_options(job.id)
    cfg = _config()
    try:
        profiles = cached_profiles(env_fleet_aliases(cfg), cfg)
    except Exception as exc:  # pragma: no cover - probe lỗi
        _record_event(job.id, "probe_failed", detail=str(exc))
        profiles = []
    old_host = job.host
    profiles = [p for p in profiles if getattr(p, "alias", "") != old_host]

    need = Need(
        tools=list(options.get("needs") or []),
        mem_mb=int(options.get("mem_mb") or job.mem_mb or 0),
        disk_mb=int(options.get("disk_mb") or 0),
        cpus=int(options.get("cpus") or job.cpus or 0),
        project=job.project,
    )
    choice = select(
        profiles,
        need,
        cfg,
        busy={},
        prefer_hosts=list(options.get("prefer_hosts") or []),
    )
    if choice is None:
        _record_event(job.id, "failover_failed", reason=reason, host=old_host)
        return False

    new_host = getattr(choice, "host", "") or ""
    profile = next(
        (p for p in profiles if getattr(p, "alias", "") == new_host), None
    )
    layout_root = getattr(profile, "layout_root", None) or _layout_root_of_job(
        job, options
    )
    if not layout_root:
        _record_event(
            job.id, "failover_failed", reason=reason, host=old_host,
            detail="host mới không có layout_root",
        )
        return False
    old_workdir = str(options.get("workdir") or "")
    # Layout root của host CŨ — phải chụp **trước** khi options bị ghi đè
    # (dùng để remap kho input: kho input nằm ngoài workdir — W3f).
    old_layout_root = str(options.get("layout_root") or "")
    old_inputs_root = shared_mod.shared_root(old_layout_root) if old_layout_root else ""
    new_inputs_root = shared_mod.shared_root(str(layout_root)) if layout_root else ""
    # Các key mà host MỚI chắc chắn có (input đã được đẩy lại ở bước dưới).
    known_keys: set[str] = set()
    primary_key = str(options.get("input_shared_key") or "")
    if primary_key:
        known_keys.add(primary_key)
    for source in options.get("input_sources") or []:
        if isinstance(source, dict):
            key = str(source.get("key") or "")
            if key:
                known_keys.add(key)
    workdir = old_workdir
    if layout_root and job.project:
        # Giữ hậu tố ``part-N``/``shard-M`` — bỏ đi thì các job của cùng lô
        # dùng chung 1 thư mục và đè lên nhau.
        suffix = _workdir_suffix(
            old_workdir, str(options.get("layout_root") or ""), job.project
        )
        workdir = f"{str(layout_root).rstrip('/')}/projects/{job.project}"
        if suffix:
            workdir = f"{workdir}/{suffix}"

    def _remap(path: str | None) -> str:
        """Chuyển đường dẫn tuyệt đối của host CŨ sang host MỚI.

        Mọi đường dẫn remote đều gốc ở ``workdir``; đổi host là đổi gốc.
        Ngoài ra, đường dẫn có thể nằm trong **kho input dùng chung** của host
        (``<layout_root>/inputs/<key>/…``) — input không nằm trong workdir
        nữa (W3f). Kho dùng chung có **cùng key trên mọi host** (key là
        sha256 của đường dẫn local) ⇒ chỉ cần đổi gốc layout root.

        Không remap được (ví dụ đường dẫn không nằm dưới workdir cũ, hoặc
        nằm trong kho input mà host MỚI không có) thì giữ nguyên và ghi log —
        tốt hơn là âm thầm trỏ sai.
        """
        text = str(path or "")
        if not text or not old_workdir:
            return text
        if text == old_workdir:
            return workdir
        if text.startswith(old_workdir.rstrip("/") + "/"):
            return workdir.rstrip("/") + text[len(old_workdir.rstrip("/")):]
        # (2) đường dẫn trong kho input của host CŨ → kho input của host MỚI.
        if (
            old_inputs_root
            and new_inputs_root
            and text.startswith(old_inputs_root.rstrip("/") + "/")
        ):
            rel = text[len(old_inputs_root.rstrip("/")) + 1:]
            key = rel.split("/", 1)[0]
            if key and key in known_keys:
                return f"{new_inputs_root.rstrip('/')}/{rel}"
            _record_event(
                job.id, "failover_path_not_remapped", path=text,
                old_workdir=old_workdir, new_workdir=workdir,
                reason="input_store_key_unknown",
            )
            return text
        _record_event(
            job.id, "failover_path_not_remapped", path=text,
            old_workdir=old_workdir, new_workdir=workdir,
        )
        return text

    # P6: host mới phải có toolchain — host thiếu ffmpeg/node thì job chết ngay
    # lúc launch (failover "thành công" nhưng job không chạy được).
    try:
        toolchain_mod.ensure_tools(
            new_host, profile, list(options.get("needs") or [])
        )
    except Exception as exc:
        _record_event(
            job.id, "failover_ensure_tools_failed", host=new_host, detail=str(exc)
        )
        return False

    # P5b: input phải được **dựng lại** trên host MỚI — input dạng path vào
    # kho dùng chung của host mới; input dạng git:/http: dựng lại từ đầu (URL
    # không nằm trong kho ⇒ nếu không dựng lại, ``$ROFF_IN`` trỏ tới path
    # không tồn tại trên host mới).
    specs = _input_specs(options)
    has_internet = bool(getattr(profile, "internet", False))
    try:
        prepared, primary_remote, primary_kind, env_extra = inputs_mod.prepare_inputs(
            specs,
            new_host,
            workdir,
            layout_root,
            has_internet=has_internet,
            allow_job_input=False,
        )
    except Exception as exc:
        if getattr(exc, "reason", "") == inputs_mod.REASON_NOT_RECREATABLE:
            # Input kiểu ``job:`` không dựng lại được (dữ liệu nằm trên host
            # của job nguồn) ⇒ failover **thất bại** (đừng launch với $ROFF_IN
            # sai).
            _record_event(
                job.id, "failover_input_unsupported", host=new_host, detail=str(exc)
            )
            job.reason = inputs_mod.REASON_NOT_RECREATABLE
            save_job(job)
            return False
        _record_event(
            job.id, "failover_push_failed", host=new_host, detail=str(exc)
        )
        return False

    if env_extra:
        try:
            saved = load_job_env(job.id)
            saved.update(env_extra)
            save_job_env(job.id, saved)
        except Exception:  # pragma: no cover
            pass

    options["input_remote"] = primary_remote
    options["input_kind"] = primary_kind
    options["host_internet"] = has_internet
    path_sources = [
        {key: value for key, value in entry.items() if key != "value"}
        for entry in prepared
        if entry.get("kind") == "path"
    ]
    if path_sources:
        options["input_sources"] = path_sources
        first = path_sources[0]
        options["input_dir"] = first.get("local") or ""
        options["input_local"] = first.get("local") or ""
        options["input_shared_key"] = first.get("key") or ""
        options["input_shared_rel"] = first.get("rel") or ""

    # Item của ``--each``: nằm trong input ⇒ dùng lại bản copy của input đó;
    # không nằm trong input nào ⇒ tự đẩy thành nguồn riêng của host mới.
    item_local = str(options.get("item_local") or "")
    if item_local:
        source_locals = [str(entry.get("local") or "") for entry in path_sources]
        try:
            remote, key, rel, inside = inputs_mod.resolve_item_path(
                new_host, item_local, layout_root, source_locals
            )
        except Exception as exc:
            _record_event(job.id, "failover_push_failed", host=new_host, detail=str(exc))
            return False
        job.item = remote
        options["item_shared_key"] = key
        options["item_shared_rel"] = rel
        options["item_inside_input"] = inside
    elif job.item:
        job.item = _remap(job.item)

    options["workdir"] = workdir
    if options.get("checkpoint"):
        # Checkpoint nằm trong workdir ⇒ đổi host ⇒ đổi path (W3n).
        options["checkpoint"] = f"{workdir.rstrip('/')}/.checkpoint"
    # Path `--sync-back` trên host ⇒ đổi host ⇒ đổi path theo gốc mới:
    # * nằm trong workdir ⇒ `<workdir mới>/…` (cwd của job vẫn là workdir);
    # * nằm trong kho input dùng chung ⇒ kho của host mới (cùng key).
    # Đích **local** (`sync_back_local`) **không** đổi (B26).
    options["sync_back_remote"] = [
        _remap(p) for p in (options.get("sync_back_remote") or [])
    ]
    if layout_root:
        options["layout_root"] = str(layout_root)
    # Ghi lại ledger TRƯỚC khi launch — launch() đọc options từ ledger để
    # dựng launch.sh (ROFF_IN, ROFF_ITEM…). Không ghi thì job chạy với đường
    # dẫn của host cũ.
    set_job_options(job.id, **options)

    job.host = new_host
    if layout_root:
        job.out_remote = f"{str(layout_root).rstrip('/')}/jobs/{job.id}/out"

    # Tạo thư mục làm việc trên host MỚI — launch() yêu cầu workdir có sẵn
    # (không có ⇒ exit 97 workdir_missing). Host mới thường chưa có thư mục
    # này (layout root của nó khác layout root cũ). Best-effort: nếu mkdir
    # lỗi thì launch tự báo workdir_missing (rõ hơn là im lặng).
    try:
        layout_mod.remote_mkdirs(new_host, workdir, run=_shell_runner)
        if job.out_remote:
            layout_mod.remote_mkdirs(
                new_host, os.path.dirname(str(job.out_remote)), run=_shell_runner
            )
        if options.get("checkpoint"):
            layout_mod.remote_mkdirs(
                new_host, str(options["checkpoint"]), run=_shell_runner
            )
    except Exception as exc:
        _record_event(job.id, "failover_workdir_failed", host=new_host, detail=str(exc))

    try:
        failover_env = load_job_env(job.id)
        if options.get("checkpoint"):
            failover_env["ROFF_CHECKPOINT"] = str(options["checkpoint"])
        launch(
            new_host,
            job,
            env=failover_env,
            cpus=job.cpus,
            mem_mb=job.mem_mb,
        )
    except Exception as exc:
        job.host = old_host
        _record_event(job.id, "failover_launch_failed", host=new_host, detail=str(exc))
        return False
    # Job được chạy lại ở máy khác ⇒ artifact của lần trước **không** còn
    # đúng (`out_remote` đổi theo layout root của host mới) ⇒ phải tính lại
    # trạng thái pull. Không để sót `pulled=false` của lần trước rồi báo
    # thành công (B23).
    # Tương tự: lỗi sync-back của lần trước **không** còn đúng (job sẽ tạo
    # lại file trên host mới) ⇒ xoá, đừng báo lỗi cũ (B26).
    try:
        set_job_options(
            job.id,
            pulled=None,
            pull_error="",
            manifest=[],
            sync_back_error="",
            # (B36/C10) Failover đã launch thật trên host mới ⇒ watcher
            # không được start lại lần nữa ở vòng poll kế tiếp.
            deferred_start=False,
        )
    except Exception:  # pragma: no cover - ledger hỏng
        pass

    job.state = "launched"
    save_job(job)
    _record_event(
        job.id,
        "failover",
        reason=reason,
        host=new_host,
        previous=old_host,
        reasons=list(getattr(choice, "reasons", []) or []),
    )
    return True


def _pull_once(job: Job) -> tuple[bool, str]:
    """Kéo artifact về local **1 lần**; trả ``(ok, chi tiết lỗi)``.

    Không ghi ``pulled``/``pull_error`` vào ledger ở đây — việc đó thuộc về
    :func:`_pull_artifacts` (chỉ ghi khi **thật sự** thành công, hoặc khi đã
    **thật sự** hỏng hẳn sau khi hết số lần thử).
    """
    try:
        result = pull(job.host, job.out_remote, job.out_local)
    except Exception as exc:
        return False, str(exc) or exc.__class__.__name__
    if result is None:
        return False, "pull không trả về kết quả"
    if getattr(result, "ok", False):
        files = list(getattr(result, "files", []) or [])
        set_job_options(
            job.id,
            manifest=files,
            pulled=True,
            pull_error="",
        )
        _record_event(
            job.id, "pulled",
            files=len(files),
            bytes=int(getattr(result, "bytes_transferred", 0) or 0),
        )
        return True, ""
    return False, str(getattr(result, "error", "") or "pull thất bại")


def _pull_artifacts(job: Job) -> bool:
    """Kéo artifact về local (khi job thành công) — **có thử lại** (B23).

    Pull hỏng **không** phải lỗi của job: reverse tunnel của fleet hay flap
    ⇒ artifact vẫn nằm trên host, kéo lại được. Vì vậy thử lại tối đa
    ``ROFF_PULL_RETRIES`` lần (mặc định 3), giãn cách
    ``ROFF_PULL_RETRY_DELAY_S`` giây (mặc định 5).

    Trả ``True`` nếu kéo xong (hoặc không có gì để kéo), ``False`` nếu **hỏng
    hẳn** (đã hết số lần thử). Khi hỏng hẳn:

    * ghi event ``pull_failed`` (kèm ``detail``);
    * ghi ``pulled=False`` + ``pull_error=<chi tiết>`` vào ledger;
    * **không** ném lỗi — job **remote** vẫn thành công (exit 0); người gọi
      (:func:`watch`) tự đặt ``reason="pull_failed"`` để ``roff wait`` báo lỗi
      thay vì báo thành công.
    """
    if not job.out_local or not job.out_remote:
        set_job_options(job.id, pulled=True, pull_error="")
        return True

    retries = _pull_retries()
    delay = _pull_retry_delay()
    detail = ""
    for attempt in range(1, retries + 1):
        ok, detail = _pull_once(job)
        if ok:
            return True
        if attempt < retries:
            _record_event(
                job.id,
                "pull_retry",
                attempt=attempt,
                retries=retries,
                delay_s=delay,
                detail=detail,
            )
            _sleep(delay)
    _record_event(job.id, "pull_failed", detail=detail, attempts=retries)
    set_job_options(job.id, pulled=False, pull_error=detail)
    return False


# --------------------------------------------------------------------------
# `--sync-back` — kéo path chỉ định về project local (B26)
# --------------------------------------------------------------------------
def _sync_back_pairs(options: dict) -> list[tuple[str, str]]:
    """Cặp ``(đường dẫn remote, đường dẫn local)`` cần kéo về (B26).

    Đọc từ ledger: ``sync_back_remote`` (đường dẫn trên host) và
    ``sync_back_local`` (đích local, **cùng thứ tự**). Job cũ (chưa có
    ``sync_back_local``) ⇒ **không** có cặp nào (không đoán bừa).
    """
    remotes = list(options.get("sync_back_remote") or [])
    locals_ = list(options.get("sync_back_local") or [])
    pairs: list[tuple[str, str]] = []
    for index, remote in enumerate(remotes):
        text = str(remote or "")
        if not text:
            continue
        local = str(locals_[index]) if index < len(locals_) else ""
        if not local:
            continue
        pairs.append((text, local))
    return pairs


def _sync_back_once(job: Job, remote: str, local: str) -> tuple[bool, str]:
    """Kéo **một** path ``--sync-back`` về local; trả ``(ok, chi tiết lỗi)``.

    Không ghi ledger ở đây — việc đó thuộc về :func:`_pull_sync_back`.
    """
    try:
        parent = os.path.dirname(local)
        if parent:
            os.makedirs(parent, exist_ok=True)
        result = pull_path(job.host, remote, local)
    except Exception as exc:
        return False, str(exc) or exc.__class__.__name__
    if result is None:
        return False, "pull_path không trả về kết quả"
    if getattr(result, "ok", False):
        return True, ""
    return False, str(getattr(result, "error", "") or "sync-back thất bại")


def _pull_sync_back(job: Job, options: dict) -> bool:
    """Kéo các path ``--sync-back`` về project local (B26).

    Không có gì để kéo ⇒ ``True`` (không phải lỗi).

    * thành công ⇒ event ``synced_back`` (kèm ``path`` = đường dẫn local) và
      **xoá** ``sync_back_error`` cũ (nếu có) trong ledger;
    * hỏng ⇒ **không im lặng** (bài học của B23): ghi ``sync_back_error``
      vào ledger + event ``sync_back_failed`` (kèm ``detail``). Người gọi
      (:func:`watch`) đặt ``reason="sync_back_failed"`` để ``roff wait`` báo
      lỗi (exit 74) thay vì báo thành công.
    """
    pairs = _sync_back_pairs(options)
    if not pairs:
        return True
    failures: list[str] = []
    for remote, local in pairs:
        ok, detail = _sync_back_once(job, remote, local)
        if ok:
            _record_event(job.id, "synced_back", path=local, remote=remote)
            continue
        failures.append(f"{local}: {detail}")
        _record_event(
            job.id, "sync_back_failed", path=local, remote=remote, detail=detail
        )
    if failures:
        set_job_options(job.id, sync_back_error="; ".join(failures))
        return False
    set_job_options(job.id, sync_back_error="")
    return True


# --------------------------------------------------------------------------
# vòng lặp giám sát
# --------------------------------------------------------------------------
def watch(job_id: str) -> None:
    """Vòng lặp giám sát 1 job (tiến trình **nền**, tách phiên).

    Mỗi vòng:

    1. đọc job từ ledger;
    2. nếu job còn ``queued`` → push input + launch;
    3. ``remote_state()`` (1 round-trip ssh);
    4. phân nhánh:
       * ``exited`` & exit==0 → pull artifact → ``exited`` → kết thúc;
       * ``exited`` & exit!=0 → ``exited`` → kết thúc (**KHÔNG retry**);
       * ``lost`` & được retry → failover (host mới, push lại, launch lại);
       * ``lost`` & không retry → ``lost`` → kết thúc;
       * không kết nối được → **chờ, thử lại, KHÔNG failover**;
       * còn chạy → ghi heartbeat, ngủ ``POLL_INTERVAL`` giây.

    Riêng verdict ``vanished`` có **2 lớp bảo vệ** (đo thật 24/11/2026: job
    chạy thành công, exit 0 — nhưng poll đọc trúng khoảng process vừa chết
    nên báo ``vanished``):

    * **P2 — khoảng miễn sau launch** (:data:`VANISH_GRACE_S`): trong khoảng
      này "không còn process" ⇒ **chưa chắc chắn** ⇒ ghi event ``unverified``,
      chờ thử lại, **không** failover;
    * **P1 — xác nhận lần 2** (:data:`VANISH_CONFIRM_DELAY_S`): ngoài khoảng
      miễn mà vẫn thấy ``vanished`` ⇒ hỏi lại host 1 lần nữa; lần 2 vẫn
      thấy vậy mới failover, lần 2 thấy ``exited`` ⇒ coi như ``exited``.

    Verdict mạnh hơn (``host_restarted`` / ``host_wiped`` / ``oom_killed``)
    **không** qua 2 lớp này ⇒ failover ngay.
    """
    try:
        options = job_options(job_id)
    except Exception:
        options = {}
    retry_limit = _retry_limit(options)
    deadline = time.monotonic() + _watch_timeout(options)

    # Ghi pid watcher vào ledger + ghi "chủ" cho ticket (W3a): reconcile cần
    # biết watcher còn sống không để thu hồi slot của job bị bỏ rơi.
    _register_watcher(job_id)

    polls = 0
    while True:
        try:
            job = load_job(job_id)
        except RoffError:
            return
        if job.state in TERMINAL_STATES:
            # Job đã kết thúc (thường do ``roff cancel``) ⇒ nhả slot rồi thoát.
            _release_slot(job_id)
            return

        polls += 1
        if polls % RECONCILE_EVERY == 0:
            # Dọn ticket/lease mồ côi (job chết / watcher chết) ⇒ host không
            # bị kẹt vĩnh viễn (W3a).
            _reconcile_slots(job_id)

        try:
            options = job_options(job_id)
        except Exception:  # pragma: no cover - ledger hỏng
            options = {}
        # (B36/C10) Job do `roff do` tạo mà **chưa** được launch: `do` chỉ
        # tạo job + xin slot, còn **đẩy input** + **launch** do watcher lo.
        # Dấu hiệu: option ``deferred_start`` còn bật **và** job chưa có
        # attempt nào (chưa từng launch) ⇒ watcher phải `_start()`.
        # (Sau khi launch thành công, `deferred_start` được tắt ⇒ không
        # launch lần nữa.)
        needs_start = job.state == "queued" or (
            bool(options.get("deferred_start")) and not job.attempts
        )
        if needs_start:
            # Job đang **xếp hàng** chờ slot. Không được launch khi chưa có
            # slot (sẽ vượt quá giới hạn của host) — phải xin slot trước.
            lease = _acquire_slot(job, options)
            if lease is None:
                # Chưa được slot ⇒ host đang bận. Đây **không phải lỗi**:
                # job vẫn ở state ``queued``, ghi heartbeat (để không bị coi
                # là mất) rồi thử lại vòng sau.
                job.heartbeat = time.time()
                if not _save_state(job, "queued"):
                    _release_slot(job_id)
                    return
                _touch_heartbeat(job_id)
                if time.monotonic() >= deadline:
                    _record_event(job_id, "watch_renewed", host=job.host)
                    spawn_watcher(job_id)
                    return
                _sleep(POLL_INTERVAL)
                continue
            set_job_options(job_id, slot_ticket=lease.ticket, slot_host=lease.host)
            if _start(job):
                # Đẩy được ⇒ ổn ⇒ xoá bộ đếm lỗi.
                _clear_start_failures(job_id)
                continue
            if job.state in TERMINAL_STATES:
                _release_slot(job_id)
                return
            # `_start` hỏng ⇒ đếm số lần liên tiếp trên cùng host; đủ N lần
            # ⇒ failover hoặc đánh dấu ``lost`` (KHÔNG quay vòng vô hạn — B17).
            if _handle_start_failure(job, job_id, job.host, retry_limit):
                return
            if time.monotonic() >= deadline:
                _record_event(job_id, "watch_renewed", host=job.host)
                spawn_watcher(job_id)
                return
            _sleep(POLL_INTERVAL)
            continue

        state = remote_state(job.host, job, timeout=REMOTE_TIMEOUT)

        if not state.get("reachable", False):
            # KHÔNG xác minh được gì → không failover, chỉ chờ rồi thử lại.
            _record_event(
                job_id,
                "disconnected",
                host=job.host,
                detail=state.get("detail"),
                error=state.get("error"),
            )
            job.heartbeat = time.time()
            if not _save_state(job, job.state):
                return
            _sleep(POLL_INTERVAL)
            continue

        kind = state.get("state")

        if kind == "exited":
            _finish_exited(job, job_id, state.get("exit"))
            return

        if kind == "lost":
            reason = _loss_reason(job, state)
            if reason == REASON_VANISHED:
                # --- P2: khoảng miễn ngay sau launch ------------------------
                # Job **vừa launch** có thể chưa hiện process trên host
                # (độ trễ ssh 4–6 s) ⇒ "không còn process" lúc này **chưa
                # chắc chắn** ⇒ ghi event ``unverified`` rồi chờ thử lại —
                # TUYỆT ĐỐI không failover.
                if _vanish_grace_remaining(job) > 0.0:
                    _record_event(
                        job_id,
                        "unverified",
                        host=job.host,
                        reason=REASON_VANISHED,
                        detail="vanished trong khoảng miễn sau launch",
                    )
                    job.heartbeat = time.time()
                    if not _save_state(job, job.state):
                        return
                    _sleep(POLL_INTERVAL)
                    continue
                # --- P1: hỏi lại host lần 2 trước khi kết luận mất ---------
                # ``vanished`` có thể **sai** khi đọc trúng khoảng process
                # vừa chết (``kill -0`` thất bại, file ``exit`` chưa kịp
                # ghi) ⇒ hỏi lại 1 lần nữa; lần 2 vẫn thấy vậy mới mất thật.
                again = _confirm_vanished(job, job_id, state)
                if not again.get("reachable", False):
                    # Hỏi lại **không được** ⇒ CHƯA CHẮC (quy tắc vàng:
                    # mất kết nối KHÔNG đồng nghĩa job chết).
                    reason = None
                elif again.get("state") == "exited":
                    # Job **đã xong** ⇒ coi như ``exited`` (kéo artifact, ghi
                    # ``finished``) — KHÔNG failover (job còn sống!).
                    _finish_exited(job, job_id, again.get("exit"))
                    return
                elif again.get("state") == "lost":
                    if again.get("pid_alive"):
                        # Không nhất quán (báo ``lost`` nhưng process **còn
                        # sống**) ⇒ chưa chắc chắn ⇒ chờ thử lại.
                        reason = None
                    else:
                        # Vẫn mất (lần 2 xác nhận) ⇒ lấy reason mới nhất.
                        reason = _loss_reason(job, again)
                else:
                    # Vẫn còn chạy ⇒ CHƯA CHẮC (verdict lần 1 là race).
                    reason = None
            if reason is None:
                # chưa chắc chắn → chờ, thử lại (quy tắc vàng)
                _record_event(job_id, "unverified", host=job.host)
                if not _save_state(job, job.state):
                    return
                _sleep(POLL_INTERVAL)
                continue
            if should_retry(
                reason, _failover_count(job), retry_limit, job.needs_retry
            ):
                if _failover(job, reason):
                    continue
                if job.reason and job.reason != reason:
                    # Failover thất bại vì lý do riêng (vd input không dựng lại
                    # được) ⇒ giữ lý do đó, đừng ghi đè bằng lý do ban đầu.
                    reason = job.reason
            job.reason = reason
            if not _save_state(job, "lost"):
                _release_slot(job_id)
                return
            _record_event(job_id, "lost", reason=reason, host=job.host)
            _release_slot(job_id)
            return

        # còn chạy
        # Dùng _mark_running: job có thể đã bị `roff cancel` trong lúc chờ
        # remote_state — không được ghi đè "cancelled" bằng "running".
        if not _mark_running(job):
            return
        _touch_heartbeat(job_id)
        if time.monotonic() >= deadline:
            # Job vẫn sống nhưng watcher hết thời gian theo dõi → nối lại.
            # Nếu không, job bị BỎ RƠI: không ai theo dõi, không ai kéo artifact,
            # `roff wait` chỉ còn đợi mãi.
            _record_event(job_id, "watch_renewed", host=job.host)
            spawn_watcher(job_id)
            return
        _sleep(POLL_INTERVAL)


# --------------------------------------------------------------------------
# spawn + wait
# --------------------------------------------------------------------------
def nice_prefix() -> list[str]:
    """Tiền tố ưu tiên CPU/I-O cho tiến trình nền (Q18/W3k).

    Watcher chạy **nền** trên máy local ⇒ không được giành CPU/I/O của người
    dùng (giống ssh/rsync mà skill vẫn dùng).

    **MỘT nguồn sự thật** (B34): gọi :func:`roffkit.config.local_nice_prefix`
    ⇒ cùng tiền tố với probe/sshx/tunnel/transfer/job. Trước B34 hàm này
    **hardcode** ``nice -n 19 ionice -c3`` ⇒ ``ROFF_NICE``/``[local] nice``
    không có tác dụng với watcher ⇒ **bản sao thứ 6** của chính sách.
    """
    return local_nice_prefix()


def spawn_watcher(job_id: str) -> None:
    """Chạy watcher ở chế độ nền, tách phiên (sống sót khi agent chết).

    ``nice -n 19 ionice -c3 setsid nohup python3 <skill>/roff.py __watch <job_id> </dev/null >/dev/null 2>&1 &``

    Pid của watcher được ghi vào ledger job (``options["watcher_pid"]``) để
    reconcile biết watcher còn sống hay không (W3a) — không có pid này thì
    không thể phân biệt "watcher chết" với "job đang chờ slot".
    """
    skill_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    launcher = os.path.join(skill_dir, "roff.py")
    if os.path.isfile(launcher):
        argv = [sys.executable, launcher, "__watch", job_id]
    else:
        argv = [
            sys.executable,
            "-c",
            "import sys;from roffkit.cli import main;sys.exit(main(['__watch', sys.argv[1]]))",
            job_id,
        ]
    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = skill_dir + (os.pathsep + existing if existing else "")

    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
        "env": env,
    }
    prefix = nice_prefix()
    if shutil.which("setsid"):
        cmd = prefix + ["setsid", "nohup"] + argv
    else:  # pragma: no cover - không có setsid
        cmd = prefix + argv
        kwargs["start_new_session"] = True
    try:
        proc = _popen(cmd, **kwargs)
    except Exception:  # pragma: no cover - không spawn được
        return
    # Ghi pid ngay (watcher tự ghi lại pid thật của nó khi bắt đầu — W3a).
    pid = int(getattr(proc, "pid", 0) or 0)
    if pid > 0:
        try:
            set_job_options(job_id, watcher_pid=pid)
        except Exception:  # pragma: no cover - ledger hỏng
            pass


def wait_for(job_id: str, timeout: float = 7200) -> Job:
    """Chờ tới trạng thái cuối — poll **LEDGER CỤC BỘ** (không ssh).

    Args:
        job_id: id job.
        timeout: giây chờ tối đa.

    Returns:
        :class:`Job` ở trạng thái cuối (``exited``/``lost``/``cancelled``).

    Raises:
        RoffError: ``EXIT_TIMEOUT`` (124) khi hết giờ — **không phải lỗi**,
            job vẫn đang chạy; ``EXIT_INPUT`` (2) khi ``timeout <= 0``.
    """
    seconds = validate_timeout(timeout)
    deadline = time.monotonic() + seconds
    while True:
        job = load_job(job_id)
        if job.state in TERMINAL_STATES:
            return job
        if time.monotonic() >= deadline:
            raise RoffError(
                EXIT_TIMEOUT,
                "timeout",
                job=job_id,
                state=job.state,
                host=job.host,
                ok=True,
            )
        _sleep(LOCAL_POLL_INTERVAL)
