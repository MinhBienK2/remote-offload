"""R27 (B17) — kho input dùng chung không được **kẹt vĩnh viễn**.

Sự cố đo thật trên fleet Muse 23/11/2026 (job ``261123-141011-5884``): thư mục
``<key>.lock`` **mồ côi** còn lại trong kho input ⇒ mọi lần đẩy sau đó đều fail
``shared_input_busy`` ⇒ watcher quay vòng **mãi** trên cùng host ⇒ job treo
2 giờ 10 phút.

Bộ test này chạy ở mức **tiến trình**: ``ssh``/``rsync`` giả có trạng thái
(filesystem host nằm trong 1 file JSON) ⇒ đi đúng đường ``sshx.run_shell``
thật của ``shared.py`` ⇒ không cần mạng, không đụng host thật.

Bất biến khoá lại ở đây:

1. lock mồ côi phải bị **dọn rồi chiếm lại** ⇒ đẩy được ⇒ **không** kẹt;
2. sau khi đẩy xong **không còn** thư mục ``.lock`` nào ⇒ không bỏ lại mồ côi;
3. lần gọi sau **không đẩy lại** (marker khớp) ⇒ kho không phình;
4. lock của tiến trình **còn sống** không được xoá.
"""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

REPO = Path(__file__).resolve().parents[2]
SKILL = REPO / "skill"
if str(SKILL) not in sys.path:
    sys.path.insert(0, str(SKILL))

HOST = "muse-fake"
REMOTE_ROOT = "/home/hatch/.roff"

#: ssh giả — mô phỏng filesystem tối giểu trên host (có trạng thái).
FAKE_SSH = r'''#!/usr/bin/env python3
"""ssh giả có trạng thái: filesystem host nằm trong file JSON."""
import json
import os
import shlex
import sys
import time

OPTS_WITH_VALUE = {"-o", "-p", "-i", "-l", "-F", "-E", "-J", "-b", "-c", "-D",
                   "-e", "-I", "-L", "-R", "-W", "-w", "-m", "-Q", "-S", "-t"}


def main() -> int:
    argv = sys.argv[1:]
    script = ""
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in OPTS_WITH_VALUE:
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        script = argv[i + 1]
        break
    state_path = os.environ["ROFF_FAKE_FS"]
    with open(state_path, encoding="utf-8") as fh:
        state = json.load(fh)
    fs = state["fs"]
    state.setdefault("commands", []).append(script)

    def save():
        with open(state_path, "w", encoding="utf-8") as fh:
            json.dump(state, fh)

    def drop(path):
        base = path.rstrip("/")
        for key in list(fs):
            if key == base or key.startswith(base + "/"):
                del fs[key]

    rc = 0
    out = ""
    toks = shlex.split(script)
    if toks[:1] == ["printf"] and ">" in toks:
        path = toks[toks.index(">") + 1]
        content = toks[2] if len(toks) > 2 else ""
        fs[path] = {"mtime": time.time(), "content": content}
    elif toks[:2] == ["mkdir", "-p"] and "&&" in toks:
        cut = toks.index("&&")
        parent = toks[2]
        fs.setdefault(parent, {"mtime": time.time(), "dir": True})
        rest = toks[cut + 1:]
        if rest[:1] == ["mkdir"]:
            lock = rest[1]
            if lock in fs:
                rc = 1          # lock đã tồn tại ⇒ mkdir fail (atomic)
            else:
                fs[lock] = {"mtime": time.time(), "dir": True}
        elif rest[:1] == ["printf"]:
            path = rest[rest.index(">") + 1]
            content = rest[2] if len(rest) > 2 else ""
            fs[path] = {"mtime": time.time(), "content": content}
    elif toks[:1] == ["stat"]:
        path = toks[3]
        if path in fs:
            out = "%.0f\n" % float(fs[path].get("mtime", 0))
    elif toks[:2] == ["test", "-d"] and "&&" in toks:
        cut = toks.index("&&")
        if toks[2] in fs and toks[cut + 2] in fs:
            out = fs[toks[cut + 2]].get("content", "")
    elif toks[:1] == ["cat"]:
        path = toks[1]
        if path in fs:
            out = fs[path].get("content", "")
    elif toks[:2] == ["test", "-e"]:
        rc = 0 if toks[2] in fs else 1
    elif toks[:2] in (["rm", "-rf"], ["rm", "-f"]):
        protected = [p.rstrip("/") for p in state.get("keep", [])]
        if toks[2].rstrip("/") not in protected:
            drop(toks[2])
        if "&&" in toks:
            want = toks[-1]
            rc = 0 if want not in fs else 1
    elif toks[:1] == ["chmod"]:
        pass
    elif toks[:2] == ["command", "-v"]:
        out = "__RSYNC__\n"
    if out:
        sys.stdout.write(out)
    save()
    return rc


if __name__ == "__main__":
    sys.exit(main())
'''

#: rsync giả — chép nội dung vào filesystem host giả (trong file JSON).
FAKE_RSYNC = r'''#!/usr/bin/env python3
"""rsync giả: chép thư mục local vào filesystem host giả (file JSON)."""
import json
import os
import sys
import time

args = [a for a in sys.argv[1:] if not a.startswith("-")]
src = dst = ""
for a in args:
    if ":" in a and not a.startswith("-"):
        _host, dst = a.split(":", 1)
    else:
        src = a
if not src or not dst:
    sys.exit(3)
state_path = os.environ["ROFF_FAKE_FS"]
with open(state_path, encoding="utf-8") as fh:
    state = json.load(fh)
fs = state["fs"]
fs.setdefault(dst.rstrip("/"), {"mtime": time.time(), "dir": True})
for root, _dirs, files in os.walk(src):
    for name in files:
        full = os.path.join(root, name)
        rel = os.path.relpath(full, src)
        try:
            with open(full, "rb") as fh:
                content = fh.read().decode("utf-8", "replace")
        except OSError:
            content = ""
        fs[dst.rstrip("/") + "/" + rel] = {
            "mtime": os.path.getmtime(full), "content": content}
state.setdefault("pushes", []).append({"src": src, "dst": dst})
with open(state_path, "w", encoding="utf-8") as fh:
    json.dump(state, fh)
sys.exit(0)
'''


class FakeFs:
    """Filesystem host giả (lưu trong 1 file JSON)."""

    def __init__(self, path: Path):
        self.path = path

    def read(self) -> dict:
        return json.loads(self.path.read_text())

    def write(self, state: dict) -> None:
        self.path.write_text(json.dumps(state))


@pytest.fixture
def fake_fs(tmp_path, monkeypatch):
    """ssh + rsync giả có trạng thái; trả :class:`FakeFs`."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    state_path = tmp_path / "fs.json"
    state_path.write_text(json.dumps({"fs": {}, "commands": [], "pushes": []}))

    ssh_script = bindir / "ssh"
    ssh_script.write_text(FAKE_SSH)
    ssh_script.chmod(0o755)
    rsync_script = bindir / "rsync"
    rsync_script.write_text(FAKE_RSYNC)
    rsync_script.chmod(0o755)

    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.setenv("ROFF_FAKE_FS", str(state_path))

    # Cache "host này có rsync" là module-level ⇒ phải xoá giữa các test.
    from roffkit import transfer

    monkeypatch.setattr(transfer, "_RSYNC_CACHE", {})

    return FakeFs(state_path)


def _seed(fs: FakeFs, path: str, *, mtime=None, content=None) -> None:
    """Đặt 1 entry vào filesystem host giả."""
    state = fs.read()
    state["fs"][path] = {
        "mtime": time.time() if mtime is None else mtime,
        "content": content or "",
    }
    fs.write(state)


def _local_source(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "v1.mp4").write_text("video-1")
    return src


def test_r27_orphan_lock_is_reclaimed_and_input_pushed(fake_fs, tmp_path):
    """Lock mồ côi (10 phút, không marker) ⇒ phải bị dọn, đẩy được, không kẹt.

    Bản cũ: ``LOCK_STALE_S = 1800`` ⇒ phải quay vòng ~30 lần (≈ 30 phút)
    mới dọn được ⇒ job treo. Bản mới: dọn ngay trong 1 lần chờ.
    """
    from roffkit import shared

    src = _local_source(tmp_path)
    key = shared.shared_key(str(src))
    lock = f"{REMOTE_ROOT}/inputs/{key}.lock"
    marker = f"{REMOTE_ROOT}/inputs/{key}.complete"

    # lock mồ côi: tồn tại, đã 10 phút, KHÔNG có marker
    _seed(fake_fs, lock, mtime=time.time() - 600)

    t0 = time.time()
    path = shared.ensure_shared_input(HOST, str(src), REMOTE_ROOT)
    elapsed = time.time() - t0

    assert path == f"{REMOTE_ROOT}/inputs/{key}"
    state = fake_fs.read()
    # 1) đẩy thật đã xảy ra
    assert len(state["pushes"]) == 1, state["pushes"]
    assert f"{REMOTE_ROOT}/inputs/{key}/v1.mp4" in state["fs"]
    # 2) marker đã ghi ⇒ lần sau dùng lại
    assert marker in state["fs"], "phải ghi marker .complete"
    # 3) KHÔNG còn lock mồ côi nào
    assert lock not in state["fs"], "lock phải được nhả (không bỏ lại mồ côi)"
    # 4) không chờ lâu như bản cũ (30 phút) — phải dọn trong 1 lần chờ
    assert elapsed < shared.MARKER_WAIT_S, elapsed


def test_r27_second_call_reuses_store_without_new_push(fake_fs, tmp_path):
    """Lần gọi sau phải **dùng lại** bản copy (không đẩy lại) ⇒ kho không phình."""
    from roffkit import shared

    src = _local_source(tmp_path)
    key = shared.shared_key(str(src))

    shared.ensure_shared_input(HOST, str(src), REMOTE_ROOT)
    assert len(fake_fs.read()["pushes"]) == 1

    path = shared.ensure_shared_input(HOST, str(src), REMOTE_ROOT)
    state = fake_fs.read()
    assert path == f"{REMOTE_ROOT}/inputs/{key}"
    assert len(state["pushes"]) == 1, "lần 2 không được đẩy lại"
    assert f"{REMOTE_ROOT}/inputs/{key}.lock" not in state["fs"]


def test_r27_lock_of_live_process_is_not_stolen(fake_fs, tmp_path, monkeypatch):
    """Lock của tiến trình **còn sống** ⇒ không được xoá (tránh đẩy trùng)."""
    from roffkit import shared
    from roffkit.errors import RoffError

    src = _local_source(tmp_path)
    key = shared.shared_key(str(src))
    lock = f"{REMOTE_ROOT}/inputs/{key}.lock"
    owner = json.dumps(
        {
            "id": "abc",
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "started": time.time(),
        }
    )
    _seed(fake_fs, lock, mtime=time.time() - 600, content=owner)
    _seed(fake_fs, f"{lock}/owner", content=owner)

    # Ngắn thời gian chờ ⇒ phải báo "busy" (không phải xoá lock của người sống)
    monkeypatch.setattr(shared, "MARKER_WAIT_S", 2.0)
    monkeypatch.setattr(shared, "POLL_INTERVAL", 0.1)

    with pytest.raises(RoffError) as excinfo:
        shared.ensure_shared_input(HOST, str(src), REMOTE_ROOT)
    assert excinfo.value.reason == "shared_input_busy"
    state = fake_fs.read()
    assert lock in state["fs"], "lock của tiến trình còn sống không được xoá"
    assert not state["pushes"], "không được đẩy khi lock còn sống"


def test_r27_release_uses_rm_rf_and_verifies(fake_fs, tmp_path):
    """Nhả lock phải dùng ``rm -rf`` + kiểm tra lại (không dùng ``rmdir``)."""
    from roffkit import shared

    src = _local_source(tmp_path)
    key = shared.shared_key(str(src))
    lock = f"{REMOTE_ROOT}/inputs/{key}.lock"
    _seed(fake_fs, lock, content="owner")

    assert shared._release_lock(HOST, lock, "abc") is True
    state = fake_fs.read()
    assert lock not in state["fs"]
    released = [c for c in state["commands"] if "rm -rf" in c and lock in c]
    assert released, state["commands"]
    assert all("rmdir" not in c for c in released), released


def test_r27_warning_goes_to_stderr_not_stdout(fake_fs, tmp_path):
    """Lock không nhả được ⇒ phải **báo** (stderr), stdout chỉ dành cho JSON."""
    from roffkit import shared

    src = _local_source(tmp_path)
    key = shared.shared_key(str(src))
    lock = f"{REMOTE_ROOT}/inputs/{key}.lock"
    _seed(fake_fs, lock, content="owner")
    _seed(fake_fs, f"{lock}/owner", content="owner")
    state = fake_fs.read()
    state["keep"] = [lock]          # host "không cho" xoá ⇒ nhả thất bại
    fake_fs.write(state)

    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0, %r);"
            "from roffkit import shared;"
            "print(shared._release_lock(%r, %r, 'abc'))" % (str(SKILL), HOST, lock),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "False" in proc.stdout, proc.stdout
    assert "lock" in proc.stderr.lower(), proc.stderr
