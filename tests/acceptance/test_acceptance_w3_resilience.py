"""Nghiệm thu độ bền của hàng đợi slot & dọn dẹp — S25, S26, S27, S28.

Chạy:  pytest tests/acceptance/test_acceptance_w3_resilience.py -q -m acceptance

Đây là phép thử cho những lỗi **đã xảy ra thật** trên fleet `muse-*`:

* **S25** — slot **rò** (watcher chết ⇒ không ai trả slot) **không được** kẹt host
  vĩnh viễn. Đây là lỗi nguy hiểm nhất của wave W2: `request_slot` ghi ticket
  **vĩnh viễn**, `try_acquire` chỉ cấp slot cho **đầu hàng** ⇒ một ticket mồ
  côi ở đầu hàng làm **mọi** job sau của host đó kẹt `queued` mãi mãi.
* **S26** — job xếp hàng **tự chạy** khi slot trống (hàng đợi FIFO thật sự,
  không phải chỉ là "trả về exit 0 rồi quên").
* **S27** — file state **không đọc được** của user khác (0600 / 0700).
* **S28** — `roff gc` dọn kho input **phía host**, nhưng **không** dọn kho mà
  job đang chạy còn dùng.

**Fleet Muse hay "flap"** (tunnel rớt giữa chừng). Mọi test ở đây vì vậy
phải *kiểm tra liveness ngay trước lúc dùng* và **thử lại trên máy khác**
khi lỗi là do host chết — không phải do logic của `roff`.
"""
from __future__ import annotations

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


# ------------------------------------------------------------------ helpers
def roff_json(args, timeout=300, env=None):
    e = dict(os.environ)
    if env:
        e.update(env)
    p = subprocess.run([sys.executable, str(LAUNCHER)] + list(args),
                       capture_output=True, text=True, timeout=timeout, env=e)
    lines = [ln for ln in p.stdout.strip().splitlines() if ln.strip()]
    try:
        obj = json.loads(lines[-1]) if lines else {}
    except ValueError:
        obj = {}
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


def ledger(state_dir: Path, job_id: str) -> dict:
    return json.loads((state_dir / "jobs" / f"{job_id}.json").read_text())


def options_of(state_dir: Path, job_id: str) -> dict:
    return dict(ledger(state_dir, job_id).get("_roff") or {})


def layout_root_of(led: dict) -> str:
    out_remote = str(led.get("out_remote") or "")
    idx = out_remote.rfind("/jobs/")
    assert idx > 0, f"out_remote lạ: {out_remote!r}"
    return out_remote[:idx]


def wait_state(state_dir: Path, job_id: str, states, timeout=120) -> str:
    """Chờ job đạt một trong ``states``; trả về state cuối cùng."""
    want = {states} if isinstance(states, str) else set(states)
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            led = ledger(state_dir, job_id)
            last = str(led.get("state") or "")
            if last in want:
                return last
        except (OSError, ValueError):
            pass
        time.sleep(2)
    return last


def watcher_pids(state_dir: Path, job_id: str) -> list[int]:
    """Pid của watcher (tiến trình **của roff**) đang giám sát ``job_id``.

    Ưu tiên pid ghi trong ledger; nếu không có thì dò ``ps``. Cả hai đều là
    tiến trình **của chính roff** — giết chúng không đụng tới dịch vụ khác
    của người dùng trên máy Muse.
    """
    pids: list[int] = []
    try:
        opts = options_of(state_dir, job_id)
    except (OSError, ValueError):
        opts = {}
    for key in ("watcher_pid", "watch_pid", "watcher_pids"):
        val = opts.get(key)
        if isinstance(val, int) and val > 0:
            pids.append(val)
        elif isinstance(val, list):
            pids.extend(int(v) for v in val if isinstance(v, int) and v > 0)
    if pids:
        return sorted(set(pids))
    try:
        r = subprocess.run(["pgrep", "-f", f"__watch {job_id}"],
                           capture_output=True, text=True, timeout=20)
        return sorted({int(x) for x in r.stdout.split() if x.strip().isdigit()})
    except (subprocess.SubprocessError, ValueError):
        return []


def kill_local(pids) -> None:
    for pid in pids:
        try:
            os.kill(int(pid), 9)
        except (ProcessLookupError, PermissionError, ValueError):
            pass


def remote_pids(state_dir: Path, job_id: str) -> list[int]:
    out = []
    for att in ledger(state_dir, job_id).get("attempts") or []:
        pid = att.get("pid")
        if pid:
            out.append(int(pid))
    return out


# ------------------------------------------------------------------ fixtures
@pytest.fixture(scope="module")
def fleet():
    live = live_muses()
    if not live:
        pytest.skip("không có máy muse nào sống — bật lại tunnel trước khi nghiệm thu")
    return live


@pytest.fixture
def cfg(tmp_path):
    """Config riêng cho test: fleet ``muse-*``, probe **không** cache."""
    live = live_muses()
    if not live:
        pytest.skip("không có máy muse nào sống")
    path = tmp_path / "config.ini"
    path.write_text("[fleet]\ninclude = muse-*\nprobe_cache_s = 0\n")
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


def pin_host(cfg, alias: str, slots: int = 1) -> None:
    """Ghim 1 host với đúng số slot (để kiểm soát hàng đợi)."""
    cfg["path"].write_text(
        "[fleet]\n"
        "include = muse-*\n"
        "probe_cache_s = 0\n"
        f"[host.{alias}]\n"
        f"slots = {slots}\n"
    )


@pytest.fixture
def local_cfg(tmp_path):
    """Config tối giản cho test **không cần fleet** (mọi lỗi phải bị chặn
    trước khi chạm mạng)."""
    path = tmp_path / "config.ini"
    path.write_text("[fleet]\ninclude = muse-*\nprobe_cache_s = 0\n")
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


def do_on(alias: str, args, cfg, timeout=600):
    return roff_json(["do", "--host", alias] + list(args),
                     timeout=timeout, env=cfg["env"])


# ------------------------------------------------------------------ S25
def test_s25_leaked_slot_does_not_block_host_forever(fleet, cfg, tmp_path):
    """S25: watcher chết ⇒ slot rò ⇒ host **không** được kẹt vĩnh viễn.

    Kịch bản:
      1. host có ``slots = 1``; ``roff do`` ⇒ job1 ``launched`` (giữ slot);
      2. ``roff do`` lần 2 ⇒ job2 ``queued`` — chứng minh slot **thật sự**
         đang bị giữ (hàng đợi FIFO hoạt động);
      3. huỷ job2 (dọn đầu hàng đợi);
      4. **giết watcher** của job1 — đây là "agent chết" / "máy local restart";
      5. giết process group remote của job1;
      6. ``roff do`` lần 3 ⇒ **phải** ``launched``.

    Với code cũ (không có reconcile), bước 6 trả ``queued`` **vĩnh viễn**:
    lease của job1 không ai trả, ``try_acquire`` thấy hết capacity ⇒ treo.

    Đạt khi: job3 có ``state == "launched"`` (không phải ``queued``), và job2
    vẫn ``queued`` đúng như FIFO (không được âm thầm chạy).
    """
    alias = fleet[0]
    pin_host(cfg, alias, slots=1)

    # 1. job1 giữ slot
    rc, obj1, err = do_on(alias, [
        "--cmd", "sleep 600",
        "--name", "s25-holder",
    ], cfg)
    assert rc == 0, f"job1 không tạo được: rc={rc} obj={obj1} err={err[-300:]}"
    job1 = obj1.get("job")
    assert job1, f"job1 không có id: {obj1}"
    assert obj1.get("state") == "launched", (
        f"job1 phải launched (giữ slot), thực tế state={obj1.get('state')!r} "
        f"({obj1})")

    # 2. job2 phải xếp hàng (slot đang bận)
    rc, obj2, err = do_on(alias, [
        "--cmd", "sleep 5",
        "--name", "s25-queued",
    ], cfg)
    assert rc == 0, f"job2 không tạo được: rc={rc} obj={obj2} err={err[-300:]}"
    job2 = obj2.get("job")
    assert job2, f"job2 không có id: {obj2}"
    assert obj2.get("state") == "queued", (
        f"job2 phải queued (host hết slot), thực tế state={obj2.get('state')!r} "
        f"({obj2})")

    # 3. huỷ job2 ⇒ dọn đầu hàng đợi (nếu không, job3 sẽ tranh chỗ với job2
    #    và phép thử mất ý nghĩa)
    rc, objc, err = roff_json(["cancel", job2], timeout=120, env=cfg["env"])
    assert rc in (0, 130), f"cancel job2 lỗi: rc={rc} obj={objc} err={err[-200:]}"
    wait_state(cfg["state"], job2, ("cancelled", "exited"), timeout=60)

    # 4. giết watcher của job1 (tiến trình của roff, không phải dịch vụ user)
    pids = watcher_pids(cfg["state"], job1)
    deadline = time.time() + 60
    while not pids and time.time() < deadline:
        time.sleep(2)
        pids = watcher_pids(cfg["state"], job1)
    assert pids, (
        "không tìm được watcher của job1 (ledger không có watcher_pid và ps "
        "không thấy) — không giết được để thử slot rò")
    kill_local(pids)
    time.sleep(3)

    # 5. giết process group remote của job1
    rmpids = remote_pids(cfg["state"], job1)
    assert rmpids, "job1 chưa ghi pid remote ⇒ không giết được"
    for pid in set(rmpids):
        ssh_out(alias, f"kill -TERM -{pid} 2>/dev/null || true")
    time.sleep(5)

    # 6. job3 phải chạy được — đây là phép thử quyết định
    rc, obj3, err = do_on(alias, [
        "--cmd", "echo s25-ok > \"$ROFF_OUT/ok.txt\"",
        "--out", str(tmp_path / "out"),
        "--name", "s25-after-leak",
    ], cfg)
    assert rc == 0, f"job3 không tạo được: rc={rc} obj={obj3} err={err[-300:]}"
    assert obj3.get("state") == "launched", (
        f"host bị kẹt vĩnh viễn bởi slot rò: job3 state={obj3.get('state')!r} "
        f"({obj3}) — reconcile phải thu hồi slot của watcher đã chết")
    job3 = obj3.get("job")
    assert job3

    # job3 phải thật sự chạy xong
    rc, res, err = roff_json(["wait", job3, "--timeout", "300"],
                             timeout=360, env=cfg["env"])
    assert rc == 0, f"job3 không hoàn tất: rc={rc} res={res} err={err[-300:]}"
    assert (tmp_path / "out" / "ok.txt").exists(), "job3 không sinh output"


# ------------------------------------------------------------------ S26
def test_s26_queued_job_auto_launches_when_slot_frees(fleet, cfg, tmp_path):
    """S26: job xếp hàng **tự chạy** khi slot trống.

    ``queued`` không phải "trả về exit 0 rồi quên" — watcher phải tự xin
    slot, launch, và job phải **thoát với exit 0** mà không ai canh nó.

    Đạt khi: job1 (giữ slot) và job2 (xếp hàng) **cùng** về exit 0, và
    output của job2 có mặt trên máy local.
    """
    alias = fleet[0]
    pin_host(cfg, alias, slots=1)

    out = tmp_path / "out"
    rc, obj1, err = do_on(alias, [
        "--cmd", "sleep 25",
        "--name", "s26-holder",
    ], cfg)
    assert rc == 0, f"job1 lỗi: rc={rc} obj={obj1} err={err[-300:]}"
    job1 = obj1.get("job")
    assert obj1.get("state") == "launched", f"job1: {obj1}"

    rc, obj2, err = do_on(alias, [
        "--cmd", "echo s26 > \"$ROFF_OUT/s26.txt\"",
        "--out", str(out),
        "--name", "s26-queued",
    ], cfg)
    assert rc == 0, f"job2 lỗi: rc={rc} obj={obj2} err={err[-300:]}"
    job2 = obj2.get("job")
    assert obj2.get("state") == "queued", f"job2 phải queued: {obj2}"

    for jid in (job1, job2):
        rc, res, err = roff_json(["wait", jid, "--timeout", "600"],
                                 timeout=660, env=cfg["env"])
        assert rc == 0, (
            f"job {jid} không về exit 0: rc={rc} res={res} err={err[-300:]} "
            f"— job xếp hàng phải tự chạy khi slot trống")
        assert res.get("exit") == 0, f"job {jid} exit={res.get('exit')}: {res}"

    assert (out / "s26.txt").exists(), (
        f"job2 chạy xong nhưng không có output: {sorted(p.name for p in out.glob('*'))}")


# ------------------------------------------------------------------ S27
def test_s27_state_files_are_private(fleet, cfg, tmp_path):
    """S27: file state **không** đọc được của user khác.

    Đo thật trên máy người dùng: ``audit.jsonl`` / ``probes.json`` /
    ``slots.json`` bị tạo với mode **0664** ⇒ mọi user trên máy đọc được
    log lệnh (có thể chứa đường dẫn dự án).

    Đạt khi: mọi file state đều mode **0600**, mọi thư mục state **0700**.
    """
    alias = fleet[0]
    pin_host(cfg, alias, slots=1)

    rc, _, err = roff_json(["do", "--host", alias, "--cmd", "true",
                             "--name", "s27-perm"], timeout=300, env=cfg["env"])
    assert rc == 0, f"roff do lỗi: rc={rc} err={err[-300:]}"

    state = cfg["state"]
    assert state.exists()

    # thư mục state: 0700
    mode = state.stat().st_mode & 0o777
    assert mode == 0o700, f"thư mục state mode {oct(mode)} — phải là 0o700"

    jobs_dir = state / "jobs"
    if jobs_dir.exists():
        mode = jobs_dir.stat().st_mode & 0o777
        assert mode == 0o700, f"thư mục jobs/ mode {oct(mode)} — phải là 0o700"

    # file state: 0600 — kiểm tra mọi file trừ file tạm (.*.tmp.*)
    offenders = []
    for path in sorted(state.rglob("*")):
        if path.is_dir():
            m = path.stat().st_mode & 0o777
            if m & 0o077:
                offenders.append((str(path.relative_to(state)), oct(m), "dir"))
            continue
        if ".tmp." in path.name:
            continue
        m = path.stat().st_mode & 0o777
        if m & 0o077:
            offenders.append((str(path.relative_to(state)), oct(m), "file"))
    assert not offenders, (
        "file/thư mục state đọc được của user khác: "
        + ", ".join(f"{n} ({m})" for n, m, _ in offenders))


# ------------------------------------------------------------------ S28
def test_s28_gc_cleans_host_input_store_but_not_running_jobs(fleet, cfg, tmp_path):
    """S28: `roff gc` dọn kho input **phía host**, nhưng **không** dọn kho
    mà job đang chạy còn dùng.

    Đo thật: kho input trên host (``<layout_root>/inputs/<key>/``) trước đây
    **không có đường dọn** ⇒ phình vô hạn theo thời gian.

    Đạt khi:
      * sau khi job chạy xong, ``roff gc`` xoá được kho input của job đó;
      * khi job **đang chạy**, ``roff gc`` **không** được xoá kho đó.
    """
    alias = fleet[0]
    pin_host(cfg, alias, slots=1)

    src = tmp_path / "data"
    src.mkdir()
    (src / "payload.bin").write_bytes(b"s28" * 4096)
    out = tmp_path / "out"

    rc, obj, err = do_on(alias, [
        "--in", str(src),
        "--cmd", "sleep 45; cp \"$ROFF_IN/payload.bin\" \"$ROFF_OUT/payload.bin\"",
        "--out", str(out),
        "--name", "s28-gc",
    ], cfg)
    assert rc == 0, f"roff do lỗi: rc={rc} obj={obj} err={err[-300:]}"
    job = obj.get("job")
    assert job

    led = ledger(cfg["state"], job)
    root = layout_root_of(led)
    key = str((led.get("_roff") or {}).get("input_shared_key") or "")
    assert key, f"job không ghi input_shared_key: {led.get('_roff')}"
    store = f"{root}/inputs/{key}"

    # kho input phải tồn tại trên host
    rc2, out2, err2 = ssh_out(alias, f"test -d {store} && echo yes || echo no")
    assert out2.strip() == "yes", (
        f"không thấy kho input {store} trên {alias}: {err2[-200:]}")

    # gc KHI job đang chạy ⇒ không được xoá
    rc3, _, err3 = roff_json(["gc"], timeout=600, env=cfg["env"])
    assert rc3 == 0, f"roff gc lỗi: rc={rc3} err={err3[-300:]}"
    rc4, out4, _ = ssh_out(alias, f"test -d {store} && echo yes || echo no")
    assert out4.strip() == "yes", (
        f"roff gc đã xoá kho input của job **đang chạy** ({store}) — "
        f"không được xoá khi job còn dùng")

    # chờ job xong rồi gc ⇒ phải dọn được
    rc5, res, err5 = roff_json(["wait", job, "--timeout", "600"],
                                timeout=660, env=cfg["env"])
    assert rc5 == 0, f"job không hoàn tất: rc={rc5} res={res} err={err5[-300:]}"
    assert res.get("exit") == 0, f"job exit={res.get('exit')}: {res}"

    rc6, _, err6 = roff_json(["gc"], timeout=600, env=cfg["env"])
    assert rc6 == 0, f"roff gc lần 2 lỗi: rc={rc6} err={err6[-300:]}"
    rc7, out7, _ = ssh_out(alias, f"test -d {store} && echo yes || echo no")
    assert out7.strip() == "no", (
        f"roff gc không dọn được kho input đã hết dùng ({store}) trên {alias} "
        f"— kho input phía host phình vô hạn")


# ------------------------------------------------------------------ S29
def test_s29_error_exit_codes_are_not_confused(local_cfg, tmp_path):
    """S29: exit code phải **phân biệt đúng** nguyên nhân.

    Hợp đồng (`docs/SPEC-CLI.md` §0, `docs/API-CONTRACT.md` §2):

    * **2** — input sai, chưa có gì chạy (path không tồn tại, ``--timeout 0``,
      job id không tồn tại);
    * **3** — config hỏng;
    * **255** — **không với được host**, và **bắt buộc** có ``detail`` (lỗi ssh
      gốc) để người đọc biết là key bị từ chối hay host chết.

    Lỗi 255 cho một việc chỉ cần sửa ở máy local là **nguy hiểm**: agent tưởng
    máy remote chết rồi chạy lại ở local ⇒ đúng cái điều skill sinh ra để tránh.

    Không cần fleet — mọi lỗi ở đây phải bị chặn **trước** khi chạm mạng.
    """
    env = local_cfg["env"]

    # --- input sai ⇒ 2 ---
    rc, obj, _ = roff_json(["do", "--cmd", "true", "--in",
                            str(tmp_path / "khong-ton-tai")], timeout=120, env=env)
    assert rc == 2, f"input không tồn tại phải là exit 2, thực tế rc={rc}: {obj}"
    assert obj.get("ok") is False
    assert obj.get("reason") == "input_not_found", f"reason sai: {obj}"
    assert obj.get("error"), f"phải có error giải thích: {obj}"

    rc, obj, _ = roff_json(["do", "--cmd", "true", "--timeout", "0"],
                           timeout=120, env=env)
    assert rc == 2, f"--timeout 0 phải là exit 2, thực tế rc={rc}: {obj}"
    assert obj.get("ok") is False

    rc, obj, _ = roff_json(["do", "--cmd", "true", "--in", "job:khong-ton-tai-xyz"],
                           timeout=120, env=env)
    assert rc == 2, f"job id không tồn tại phải là exit 2, thực tế rc={rc}: {obj}"
    assert obj.get("ok") is False

    rc, obj, _ = roff_json(["status", "khong-ton-tai-xyz"], timeout=60, env=env)
    assert rc == 2, f"status job lạ phải là exit 2, thực tế rc={rc}: {obj}"

    # --- config hỏng ⇒ 3 ---
    bad_cfg = tmp_path / "bad-config.ini"
    bad_cfg.write_text("[fleet\ninclude = muse-*\n")   # thiếu dấu ]
    env_bad = dict(env)
    env_bad["ROFF_CONFIG"] = str(bad_cfg)
    rc, obj, _ = roff_json(["hosts", "--json"], timeout=120, env=env_bad)
    assert rc == 3, f"config hỏng phải là exit 3, thực tế rc={rc}: {obj}"
    assert obj.get("ok") is False

    # --- host không với được ⇒ 255 **và** có detail ---
    rc, obj, _ = roff_json(["do", "--cmd", "true", "--host",
                            "zzz-khong-phai-host-that-xyz"], timeout=180, env=env)
    assert rc == 255, f"host không với được phải là exit 255, thực tế rc={rc}: {obj}"
    assert obj.get("ok") is False
    assert obj.get("detail"), (
        f"exit 255 **bắt buộc** phải có detail (lỗi ssh gốc): {obj}")
    assert obj.get("reason") in ("unknown_host", "host_unreachable"), (
        f"reason phải nói rõ host không tồn tại hay không với được: {obj}")


# ------------------------------------------------------------------ S30
def test_s30_job_routed_to_host_that_has_the_tool(fleet, cfg, tmp_path):
    """S30: job có ``--need`` phải chạy trên máy **có** đúng công cụ.

    Yêu cầu của người dùng: *"task sẽ phù hợp hơn với công việc mà máy kia có"*.

    Đạt khi:
    * ``--need <tool không tồn tại, không có script setup>`` ⇒ mọi host bị loại
      **với lý do nói rõ**, exit 10 (không phải exit 255, không phải chạy local);
    * ``--need ffmpeg`` ⇒ host được chọn **thật sự** có `ffmpeg` (kiểm bằng
      `command -v` trên chính máy đó, không tin lời khai của probe).
    """
    env = cfg["env"]

    # --- tool không có ở đâu cả ⇒ loại host, nói rõ lý do ---
    rc, obj, err = roff_json(["do", "--cmd", "true", "--need",
                              "zzz-cong-cu-khong-ton-tai"], timeout=300, env=env)
    assert rc == 10, (
        f"tool không tồn tại ⇒ phải exit 10 (no_host), thực tế rc={rc}: {obj}")
    rejected = obj.get("rejected") or {}
    assert rejected, f"phải nói lý do loại từng host: {obj}"
    assert any("thiếu tool" in str(v) for v in rejected.values()), (
        f"lý do loại phải nói 'thiếu tool': {rejected}")

    # --- tool có sẵn ⇒ host được chọn phải thật sự có nó ---
    alias = fleet[0]
    pin_host(cfg, alias, slots=1)
    rc, obj, err = do_on(alias, [
        "--cmd", "true",
        "--need", "ffmpeg",
        "--name", "s30-need",
    ], cfg)
    assert rc == 0, f"roff do lỗi: rc={rc} obj={obj} err={err[-300:]}"
    host = obj.get("host")
    assert host, f"không có host trong JSON: {obj}"
    rc2, out2, err2 = ssh_out(host, "command -v ffmpeg >/dev/null && echo yes || echo no")
    assert out2.strip() == "yes", (
        f"host {host} được chọn cho job --need ffmpeg nhưng **không có** ffmpeg "
        f"({err2[-200:]}) — scheduler đã giao task sai máy")
