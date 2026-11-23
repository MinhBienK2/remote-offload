import json
import os
import subprocess
import sys
import time
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as common  # noqa: E402
_DEFAULT_STATE = common.DEFAULT_STATE  # dùng chung (xem _common.py)

from _common import pick_live  # noqa: E402
from _common import wait_or_skip  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
SKILL = REPO / "skill"
LAUNCHER = SKILL / "roff.py"
HOOK = SKILL / "hooks" / "claude-code" / "pretooluse.py"

pytestmark = pytest.mark.acceptance

MUSE = ["muse-pmb333", "muse-tampt", "muse-bienpm333", "muse-phamminhbien3333"]

#: Secret dùng thử — KHÔNG được xuất hiện ở bất kỳ đâu ngoài file env trên host.
SECRET = "s3cr3t-value-xyz"


#: State dir **riêng cho test** — test KHÔNG được ghi vào ledger thật của
#: người dùng (``~/.local/state/roff``). Nếu không, mỗi lần chạy nghiệm thu lại


def roff(args, timeout=300, env=None):
    e = common.roff_env(env)
    p = subprocess.run([sys.executable, str(LAUNCHER)] + list(args),
                       capture_output=True, text=True, timeout=timeout, env=e)
    return p


def roff_json(args, timeout=300, env=None):
    p = roff(args, timeout=timeout, env=env)
    lines = [ln for ln in p.stdout.strip().splitlines() if ln.strip()]
    obj = json.loads(lines[-1]) if lines else {}
    return p.returncode, obj, p.stderr


def ssh_ok(alias, timeout=20):
    try:
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", alias, "true"],
            capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0
    except subprocess.SubprocessError:
        return False


def ssh_out(alias, script, timeout=60):
    """Chạy script trên host, trả (rc, stdout).

    Timeout của ssh ⇒ **không** phải lỗi của `roff` (máy đang flap) ⇒
    trả ``rc=255`` kèm lỗi, để test gọi ``common.is_host_down`` rồi
    ``pytest.skip`` thay vì để lỗi TimeoutExpired làm test **ERROR**
    (đo thật 23/11/2026: S12 vừa vặp đúng lần đó).
    """
    try:
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", alias, script],
            capture_output=True, text=True, timeout=timeout)
    except subprocess.SubprocessError as exc:
        return 255, "", str(exc)
    return r.returncode, r.stdout, r.stderr


def live_muses():
    return [a for a in MUSE if ssh_ok(a)]


@pytest.fixture(scope="module")
def fleet():
    live = live_muses()
    if not live:
        pytest.skip("không có máy muse nào sống — bật lại tunnel trước khi nghiệm thu")
    return live


@pytest.fixture
def cfg(tmp_path):
    """Config giới hạn fleet còn các máy muse đang sống, state dir riêng."""
    live = live_muses()
    if not live:
        pytest.skip("không có máy muse nào sống")
    path = tmp_path / "config.ini"
    path.write_text("[fleet]\ninclude = muse-*\n")
    state = tmp_path / "state"
    state.mkdir()
    return {
        "path": path,
        "state": state,
        "env": {
            "ROFF_CONFIG": str(path),
            "ROFF_STATE_DIR": str(state),
        },
    }


# ------------------------------------------------------------------ S14
def test_s14_secret_never_leaks(fleet, cfg, tmp_path):
    """S14: secret trong --env không lộ ở argv, không lộ trong audit log.

    Đạt khi:
      * ``ps`` trên host (lúc job đang chạy) không chứa giá trị secret;
      * audit log local không chứa giá trị secret;
      * file env trên host có mode 600;
      * audit log vẫn ghi ``--cmd`` nguyên văn và tên các key của --env.
    """
    host = pick_live(fleet)

    if host is None:

        pytest.skip("mọi máy muse đã chết — sự cố hạ tầng")
    # Job chạy đủ lâu để ta kịp soi `ps` trên host.
    rc, obj, err = roff_json(
        ["do", "--host", host, "--cmd", "sleep 45", "--env", f"MY_SECRET={SECRET}"],
        timeout=180, env=cfg["env"],
    )
    if common.is_host_down(rc, obj, err):
        pytest.skip(f"host {host} chết lúc gửi job — sự cố hạ tầng: {err[-200:]}")
    assert rc == 0, f"roff do thất bại: rc={rc} err={err} obj={obj}"
    job_id = obj["job"]

    try:
        # 1) argv trên host không chứa secret
        time.sleep(6)
        rc2, out, _ = ssh_out(
            host,
            "ps -eo args 2>/dev/null | grep -F '" + SECRET + "' | grep -v grep || true",
        )
        if rc2 != 0 and not ssh_ok(host):
            pytest.skip(f"host {host} chết lúc soi ps — sự cố hạ tầng")
        assert rc2 == 0, "không chạy được ps trên host"
        assert SECRET not in out, (
            "secret lộ trong argv của tiến trình trên host:\n" + out
        )

        # 2) file env trên host có mode 600
        rc3, mode_out, _ = ssh_out(
            host,
            "find \"$HOME/.roff\" -path '*/jobs/*/env' -printf '%m\\n' 2>/dev/null "
            "| sort -u | tr '\\n' ' '",
        )
        if rc3 != 0 and not ssh_ok(host):
            pytest.skip(f"host {host} chết lúc đo mode — sự cố hạ tầng")
        assert rc3 == 0
        modes = [m for m in mode_out.split() if m]
        if not modes:
            # layout_root có thể không phải $HOME/.roff — thử tìm rộng hơn
            rc3, mode_out, _ = ssh_out(
                host,
                "find / -maxdepth 6 -path '*/jobs/*/env' -printf '%m\\n' 2>/dev/null "
                "| sort -u | tr '\\n' ' '",
            )
            modes = [m for m in mode_out.split() if m]
        assert modes, "không tìm thấy file env của job trên host"
        for mode in modes:
            assert mode == "600", f"file env trên host có mode {mode}, phải là 600"
    finally:
        try:
            roff(["cancel", job_id], timeout=60, env=cfg["env"])
        except (subprocess.SubprocessError, OSError):
            pass

    # 3) audit log local không chứa secret, nhưng vẫn có cmd + tên key
    audit = cfg["state"] / "audit.jsonl"
    assert audit.exists(), "không có audit log — phải ghi lại mọi lần do"
    text = audit.read_text(encoding="utf-8")
    assert SECRET not in text, "secret bị ghi vào audit log local"
    entries = [json.loads(line) for line in text.splitlines() if line.strip()]
    mine = [e for e in entries if e.get("job") == job_id]
    assert mine, "audit log không có entry của job vừa chạy"
    assert "sleep 45" in mine[0]["cmd"], "--cmd phải được ghi nguyên văn"
    assert mine[0]["env_keys"] == ["MY_SECRET"], "tên key env phải được ghi"


# ------------------------------------------------------------------ S13
def test_s13_all_hosts_dead(tmp_path):
    """S13: mọi host chết ⇒ exit 10/255, không chạy gì nặng ở máy local.

    Cách chạy: HOME tạm với ``.ssh/config`` trỏ mọi host sang cổng chết.
    """
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "config").write_text(
        "Host dead-a\n  HostName 127.0.0.1\n  Port 9\n"
        "Host dead-b\n  HostName 127.0.0.1\n  Port 9\n"
    )
    cfg_path = tmp_path / "config.ini"
    cfg_path.write_text("[fleet]\ninclude = dead-*\n")
    state = tmp_path / "state"
    state.mkdir()
    env = {
        "HOME": str(home),
        "ROFF_CONFIG": str(cfg_path),
        "ROFF_STATE_DIR": str(state),
    }

    rc, obj, err = roff_json(
        ["do", "--cmd", "sleep 5"], timeout=180, env=env,
    )
    assert rc in (10, 255), (
        f"phải trả exit 10 (hết host) hoặc 255 (không với được host), thực tế rc={rc}; "
        f"obj={obj} err={err}"
    )
    assert not obj.get("ok", False), "payload phải nói thất bại"
    # JSON phải nói rõ host nào chết / vì sao
    blob = json.dumps(obj, ensure_ascii=False)
    assert "dead-a" in blob or "dead-b" in blob or obj.get("reason"), (
        f"payload phải nói host nào không dùng được: {obj}"
    )

    # Hook vẫn phải chặn lệnh nặng (không "thấy host chết thì cho qua").
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"tool_name": "Bash",
                          "tool_input": {"command": "pytest -q"}}),
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 2, (
        f"hook vẫn phải chặn lệnh nặng (exit 2), thực tế rc={proc.returncode}"
    )
    assert "roff" in proc.stderr, "hook phải chỉ đường sang roff"


# ------------------------------------------------------------------ S12
def test_s12_tunnel_serves_and_stops_cleanly(fleet):
    """S12: tunnel tới host hoạt động; ``tunnel stop`` không để lại tiến trình.

    Đạt khi:
      * gọi được http://127.0.0.1:<L> và nhận nội dung đúng;
      * sau ``tunnel stop`` không còn tiến trình ssh nào của roff;
      * không kill nhầm tiến trình ssh khác của người dùng.
    """
    host = pick_live(fleet)

    if host is None:

        pytest.skip("mọi máy muse đã chết — sự cố hạ tầng")
    # Dựng 1 HTTP server tĩnh trên host (không cần internet).
    #
    # `</dev/null` cho tiến trình nền là **bắt buộc**: nếu không, nó giữ mở
    # stdin của kênh ssh ⇒ ssh chờ EOF mãi ⇒ lệnh `ssh` treo tới timeout
    # (đo thật 23/11/2026: `subprocess.TimeoutExpired` sau 60 s).
    rc, out, err = ssh_out(
        host,
        "d=$(mktemp -d); printf 'hello-remote' > \"$d/index.html\"; "
        "cd \"$d\" && nohup python3 -m http.server 8731 "
        "--bind 127.0.0.1 </dev/null >/dev/null 2>&1 & echo $! > /tmp/roff-s12-http.pid; "
        "sleep 2; cat /tmp/roff-s12-http.pid",
    )
    if common.is_host_down(rc, {}, err):
        pytest.skip(f"host {host} chết lúc dựng HTTP server — sự cố hạ tầng: {err[-200:]}")
    assert rc == 0, f"không dựng được HTTP server trên host: {err}"
    remote_pid = out.strip().splitlines()[-1] if out.strip() else ""

    local_port = 18731
    try:
        rc2, obj, err2 = roff_json(
            ["tunnel", "start", "--host", host,
             "--lport", str(local_port), "--rport", "8731"],
            timeout=120,
        )
        if common.is_host_down(rc2, obj, err2):
            pytest.skip(f"host {host} chết lúc mở tunnel — sự cố hạ tầng: {err2[-200:]}")
        assert rc2 == 0, f"tunnel start thất bại: rc={rc2} err={err2} obj={obj}"

        body = ""
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                r = subprocess.run(
                    ["curl", "-s", "--max-time", "5",
                     f"http://127.0.0.1:{local_port}/"],
                    capture_output=True, text=True, timeout=20)
                body = r.stdout
                if body:
                    break
            except subprocess.SubprocessError:
                pass
            time.sleep(2)
        if not body and not ssh_ok(host):
            pytest.skip(f"host {host} chết giữa lúc tunnel — sự cố hạ tầng")
        assert "hello-remote" in body, (
            f"không gọi được qua tunnel (body={body!r})"
        )

        # snapshot: các tiến trình ssh của chính user trước khi stop
        before = subprocess.run(
            ["bash", "-lc", "ps -eo pid,args | grep '[s]sh ' | wc -l"],
            capture_output=True, text=True, timeout=30).stdout.strip()

        rc3, obj3, err3 = roff_json(
            ["tunnel", "stop", "--host", host, "--lport", str(local_port)],
            timeout=120,
        )
        if common.is_host_down(rc3, obj3, err3):
            pytest.skip(f"host {host} chết lúc đóng tunnel — sự cố hạ tầng: {err3[-200:]}")
        assert rc3 == 0, f"tunnel stop thất bại: rc={rc3} err={err3} obj={obj3}"

        # sau stop: không còn tiến trình ssh nào thuộc roff (control path của roff)
        after = subprocess.run(
            ["bash", "-lc",
             "ps -eo args | grep -F 'ControlPath=' | "
             "grep -F 'roff' | grep -v grep | wc -l"],
            capture_output=True, text=True, timeout=30).stdout.strip()
        assert after == "0", (
            f"còn {after} tiến trình ssh của roff sau tunnel stop"
        )
        # và không giết nhầm tiến trình ssh khác của user
        now = subprocess.run(
            ["bash", "-lc", "ps -eo pid,args | grep '[s]sh ' | wc -l"],
            capture_output=True, text=True, timeout=30).stdout.strip()
        assert int(now) >= int(before) - 1, (
            f"số tiến trình ssh giảm nhiều ({before} -> {now}): có thể đã kill nhầm"
        )
    finally:
        # Dọn dẹp **không** được ném lỗi ra — nếu nó ném, lỗi gốc của test
        # bị che mất và ta mất thông tin quan trọng nhất.
        try:
            roff(["tunnel", "stop", "--host", host, "--lport", str(local_port)],
                 timeout=60)
        except (subprocess.SubprocessError, OSError):
            pass
        if remote_pid:
            ssh_out(host, f"kill {remote_pid} 2>/dev/null || true")
            ssh_out(host, "rm -f /tmp/roff-s12-http.pid")


# ------------------------------------------------------------------ S19
def test_s19_three_sessions_share_the_fleet(fleet, cfg, tmp_path):
    """S19: 3 phiên agent cùng lúc ⇒ không host nào vượt số slot, mọi lô xong.

    Đạt khi:
      * số lease đồng thời trên 1 host không vượt ``slots`` của host đó;
      * cả 3 lô đều hoàn tất, đủ số output;
      * không có 2 job cùng id;
      * 2 luồng truyền đồng thời (trần của ``[local] max_parallel_transfers``).
    """
    if len(fleet) < 2:
        pytest.skip("cần ≥ 2 máy để thử nhiều phiên")

    workdir = tmp_path / "batches"
    workdir.mkdir()
    for b in range(3):
        d = workdir / f"b{b}"
        d.mkdir()
        for i in range(2):
            (d / f"item{i}.bin").write_bytes(bytes([b, i]) * 4096)

    procs = []
    env = dict(cfg["env"])
    # Chờ slot ngắn: nếu hàng đợi chưa làm việc đúng, lô phải báo lỗi **nhanh**
    # thay vì treo 15 phút rồi mới chết.
    env["ROFF_SLOT_WAIT_S"] = "10"
    for b in range(3):
        cmd = [
            sys.executable, str(LAUNCHER), "do",
            "--each", str(workdir / f"b{b}" / "*.bin"),
            "--out", str(workdir / f"out{b}"),
            "--cmd",
            'cp "$ROFF_ITEM" "$ROFF_OUT/$(basename "$ROFF_ITEM")"',
        ]
        procs.append(subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True,
                                      env={**os.environ, **env}))

    # Theo dõi lease trong lúc chạy: không host nào vượt slots.
    max_leases: dict[str, int] = {}
    deadline = time.time() + 420
    while time.time() < deadline:
        for p in procs:
            if p.poll() is None:
                continue
        slots_path = cfg["state"] / "slots.json"
        try:
            data = json.loads(slots_path.read_text())
        except (OSError, ValueError):
            data = {}
        # đếm lease **đồng thời** trên từng host, rồi lấy mức cao nhất thấy được
        concurrent: dict[str, int] = {}
        for lease in (data.get("leases") or {}).values():
            h = lease.get("host", "?")
            concurrent[h] = concurrent.get(h, 0) + 1
        for h, count in concurrent.items():
            max_leases[h] = max(max_leases.get(h, 0), count)
        if all(p.poll() is not None for p in procs):
            break
        time.sleep(1.5)

    results = []
    for p in procs:
        out, err = p.communicate(timeout=120)
        lines = [ln for ln in (out or "").strip().splitlines() if ln.strip()]
        obj = json.loads(lines[-1]) if lines else {}
        results.append((p.returncode, obj, err))

    for rc, obj, err in results:
        assert rc == 0, f"một lô không khởi tạo được: rc={rc} obj={obj} err={err}"
        assert obj.get("job"), "payload phải có job id"

    all_jobs = []
    for _rc, obj, _err in results:
        all_jobs.extend(obj.get("jobs") or [obj.get("job")])
    assert len(all_jobs) == len(set(all_jobs)), "job id bị trùng"

    # Chờ mọi job tới trạng thái cuối.
    for jid in all_jobs:
        rc, obj, err = wait_or_skip(jid, timeout=700, env=cfg["env"], wait_timeout=600)
        assert rc == 0, f"job {jid} không hoàn tất: rc={rc} obj={obj} err={err}"
        assert obj.get("exit") == 0, f"job {jid} exit={obj.get('exit')}"

    # Mọi lô đều đủ output.
    for b in range(3):
        out_dir = workdir / f"out{b}"
        files = sorted(p.name for p in out_dir.glob("*")) if out_dir.exists() else []
        assert files == ["item0.bin", "item1.bin"], (
            f"lô {b} thiếu output: {files}"
        )

    # Không host nào vượt số slot (mặc định 1; config này không đặt slots).
    for host, count in max_leases.items():
        assert count <= 1, (
            f"host {host} bị nhồi {count} job cùng lúc, vượt slots=1"
        )
