"""Nghiệm thu W4/W5 trên **fleet thật**.

S33 — ``roff tunnel link`` nối 2 máy Muse thật: backend ở B, consumer ở A.
       Mở link ⇒ phải **thật sự** thông (A gọi được service của B qua
       ``127.0.0.1:<rport>``), rồi ``stop`` ⇒ **không** còn listener ở đầu nào.
S34 — ``roff doctor``: JSON đúng khoá, exit code theo số vấn đề, **không** ghi
       file, và mặc định **không** probe mạng (chạy nhanh).

Cả hai test **bỏ qua** (skip) nếu fleet không đủ máy — không đánh dấu xanh
khi không kiểm chứng được.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as common  # noqa: E402
_DEFAULT_STATE = common.DEFAULT_STATE  # dùng chung (xem _common.py)



REPO = Path(__file__).resolve().parents[2]
SKILL = REPO / "skill"
LAUNCHER = SKILL / "roff.py"

pytestmark = pytest.mark.acceptance


ALIASES = ["muse-pmb333", "muse-tampt", "muse-bienpm333", "muse-phamminhbien3333"]


def roff(args, timeout=180, env=None):
    e = common.roff_env(env)
    return subprocess.run(
        [sys.executable, str(LAUNCHER), *args],
        capture_output=True, text=True, timeout=timeout, env=e,
    )


def roff_json(args, timeout=180, env=None):
    p = roff(args, timeout=timeout, env=env)
    lines = [ln for ln in p.stdout.strip().splitlines() if ln.strip()]
    try:
        obj = json.loads(lines[-1]) if lines else {}
    except json.JSONDecodeError:
        obj = {}
    return p.returncode, obj, p.stderr


def ssh_ok(alias, cmd="true", timeout=30):
    try:
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", alias, cmd],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return 255, ""
    return r.returncode, r.stdout


def live_aliases(minimum=2):
    """Các alias **đang** sống (probe thật, không đọc cache)."""
    out = []
    for a in ALIASES:
        rc, _ = ssh_ok(a)
        if rc == 0:
            out.append(a)
    return out if len(out) >= minimum else []


def local_port_listening(port: int) -> bool:
    """Cổng local có đang LISTEN không (không cần quyền root)."""
    for family, addr in ((socket.AF_INET, "127.0.0.1"),):
        s = socket.socket(family, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((addr, port))
        except OSError:
            return True
        finally:
            s.close()
    return False


def remote_port_listening(alias: str, port: int) -> bool:
    """Host remote có LISTEN ở ``port`` không? Đọc ``ss``/``netstat``."""
    rc, out = ssh_ok(
        alias,
        "ss -ltn 2>/dev/null || netstat -ltn 2>/dev/null || cat /proc/net/tcp",
        timeout=40,
    )
    if rc != 0:
        return False
    return f":{port:04X}" in out.upper() or f":{port} " in out


@pytest.fixture(scope="module")
def two_hosts():
    hosts = live_aliases(minimum=2)
    if not hosts:
        pytest.skip("cần ít nhất 2 máy Muse sống để test tunnel link")
    return hosts


# --------------------------------------------------------------------- S33


@pytest.fixture(scope="module")
def backend_on_b(two_hosts):
    """Dựng HTTP server nhỏ trên máy B (port 18899) + dọn dẹp.

    Dùng ``setsid --fork`` để tiến trình **thật sự** tách khỏi phiên ssh —
    nếu không, ssh chờ tới khi channel đóng và treo.
    """
    b = two_hosts[1]
    port = 18899
    script = (
        "setsid --fork python3 -c 'import http.server; "
        "h=http.server.HTTPServer((\"127.0.0.1\",%d),"
        "http.server.SimpleHTTPRequestHandler); h.serve_forever()' "
        "</dev/null >/tmp/roff-acceptance-http.log 2>&1; exit 0" % port
    )
    rc, out = ssh_ok(b, script, timeout=40)
    time.sleep(2)
    if not remote_port_listening(b, port):
        pytest.skip("không dựng được HTTP server trên %s" % b)
    yield b, port
    # dọn: chỉ dừng đúng tiến trình test này
    try:
        ssh_ok(b, "pkill -f 'http.server.HTTPServer' 2>/dev/null; true", timeout=30)
    except Exception:
        pass


def test_s33_tunnel_link_end_to_end_on_real_fleet(two_hosts, backend_on_b):
    a, b = two_hosts[0], two_hosts[1]
    _, b_port = backend_on_b
    rport = 19000

    # 1) mở link
    rc, obj, err = roff_json(
        ["tunnel", "link", "--from", a, "--to", b,
         "--rport", str(rport), "--to-port", str(b_port)],
        timeout=180,
    )
    blob = json.dumps(obj, ensure_ascii=False)
    if common.is_host_down(rc, obj, err):
        pytest.skip(f"máy chết lúc mở link — sự cố hạ tầng: {err[-200:]}")
    assert rc == 0, "tunnel link fail: rc=%s %s %s" % (rc, blob, err)
    assert obj.get("ok") is True, blob
    assert obj.get("from") == a and obj.get("to") == b, blob
    assert obj.get("rport") == rport, blob
    assert obj.get("to_port") == b_port, blob
    via = obj.get("via_lport")
    assert isinstance(via, int) and 18500 <= via <= 18599, blob
    assert obj.get("a_listening") is True, blob
    assert obj.get("b_listening") is True, blob

    # 2) link **thật sự** thông: từ A gọi service của B qua 127.0.0.1:<rport>
    rc2, out2 = ssh_ok(
        a,
        "curl -sS -m 12 -o /dev/null -w '%%{http_code}' http://127.0.0.1:%d/ 2>&1" % rport,
        timeout=40,
    )
    if rc2 != 0 and ssh_ok(a)[0] != 0:
        pytest.skip(f"{a} chết lúc gọi qua link — sự cố hạ tầng")
    assert rc2 == 0 and out2.strip().startswith("200"), \
        "A không gọi được service của B qua link: rc=%s out=%r" % (rc2, out2)

    # 3) stop ⇒ không còn listener ở đầu nào
    rc3, obj3, err3 = roff_json(["tunnel", "stop", "--from", a, "--rport", str(rport)],
                                 timeout=180)
    if common.is_host_down(rc3, obj3, err3):
        pytest.skip(f"máy chết lúc đóng link — sự cố hạ tầng: {err3[-200:]}")
    assert rc3 == 0, "tunnel stop fail: rc=%s %s %s" % (rc3, json.dumps(obj3), err3)

    deadline = time.time() + 30
    a_clean = local_clean = False
    while time.time() < deadline:
        a_clean = not remote_port_listening(a, rport)
        local_clean = not local_port_listening(via)
        if a_clean and local_clean:
            break
        time.sleep(2)
    assert a_clean, "còn listener ở %s:%d sau khi stop" % (a, rport)
    assert local_clean, "còn listener local ở :%d sau khi stop" % via


def test_s33b_tunnel_link_reports_remote_not_listening(two_hosts):
    """B không LISTEN ở cổng đó ⇒ phải báo lỗi, **không** mở forward."""
    a, b = two_hosts[0], two_hosts[1]
    dead_port = 19876  # không ai LISTEN
    rc, obj, err = roff_json(
        ["tunnel", "link", "--from", a, "--to", b,
         "--rport", "19001", "--to-port", str(dead_port)],
        timeout=180,
    )
    blob = json.dumps(obj, ensure_ascii=False)
    assert rc != 0, blob
    assert obj.get("ok") is False, blob
    assert obj.get("reason") in ("remote_not_listening", "host_unreachable"), blob
    # không được để lại listener ở A
    assert not remote_port_listening(a, 19001), "vẫn còn listener ở %s:19001" % a


def test_s33c_tunnel_list_shows_then_hides_the_link(two_hosts, backend_on_b):
    a, b = two_hosts[0], two_hosts[1]
    _, b_port = backend_on_b
    rc, obj, err = roff_json(
        ["tunnel", "link", "--from", a, "--to", b,
         "--rport", "19002", "--to-port", str(b_port)],
        timeout=180,
    )
    if rc != 0:
        # Chỉ bỏ qua khi lỗi là **hạ tầng**; link hỏng vì lý do khác là
        # lỗi của roff ⇒ phải FAIL để lộ ra.
        if common.is_host_down(rc, obj, err):
            pytest.skip("máy chết lúc mở link — sự cố hạ tầng: %s" % err[-200:])
        raise AssertionError("không mở được link: %s" % json.dumps(obj, ensure_ascii=False))
    try:
        rc2, obj2, _ = roff_json(["tunnel", "list"], timeout=60)
        blob = json.dumps(obj2, ensure_ascii=False)
        assert rc2 == 0, blob
        assert "19002" in blob, blob
    finally:
        roff_json(["tunnel", "stop", "--from", a, "--rport", "19002"], timeout=120)
    rc3, obj3, _ = roff_json(["tunnel", "list"], timeout=60)
    assert "19002" not in json.dumps(obj3, ensure_ascii=False)


# --------------------------------------------------------------------- S34


def test_s34_doctor_reports_json_and_exit_code():
    rc, obj, err = roff_json(["doctor"], timeout=60)
    blob = json.dumps(obj, ensure_ascii=False)
    assert rc in (0, 1), "doctor exit=%s %s %s" % (rc, blob, err)
    assert isinstance(obj.get("checks"), list) and obj.get("checks"), blob
    assert isinstance(obj.get("problems"), int), blob
    problems = sum(1 for c in obj["checks"] if not c.get("ok"))
    assert obj["problems"] == problems, blob
    assert obj.get("ok") is (problems == 0), blob
    assert rc == (1 if problems else 0), blob
    names = {c.get("name") for c in obj["checks"]}
    assert {"config", "fleet", "state_perms"} <= names, blob
    for c in obj["checks"]:
        assert "name" in c and "ok" in c and "detail" in c, blob


def test_s34b_doctor_does_not_write_anything():
    """``doctor`` chỉ **báo** — không ghi file nào."""
    state = Path(tempfile.mkdtemp(prefix="roff-doctor-state-"))
    try:
        roff_json(["doctor"], timeout=60, env={"ROFF_STATE_DIR": str(state)})
        before = sorted(str(p.relative_to(state)) for p in state.rglob("*"))
        roff_json(["doctor"], timeout=60, env={"ROFF_STATE_DIR": str(state)})
        after = sorted(str(p.relative_to(state)) for p in state.rglob("*"))
        assert before == after, "doctor đã ghi file: %s" % (set(after) - set(before))
    finally:
        shutil.rmtree(state, ignore_errors=True)


def test_s34c_doctor_default_does_not_probe_network():
    """Mặc định ``doctor`` **không** probe mạng ⇒ phải nhanh (< 5 s)."""
    t0 = time.time()
    roff_json(["doctor"], timeout=60)
    dt = time.time() - t0
    assert dt < 5.0, "doctor mất %.1fs — có vẻ đã probe mạng" % dt


# --------------------------------------------------------------------- S35
# S35 — ledger bị "bẩn" (job định dạng cũ, job terminal cũ) ⇒ `roff gc` phải dọn,
#        và job định dạng cũ **không** được treo bởi `roff wait`.

LEGACY_JOB = {
    "job": "200101-000000-legacy",
    "remote": "muse-pmb333",
    "pid": 12345,
    "submitted_at": 1600000000,
    "cmd": "echo hi",
}
OLD_TERMINAL_JOB = {
    "job": "200101-000000-old",
    "state": "exited",
    "exit": 0,
    "host": "muse-pmb333",
    "created_at": 1000000000,
    "finished_at": 1000000100,
}
RUNNING_JOB = {
    "job": "261123-000000-run",
    "state": "launched",
    "host": "muse-pmb333",
    "created_at": 9999999999,
}


@pytest.fixture()
def dirty_ledger():
    """State dir có sẵn: 1 job định dạng cũ + 1 job terminal cũ + 1 job đang chạy."""
    state = Path(tempfile.mkdtemp(prefix="roff-gc-state-"))
    jobs = state / "jobs"
    jobs.mkdir(parents=True, exist_ok=True)
    (jobs / "200101-000000-legacy.json").write_text(json.dumps(LEGACY_JOB))
    (jobs / "200101-000000-old.json").write_text(json.dumps(OLD_TERMINAL_JOB))
    (jobs / "261123-000000-run.json").write_text(json.dumps(RUNNING_JOB))
    yield state
    shutil.rmtree(state, ignore_errors=True)


def test_s35_gc_removes_legacy_and_old_terminal_jobs(dirty_ledger):
    rc, obj, err = roff_json(["gc"], timeout=120, env={"ROFF_STATE_DIR": str(dirty_ledger)})
    blob = json.dumps(obj, ensure_ascii=False)
    assert rc == 0, "gc fail: rc=%s %s %s" % (rc, blob, err)
    jobs = dirty_ledger / "jobs"
    assert not (jobs / "200101-000000-legacy.json").exists(), \
        "job định dạng cũ chưa bị dọn: %s" % sorted(p.name for p in jobs.iterdir())
    assert not (jobs / "200101-000000-old.json").exists(), \
        "job terminal cũ chưa bị dọn: %s" % sorted(p.name for p in jobs.iterdir())
    assert (jobs / "261123-000000-run.json").exists(), \
        "job đang chạy bị xoá nhầm: %s" % sorted(p.name for p in jobs.iterdir())
    # payload phải **nói ra** đã xoá gì (dạng list hoặc count — miễn đủ thông tin)
    removed = obj.get("removed_jobs")
    count = obj.get("removed_jobs_count")
    assert isinstance(removed, list) or isinstance(count, int), blob
    if isinstance(removed, list):
        assert "200101-000000-legacy" in removed, blob
        assert "261123-000000-run" not in removed, blob
    if isinstance(count, int):
        assert count >= 2, "phải báo đã xoá ≥ 2 job: %s" % blob


def test_s35b_legacy_job_is_not_held_as_running(dirty_ledger):
    """Job định dạng cũ không được coi là ``queued`` (⇒ không giữ slot, không treo)."""
    sys.path.insert(0, str(SKILL))
    try:
        import roffkit.job as job_mod  # noqa: E402
    finally:
        sys.path.pop(0)
    parsed = job_mod.Job.from_dict(LEGACY_JOB)
    assert parsed.state == "unknown", "state=%r" % parsed.state
    assert parsed.state in job_mod.TERMINAL_STATES, \
        "job 'unknown' phải là terminal, không phải terminal: %r" % job_mod.TERMINAL_STATES


def test_s35c_wait_on_legacy_job_returns_immediately(dirty_ledger):
    """``roff wait`` trên job định dạng cũ phải **trả về ngay**, không treo."""
    t0 = time.time()
    rc, obj, err = roff_json(["wait", "200101-000000-legacy", "--timeout", "600"],
                              timeout=90, env={"ROFF_STATE_DIR": str(dirty_ledger)})
    dt = time.time() - t0
    blob = json.dumps(obj, ensure_ascii=False)
    assert dt < 30, "wait treo %.1fs trên job định dạng cũ" % dt
    assert rc != 124, "wait trả 124 (hết giờ) thay vì kết luận ngay: %s" % blob
    assert obj.get("state") in ("unknown", "lost", "exited"), blob


# --------------------------------------------------------------------- S36
# S36 — B14: đổi `~/.ssh/config` ⇒ KHÔNG được nói chuyện với máy CŨ.

#: Cổng thật của `muse-pmb333` trong `~/.ssh/config` của người dùng (reverse
#: tunnel tới 127.0.0.1). Nếu máy này chết, test **skip** — không đánh dấu
#: xanh khi không kiểm chứng được.
LIVE_PORT = 2222

_SSH_BLOCK = """Host roff-b14-probe
    HostName 127.0.0.1
    Port {port}
    User root
    IdentityFile ~/.ssh/id_ed25519
    StrictHostKeyChecking no
    UserKnownHostsFile /dev/null
    LogLevel ERROR
"""


def _live_muse_port():
    """Cổng của 1 máy Muse đang sống (đọc `~/.ssh/config`), hoặc None."""
    cfg = os.path.expanduser("~/.ssh/config")
    try:
        text = Path(cfg).read_text(errors="replace")
    except OSError:
        return None
    alias = None
    port = None
    for line in text.splitlines():
        line = line.strip()
        if line.lower().startswith("host "):
            alias = line.split()[1]
        elif line.lower().startswith("port ") and alias and alias.startswith("muse-"):
            port = int(line.split()[1])
            break
    if not port:
        return None
    rc, _ = ssh_ok("muse-pmb333")
    return port if rc == 0 else None


def test_s36_changing_ssh_config_does_not_reuse_the_old_master(tmp_path):
    """Đổi config (cùng alias, cổng chết) ⇒ phải **thất bại**, không tái dùng master cũ.

    Trước B14: socket ControlMaster khoá theo *alias* ⇒ lần probe thứ hai
    **tái dùng master cũ** ⇒ vẫn trả về thông tin của **máy cũ**.
    """
    port = _live_muse_port()
    if not port:
        pytest.skip("không xác định được cổng của máy Muse đang sống")

    live = tmp_path / "ssh_live"
    live.write_text(_SSH_BLOCK.format(port=port))
    dead = tmp_path / "ssh_dead"
    dead.write_text(_SSH_BLOCK.format(port=9))  # cổng chết

    state = tmp_path / "state"

    # 1) với config **thật**: phải probe thành công
    rc1, obj1, err1 = roff_json(["probe", "roff-b14-probe"], timeout=180,
                                 env={"ROFF_SSH_CONFIG": str(live),
                                      "ROFF_STATE_DIR": str(state)})
    if common.is_host_down(rc1, obj1, err1):
        pytest.skip(f"máy Muse chết lúc probe — sự cố hạ tầng: {err1[-200:]}")
    assert rc1 == 0, "probe với config thật phải thành công: %s %s" % (rc1, err1[-300:])
    assert obj1.get("reachable") is True, json.dumps(obj1, ensure_ascii=False)[:400]

    # 2) ĐỔI config (cùng alias, cổng chết) ⇒ phải thất bại
    rc2, obj2, err2 = roff_json(["probe", "roff-b14-probe"], timeout=180,
                                 env={"ROFF_SSH_CONFIG": str(dead),
                                      "ROFF_STATE_DIR": str(state)})
    blob = json.dumps(obj2, ensure_ascii=False)
    assert obj2.get("reachable") is False, \
        "vẫn nói chuyện với MÁY CŨ (tái dùng master cũ): %s" % blob[:400]
    assert rc2 != 0, blob
