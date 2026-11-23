"""Unit test cho ``roffkit.shared`` — kho input dùng chung trên host.

Bài toán (P1): băng thông local→host trên fleet Muse chỉ **0,5–1 MB/s** ⇒
đẩy cùng một dataset cho N host là N lần chờ. Bản cũ đẩy **toàn bộ** input
vào ``workdir`` riêng cho từng job ⇒ ``--each`` 10 item × 4 host = 10 lần
đẩy 10 GB.

Kho dùng chung: mỗi **(host, nguồn local)** chỉ đẩy **1 lần**, các job sau
dùng lại bản copy đó (marker ``size``/``mtime`` khớp ⇒ không truyền lại).

Các test ở đây dùng **runner giả** (không mạng thật): 1 lớp giả lập đúng
phần filesystem trên host mà ``shared`` dùng (marker + lock).
"""

from __future__ import annotations

import argparse
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
# runner giả: giả lập phần filesystem trên host mà ``shared`` dùng
# --------------------------------------------------------------------------
class FakeRemote:
    """Giả lập host: marker + lock + vài lệnh vặt.

    Không mô phỏng fs thật — chỉ những gì :mod:`roffkit.shared` cần:
    đọc/ghi marker (``cat``/``printf``), mutex (``mkdir``/``rmdir``/``rm -rf``),
    khoá read-only (``chmod``) và tuổi của lock (``stat``).
    """

    def __init__(self) -> None:
        self.files: dict[str, str] = {}
        self.locks: set[str] = set()
        self.scripts: list[str] = []
        self.lock_mtime = time.time()

    # --- seam cho sshx.run_shell ---
    def exec(self, script: str) -> tuple[int, str]:
        """Chạy 1 script trên host giả; trả ``(rc, stdout)``."""
        self.scripts.append(script)
        out = ""
        for part in script.split("&&"):
            rc, text = self._exec(part.strip())
            if rc != 0:
                return rc, text
            out = text or out
        return 0, out

    def run_shell(self, host: str, script: str, timeout: float = 60.0):
        """Seam cho :func:`roffkit.shared._run_shell` (trả bộ ba)."""
        rc, out = self.exec(script)
        return rc, out, ""

    def run_shell_result(self, host: str, script: str, timeout: float = 60.0):
        """Seam cho :func:`roffkit.sshx.run_shell` (trả ``RunResult``)."""
        from roffkit import sshx

        rc, out = self.exec(script)
        return sshx.RunResult(rc=rc, stdout=out, stderr="")

    def _exec(self, part: str) -> tuple[int, str]:
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
                return 1, ""
            self.locks.add(tokens[1])
            return 0, ""
        if head == "rmdir" and len(tokens) > 1:
            self.locks.discard(tokens[1])
            return 0, ""
        if head == "rm" and len(tokens) > 2:
            self.locks.discard(tokens[2])
            self.files.pop(tokens[2], None)
            return 0, ""
        if head == "stat" and len(tokens) > 3:
            return 0, str(int(self.lock_mtime))
        if head == "printf" and len(tokens) >= 5 and tokens[-2] == ">":
            self.files[tokens[-1]] = tokens[2]
            return 0, ""
        # chmod và mọi lệnh lạ ⇒ coi như thành công
        return 0, ""


@pytest.fixture
def remote(monkeypatch):
    """Runner giả cho mọi lệnh ssh của ``shared``."""
    fake = FakeRemote()
    monkeypatch.setattr(shared_mod, "_run_shell", fake.run_shell)
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

    def fake_push(host, local_dir, remote_dir, *args, **kwargs):
        calls.append((host, local_dir, remote_dir))
        return Result()

    monkeypatch.setattr(shared_mod.transfer_mod, "push", fake_push)
    return calls


# --------------------------------------------------------------------------
# shared_key — ổn định theo đường dẫn
# --------------------------------------------------------------------------
def test_shared_key_is_stable_for_same_path(tmp_path):
    """Cùng 1 đường dẫn ⇒ cùng 1 key (kể cả khi viết khác kiểu)."""
    target = tmp_path / "data"
    target.mkdir()
    assert shared_mod.shared_key(str(target)) == shared_mod.shared_key(str(target))
    monkey = os.getcwd()
    try:
        os.chdir(tmp_path)
        assert shared_mod.shared_key("data") == shared_mod.shared_key(str(target))
    finally:
        os.chdir(monkey)


def test_shared_key_differs_for_different_paths(tmp_path):
    """Đường dẫn khác ⇒ key khác (không đụng nhau trong kho)."""
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    assert shared_mod.shared_key(str(a)) != shared_mod.shared_key(str(b))


def test_shared_key_is_16_hex_chars(tmp_path):
    """Key là sha256[:16] (thư mục con của kho, không quá dài)."""
    key = shared_mod.shared_key(str(tmp_path))
    assert len(key) == 16
    assert all(ch in "0123456789abcdef" for ch in key)


def test_shared_paths_share_one_root(tmp_path):
    """Kho: ``{layout_root}/inputs/{key}`` (+ marker/lock nằm CẠNH)."""
    root = "/home/hatch/.roff"
    source = str(tmp_path / "src")
    key = shared_mod.shared_key(source)
    assert shared_mod.shared_root(root) == f"{root}/inputs"
    assert shared_mod.shared_dir(root, source) == f"{root}/inputs/{key}"
    assert shared_mod.shared_marker(root, source) == f"{root}/inputs/{key}.complete"
    assert shared_mod.shared_lock(root, source) == f"{root}/inputs/{key}.lock"


# --------------------------------------------------------------------------
# ensure_shared_input — đẩy 1 lần, dùng lại khi nguồn không đổi
# --------------------------------------------------------------------------
def test_ensure_shared_input_pushes_once_for_many_jobs(remote, pushes, tmp_path):
    """N job trên cùng host dùng chung 1 nguồn ⇒ **chỉ 1 lần đẩy** (P1)."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("x")

    root = "/home/hatch/.roff"
    paths = [
        shared_mod.ensure_shared_input("muse-a", str(src), root) for _ in range(3)
    ]

    assert len(pushes) == 1, f"phải đẩy đúng 1 lần, thấy {len(pushes)}"
    assert len(set(paths)) == 1, "mọi job phải trỏ cùng 1 bản copy"
    key = shared_mod.shared_key(str(src))
    assert paths[0] == f"{root}/inputs/{key}"


def test_ensure_shared_input_reuses_copy_when_source_unchanged(remote, pushes, tmp_path):
    """Marker khớp ⇒ không truyền lại (điều kiện quan trọng nhất)."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("x")

    root = "/home/hatch/.roff"
    first = shared_mod.ensure_shared_input("muse-a", str(src), root)
    second = shared_mod.ensure_shared_input("muse-a", str(src), root)

    assert first == second
    assert len(pushes) == 1


def test_ensure_shared_input_repushes_when_source_changed(remote, pushes, tmp_path):
    """Nguồn local đổi ⇒ dọn bản cũ rồi đẩy lại."""
    src = tmp_path / "src"
    src.mkdir()
    target = src / "a.txt"
    target.write_text("x")

    root = "/home/hatch/.roff"
    shared_mod.ensure_shared_input("muse-a", str(src), root)
    assert len(pushes) == 1

    # đổi nội dung + mtime ⇒ marker cũ không còn khớp
    target.write_text("noi-dung-moi")
    future = time.time() + 10
    os.utime(target, (future, future))

    again = shared_mod.ensure_shared_input("muse-a", str(src), root)

    assert len(pushes) == 2, "nguồn đổi thì phải đẩy lại"
    key = shared_mod.shared_key(str(src))
    assert again == f"{root}/inputs/{key}"


def test_ensure_shared_input_writes_marker_with_signature(remote, pushes, tmp_path):
    """Marker ghi source/kind/size/mtime/basename (để lần sau biết nguồn đổi chưa)."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("x")

    root = "/home/hatch/.roff"
    shared_mod.ensure_shared_input("muse-a", str(src), root)

    marker = shared_mod.shared_marker(root, str(src))
    assert marker in remote.files, "phải ghi marker"
    data = json.loads(remote.files[marker])
    assert data["source"] == str(src)
    assert data["kind"] == "dir"
    assert data["basename"] == "src"
    assert data["size"] > 0
    assert data["mtime"] > 0


def test_ensure_shared_input_file_goes_to_key_basename(remote, pushes, tmp_path):
    """Nguồn là **file** ⇒ bản copy nằm ở ``{key}/{basename}``."""
    src = tmp_path / "clip.mp4"
    src.write_text("data")

    root = "/home/hatch/.roff"
    path = shared_mod.ensure_shared_input("muse-a", str(src), root)

    key = shared_mod.shared_key(str(src))
    assert path == f"{root}/inputs/{key}/clip.mp4"
    assert len(pushes) == 1


def test_ensure_shared_input_locks_and_unlocks(remote, pushes, tmp_path):
    """Mutex (``mkdir``) phải được nhả sau khi xong — kẹt lock là chết cả kho."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("x")

    root = "/home/hatch/.roff"
    shared_mod.ensure_shared_input("muse-a", str(src), root)

    assert not remote.locks, f"lock phải được nhả: {remote.locks}"


def test_ensure_shared_input_makes_shared_copy_read_only(remote, pushes, tmp_path):
    """Cây input trong kho phải bị khoá read-only (job sửa input của job khác
    thì fail loudly, không hỏng dữ liệu âm thầm)."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("x")

    root = "/home/hatch/.roff"
    shared_mod.ensure_shared_input("muse-a", str(src), root)

    joined = " ".join(remote.scripts)
    assert "chmod -R a-w" in joined, f"phải khoá cây input: {remote.scripts}"


def test_ensure_shared_input_purges_old_copy_when_source_changed(remote, pushes, tmp_path):
    """Nguồn đổi ⇒ mở khoá quyền ghi rồi xoá bản cũ trước khi đẩy lại."""
    src = tmp_path / "src"
    src.mkdir()
    target = src / "a.txt"
    target.write_text("x")

    root = "/home/hatch/.roff"
    shared_mod.ensure_shared_input("muse-a", str(src), root)

    target.write_text("noi-dung-moi")
    future = time.time() + 10
    os.utime(target, (future, future))
    remote.scripts.clear()

    shared_mod.ensure_shared_input("muse-a", str(src), root)

    joined = " ".join(remote.scripts)
    assert "chmod -R u+w" in joined, f"phải mở khoá trước khi xoá: {remote.scripts}"
    assert "rm -rf" in joined, f"phải xoá bản copy cũ: {remote.scripts}"


def test_ensure_shared_input_takes_over_stale_lock(remote, pushes, tmp_path):
    """Lock quá 30 phút (agent bị kill) ⇒ dọn rồi chiếm lại, không kẹt vĩnh viễn."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("x")

    root = "/home/hatch/.roff"
    remote.locks.add(shared_mod.shared_lock(root, str(src)))
    remote.lock_mtime = time.time() - 3600

    path = shared_mod.ensure_shared_input("muse-a", str(src), root)

    assert len(pushes) == 1
    assert not remote.locks
    assert path == f"{root}/inputs/{shared_mod.shared_key(str(src))}"


def test_ensure_shared_input_waits_for_other_pusher(remote, pushes, tmp_path,
                                                     monkeypatch):
    """Có người khác đang đẩy (lock bị giữ, marker chưa có) ⇒ chờ marker, không
    đẩy trùng."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("x")

    root = "/home/hatch/.roff"
    remote.locks.add(shared_mod.shared_lock(root, str(src)))
    monkeypatch.setattr(shared_mod, "_sleep", lambda seconds: None)

    # Người khác "xong" sau vài lần poll ⇒ marker xuất hiện.
    marker_path = shared_mod.shared_marker(root, str(src))
    state = {"reads": 0}
    original = remote.run_shell

    def run_shell(host, script, timeout=60.0):
        state["reads"] += 1
        if "cat" in script and state["reads"] > 1 and marker_path not in remote.files:
            size, mtime = shared_mod._source_signature(str(src))
            remote.files[marker_path] = json.dumps(
                {
                    "source": str(src),
                    "kind": "dir",
                    "size": size,
                    "mtime": mtime,
                    "basename": "src",
                }
            )
        return original(host, script, timeout)

    monkeypatch.setattr(shared_mod, "_run_shell", run_shell)

    path = shared_mod.ensure_shared_input("muse-a", str(src), root)

    assert path == f"{root}/inputs/{shared_mod.shared_key(str(src))}"
    assert not pushes, "đã có marker ⇒ không được đẩy thêm"


def test_ensure_shared_input_missing_source_raises(remote, pushes, tmp_path):
    """Nguồn không tồn tại ⇒ lỗi input (exit 2), không đẩy gì."""
    from roffkit.errors import EXIT_INPUT

    with pytest.raises(Exception) as exc:
        shared_mod.ensure_shared_input("muse-a", str(tmp_path / "khong-co"), "/r")
    assert exc.value.code == EXIT_INPUT
    assert not pushes


# --------------------------------------------------------------------------
# End-to-end: ``roff do`` với 3 job trên cùng 1 host
# --------------------------------------------------------------------------
def test_do_three_jobs_same_host_push_input_only_once(monkeypatch, tmp_path):
    """P1 (end-to-end): 3 job cùng host, cùng 1 input dir ⇒ **1 lần đẩy**.

    Đây là bằng chứng cho bug gốc: bản cũ đẩy toàn bộ input dir vào
    ``workdir`` **riêng cho từng job** ⇒ 3 job = 3 lần đẩy.
    """
    from roffkit import cli as cli_mod
    from roffkit import job as job_mod
    from roffkit import probe as probe_mod
    from roffkit import sshx
    from roffkit import transfer as transfer_mod

    state = tmp_path / "state"
    state.mkdir()
    config = tmp_path / "config.ini"
    config.write_text(
        "[fleet]\ninclude = muse-*\n\n[host.muse-a]\nslots = 5\n"
    )
    monkeypatch.setenv("ROFF_STATE_DIR", str(state))
    monkeypatch.setenv("ROFF_CONFIG", str(config))
    monkeypatch.setenv("ROFF_NO_WATCHER", "1")
    monkeypatch.setenv("ROFF_SLOT_WAIT_S", "5")

    profile = probe_mod.Profile(
        alias="muse-a",
        reachable=True,
        cores=4,
        mem_available_mb=4096,
        disk_free_mb=100000,
        layout_root="/home/hatch/.roff",
        home="/root",
        boot_id="boot-1",
        instance_token="tok-1",
        internet=True,
    )
    monkeypatch.setattr(cli_mod, "_fleet_aliases", lambda cfg: ["muse-a"])
    monkeypatch.setattr(
        probe_mod, "cached_profiles", lambda aliases, cfg: [profile]
    )

    fake = FakeRemote()
    monkeypatch.setattr(sshx, "run_shell", fake.run_shell_result)

    pushed: list[tuple] = []

    class Result:
        ok = True
        bytes_transferred = 1
        files: list = []
        error = None

    def fake_push(host, local_dir, remote_dir, *args, **kwargs):
        pushed.append((host, local_dir, remote_dir))
        return Result()

    monkeypatch.setattr(transfer_mod, "push", fake_push)
    monkeypatch.setattr(
        job_mod,
        "_run_ssh",
        lambda *a, **k: (
            0,
            "ROFF_PID=1\nROFF_BOOT_ID=boot-1\nROFF_INSTANCE_TOKEN=tok-1\n",
            "",
        ),
    )

    vids = tmp_path / "vids"
    vids.mkdir()
    for name in ("a.mp4", "b.mp4", "c.mp4"):
        (vids / name).write_text(name)

    args = argparse.Namespace(
        cmd="ffmpeg -i $ROFF_ITEM $ROFF_OUT/out.mp4",
        inputs=[str(vids)],
        out=None,
        need=[],
        cpus=0,
        mem=None,
        disk=None,
        each=str(vids / "*.mp4"),
        shards=1,
        after=None,
        name="proj",
        sync_back=[],
        checkpoint=None,
        env=[],
        retry=1,
        side_effects=False,
        timeout=600,
        priority="normal",
        prefer_same_host=True,
        host=None,
    )

    payload, code = cli_mod._cmd_do(args)

    assert code == 0
    assert payload["count"] == 3
    assert len(pushed) == 1, f"3 job cùng host phải chỉ đẩy 1 lần: {pushed}"

    # Mọi job trỏ cùng 1 bản copy trong kho (không phải workdir riêng).
    key = shared_mod.shared_key(str(vids))
    for job_id in payload["jobs"]:
        data = json.loads((state / "jobs" / f"{job_id}.json").read_text())
        assert data["item"] == f"/home/hatch/.roff/inputs/{key}/{Path(data['item']).name}"
        assert data["item"].startswith("/home/hatch/.roff/inputs/")
