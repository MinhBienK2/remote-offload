"""Unit test cho ``roffkit.transfer``.

Không cần mạng thật: các seam ``_run_ssh`` / ``_run_cmd`` / ``_popen`` được
monkeypatch để trả về kết quả giả.

Nguyên tắc được kiểm tra (đo thật trên fleet — DESIGN.md §3.3):
  * rsync **luôn** có ``--partial`` (resume), ``--no-owner --no-group`` (bug R4);
  * mọi lệnh ssh/rsync chạy ``nice -n 19 ionice -c3``;
  * **không nén** file đã nén (mp4/mov/mkv/jpg/zip/gz…);
  * ``stream_between`` đi qua PIPE, **không ghi đĩa local** (Q43);
  * ``pull`` trả manifest sha256 của các file **mới** mang về;
  * trần số luồng truyền đồng thời toàn máy (Q37).
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill"))

from roffkit import transfer  # noqa: E402
from roffkit.transfer import (  # noqa: E402
    DEFAULT_EXCLUDES,
    INCOMPRESSIBLE_SUFFIXES,
    TransferResult,
    pull,
    pull_path,
    push,
    stream_between,
    transfer_slot,
)


# --------------------------------------------------------------------------
# giả lập
# --------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def fake_ssh_probe(monkeypatch):
    """Chặn mọi ssh thật (dò rsync) — test không được chạm mạng.

    Mặc định host "có rsync"; test cần trường hợp khác thì tự monkeypatch
    ``transfer._run_ssh`` (đè lên fixture này).
    """
    monkeypatch.setattr(transfer, "_RSYNC_CACHE", {})
    monkeypatch.setattr(
        transfer, "_run_ssh", lambda *a, **k: (0, "__RSYNC__\n", "")
    )


class FakeCmd:
    """Một lệnh con đã được gọi (giả)."""

    def __init__(self, cmd, timeout=None, stdin=None):
        self.cmd = list(cmd)
        self.timeout = timeout
        self.stdin = stdin

    @property
    def joined(self) -> str:
        return " ".join(self.cmd)


class FakeRunner:
    """Giả lập ``_run_cmd`` / ``_run_ssh``: khớp theo substring trong lệnh."""

    def __init__(self, rules=None, default=(0, "", "")):
        self.rules = list(rules or [])
        self.default = default
        self.calls: list[FakeCmd] = []

    def __call__(self, *args, **kwargs):
        if len(args) >= 2 and isinstance(args[0], str):
            alias, script = args[0], args[1]
            timeout = args[2] if len(args) > 2 else kwargs.get("timeout")
            stdin = kwargs.get("stdin")
            joined = f"{alias} {script}"
        else:
            cmd = args[0]
            timeout = args[1] if len(args) > 1 else kwargs.get("timeout")
            stdin = kwargs.get("stdin")
            joined = " ".join(cmd)
        self.calls.append(FakeCmd([joined], timeout, stdin))
        for needle, resp in self.rules:
            if needle in joined:
                return resp
        return self.default

    def joined_calls(self) -> list[str]:
        return [c.joined for c in self.calls]


class FakePipe:
    """Pipe giả cho ``_popen``."""

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True

    def fileno(self):
        return 0


class FakePopen:
    """Tiến trình con giả (dùng cho pipeline)."""

    def __init__(self, cmd, **kwargs):
        self.cmd = list(cmd)
        self.kwargs = kwargs
        self.args = list(cmd)
        self.stdout = FakePipe()
        self.returncode = 0
        self.killed = False

    def communicate(self, input=None, timeout=None):
        return (b"", b"")

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


def _all_files(root: Path, skip: str = "") -> set[str]:
    """Tập file (đường dẫn tương đối) bên trong ``root`` — dùng để chứng minh
    stream_between không ghi gì xuống đĩa local (``skip`` = thư mục state)."""
    out: set[str] = set()
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            rel = os.path.relpath(os.path.join(dirpath, name), root)
            if skip and rel.startswith(skip):
                continue
            out.add(rel)
    return out


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Cô lập: không đụng config/state thật của máy người dùng."""
    monkeypatch.setenv("ROFF_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ROFF_CONFIG", str(tmp_path / "none.ini"))
    monkeypatch.setattr(
        transfer, "_prefix_nice", lambda: ["nice", "-n", "19", "ionice", "-c3"]
    )


# --------------------------------------------------------------------------
# hằng số
# --------------------------------------------------------------------------
def test_default_excludes_cover_heavy_dirs():
    for name in (".git", "node_modules", ".venv", "__pycache__"):
        assert name in DEFAULT_EXCLUDES


def test_incompressible_suffixes_cover_media_and_archives():
    for suffix in (".mp4", ".mov", ".mkv", ".png", ".jpg", ".zip", ".gz"):
        assert suffix in INCOMPRESSIBLE_SUFFIXES


def test_prefix_nice_is_nice19_ionice_idle():
    parts = transfer._prefix_nice()
    assert parts[:3] == ["nice", "-n", "19"]
    assert "ionice" in parts
    assert "-c3" in parts


# --------------------------------------------------------------------------
# push
# --------------------------------------------------------------------------
def test_push_uses_rsync_no_owner_no_group_partial(tmp_path, monkeypatch):
    src = tmp_path / "proj"
    src.mkdir()
    (src / "a.txt").write_text("hello")
    runner = FakeRunner()
    monkeypatch.setattr(transfer, "_run_cmd", runner)

    res = push("host1", str(src), "/remote/proj")

    assert isinstance(res, TransferResult)
    assert res.ok is True
    assert len(runner.calls) == 1
    cmd = runner.calls[0].cmd[0]
    assert "rsync" in cmd
    for flag in ("-a", "--no-owner", "--no-group", "--partial"):
        assert flag in cmd, f"thiếu {flag} trong: {cmd}"
    assert cmd.startswith("nice -n 19 ionice -c3")


def test_push_compresses_text_only_tree(tmp_path, monkeypatch):
    src = tmp_path / "proj"
    src.mkdir()
    (src / "a.txt").write_text("x" * 100)
    (src / "b.py").write_text("print(1)")
    runner = FakeRunner()
    monkeypatch.setattr(transfer, "_run_cmd", runner)

    push("host1", str(src), "/remote/proj")

    assert " -z " in f" {runner.calls[0].cmd[0]} "


def test_push_does_not_compress_media_tree(tmp_path, monkeypatch):
    src = tmp_path / "proj"
    src.mkdir()
    (src / "clip.mp4").write_bytes(b"\0" * 100)
    runner = FakeRunner()
    monkeypatch.setattr(transfer, "_run_cmd", runner)

    push("host1", str(src), "/remote/proj")

    assert " -z " not in f" {runner.calls[0].cmd[0]} "


def test_push_excludes_default_dirs(tmp_path, monkeypatch):
    src = tmp_path / "proj"
    src.mkdir()
    (src / "node_modules").mkdir()
    (src / "node_modules" / "x.js").write_text("x")
    runner = FakeRunner()
    monkeypatch.setattr(transfer, "_run_cmd", runner)

    push("host1", str(src), "/remote/proj")

    cmd = runner.calls[0].cmd[0]
    assert "--exclude=node_modules" in cmd
    assert "--exclude=.git" in cmd


def test_push_falls_back_to_tar_when_rsync_missing(tmp_path, monkeypatch):
    src = tmp_path / "proj"
    src.mkdir()
    (src / "a.txt").write_text("hello")
    runner = FakeRunner(
        rules=[("rsync", (127, "", "rsync: command not found"))]
    )
    monkeypatch.setattr(transfer, "_run_cmd", runner)
    piped = []
    monkeypatch.setattr(
        transfer,
        "_pipeline",
        lambda cmds, timeout, capture=True: piped.append(cmds) or (0, "", ""),
    )

    res = push("host1", str(src), "/remote/proj")

    assert res.ok is True
    assert piped, "phải fallback sang tar-over-ssh"
    assert any("tar" in c for cmd in piped[0] for c in cmd)


def test_push_rejects_zero_timeout(tmp_path):
    src = tmp_path / "proj"
    src.mkdir()
    with pytest.raises(Exception) as exc:
        push("host1", str(src), "/remote/proj", timeout=0)
    assert exc.value.code == 2  # EXIT_INPUT


def test_push_rejects_non_absolute_remote_path(tmp_path):
    src = tmp_path / "proj"
    src.mkdir()
    with pytest.raises(Exception) as exc:
        push("host1", str(src), "relative/dir")
    assert exc.value.code == 2  # EXIT_INPUT


def test_push_rejects_missing_local_dir(tmp_path):
    with pytest.raises(Exception) as exc:
        push("host1", str(tmp_path / "nope"), "/remote/proj")
    assert exc.value.code == 2  # EXIT_INPUT


def test_push_reports_error_when_rsync_fails(tmp_path, monkeypatch):
    src = tmp_path / "proj"
    src.mkdir()
    (src / "a.txt").write_text("hello")
    runner = FakeRunner(
        rules=[
            ("rsync", (23, "", "rsync: some files could not be transferred")),
        ]
    )
    monkeypatch.setattr(transfer, "_run_cmd", runner)
    monkeypatch.setattr(transfer, "_run_ssh", lambda *a, **k: (0, "", ""))

    res = push("host1", str(src), "/remote/proj")

    assert res.ok is False
    assert res.error


# --------------------------------------------------------------------------
# pull
# --------------------------------------------------------------------------
def test_pull_manifest_lists_only_new_files(tmp_path, monkeypatch):
    remote = tmp_path / "remote"
    remote.mkdir()
    (remote / "keep.txt").write_text("old")
    (remote / "new.txt").write_text("brand new")
    local = tmp_path / "local"
    local.mkdir()
    (local / "keep.txt").write_text("old")

    listing = "ROFF_FILE=3\tkeep.txt\nROFF_FILE=9\tnew.txt\n"

    def fake_run_cmd(cmd, timeout=None, stdin=None):
        joined = " ".join(cmd)
        if "rsync" in joined:
            (local / "new.txt").write_text("brand new")
            return (0, "Total transferred file size: 9 bytes\n", "")
        return (0, "", "")

    monkeypatch.setattr(transfer, "_run_cmd", fake_run_cmd)
    monkeypatch.setattr(
        transfer, "_run_ssh", lambda *a, **k: (0, listing, "")
    )

    res = pull("host1", str(remote), str(local))

    assert res.ok is True
    assert [f["path"] for f in res.files] == ["new.txt"]
    entry = res.files[0]
    assert entry["sha256"] == hashlib.sha256(b"brand new").hexdigest()
    assert entry["bytes"] == 9
    assert res.bytes_transferred == 9


def test_pull_uses_rsync_and_compresses_text(tmp_path, monkeypatch):
    remote = tmp_path / "remote"
    remote.mkdir()
    (remote / "a.txt").write_text("hello")
    local = tmp_path / "local"
    local.mkdir()
    seen = {}

    def fake_run_cmd(cmd, timeout=None, stdin=None):
        seen["cmd"] = " ".join(cmd)
        return (0, "", "")

    monkeypatch.setattr(transfer, "_run_cmd", fake_run_cmd)
    monkeypatch.setattr(
        transfer, "_run_ssh", lambda *a, **k: (0, "ROFF_FILE=5\ta.txt\n", "")
    )

    pull("host1", str(remote), str(local))

    assert "rsync" in seen["cmd"]
    assert " -z " in f" {seen['cmd']} "
    assert "--no-owner" in seen["cmd"] and "--no-group" in seen["cmd"]


def test_pull_does_not_compress_media(tmp_path, monkeypatch):
    remote = tmp_path / "remote"
    remote.mkdir()
    (remote / "clip.mp4").write_bytes(b"\0" * 8)
    local = tmp_path / "local"
    local.mkdir()
    seen = {}

    def fake_run_cmd(cmd, timeout=None, stdin=None):
        seen["cmd"] = " ".join(cmd)
        return (0, "", "")

    monkeypatch.setattr(transfer, "_run_cmd", fake_run_cmd)
    monkeypatch.setattr(
        transfer, "_run_ssh", lambda *a, **k: (0, "ROFF_FILE=8\tclip.mp4\n", "")
    )

    pull("host1", str(remote), str(local))

    assert " -z " not in f" {seen['cmd']} "


def test_pull_path_pulls_single_file(tmp_path, monkeypatch):
    local = tmp_path / "out.mp4"
    seen = {}

    def fake_run_cmd(cmd, timeout=None, stdin=None):
        seen["cmd"] = " ".join(cmd)
        local.write_bytes(b"\0" * 4)
        return (0, "", "")

    monkeypatch.setattr(transfer, "_run_cmd", fake_run_cmd)
    monkeypatch.setattr(
        transfer, "_run_ssh", lambda *a, **k: (0, "ROFF_KIND=file\n", "")
    )

    res = pull_path("host1", "/remote/clip.mp4", str(local))

    assert res.ok is True
    assert "rsync" in seen["cmd"]
    assert res.files and res.files[0]["sha256"]


def test_pull_path_rejects_relative_remote_path(tmp_path):
    with pytest.raises(Exception) as exc:
        pull_path("host1", "rel/path", str(tmp_path / "x"))
    assert exc.value.code == 2  # EXIT_INPUT


# --------------------------------------------------------------------------
# stream_between
# --------------------------------------------------------------------------
def test_stream_between_pipes_without_writing_local_disk(tmp_path, monkeypatch):
    procs = []

    def fake_popen(cmd, **kwargs):
        proc = FakePopen(cmd, **kwargs)
        procs.append(proc)
        return proc

    monkeypatch.setattr(transfer, "_popen", fake_popen)

    before = _all_files(tmp_path, skip="state")
    res = stream_between("src-host", "/data/in", "dst-host", "/data/out")
    after = _all_files(tmp_path, skip="state")

    assert res.ok is True
    assert before == after, "stream_between không được ghi đĩa local"
    assert len(procs) == 2
    assert "tar" in " ".join(procs[0].cmd)
    assert "src-host" in " ".join(procs[0].cmd)
    assert "dst-host" in " ".join(procs[1].cmd)
    # tiến trình cuối không capture stdout (không đọc hết vào RAM)
    assert procs[1].kwargs.get("stdout") == subprocess.DEVNULL
    # tiến trình đầu nối qua pipe, không qua đĩa
    assert procs[0].kwargs.get("stdout") == subprocess.PIPE
    assert procs[1].kwargs.get("stdin") is procs[0].stdout


def test_stream_between_rejects_zero_timeout():
    with pytest.raises(Exception) as exc:
        stream_between("a", "/x", "b", "/y", timeout=0)
    assert exc.value.code == 2  # EXIT_INPUT


# --------------------------------------------------------------------------
# transfer_slot
# --------------------------------------------------------------------------
def test_transfer_slot_limits_parallel_transfers(tmp_path, monkeypatch):
    monkeypatch.setattr(transfer, "_max_parallel_transfers", lambda: 1)

    with transfer_slot(timeout=5):
        with pytest.raises(Exception) as exc:
            with transfer_slot(timeout=0.2):
                pass
        assert exc.value.code in (10, 124)  # EXIT_NO_HOST / EXIT_TIMEOUT


def test_transfer_slot_released_after_use(tmp_path, monkeypatch):
    monkeypatch.setattr(transfer, "_max_parallel_transfers", lambda: 1)
    with transfer_slot(timeout=5):
        pass
    with transfer_slot(timeout=1):
        pass  # phải vào được ngay → slot đã được trả


def test_max_parallel_transfers_reads_config(tmp_path, monkeypatch):
    cfg = tmp_path / "config.ini"
    cfg.write_text("[fleet]\nmax_parallel_transfers = 7\n")
    monkeypatch.setenv("ROFF_CONFIG", str(cfg))
    assert transfer._max_parallel_transfers() == 7
