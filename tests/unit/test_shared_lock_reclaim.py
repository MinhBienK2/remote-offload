"""B39 — lần thử lại phải **chiếm lại được lock của chính mình**.

Sự cố đo thật trên fleet Muse 24/11/2026 (job ``261124-002345-4c9b``)::

    00:24:23  push_failed | shared_push_failed   ← đẩy hỏng (ssh flap)
    00:25:32  push_failed | shared_input_busy    ← chờ đủ 60 s rồi bỏ
    00:26:42  push_failed | shared_input_busy    ← lại chờ đủ 60 s
    00:26:42  input_push_failed ⇒ failover sang máy khác

⇒ **~2 phút 20 giây lãng phí**, rồi failover để **đẩy lại một file vốn đã
có** trên host.

Nguyên nhân: lần đẩy đầu hỏng ⇒ ``finally`` gọi ``_safe_release_lock()``,
nhưng lệnh ``rm -rf`` trên host **cũng hỏng** (cùng đường ssh đang chết) ⇒
**lock của chính ta còn lại** trên host. Lần thử lại **trong cùng tiến
trình** (watcher retry) thấy "chủ lock còn sống" — mà chủ lock **chính là tiến
trình đang retry** ⇒ không dọn ⇒ chờ hết ``MARKER_WAIT_S`` (60 s) ⇒
``shared_input_busy`` ⇒ lặp lại.

Luật sửa (xem :func:`roffkit.shared._own_leftover_lock`): lock mà **chính
tiến trình này** để lại ⇒ **chiếm lại ngay**; lock của tiến trình **khác**
còn sống ⇒ **không** được cướp (giữ nguyên hành vi cũ).

Test ở đây dùng **runner giả** (không mạng thật): seam ``_run_shell``.
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

from roffkit import shared as shared_mod  # noqa: E402


# --------------------------------------------------------------------------
# runner giả: giả lập phần filesystem trên host mà ``shared`` cần
# --------------------------------------------------------------------------
class FakeHost:
    """Host giả: marker + lock + owner + tuổi lock.

    Mô phỏng đúng những gì :mod:`roffkit.shared` dùng: ``mkdir`` (chiếm
    lock, fail nếu lock đã có), ``printf >`` (ghi marker/owner), ``cat``
    (đọc marker/owner), ``stat -c %Y`` (tuổi lock), ``test -e`` /
    ``test ! -e`` (lock còn hay không) và ``rm -rf`` (nhả/dọn lock).
    """

    def __init__(self) -> None:
        self.files: dict[str, str] = {}
        self.locks: set[str] = set()
        self.scripts: list[str] = []
        self.lock_mtime = time.time()
        #: ``rm -rf`` luôn hỏng ⇒ mô phỏng "host flap lúc nhả lock" ⇒ lock
        #: sót lại trên host (đúng nguyên nhân B39).
        self.fail_rm = False

    # --- seam cho shared._run_shell --------------------------------------
    def run_shell(self, host: str, script: str, timeout: float = 60.0):
        self.scripts.append(script)
        rc, out = self._exec(script)
        return rc, out, ""

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
        if head == "cat" and len(tokens) > 1:
            return 0, self.files.get(tokens[1], "")
        if head == "mkdir" and "-p" in tokens[:2]:
            return 0, ""
        if head == "mkdir" and len(tokens) > 1:
            if tokens[1] in self.locks:
                return 1, ""          # lock đã tồn tại ⇒ mkdir fail (atomic)
            self.locks.add(tokens[1])
            return 0, ""
        if head == "rm" and len(tokens) > 2:
            if self.fail_rm:
                return 255, ""        # host flap ⇒ lệnh ``rm`` không chạy
            self.locks.discard(tokens[2])
            self.files.pop(tokens[2], None)
            self.files.pop(tokens[2] + "/owner", None)
            return 0, ""
        if head == "stat" and len(tokens) > 3:
            path = tokens[3]
            if path in self.locks or path in self.files:
                return 0, str(int(self.lock_mtime))
            return 1, ""
        if head == "test" and len(tokens) > 2:
            path = tokens[-1]
            exists = path in self.locks or path in self.files
            if tokens[1] == "!":
                return (0, "") if not exists else (1, "")
            if tokens[1] == "-e":
                return (0, "") if exists else (1, "")
            return 0, ""               # ``test -d`` ⇒ coi như tồn tại
        if head == "printf" and len(tokens) >= 5 and tokens[-2] == ">":
            self.files[tokens[-1]] = tokens[2]
            return 0, ""
        # chmod / noop / lệnh lạ ⇒ coi như thành công
        return 0, ""


@pytest.fixture
def host(monkeypatch):
    """Runner giả cho mọi lệnh ssh của ``shared``."""
    fake = FakeHost()
    monkeypatch.setattr(shared_mod, "_run_shell", fake.run_shell)
    return fake


@pytest.fixture
def sleeps(monkeypatch):
    """Ghi lại mọi lần ``_sleep`` (chờ) — không ngủ thật.

    Trả về list các giây đã chờ ⇒ test đo được "có chờ 60 s không" mà
    không phải chờ thật.
    """
    waited: list[float] = []
    monkeypatch.setattr(shared_mod, "_sleep", lambda seconds: waited.append(seconds))
    return waited


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
    """Nguồn local (thư mục) để đẩy."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "clip.mp4").write_text("video")
    return src


def _self_owner():
    """Owner payload **do chính tiến trình này cấp**.

    Dùng :func:`roffkit.shared._owner_payload` (chứ không tự chế) vì luật
    nhận diện "lock sót của ta" đòi **token do chính tiến trình này cấp**
    (xem :func:`roffkit.shared._owner_token_is_ours`) — token tự chế ra thì
    không chứng minh được gì.
    """
    payload = shared_mod._owner_payload()
    assert payload["pid"] == os.getpid()
    assert payload["host"] == shared_mod._local_host()
    return payload


def _seed_leftover_lock(host, root, src, owner, age_s=0.0):
    """Gieo 1 lock còn lại trên host (kèm file ``owner``)."""
    lock = shared_mod.shared_lock(root, str(src))
    host.locks.add(lock)
    host.lock_mtime = time.time() - age_s
    host.files[lock + "/owner"] = json.dumps(owner)
    return lock


def waits_too_long(waited) -> bool:
    """Có chờ quá lâu không (một nhịp poll hay hơn)?"""
    return [seconds for seconds in waited if seconds >= 1.0]


# --------------------------------------------------------------------------
# 1) lock sót của CHÍNH mình ⇒ chiếm lại ngay, không chờ 60 s
# --------------------------------------------------------------------------
def test_own_leftover_lock_is_reclaimed_without_waiting(host, pushes, sleeps, tmp_path):
    """Lock do **chính tiến trình này** để lại ⇒ phải chiếm lại **ngay**.

    Trước B39: ``_lock_is_stale()`` thấy "chủ lock còn sống" (đúng tiến
    trình đang retry) ⇒ không dọn ⇒ chờ đủ ``MARKER_WAIT_S`` (60 s) ⇒
    ``shared_input_busy``.
    """
    src = _source(tmp_path)
    root = "/home/hatch/.roff"
    lock = _seed_leftover_lock(host, root, src, _self_owner())

    started = time.monotonic()
    path = shared_mod.ensure_shared_input("muse-a", str(src), root)
    elapsed = time.monotonic() - started

    assert path == f"{root}/inputs/{shared_mod.shared_key(str(src))}"
    assert len(pushes) == 1, "phải đẩy được ngay (lock của ta ⇒ chiếm lại)"
    assert not waits_too_long(sleeps), f"không được chờ: {sleeps}"
    assert elapsed < 5.0, f"không được chờ 60 s: {elapsed:.1f}s"
    assert not host.locks, f"lock phải được nhả sau khi xong: {host.locks}"
    assert lock not in host.locks


def test_retry_after_failed_push_reclaims_own_leftover_lock(
    host, pushes, sleeps, tmp_path, monkeypatch
):
    """Kịch bản **thật**: đẩy hỏng ⇒ lock sót ⇒ lần thử lại phải đẩy được.

    Đây là đúng sự cố đo được 24/11/2026: lần đẩy đầu hỏng (ssh flap) ⇒
    lệnh ``rm -rf`` nhả lock **cũng hỏng** ⇒ lock của ta còn lại ⇒ lần thử
    lại trong cùng tiến trình phải **chiếm lại ngay**, không chờ 60 s.
    """
    src = _source(tmp_path)
    root = "/home/hatch/.roff"
    lock_path = shared_mod.shared_lock(root, str(src))
    attempts: list[str] = []

    # Lần 1: đẩy hỏng + nhả lock hỏng (host flap) ⇒ lock sót lại.
    def failing_push(*args, **kwargs):
        attempts.append("fail")

        class Failed:
            ok = False
            bytes_transferred = 0
            files: list = []
            error = "ssh: đứt khi nửa chừng"

        return Failed()

    monkeypatch.setattr(shared_mod.transfer_mod, "push", failing_push)
    host.fail_rm = True          # lệnh ``rm -rf`` trên host cũng hỏng
    with pytest.raises(Exception) as exc:
        shared_mod.ensure_shared_input("muse-a", str(src), root)
    assert exc.value.reason == "shared_push_failed"
    assert lock_path in host.locks, "lock phải còn lại (nhả lock hỏng)"
    owner_left = json.loads(host.files[lock_path + "/owner"])
    assert owner_left["pid"] == os.getpid(), "lock sót phải là của ta"

    # Lần 2 (cùng tiến trình): host đã sống lại ⇒ phải chiếm lại lock.
    host.fail_rm = False
    sleeps.clear()               # chỉ đo thời gian chờ của lần 2

    class Ok:
        ok = True
        bytes_transferred = 1
        files: list = []
        error = None

    def ok_push(*args, **kwargs):
        attempts.append("ok")
        return Ok()

    monkeypatch.setattr(shared_mod.transfer_mod, "push", ok_push)
    started = time.monotonic()
    path = shared_mod.ensure_shared_input("muse-a", str(src), root)
    elapsed = time.monotonic() - started

    assert path == f"{root}/inputs/{shared_mod.shared_key(str(src))}"
    assert attempts == ["fail", "ok"], f"phải đẩy lại ngay: {attempts}"
    assert elapsed < 5.0, f"không được chờ 60 s: {elapsed:.1f}s"
    assert not waits_too_long(sleeps), f"không được chờ: {sleeps}"
    assert not host.locks, "lock phải được nhả sau khi xong"
    marker = shared_mod.shared_marker(root, str(src))
    assert marker in host.files, "phải ghi marker cho lần sau"


# --------------------------------------------------------------------------
# 2) lock của tiến trình KHÁC còn sống ⇒ không được cướp
# --------------------------------------------------------------------------
def test_live_lock_of_other_process_is_not_stolen(host, pushes, sleeps, tmp_path,
                                                 monkeypatch):
    """Lock của tiến trình **khác** (còn sống) ⇒ **không** cướp.

    Giữ nguyên hành vi cũ: chờ hết ``MARKER_WAIT_S`` rồi báo
    ``shared_input_busy`` — không đẩy trùng, không xoá lock của người khác.
    """
    src = _source(tmp_path)
    root = "/home/hatch/.roff"
    other_pid = os.getppid()          # tiến trình khác, còn sống
    assert other_pid != os.getpid()
    owner = {
        "id": "token-cua-tien-trinh-khac",
        "pid": other_pid,
        "host": shared_mod._local_host(),
        "started": time.time(),
    }
    lock = _seed_leftover_lock(host, root, src, owner)

    monkeypatch.setattr(shared_mod, "MARKER_WAIT_S", 0.05)

    with pytest.raises(Exception) as exc:
        shared_mod.ensure_shared_input("muse-a", str(src), root)

    assert exc.value.reason == "shared_input_busy"
    assert lock in host.locks, "lock của tiến trình khác không được xoá"
    assert not pushes, "không được đẩy trùng"


def test_lock_of_other_machine_with_same_pid_is_not_stolen(
    host, pushes, sleeps, tmp_path, monkeypatch
):
    """Cùng ``pid`` nhưng **khác máy** ⇒ không phải của ta ⇒ không cướp.

    ``pid`` chỉ có ý nghĩa **trong 1 máy** (mỗi máy đều có pid 1234) ⇒ nếu
    chỉ so ``pid`` thì sẽ cướp nhầm lock của tiến trình khác.
    """
    src = _source(tmp_path)
    root = "/home/hatch/.roff"
    owner = _self_owner()
    owner["host"] = "may-khac"          # token là của ta, nhưng máy thì khác
    lock = _seed_leftover_lock(host, root, src, owner)

    monkeypatch.setattr(shared_mod, "MARKER_WAIT_S", 0.05)

    with pytest.raises(Exception) as exc:
        shared_mod.ensure_shared_input("muse-a", str(src), root)

    assert exc.value.reason == "shared_input_busy"
    assert lock in host.locks, "lock của máy khác không được xoá"
    assert not pushes, "không được đẩy trùng"


def test_lock_we_are_currently_holding_is_not_stolen(
    host, pushes, sleeps, tmp_path, monkeypatch
):
    """Lock **chính ta đang giữ** (luồng khác cùng process) ⇒ không cướp.

    Cướp lock đang được luồng khác dùng ⇒ 2 luồng đẩy cùng 1 nguồn ⇒ ghi
    đè lên nhau ⇒ hỏng dữ liệu âm thầm.
    """
    src = _source(tmp_path)
    root = "/home/hatch/.roff"
    owner = _self_owner()
    lock = _seed_leftover_lock(host, root, src, owner)
    shared_mod._HELD_LOCKS[("muse-a", lock)] = str(owner["id"])
    try:
        monkeypatch.setattr(shared_mod, "MARKER_WAIT_S", 0.05)
        with pytest.raises(Exception) as exc:
            shared_mod.ensure_shared_input("muse-a", str(src), root)
    finally:
        shared_mod._HELD_LOCKS.pop(("muse-a", lock), None)

    assert exc.value.reason == "shared_input_busy"
    assert lock in host.locks, "lock đang được giữ không được xoá"
    assert not pushes, "không được đẩy trùng"


# --------------------------------------------------------------------------
# 3) hành vi cũ phải giữ nguyên (B22/B40 + điều kiện quan trọng nhất)
# --------------------------------------------------------------------------
def test_orphan_lock_of_dead_owner_is_still_cleaned(host, pushes, sleeps, tmp_path):
    """Lock **mồ côi** (chủ đã chết / không có owner) ⇒ vẫn được dọn.

    B22/B40: dọn lock mồ côi trong 1 lần chờ (không kẹt vĩnh viễn).
    """
    src = _source(tmp_path)
    root = "/home/hatch/.roff"
    lock = shared_mod.shared_lock(root, str(src))
    host.locks.add(lock)
    host.lock_mtime = time.time() - 45.0     # tuổi > LOCK_STALE_S (30 s)

    path = shared_mod.ensure_shared_input("muse-a", str(src), root)

    assert len(pushes) == 1, "lock mồ côi phải được dọn rồi đẩy"
    assert not host.locks
    assert path == f"{root}/inputs/{shared_mod.shared_key(str(src))}"


def test_other_pusher_copy_is_reused_without_second_push(
    host, pushes, sleeps, tmp_path, monkeypatch
):
    """Người khác đang đẩy cùng nguồn ⇒ chờ marker rồi **dùng lại** bản copy.

    Đây là điều kiện **quan trọng nhất** của kho dùng chung: không đẩy 2 lần.
    """
    src = _source(tmp_path)
    root = "/home/hatch/.roff"
    lock = shared_mod.shared_lock(root, str(src))
    host.locks.add(lock)
    marker_path = shared_mod.shared_marker(root, str(src))
    size, mtime = shared_mod._source_signature(str(src))
    state = {"reads": 0}
    original = host.run_shell

    def run_shell(host_name, script, timeout=60.0):
        state["reads"] += 1
        if "cat" in script and state["reads"] > 1 and marker_path not in host.files:
            # Người khác "xong" ⇒ marker xuất hiện.
            host.files[marker_path] = json.dumps(
                {
                    "source": str(src),
                    "kind": "dir",
                    "size": size,
                    "mtime": mtime,
                    "basename": "src",
                }
            )
        return original(host_name, script, timeout)

    monkeypatch.setattr(shared_mod, "_run_shell", run_shell)

    path = shared_mod.ensure_shared_input("muse-a", str(src), root)

    assert path == f"{root}/inputs/{shared_mod.shared_key(str(src))}"
    assert not pushes, "đã có marker ⇒ không được đẩy thêm"
