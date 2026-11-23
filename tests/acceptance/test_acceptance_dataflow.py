"""Nghiệm thu luồng dữ liệu & failover — S23, S24.

Chạy:  pytest tests/acceptance/test_acceptance_dataflow.py -q -m acceptance

S23: input đẩy **một lần cho mỗi host** (không nhân bản theo số job).
S24: failover giữ nguyên item của ``--each`` và hậu tố thư mục làm việc.

**Fleet Muse hay "flap"** (tunnel rớt giữa chừng). Mọi test ở đây vì vậy
phải *kiểm tra liveness ngay trước lúc dùng* và **thử lại trên máy khác**
khi lỗi là do host chết (không phải do logic của roff).
"""
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SKILL = REPO / "skill"
LAUNCHER = SKILL / "roff.py"

pytestmark = pytest.mark.acceptance

MUSE = ["muse-pmb333", "muse-tampt", "muse-bienpm333", "muse-phamminhbien3333"]

#: Lý do lỗi **do host chết** ⇒ đáng thử lại trên máy khác.
HOST_DEATH_REASONS = {"no_host", "host_unreachable", "unknown_host", "empty_fleet"}


def roff(args, timeout=300, env=None):
    e = dict(os.environ)
    if env:
        e.update(env)
    p = subprocess.run([sys.executable, str(LAUNCHER)] + list(args),
                       capture_output=True, text=True, timeout=timeout, env=e)
    return p


def roff_json(args, timeout=300, env=None):
    p = roff(args, timeout=timeout, env=env)
    lines = [ln for ln in p.stdout.strip().splitlines() if ln.strip()]
    obj = json.loads(lines[-1]) if lines else {}
    return p.returncode, obj, p.stderr


def ssh_out(alias, script, timeout=60):
    r = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", alias, script],
        capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout, r.stderr


def ssh_ok(alias, timeout=20):
    try:
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", alias, "true"],
            capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0
    except subprocess.SubprocessError:
        return False


def live_muses():
    return [a for a in MUSE if ssh_ok(a)]


def pick_live(n=1, exclude=()):
    """Chọn *n* máy đang sống (kiểm tra ngay lúc gọi), loại trừ ``exclude``."""
    alive = [a for a in live_muses() if a not in exclude]
    return alive[:n]


@pytest.fixture(scope="module")
def fleet():
    live = live_muses()
    if not live:
        pytest.skip("không có máy muse nào sống — bật lại tunnel trước khi nghiệm thu")
    return live


@pytest.fixture
def cfg(tmp_path):
    """Config riêng cho test: fleet ``muse-*``, probe **không** cache.

    ``probe_cache_s = 0`` ⇒ mỗi lần gọi ``roff do`` đều probe lại ⇒ không
    có host chết nào bị cache che mất. ``ROFF_SLOT_WAIT_S`` nhỏ ⇒ hàng đợi
    slot hỏng thì fail nhanh thay vì treo 15 phút.
    """
    live = live_muses()
    if not live:
        pytest.skip("không có máy muse nào sống")
    path = tmp_path / "config.ini"
    path.write_text(
        "[fleet]\n"
        "include = muse-*\n"
        "probe_cache_s = 0\n"
    )
    state = tmp_path / "state"
    state.mkdir()
    return {
        "path": path,
        "state": state,
        "env": {
            "ROFF_CONFIG": str(path),
            "ROFF_STATE_DIR": str(state),
            "ROFF_SLOT_WAIT_S": "20",
        },
    }


def _ledger(state_dir: Path, job_id: str) -> dict:
    return json.loads((state_dir / "jobs" / f"{job_id}.json").read_text())


def _layout_root_of(ledger: dict) -> str:
    """layout_root suy ra từ ``out_remote`` = ``<layout_root>/jobs/<id>/out``."""
    out_remote = str(ledger.get("out_remote") or "")
    marker = "/jobs/"
    idx = out_remote.rfind(marker)
    assert idx > 0, f"out_remote lạ: {out_remote!r}"
    return out_remote[:idx]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _wait_all(state_dir: Path, job_ids, timeout=900, env=None):
    """Chờ mọi job tới trạng thái cuối; trả về dict job_id -> payload."""
    out = {}
    deadline = time.time() + timeout
    for jid in job_ids:
        remaining = max(30, deadline - time.time())
        rc, obj, err = roff_json(["wait", jid, "--timeout", str(int(remaining))],
                                  timeout=remaining + 60, env=env)
        assert rc == 0, f"job {jid} không hoàn tất: rc={rc} obj={obj} err={err[-300:]}"
        out[jid] = obj
    return out


def _do_with_retry(build_args, cfg, tries=3, timeout=1200):
    """Gọi ``roff do``; lỗi do **host chết** thì thử lại (máy khác, config khác).

    ``build_args`` là hàm nhận danh sách máy đang sống ⇒ trả về argv.
    Trả về ``(rc, obj, err)`` của lần cuối.
    """
    last = (1, {}, "")
    for attempt in range(tries):
        alive = live_muses()
        if not alive:
            pytest.skip("không còn máy muse nào sống")
        args = build_args(alive)
        rc, obj, err = roff_json(args, timeout=timeout, env=cfg["env"])
        last = (rc, obj, err)
        if rc == 0:
            return rc, obj, err
        reason = str(obj.get("reason") or "")
        if reason not in HOST_DEATH_REASONS:
            break
        # host chết giữa chừng ⇒ chờ một chút rồi thử lại
        time.sleep(5)
        continue
    return last


# ------------------------------------------------------------------ S23
def test_s23_input_pushed_once_per_host(fleet, cfg, tmp_path):
    """S23: input đẩy 1 lần cho mỗi host, không nhân bản theo số job.

    Cách chạy: 4 item (~2 MB) nằm trong 1 thư mục input; ``--each`` + ``--in``.

    Đạt khi:
      * mọi output về đúng nội dung (sha256);
      * trên mỗi host, kho input dùng chung giữ **đúng 1 bản** của nguồn
        (tổng byte ≈ kích thước input, không phải N lần);
      * thư mục làm việc của từng job **không** chứa bản copy input.
    """
    src = tmp_path / "data"
    src.mkdir()
    payloads = {}
    for i in range(4):
        data = bytes([i]) * (2 * 1024 * 1024)  # 2 MB, nội dung khác nhau
        (src / f"item{i}.bin").write_bytes(data)
        payloads[f"item{i}.bin"] = hashlib.sha256(data).hexdigest()
    total_bytes = sum((src / n).stat().st_size for n in payloads)

    out = tmp_path / "out"

    def build(_alive):
        return [
            "do",
            "--each", str(src / "*.bin"),
            "--in", str(src),
            "--out", str(out),
            "--cmd", 'cp "$ROFF_ITEM" "$ROFF_OUT/$(basename "$ROFF_ITEM")"',
        ]

    rc, obj, err = _do_with_retry(build, cfg, timeout=1200)
    assert rc == 0, f"roff do thất bại: rc={rc} obj={obj} err={err[-400:]}"
    job_ids = obj.get("jobs") or [obj.get("job")]
    job_ids = [j for j in job_ids if j]
    assert len(job_ids) == 4, f"phải tạo 4 job: {job_ids}"

    _wait_all(cfg["state"], job_ids, timeout=1800, env=cfg["env"])

    # output đúng nội dung
    for name, digest in payloads.items():
        path = out / name
        assert path.exists(), f"thiếu output {name}: {sorted(p.name for p in out.glob('*'))}"
        assert _sha256(path) == digest, f"output {name} sai nội dung"

    # theo từng host: 1 bản copy input, và workdir không chứa bản copy
    per_host: dict[str, list[dict]] = {}
    for jid in job_ids:
        led = _ledger(cfg["state"], jid)
        per_host.setdefault(str(led.get("host") or ""), []).append(led)

    assert per_host, "không có job nào có host"

    for host, leds in per_host.items():
        assert host, "job không có host"
        layout_root = _layout_root_of(leds[0])
        keys = {
            str((led.get("_roff") or {}).get("input_shared_key") or "")
            for led in leds
        }
        keys.discard("")
        assert len(keys) == 1, (
            f"host {host} có {len(keys)} kho input cho {len(leds)} job "
            f"({sorted(keys)}) — các job không dùng chung 1 bản copy"
        )
        key = keys.pop()

        rc2, size_out, err2 = ssh_out(
            host,
            f"du -sb {layout_root}/inputs/{key} 2>/dev/null | cut -f1")
        assert rc2 == 0, f"không đọc được kho input trên {host}: {err2}"
        stored = int(size_out.split()[0]) if size_out.strip() else 0
        assert stored >= total_bytes * 0.9, (
            f"host {host} chỉ giữ {stored} byte, input gốc {total_bytes} byte — "
            f"input không nằm trên host"
        )
        assert stored <= total_bytes * 1.35 + 64 * 1024, (
            f"host {host} giữ {stored} byte cho {len(leds)} job — phải là 1 bản copy "
            f"dùng chung, không phải {len(leds)} bản"
        )

        # workdir của từng job không được chứa bản copy input
        for led in leds:
            opts = (led.get("_roff") or {})
            workdir = str(opts.get("workdir") or "")
            if not workdir:
                continue
            rc3, wd_out, _ = ssh_out(host, f"du -sb {workdir} 2>/dev/null | cut -f1")
            wd_size = int(wd_out.split()[0]) if wd_out.strip() else 0
            assert wd_size < total_bytes * 0.5, (
                f"workdir {workdir} trên {host} chứa {wd_size} byte — input bị "
                f"nhân bản vào từng job"
            )


# ------------------------------------------------------------------ S24
def test_s24_failover_keeps_item_and_workdir(fleet, cfg, tmp_path):
    """S24: job chết trên host A ⇒ chạy lại trên host B, item và thư mục đúng.

    Cách chạy: job ``--each`` (2 item) ghim host A; giết **process group của
    chính job đó** trên A (dùng pid đã ghi trong ledger) ⇒ watcher phải
    failover sang host B.

    Đạt khi:
      * ledger có event ``failover``;
      * host cuối cùng khác host đầu;
      * exit 0 và **mọi** output về đúng nội dung;
      * trên host mới, mỗi job có **thư mục làm việc riêng** (giữ hậu tố
        ``part-N``) — không được gộp các item của lô vào chung 1 thư mục;
      * ``$ROFF_ITEM`` trên host mới tồn tại (job không chạy với path của host cũ).
    """
    alive = live_muses()
    if len(alive) < 2:
        pytest.skip("cần ≥ 2 máy để thử failover")

    src = tmp_path / "data"
    src.mkdir()
    payloads = {}
    for i in range(2):
        data = bytes([9 - i]) * (512 * 1024)
        (src / f"item{i}.bin").write_bytes(data)
        payloads[f"item{i}.bin"] = hashlib.sha256(data).hexdigest()

    out = tmp_path / "out"
    used_hosts: list[str] = []
    rc = obj = err = None

    # Thử từng máy sống làm "host A" cho tới khi `roff do` thành công.
    for alias in alive:
        # 2 job trên **cùng** 1 host ⇒ cần ≥ 2 slot cho host đó, nếu không
        # job thứ 2 phải xếp hàng (vẫn đúng, nhưng chậm và dễ đụng flap).
        cfg["path"].write_text(
            "[fleet]\n"
            "include = muse-*\n"
            "probe_cache_s = 0\n"
            f"[host.{alias}]\n"
            "slots = 4\n"
        )
        used_hosts.append(alias)
        rc, obj, err = roff_json([
            "do",
            "--each", str(src / "*.bin"),
            "--in", str(src),
            "--host", alias,
            "--out", str(out),
            "--cmd", 'sleep 40; cp "$ROFF_ITEM" "$ROFF_OUT/$(basename "$ROFF_ITEM")"',
        ], timeout=600, env=cfg["env"])
        if rc == 0:
            break
        reason = str(obj.get("reason") or "")
        if reason not in HOST_DEATH_REASONS:
            break
        time.sleep(5)

    assert rc == 0, f"roff do thất bại: rc={rc} obj={obj} err={err[-400:] if err else ''}"
    alias = used_hosts[-1]
    job_ids = [j for j in (obj.get("jobs") or [obj.get("job")]) if j]
    assert len(job_ids) == 2, f"phải tạo 2 job: {job_ids}"

    # Chờ job thực sự chạy (có pid trong ledger)
    pids: list[int] = []
    deadline = time.time() + 180
    while time.time() < deadline and not pids:
        for jid in job_ids:
            led = _ledger(cfg["state"], jid)
            for att in led.get("attempts") or []:
                if att.get("pid"):
                    pids.append(int(att["pid"]))
        if not pids:
            time.sleep(3)
    assert pids, "job chưa ghi nhận pid ⇒ không giết được để thử failover"

    # Giết process group của chính job (không đụng tiến trình khác của user)
    for pid in set(pids):
        ssh_out(alias, f"kill -TERM -{pid} 2>/dev/null || true")
    time.sleep(10)

    results = _wait_all(cfg["state"], job_ids, timeout=900, env=cfg["env"])

    failover_hosts: dict[str, str] = {}
    for jid in job_ids:
        led = _ledger(cfg["state"], jid)
        events = [e.get("event") for e in ((led.get("_roff") or {}).get("events") or [])]
        assert "failover" in events, f"job {jid} không failover: {events}"
        hosts = [a.get("host") for a in (led.get("attempts") or [])]
        assert len(hosts) >= 2 and hosts[-1] != hosts[0], (
            f"job {jid} không chạy lại trên host khác: {hosts}"
        )
        failover_hosts[jid] = str(hosts[-1])
        assert results[jid].get("exit") == 0, (
            f"job {jid} sau failover exit={results[jid].get('exit')}")

    # output đúng nội dung
    for name, digest in payloads.items():
        path = out / name
        assert path.exists(), f"thiếu output {name}: {sorted(p.name for p in out.glob('*'))}"
        assert _sha256(path) == digest, f"output {name} sai nội dung"

    # trên host mới: mỗi job một thư mục riêng (giữ hậu tố part-N)
    by_host: dict[str, list[dict]] = {}
    for jid in job_ids:
        led = _ledger(cfg["state"], jid)
        by_host.setdefault(failover_hosts[jid], []).append(led)
    for host, leds in by_host.items():
        project = str(leds[0].get("project") or "")
        layout_root = _layout_root_of(leds[0])
        rc2, dirs, err2 = ssh_out(
            host,
            f"ls -d {layout_root}/projects/{project}/part-* 2>/dev/null | wc -l",
        )
        assert rc2 == 0, f"không liệt kê được workdir trên {host}: {err2}"
        count = int(dirs.strip() or 0)
        assert count >= len(leds), (
            f"host {host} chỉ có {count} thư mục part-* cho {len(leds)} job — "
            f"các item của lô bị gộp chung thư mục"
        )
