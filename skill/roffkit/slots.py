"""Sổ chiếm chỗ **dùng chung** cho mọi phiên / agent / harness (Q32).

Dữ liệu nằm ở local:

* ``~/.local/state/roff/slots.json`` — trạng thái (lease + hàng đợi)
* ``~/.local/state/roff/slots.lock`` — khoá ``fcntl.flock``

Mọi thao tác đọc/ghi trạng thái đều nằm trong khoá flock ⇒ nhiều tiến trình
(nhiều phiên agent chạy song song) không giẫm chân nhau.

Hàng đợi: FIFO theo thời điểm xin; ``priority="high"`` chen đầu hàng.
Hết timeout ⇒ ``RoffError(EXIT_NO_HOST, "no slot on <host>")``.

Module này **không in ra stdout**.
"""

from __future__ import annotations

import errno
import json
import os
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

try:  # phần A (errors.py) có thể chưa sẵn sàng -> fallback tối thiểu
    from roffkit.errors import EXIT_NO_HOST, RoffError  # type: ignore
except ImportError:  # pragma: no cover - chỉ xảy ra khi chạy lẻ module này
    EXIT_NO_HOST = 10

    class RoffError(Exception):  # type: ignore[no-redef]
        """Bản sao tối thiểu của ``roffkit.errors.RoffError``."""

        def __init__(self, code: int, reason: str = "", **fields: Any):
            super().__init__(reason or f"exit {code}")
            self.code = code
            self.reason = reason
            self.fields = fields

        def payload(self) -> dict:
            out: dict[str, Any] = {"ok": False, "exit_code": self.code}
            if self.reason:
                out["reason"] = self.reason
            out.update(self.fields)
            return out


try:
    from roffkit import perm  # type: ignore
except ImportError:  # pragma: no cover - chạy module lẻ
    import types

    perm = types.SimpleNamespace(  # type: ignore[assignment]
        secure_dir=lambda path: None,
        secure_file=lambda path: None,
    )


__all__ = [
    "Lease",
    "SlotLedger",
    "EXIT_NO_HOST",
    "RoffError",
    "INTERNAL_OWNER",
    "DEFAULT_MAX_AGE_S",
    "reconcile_slots",
    "pid_alive",
    "proc_start_time",
]


try:
    import fcntl  # POSIX
except ImportError:  # pragma: no cover - Windows không được hỗ trợ
    fcntl = None  # type: ignore[assignment]


#: Chu kỳ nghỉ giữa 2 lần thử lấy khoá (giây).
POLL_INTERVAL = 0.02

#: Trần chờ khoá cho các thao tác không chặn (release / đọc trạng thái).
LOCK_TIMEOUT_S = 30.0

#: Owner nội bộ — ticket không thuộc job nào nên **luôn** coi là còn sống.
INTERNAL_OWNER = "__internal__"

#: Tuổi tối đa của ticket/lease (giây) — quá hạn ⇒ thu hồi (24 h).
#:
#: Đủ dài để 1 job chờ slot cả ngày không bị dọn oan.
DEFAULT_MAX_AGE_S = 24 * 3600.0


@dataclass
class Lease:
    """Phiếu chiếm 1 slot trên 1 host."""

    host: str
    ticket: str
    acquired_at: float
    owner: str = ""
    """Job sở hữu ticket (rỗng nếu chưa biết)."""
    holder_pid: int = 0
    """Pid của tiến trình đang **giữ** slot (watcher); 0 = chưa biết."""
    holder_started: float = 0.0
    """Starttime của tiến trình giữ slot (chống pid bị tái sử dụng)."""


# --------------------------------------------------------------------------
# tiến trình giữ slot
# --------------------------------------------------------------------------
def proc_start_time(pid: int) -> float:
    """Starttime của tiến trình (giây) — đọc ``/proc/<pid>/stat`` field 22.

    Trả ``0.0`` nếu không đọc được (tiến trình đã chết / không có ``/proc``).
    """
    if not pid or pid <= 0:
        return 0.0
    try:
        with open(f"/proc/{int(pid)}/stat", "r", encoding="utf-8", errors="replace") as fh:
            data = fh.read()
    except (OSError, ValueError):
        return 0.0
    # ``comm`` có thể chứa khoảng trắng ⇒ cắt sau dấu ')' cuối cùng.
    index = data.rfind(")")
    if index < 0:
        return 0.0
    fields = data[index + 1:].split()
    # fields[0] = field 3 (state) ⇒ field 22 (starttime) = fields[19]
    if len(fields) < 20:
        return 0.0
    try:
        ticks = int(fields[19])
    except ValueError:
        return 0.0
    try:
        hz = os.sysconf("SC_CLK_TCK")
    except (AttributeError, ValueError, OSError):  # pragma: no cover
        hz = 100
    return float(ticks) / float(hz or 100)


def pid_alive(pid: int, started: float | None = None) -> bool:
    """Tiến trình ``pid`` còn sống không.

    Nếu có ``started`` (starttime) thì còn đối chiếu: pid sống nhưng là pid
    **khác** (tái sử dụng) ⇒ coi như đã chết.
    """
    try:
        number = int(pid)
    except (TypeError, ValueError):
        return False
    if number <= 0:
        return False
    try:
        os.kill(number, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - pid của user khác
        return True
    except OSError:  # pragma: no cover
        return False
    if started:
        current = proc_start_time(number)
        if current and abs(current - float(started)) > 0.01:
            # pid đã bị tái sử dụng cho tiến trình khác.
            return False
    return True


def default_state_dir() -> str:
    """Thư mục state mặc định: ``~/.local/state/roff`` (ghi đè bằng $ROFF_STATE_DIR)."""
    env = os.environ.get("ROFF_STATE_DIR")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return os.path.join(os.path.expanduser("~"), ".local", "state", "roff")


def _empty_state() -> dict:
    return {"version": 1, "hosts": {}, "leases": {}, "queue": []}


class SlotLedger:
    """Sổ chiếm chỗ dùng chung, an toàn với nhiều tiến trình.

    Args:
        state_dir: thư mục state; mặc định ``~/.local/state/roff``.
            Sổ truyền dữ liệu (Q37) dùng thư mục riêng, ví dụ ``<state>/transfers``.
    """

    def __init__(self, state_dir: str | None = None):
        self.state_dir = os.path.abspath(os.path.expanduser(state_dir or default_state_dir()))
        self.state_path = os.path.join(self.state_dir, "slots.json")
        self.lock_path = os.path.join(self.state_dir, "slots.lock")
        try:
            os.makedirs(self.state_dir, exist_ok=True)
        except OSError:  # pragma: no cover
            pass

    # ------------------------------------------------------------------
    # khoá
    # ------------------------------------------------------------------
    @contextmanager
    def _locked(self, deadline: float | None = None) -> Iterator[None]:
        """Giữ khoá flock độc quyền trên ``slots.lock`` trong khi `yield`.

        ``deadline`` = mốc ``time.monotonic()``; nếu quá hạn thì ném lỗi.
        """
        if fcntl is None:  # pragma: no cover
            raise RoffError(EXIT_NO_HOST, "flock không khả dụng trên nền tảng này")
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN):
                        raise
                    if deadline is not None and time.monotonic() >= deadline:
                        raise TimeoutError("hết giờ chờ khoá slot")
                    time.sleep(POLL_INTERVAL)
            try:
                yield
            finally:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:  # pragma: no cover
                    pass
        finally:
            os.close(fd)

    # ------------------------------------------------------------------
    # đọc / ghi trạng thái
    # ------------------------------------------------------------------
    def _read_state(self) -> dict:
        try:
            with open(self.state_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return _empty_state()
        if not isinstance(data, dict):
            return _empty_state()
        state = _empty_state()
        state["version"] = data.get("version", 1)
        hosts = data.get("hosts")
        leases = data.get("leases")
        queue = data.get("queue")
        state["hosts"] = hosts if isinstance(hosts, dict) else {}
        state["leases"] = leases if isinstance(leases, dict) else {}
        state["queue"] = [q for q in queue if isinstance(q, dict)] if isinstance(queue, list) else []
        return state

    def _write_state(self, state: dict) -> None:
        os.makedirs(self.state_dir, exist_ok=True)
        perm.secure_dir(self.state_dir)
        tmp = f"{self.state_path}.tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(state, fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            perm.secure_file(tmp)
            os.replace(tmp, self.state_path)
            perm.secure_file(self.state_path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    # ------------------------------------------------------------------
    # trợ giúp trên trạng thái
    # ------------------------------------------------------------------
    @staticmethod
    def _used_for(state: dict, host: str) -> int:
        return sum(
            1 for lease in state["leases"].values()
            if isinstance(lease, dict) and lease.get("host") == host
        )

    @staticmethod
    def _capacity(state: dict, host: str) -> int | None:
        """Số slot tối đa đã biết của host; ``None`` nếu chưa biết."""
        entry = state["hosts"].get(host)
        if isinstance(entry, dict):
            try:
                return int(entry.get("capacity", 1))
            except (TypeError, ValueError):
                return 1
        return None

    @staticmethod
    def _queue_index(state: dict, ticket: str) -> int:
        for idx, entry in enumerate(state["queue"]):
            if entry.get("ticket") == ticket:
                return idx
        return -1

    def _first_for_host(self, state: dict, host: str) -> str | None:
        """Ticket của request đầu hàng (theo thứ tự hàng đợi) của host."""
        for entry in state["queue"]:
            if entry.get("host") == host:
                return entry.get("ticket")
        return None

    # ------------------------------------------------------------------
    # API công khai
    # ------------------------------------------------------------------
    def request_slot(
        self,
        host: str,
        slots: int,
        priority: str = "normal",
        owner: str = "",
    ) -> str:
        """Đăng ký vào hàng đợi của ``host`` — **vĩnh viễn**, trả ``ticket``.

        Hàm này **không chờ** và **không chiếm slot**: nó chỉ ghi 1 yêu cầu
        vào hàng đợi (FIFO). Dùng :meth:`try_acquire` để thử chiếm slot khi
        tới lượt.

        Ticket giữ nguyên vị trí trong hàng đợi cho tới khi được cấp slot
        (:meth:`try_acquire`) hoặc bị huỷ (:meth:`cancel_slot`) — nhờ vậy mỗi
        vòng poll của watcher **không** tạo ticket mới ở đuôi hàng đợi (nếu
        không, thứ tự chờ bị phá).

        Args:
            host: alias host.
            slots: số slot tối đa của host (thường lấy ``cfg.host(alias).slots``).
            priority: ``"high"`` chen đầu hàng, ``"normal"`` (mặc định) FIFO.
            owner: id của job sở hữu ticket (rỗng nếu chưa biết). Nhờ
                ``owner`` mà ticket mồ côi (job chết trước khi watcher chạy)
                nhận ra và dọn được — nếu không, ticket mồ côi ở đầu hàng làm
                **kẹt vĩnh viễn** mọi job sau của host đó.

        Returns:
            ``ticket`` (chuỗi) dùng cho :meth:`try_acquire` / :meth:`cancel_slot`.
        """
        ticket = f"{host}-{uuid.uuid4().hex[:16]}"
        requested_at = time.time()
        priority = (priority or "normal").lower()

        with self._locked(time.monotonic() + LOCK_TIMEOUT_S):
            state = self._read_state()
            try:
                wanted = int(slots)
            except (TypeError, ValueError):
                wanted = 1
            wanted = max(1, wanted)
            # **Config là nguồn chuẩn**: sửa `~/.config/roff/config.ini`
            # (slots = 1 → 2) phải có tác dụng ngay, không cần xoá sổ slot.
            # Nhưng không được xuống dưới số lease **đang giữ** (job đang
            # chạy không được rơi slot ngoài ý muốn).
            used = self._used_for(state, host)
            capacity = max(wanted, used)
            state["hosts"][host] = {"capacity": capacity}
            self._enqueue(state, host, ticket, priority, requested_at, owner)
            self._write_state(state)
        return ticket

    def try_acquire(
        self,
        ticket: str,
        owner: str = "",
        holder_pid: int = 0,
        holder_started: float = 0.0,
    ) -> Lease | None:
        """Thử chiếm slot cho ``ticket`` — không chờ, không xoá ticket khi thất bại.

        Chỉ ticket **đầu hàng** của host mới được cấp slot (FIFO, giống logic
        ``head == ticket`` cũ). Nếu host còn capacity nhưng ticket chưa tới
        lượt ⇒ trả ``None`` và **giữ nguyên** ticket trong hàng đợi.

        Args:
            ticket: ticket cần cấp slot.
            owner: job sở hữu ticket; rỗng ⇒ **kế thừa** từ entry trong hàng
                đợi (watcher đã ghi owner lúc xin ticket).
            holder_pid: pid tiến trình giữ slot; 0 ⇒ kế thừa từ entry.
            holder_started: starttime của tiến trình giữ slot.
        """
        if not ticket:
            return None
        with self._locked(time.monotonic() + LOCK_TIMEOUT_S):
            state = self._read_state()
            idx = self._queue_index(state, ticket)
            if idx < 0:
                # ticket không còn trong hàng đợi (bị xoá / state bị dọn)
                return None
            entry = state["queue"][idx]
            host = str(entry.get("host") or "")
            if not host:
                return None
            capacity = self._capacity(state, host)
            if capacity is None:
                capacity = 1
            used = self._used_for(state, host)
            head = self._first_for_host(state, host)
            # chỉ request đầu hàng của host mới được cấp slot (FIFO)
            if head != ticket or used >= capacity:
                return None
            acquired_at = time.time()
            state["queue"].pop(idx)
            state["leases"][ticket] = {
                "host": host,
                "ticket": ticket,
                "acquired_at": acquired_at,
                "owner": str(owner or entry.get("owner") or ""),
                "holder_pid": int(holder_pid or entry.get("holder_pid") or 0),
                "holder_started": float(
                    holder_started or entry.get("holder_started") or 0.0
                ),
            }
            self._write_state(state)
            return Lease(
                host=host,
                ticket=ticket,
                acquired_at=acquired_at,
                owner=str(owner or entry.get("owner") or ""),
                holder_pid=int(holder_pid or entry.get("holder_pid") or 0),
                holder_started=float(
                    holder_started or entry.get("holder_started") or 0.0
                ),
            )

    def set_holder(self, ticket: str, pid: int, started: float | None = None) -> bool:
        """Ghi lại tiến trình đang **giữ** ticket (watcher).

        Áp dụng cho cả ticket **đang xếp hàng** lẫn lease đã cấp slot: nhờ vậy
        reconcile biết ticket/lease nào đã mồ côi (watcher chết) mà không cần
        đoán.

        Args:
            ticket: ticket cần ghi holder.
            pid: pid của tiến trình giữ (watcher).
            started: starttime của tiến trình (chống pid tái sử dụng); ``None``
                ⇒ tự đọc ``/proc/<pid>/stat``.

        Returns:
            ``True`` nếu ticket/lease tồn tại và đã được ghi holder.
        """
        if not ticket:
            return False
        try:
            number = int(pid)
        except (TypeError, ValueError):
            return False
        if number <= 0:
            return False
        if started is None:
            started = proc_start_time(number)
        try:
            with self._locked(time.monotonic() + LOCK_TIMEOUT_S):
                state = self._read_state()
                lease = state["leases"].get(ticket)
                if isinstance(lease, dict):
                    lease["holder_pid"] = number
                    lease["holder_started"] = float(started or 0.0)
                    self._write_state(state)
                    return True
                idx = self._queue_index(state, ticket)
                if idx >= 0:
                    entry = state["queue"][idx]
                    entry["holder_pid"] = number
                    entry["holder_started"] = float(started or 0.0)
                    self._write_state(state)
                    return True
        except (TimeoutError, OSError):  # pragma: no cover - khoá bị kẹt
            return False
        return False

    def cancel_slot(self, ticket: str) -> None:
        """Bỏ ``ticket`` khỏi hàng đợi (job bị huỷ / hết hạn / thất bại).

        Idempotent. Nếu ticket đã được cấp slot thì lease cũng được trả lại
        (tránh rò slot ⇒ fleet bị khoá vĩnh viễn).
        """
        if not ticket:
            return
        try:
            with self._locked(time.monotonic() + LOCK_TIMEOUT_S):
                state = self._read_state()
                idx = self._queue_index(state, ticket)
                changed = False
                if idx >= 0:
                    state["queue"].pop(idx)
                    changed = True
                if ticket in state["leases"]:
                    state["leases"].pop(ticket, None)
                    changed = True
                if changed:
                    self._write_state(state)
        except (TimeoutError, OSError):  # pragma: no cover - khoá bị kẹt
            pass

    def lease_for(self, ticket: str) -> Lease | None:
        """Lease đang giữ slot cho ``ticket``; ``None`` nếu ticket chưa có slot.

        Dùng để biết "ticket đã được cấp slot rồi" (nó không còn nằm trong
        hàng đợi) — tránh đăng ký ticket mới và làm rò slot.
        """
        if not ticket:
            return None
        try:
            with self._locked(time.monotonic() + LOCK_TIMEOUT_S):
                state = self._read_state()
                lease = state["leases"].get(ticket)
        except (TimeoutError, OSError):  # pragma: no cover
            return None
        if not isinstance(lease, dict):
            return None
        return Lease(
            host=str(lease.get("host") or ""),
            ticket=ticket,
            acquired_at=float(lease.get("acquired_at") or 0.0),
        )

    def acquire(
        self,
        host: str,
        slots: int,
        timeout: float,
        priority: str = "normal",
    ) -> Lease:
        """Chờ tới khi host còn slot trống rồi chiếm 1 slot.

        Args:
            host: alias host.
            slots: số slot tối đa của host (thường lấy ``cfg.host(alias).slots``).
            timeout: số giây chờ tối đa. ``0`` = thử 1 lần rồi bỏ.
            priority: ``"high"`` chen đầu hàng, ``"normal"`` (mặc định) FIFO.

        Returns:
            ``Lease`` khi chiếm được.

        Raises:
            RoffError: ``EXIT_NO_HOST`` khi hết giờ chờ.
        """
        deadline = time.monotonic() + max(0.0, float(timeout))
        # Ticket của ``acquire`` không thuộc job nào ⇒ owner nội bộ (luôn sống).
        ticket = self.request_slot(host, slots, priority, owner=INTERNAL_OWNER)
        requested_at = time.time()
        priority = (priority or "normal").lower()

        try:
            while True:
                lease = self.try_acquire(ticket)
                if lease is not None:
                    return lease
                if self._queue_index_of(ticket) < 0:
                    # mất entry (ví dụ bị xoá) -> đăng ký lại (giữ ticket)
                    self._requeue(ticket, host, priority, requested_at, INTERNAL_OWNER)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(POLL_INTERVAL * 5, remaining))
        except TimeoutError:
            self._forget(ticket)
            raise RoffError(
                EXIT_NO_HOST,
                f"no slot on {host}",
                host=host,
                timeout=float(timeout),
            )

        # hết giờ chờ
        self._forget(ticket)
        raise RoffError(
            EXIT_NO_HOST,
            f"no slot on {host}",
            host=host,
            timeout=float(timeout),
        )

    @staticmethod
    def _enqueue(
        state: dict,
        host: str,
        ticket: str,
        priority: str,
        requested_at: float,
        owner: str = "",
    ) -> None:
        """Thêm request vào hàng đợi (``high`` chen đầu, còn lại FIFO)."""
        entry = {
            "ticket": ticket,
            "host": host,
            "priority": priority,
            "requested_at": requested_at,
            "owner": str(owner or ""),
            "holder_pid": 0,
            "holder_started": 0.0,
        }
        if priority == "high":
            state["queue"].insert(0, entry)
        else:
            state["queue"].append(entry)

    def release(self, lease: Lease) -> None:
        """Trả slot đã chiếm (idempotent)."""
        if lease is None:
            return
        ticket = getattr(lease, "ticket", None)
        if not ticket:
            return
        try:
            with self._locked(time.monotonic() + LOCK_TIMEOUT_S):
                state = self._read_state()
                state["leases"].pop(ticket, None)
                idx = self._queue_index(state, ticket)
                if idx >= 0:
                    state["queue"].pop(idx)
                self._write_state(state)
        except TimeoutError:  # pragma: no cover - khoá bị kẹt
            pass

    def counts(self) -> dict[str, int]:
        """Số slot đang dùng trên mỗi host."""
        try:
            with self._locked(time.monotonic() + LOCK_TIMEOUT_S):
                state = self._read_state()
        except TimeoutError:  # pragma: no cover
            return {}
        out: dict[str, int] = {}
        for lease in state["leases"].values():
            if isinstance(lease, dict):
                host = lease.get("host", "?")
                out[host] = out.get(host, 0) + 1
        return out

    def queue_counts(self) -> dict[str, int]:
        """Số request **đang xếp hàng** trên mỗi host."""
        try:
            with self._locked(time.monotonic() + LOCK_TIMEOUT_S):
                state = self._read_state()
        except TimeoutError:  # pragma: no cover
            return {}
        out: dict[str, int] = {}
        for entry in state["queue"]:
            if isinstance(entry, dict):
                host = str(entry.get("host") or "?")
                out[host] = out.get(host, 0) + 1
        return out

    def busy_counts(self) -> dict[str, int]:
        """Số job **đang bận** trên mỗi host = lease + đang xếp hàng.

        Điểm "đang bận" (−30 mỗi job) phải phản ánh cả hàng đợi: nếu không,
        host ít tải được chọn lại nhiều lần hơn cần.
        """
        out = self.counts()
        for host, count in self.queue_counts().items():
            out[host] = out.get(host, 0) + int(count)
        return out

    def reclaim_orphans(
        self,
        alive_owners: Any = None,
        max_age_s: float = DEFAULT_MAX_AGE_S,
        exclude_owners: Any = None,
        keep_leases: Any = None,
    ) -> list[dict]:
        """Thu hồi ticket/lease **mồ côi** (chủ đã chết hoặc quá hạn).

        Luật:

        * **ticket đang xếp hàng**: bỏ nếu tiến trình giữ nó đã chết, hoặc
          ``owner`` không còn sống (job không có trong ledger / đã terminal), hoặc
          quá hạn (``max_age_s``, mặc định 24 h);
        * **lease đã cấp slot**: bỏ nếu tiến trình giữ nó đã chết (watcher chết
          ⇒ không ai trả slot ⇒ host kẹt vĩnh viễn), hoặc quá hạn. Lease
          **không có** holder (bản cũ) ⇒ chỉ thu hồi bằng TTL.

        Args:
            alive_owners: tập owner (job id) còn sống. ``None`` ⇒ **không**
                dọn theo owner (chỉ dọn theo holder/TTL) — an toàn khi
                không biết ledger.
            max_age_s: ngưỡng quá hạn (giây).
            exclude_owners: owner không được đụng tới (vd job đang chạy
                reconcile).
            keep_leases: ticket được giữ **dù** mọi luật (vd lease của job có
                watcher còn sống).

        Returns:
            list dict: ``{"ticket", "host", "owner", "reason"}`` với ``reason``
            ∈ ``"holder_dead"`` | ``"owner_dead"`` | ``"expired"``.
        """
        skip = {str(item) for item in (exclude_owners or [])}
        keep = {str(item) for item in (keep_leases or [])}
        alive: set[str] | None = None
        if alive_owners is not None:
            alive = {str(item) for item in alive_owners}
            alive.add(INTERNAL_OWNER)

        now = time.time()
        removed: list[dict] = []
        try:
            with self._locked(time.monotonic() + LOCK_TIMEOUT_S):
                state = self._read_state()
                changed = False

                # --- lease đã cấp slot -------------------------------------
                for ticket, lease in list(state["leases"].items()):
                    if not isinstance(lease, dict):
                        state["leases"].pop(ticket, None)
                        changed = True
                        continue
                    if ticket in keep:
                        continue
                    owner = str(lease.get("owner") or "")
                    if owner in skip:
                        continue
                    holder_pid = int(lease.get("holder_pid") or 0)
                    holder_started = float(lease.get("holder_started") or 0.0)
                    acquired_at = float(lease.get("acquired_at") or 0.0)
                    reason = ""
                    if holder_pid:
                        if not pid_alive(holder_pid, holder_started):
                            reason = "holder_dead"
                    elif now - acquired_at > max_age_s:
                        reason = "expired"
                    if not reason:
                        continue
                    state["leases"].pop(ticket, None)
                    changed = True
                    removed.append(
                        {
                            "ticket": ticket,
                            "host": str(lease.get("host") or ""),
                            "owner": owner,
                            "reason": reason,
                        }
                    )

                # --- ticket đang xếp hàng ----------------------------------
                keep_queue: list[dict] = []
                for entry in state["queue"]:
                    if not isinstance(entry, dict):
                        changed = True
                        continue
                    ticket = str(entry.get("ticket") or "")
                    owner = str(entry.get("owner") or "")
                    requested_at = float(entry.get("requested_at") or 0.0)
                    holder_pid = int(entry.get("holder_pid") or 0)
                    holder_started = float(entry.get("holder_started") or 0.0)
                    if owner in skip or ticket in keep:
                        keep_queue.append(entry)
                        continue
                    reason = ""
                    if holder_pid and not pid_alive(holder_pid, holder_started):
                        reason = "holder_dead"
                    elif alive is not None and owner not in alive:
                        reason = "owner_dead"
                    elif now - requested_at > max_age_s:
                        reason = "expired"
                    if reason:
                        changed = True
                        removed.append(
                            {
                                "ticket": ticket,
                                "host": str(entry.get("host") or ""),
                                "owner": owner,
                                "reason": reason,
                            }
                        )
                        continue
                    keep_queue.append(entry)
                if len(keep_queue) != len(state["queue"]):
                    state["queue"] = keep_queue
                    changed = True

                if changed:
                    self._write_state(state)
                    self._promote_heads(state)
        except (TimeoutError, OSError):  # pragma: no cover - khoá bị kẹt
            return removed
        return removed

    def _promote_heads(self, state: dict) -> None:
        """Cấp slot cho đầu hàng của mỗi host vừa được giải phóng.

        Slot vừa trả mà đầu hàng còn chờ ⇒ trao ngay (job không phải đợi tới
        vòng poll kế tiếp của watcher).
        """
        for host in list(state["hosts"].keys()):
            capacity = self._capacity(state, host)
            if capacity is None:
                capacity = 1
            if self._used_for(state, host) >= capacity:
                continue
            head = self._first_for_host(state, host)
            if not head:
                continue
            idx = self._queue_index(state, head)
            if idx < 0:
                continue
            entry = state["queue"][idx]
            acquired_at = time.time()
            state["queue"].pop(idx)
            state["leases"][head] = {
                "host": host,
                "ticket": head,
                "acquired_at": acquired_at,
                "owner": str(entry.get("owner") or ""),
                "holder_pid": int(entry.get("holder_pid") or 0),
                "holder_started": float(entry.get("holder_started") or 0.0),
            }
            self._write_state(state)

    def queue_length(self) -> int:
        """Số request đang chờ trong hàng đợi (mọi host)."""
        try:
            with self._locked(time.monotonic() + LOCK_TIMEOUT_S):
                state = self._read_state()
        except TimeoutError:  # pragma: no cover
            return 0
        return len(state["queue"])

    def position(self, ticket: str) -> int:
        """Vị trí của ``ticket`` trong hàng đợi (1-based); ``0`` nếu không có."""
        try:
            with self._locked(time.monotonic() + LOCK_TIMEOUT_S):
                state = self._read_state()
        except TimeoutError:  # pragma: no cover
            return 0
        idx = self._queue_index(state, ticket)
        return idx + 1 if idx >= 0 else 0

    # ------------------------------------------------------------------
    # nội bộ
    # ------------------------------------------------------------------
    def _queue_index_of(self, ticket: str) -> int:
        """Vị trí của ``ticket`` trong hàng đợi; ``-1`` nếu không có."""
        try:
            with self._locked(time.monotonic() + LOCK_TIMEOUT_S):
                state = self._read_state()
                return self._queue_index(state, ticket)
        except (TimeoutError, OSError):  # pragma: no cover
            return -1

    def _requeue(
        self,
        ticket: str,
        host: str,
        priority: str,
        requested_at: float,
        owner: str = "",
    ) -> None:
        """Đăng ký lại ``ticket`` vào hàng đợi (giữ nguyên mã ticket)."""
        try:
            with self._locked(time.monotonic() + LOCK_TIMEOUT_S):
                state = self._read_state()
                if self._queue_index(state, ticket) < 0:
                    self._enqueue(state, host, ticket, priority, requested_at, owner)
                    self._write_state(state)
        except (TimeoutError, OSError):  # pragma: no cover
            pass

    def _forget(self, ticket: str) -> None:
        """Gỡ request khỏi hàng đợi (dùng khi hết giờ chờ)."""
        try:
            with self._locked(time.monotonic() + LOCK_TIMEOUT_S):
                state = self._read_state()
                idx = self._queue_index(state, ticket)
                if idx >= 0:
                    state["queue"].pop(idx)
                    self._write_state(state)
        except (TimeoutError, OSError):  # pragma: no cover
            pass


# --------------------------------------------------------------------------
# reconcile — đối chiếu sổ slot với ledger job
# --------------------------------------------------------------------------
def reconcile_slots(
    state_dir: str | None = None,
    max_age_s: float = DEFAULT_MAX_AGE_S,
    exclude_owners: Any = None,
    mark_lost: bool = True,
) -> dict:
    """Dọn ticket/lease mồ côi của sổ slot (W3a).

    Nguồn sự thật: **ledger job local** (:func:`roffkit.job.list_jobs`).

    * ``owner`` còn sống = job **tồn tại** và state **không** terminal;
    * lease mà tiến trình giữ (watcher) đã chết ⇒ thu hồi slot **và** đánh
      dấu job chủ là ``lost`` với reason ``watcher_died`` (exit 69) — roff đã
      mất khả năng theo dõi job đó nên giữ slot là vô nghĩa;
    * lease của job **có** watcher còn sống ⇒ **không** đụng tới;
    * ticket quá hạn (``max_age_s``, mặc định 24 h) ⇒ thu hồi.

    Idempotent: gọi nhiều lần cho cùng một trạng thái ⇒ kết quả giống nhau.

    Returns:
        ``{"removed": [...], "lost_jobs": [...]}``.
    """
    try:
        from . import job as job_mod  # import cục bộ ⇒ tránh import vòng
    except ImportError:  # pragma: no cover - chạy module lẻ
        job_mod = None  # type: ignore[assignment]

    # ``alive = None`` ⇒ **không biết** owner nào còn sống ⇒ KHÔNG được dọn
    # theo owner (nếu coi là "không ai sống" thì mọi ticket đang xếp hàng bị
    # dọn oan ⇒ job xếp hàng chết âm thầm).
    alive: set[str] | None = set()
    jobs: dict[str, Any] = {}
    keep: set[str] = set()
    if job_mod is not None:
        try:
            # Ledger phải đọc **cùng** state dir với sổ slot — nếu không,
            # reconcile đọc ledger thật của người dùng và dọn oan ticket
            # trong sổ slot thử nghiệm (P4).
            for job in job_mod.list_jobs(state_dir=state_dir):
                jobs[job.id] = job
                if job.state in job_mod.TERMINAL_STATES:
                    continue
                alive.add(job.id)
                options = job_mod.job_options(job.id, state_dir=state_dir)
                pid = int(options.get("watcher_pid") or 0)
                started = float(options.get("watcher_started") or 0.0)
                if pid and pid_alive(pid, started):
                    # Watcher của job này còn sống ⇒ giữ lease của nó.
                    ticket = str(options.get("slot_ticket") or "")
                    if ticket:
                        keep.add(ticket)
        except Exception:  # pragma: no cover - ledger hỏng
            alive = None

    ledger = SlotLedger(state_dir=state_dir)
    removed = ledger.reclaim_orphans(
        alive,
        max_age_s=max_age_s,
        exclude_owners=exclude_owners,
        keep_leases=keep,
    )

    lost: list[str] = []
    if mark_lost and job_mod is not None:
        for entry in removed:
            if entry.get("reason") != "holder_dead":
                continue
            owner = str(entry.get("owner") or "")
            job = jobs.get(owner)
            if job is None or job.state in job_mod.TERMINAL_STATES:
                continue
            job.state = "lost"
            job.reason = "watcher_died"
            try:
                job_mod.save_job(job, state_dir=state_dir)
            except Exception:  # pragma: no cover
                continue
            lost.append(owner)
    return {"removed": removed, "lost_jobs": lost}
