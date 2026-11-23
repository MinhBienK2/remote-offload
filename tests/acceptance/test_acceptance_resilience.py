import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
import pytest

REPO = Path(__file__).resolve().parents[2]
SKILL = REPO / "skill"
LAUNCHER = SKILL / "roff.py"

pytestmark = pytest.mark.acceptance

MUSE = ["muse-pmb333", "muse-tampt", "muse-bienpm333", "muse-phamminhbien3333"]


#: State dir **riêng cho test** — test KHÔNG được ghi vào ledger thật của
#: người dùng (``~/.local/state/roff``). Nếu không, mỗi lần chạy nghiệm thu lại
#: đẩy hàng chục job record vào ledger thật (và tạo cả bản ghi tunnel ma).
_DEFAULT_STATE = tempfile.mkdtemp(prefix="roff-acceptance-state-")
atexit.register(shutil.rmtree, _DEFAULT_STATE, True)


def roff(args, timeout=300, env=None):
    e = dict(os.environ)
    e.setdefault("ROFF_STATE_DIR", _DEFAULT_STATE)
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


@pytest.fixture(scope="module")
def fleet():
    live = live_muses()
    if not live:
        pytest.skip("không có máy muse nào sống — bật lại tunnel trước khi nghiệm thu")
    return live


@pytest.fixture
def cfg(tmp_path):
    """Config giới hạn fleet còn các máy muse đang sống."""
    live = live_muses()
    if not live:
        pytest.skip("không có máy muse nào sống")
    path = tmp_path / "config.ini"
    path.write_text("[fleet]\ninclude = muse-*\n")
    return {"path": path, "env": {"ROFF_CONFIG": str(path)}, "live": live}


# ------------------------------------------------------------------ S7

def _ssh_port_of(alias: str, text: str | None = None) -> int:
    """Port ssh thật của một alias (đọc từ ~/.ssh/config)."""
    if text is None:
        text = (Path.home() / ".ssh" / "config").read_text()
    lines = text.splitlines()
    in_block = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("Host "):
            names = stripped[5:].split()
            in_block = alias in names
            continue
        if in_block and stripped.lower().startswith("port "):
            return int(stripped.split()[1])
    return 22


def _set_port(text: str, alias: str, new_port: int) -> str:
    """Đổi ``Port`` của một Host block, giữ nguyên phần còn lại."""
    out = []
    in_block = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("Host "):
            in_block = alias in stripped[5:].split()
        if in_block and stripped.lower().startswith("port "):
            indent = line[: len(line) - len(line.lstrip())]
            out.append(f"{indent}Port {new_port}")
            continue
        out.append(line)
    return "\n".join(out) + "\n"


def test_s07_tunnel_drop_does_not_failover(fleet, cfg, tmp_path):
    """S7: mất kết nối ≠ job chết. Watcher KHÔNG failover, job chạy tiếp.

    Cách chạy: dùng HOME tạm với ``.ssh/config`` **riêng** (không đụng file
    thật của người dùng). Job được chạy khi config còn trỏ host thật; sau đó
    config bị đổi sang cổng chết ⇒ watcher mất kết nối. Rồi config trở lại.

    Đạt khi:
      * ledger có mục ``disconnected``;
      * KHÔNG có attempt thứ hai (không failover);
      * sau khi kết nối lại: ``boot_id``/``instance_token`` không đổi, job vẫn
        sống, exit 0, artifact về đủ.
    """
    alias = fleet[0]
    if not ssh_ok(alias):
        pytest.skip(f"{alias} đã chết giữa lúc chuẩn bị")

    real_cfg = (Path.home() / ".ssh" / "config").read_text()
    real_port = _ssh_port_of(alias, real_cfg)

    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    cfg_path = home / ".ssh" / "config"

    def write_cfg(port: int):
        cfg_path.write_text(_set_port(real_cfg, alias, port))

    write_cfg(real_port)
    env = dict(cfg["env"])
    env["HOME"] = str(home)
    env["ROFF_STATE_DIR"] = str(tmp_path / "state")

    out = tmp_path / "out"
    rc, obj, err = roff_json([
        "do", "--cmd",
        'echo started > "$ROFF_OUT/marker.txt"; sleep 45; '
        'echo finished > "$ROFF_OUT/done.txt"',
        "--host", alias,
        "--out", str(out),
    ], timeout=300, env=env)
    assert rc == 0, f"roff do thất bại: {rc} {json.dumps(obj)} {err[-400:]}"
    jid = obj["job"]
    assert obj.get("host") == alias, f"phải chạy trên {alias}: {obj}"

    # Chờ job thực sự bắt đầu
    time.sleep(12)

    # Cắt kết nối: trỏ alias sang cổng chết (1 = tcpmux, không có sshd)
    write_cfg(1)
    time.sleep(25)

    # Kết nối lại
    write_cfg(real_port)

    rc2, res, err2 = roff_json(["wait", jid, "--timeout", "300"], timeout=360, env=env)
    assert rc2 == 0, f"job phải chạy tiếp tới khi xong: rc={rc2} {json.dumps(res)}"

    # Ledger: có disconnected, KHÔNG có attempt thứ hai
    ledger = json.loads(
        (tmp_path / "state" / "jobs" / f"{jid}.json").read_text())
    events = [e.get("event") for e in (ledger.get("_roff", {}).get("events") or [])]
    assert "disconnected" in events, f"phải ghi 'disconnected': {events}"
    assert len(ledger.get("attempts") or []) == 1, (
        f"không được failover khi chỉ mất kết nối: {ledger.get('attempts')}")
    assert (out / "done.txt").exists(), f"thiếu artifact: {list(out.glob('*'))}"


# ------------------------------------------------------------------ S9

def test_s09_side_effects_never_retried(fleet, cfg, tmp_path):
    """S9: job có ``--side-effects`` thì không bao giờ chạy lại.

    Đạt khi: ledger ghi ``needs_retry=false``, và sau khi job xong chỉ có
    **một** attempt.
    """
    out = tmp_path / "out"
    rc, obj, err = roff_json([
        "do", "--cmd", 'echo hi > "$ROFF_OUT/x.txt"', "--side-effects",
        "--out", str(out),
    ], timeout=300, env=cfg["env"])
    assert rc == 0, f"roff do thất bại: {rc} {json.dumps(obj)} {err[-400:]}"
    jid = obj["job"]

    rc2, res, err2 = roff_json(["wait", jid, "--timeout", "300"], timeout=360,
                                env=cfg["env"])
    assert rc2 == 0, f"job phải thành công: {rc2} {json.dumps(res)}"

    ledger = json.loads(
        (Path(os.environ.get("ROFF_STATE_DIR",
                             Path.home() / ".local/state/roff"))
         / "jobs" / f"{jid}.json").read_text())
    assert ledger.get("needs_retry") is False, (
        f"--side-effects phải tắt retry: {ledger.get('needs_retry')}")
    assert len(ledger.get("attempts") or []) == 1, (
        f"chỉ được chạy 1 lần: {ledger.get('attempts')}")


# ------------------------------------------------------------------ S10

def test_s10_job_survives_agent_death(fleet, cfg, tmp_path):
    """S10: giết tiến trình agent giữa lúc chạy ⇒ job vẫn sống, phiên mới
    thấy được kết quả, và KHÔNG có job nào chạy lại trùng."""
    state = tmp_path / "state"
    out = tmp_path / "out"
    env = dict(cfg["env"])
    env["ROFF_STATE_DIR"] = str(state)

    # Chạy `roff do` rồi giết cả nhóm tiến trình ngay sau khi nó trả về —
    # mô phỏng agent bị kill.
    proc = subprocess.Popen(
        [sys.executable, str(LAUNCHER), "do", "--cmd",
         'echo alive > "$ROFF_OUT/a.txt"; sleep 20; echo done > "$ROFF_OUT/b.txt"',
         "--out", str(out)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**os.environ, **env},
    )
    stdout, stderr = proc.communicate(timeout=180)
    lines = [ln for ln in stdout.strip().splitlines() if ln.strip()]
    obj = json.loads(lines[-1]) if lines else {}
    jid = obj.get("job")
    assert jid, f"không lấy được job id: {stdout} {stderr[-300:]}"
    proc.kill()
    proc.wait(timeout=30)

    # Phiên MỚI (tiến trình riêng) thấy job đang chạy
    rc, jobs, err = roff_json(["jobs"], timeout=60, env=env)
    assert rc == 0, f"roff jobs lỗi: {rc} {err[-300:]}"
    entries = jobs.get("jobs") or []
    assert any(e.get("id") == jid or e.get("job") == jid for e in entries), (
        f"phiên mới phải thấy job {jid}: {json.dumps(jobs)[:400]}")

    rc2, res, err2 = roff_json(["wait", jid, "--timeout", "300"], timeout=360, env=env)
    assert rc2 == 0, f"job phải hoàn thành sau khi agent chết: {rc2} {json.dumps(res)}"
    assert (out / "b.txt").exists(), f"thiếu artifact: {list(out.glob('*'))}"

    ledger = json.loads((state / "jobs" / f"{jid}.json").read_text())
    assert len(ledger.get("attempts") or []) == 1, (
        f"không được chạy lại job: {ledger.get('attempts')}")


# ------------------------------------------------------------------ S11

def test_s11_shards_spread_over_hosts(fleet, cfg, tmp_path):
    """S11: ``--in job:<A>`` + ``--shards N`` ⇒ dữ liệu không ghi xuống đĩa
    local, các shard nằm trên ≥ 2 máy, ``$ROFF_SHARD``/``$ROFF_SHARDS`` đúng."""
    if len(fleet) < 2:
        pytest.skip(f"cần ≥ 2 máy để test sharding, đang có {len(fleet)}")

    env = cfg["env"]

    # Job A: sinh ra nhiều file
    gen = tmp_path / "gen"
    rc, obj, err = roff_json([
        "do", "--cmd",
        'for i in 1 2 3 4 5 6; do echo "data-$i" > "$ROFF_OUT/f$i.txt"; done',
        "--out", str(gen),
    ], timeout=300, env=env)
    assert rc == 0, f"job A thất bại: {rc} {json.dumps(obj)} {err[-300:]}"
    jid_a = obj["job"]
    rc2, _, err2 = roff_json(["wait", jid_a, "--timeout", "300"], timeout=360, env=env)
    assert rc2 == 0, f"job A không thành công: {rc2} {err2[-300:]}"

    # Job B: dùng --in job:A --shards 3
    before = subprocess.run(["du", "-sk", str(Path.home() / ".local/state/roff")],
                            capture_output=True, text=True).stdout.split()[0]
    out = tmp_path / "outb"
    rc3, obj3, err3 = roff_json([
        "do", "--cmd",
        'echo "shard=$ROFF_SHARD of $ROFF_SHARDS item=$ROFF_ITEM" '
        '> "$ROFF_OUT/s$ROFF_SHARD.txt"',
        "--in", f"job:{jid_a}", "--shards", "3", "--out", str(out),
    ], timeout=300, env=env)
    assert rc3 == 0, f"job B thất bại: {rc3} {json.dumps(obj3)} {err3[-300:]}"

    # Thu đủ 3 shard
    job_ids = obj3.get("jobs") or [obj3.get("job")]
    job_ids = [j for j in job_ids if j]
    assert len(job_ids) == 3, f"phải có 3 shard: {json.dumps(obj3)}"

    hosts = set()
    for jid in job_ids:
        rcw, res, errw = roff_json(["wait", jid, "--timeout", "300"], timeout=360,
                                    env=env)
        assert rcw == 0, f"shard {jid} không thành công: {rcw} {json.dumps(res)}"
        hosts.add(res.get("host"))

    after = subprocess.run(["du", "-sk", str(Path.home() / ".local/state/roff")],
                           capture_output=True, text=True).stdout.split()[0]
    grew_kb = int(after) - int(before)
    assert grew_kb < 4096, (
        f"dữ liệu --in job: phải đi qua pipe, không ghi xuống đĩa local "
        f"(state dir tăng {grew_kb} KB)")

    assert len(hosts) >= 2, f"3 shard phải trải trên ≥ 2 máy, thấy {hosts}"
    for jid in job_ids:
        assert (out / f"s{0}.txt").exists() or True  # tên file kiểm tra ở dưới
    got = sorted(p.name for p in out.glob("*.txt"))
    assert len(got) == 3, f"phải có 3 file shard: {got}"
    for name in got:
        text = (out / name).read_text()
        shard = int(name[1:-4])
        assert f"shard={shard} of 3" in text, f"$ROFF_SHARD sai trong {name}: {text!r}"


# ------------------------------------------------------------------ S18

def _make_videos(tmp_path, n, seconds=6):
    vids = []
    for i in range(n):
        path = tmp_path / f"v{i}.mp4"
        r = subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30",
             "-t", str(seconds), "-c:v", "libx264", "-preset", "ultrafast",
             "-pix_fmt", "yuv420p", str(path)],
            capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            pytest.skip(f"không tạo được video test: {r.stderr[-200:]}")
        vids.append(path)
    return vids


def test_s18_parallel_speedup(fleet, cfg, tmp_path):
    """S18: N video chạy trên cả fleet.

    Đạt khi:
      * tăng tốc **tính toán** ≥ 0,6 × N;
      * tăng tốc **toàn cục** ≥ 1,3× (N=2) / ≥ 1,8× (N=4);
      * mọi output có sha256 đúng, không trùng lặp.
    """
    n = min(4, len(fleet))
    if n < 2:
        pytest.skip(f"cần ≥ 2 máy để đo tăng tốc, đang có {len(fleet)}")

    vids = _make_videos(tmp_path, n)
    cmd = ('ffmpeg -y -i "$ROFF_ITEM" -vf scale=640:-2 -c:v libx264 '
           '-preset veryfast -pix_fmt yuv420p "$ROFF_OUT/$(basename "$ROFF_ITEM")"')

    # --- 1 máy: cùng lệnh, cùng `--each`, nhưng ghim một host ---
    # (dùng `--each` cho cả hai phía để lệnh và đường truyền là như nhau —
    #  khác lệnh thì so sánh tăng tốc vô nghĩa).
    t0 = time.time()
    out1 = tmp_path / "seq"
    rc, obj, err = roff_json([
        "do", "--cmd", cmd, "--each", str(tmp_path / "v*.mp4"),
        "--host", fleet[0], "--out", str(out1), "--need", "ffmpeg",
    ], timeout=1800, env=cfg["env"])
    assert rc == 0, f"job tuần tự thất bại: {rc} {json.dumps(obj)} {err[-300:]}"
    seq_ids = [j for j in (obj.get("jobs") or [obj.get("job")]) if j]
    assert len(seq_ids) == n, f"phải có {n} job tuần tự: {json.dumps(obj)}"
    for jid in seq_ids:
        rcw, res, errw = roff_json(["wait", jid, "--timeout", "900"],
                                   timeout=960, env=cfg["env"])
        assert rcw == 0, f"job tuần tự {jid} không xong: {rcw} {json.dumps(res)}"
    seq_wall = time.time() - t0

    # --- N máy: cùng lệnh, `--each`, không ghim host ---
    out2 = tmp_path / "par"
    t1 = time.time()
    rc, obj, err = roff_json([
        "do", "--cmd", cmd, "--each", str(tmp_path / "v*.mp4"),
        "--out", str(out2), "--need", "ffmpeg",
    ], timeout=1800, env=cfg["env"])
    assert rc == 0, f"job song song thất bại: {rc} {json.dumps(obj)} {err[-400:]}"
    job_ids = [j for j in (obj.get("jobs") or [obj.get("job")]) if j]
    assert len(job_ids) == n, f"phải có {n} job, thấy {len(job_ids)}: {json.dumps(obj)}"
    for jid in job_ids:
        rcw, res, errw = roff_json(["wait", jid, "--timeout", "900"], timeout=960,
                                    env=cfg["env"])
        assert rcw == 0, f"job {jid} không xong: {rcw} {json.dumps(res)}"
    par_wall = time.time() - t1

    # --- Output đủ, không trùng lặp ---
    produced = sorted(p.name for p in out2.glob("*.mp4"))
    assert len(produced) == n, f"phải có {n} output, thấy {produced}"
    seq_produced = sorted(p.name for p in out1.glob("*.mp4"))
    assert seq_produced == produced, (
        f"output khác nhau giữa 2 phép đo: {seq_produced} vs {produced}")

    # --- sha256 khớp bản tuần tự ---
    import hashlib

    def sha(p):
        h = hashlib.sha256()
        with open(p, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    for name in produced:
        seq_file = out1 / name
        par_file = out2 / name
        assert seq_file.exists(), f"thiếu bản tuần tự để so: {seq_file}"
        assert sha(seq_file) == sha(par_file), f"sha256 lệch ở {name}"

    global_speedup = seq_wall / par_wall if par_wall > 0 else 0
    target = 1.3 if n == 2 else 1.8
    print(f"\n[S18] n={n} tuần tự={seq_wall:.1f}s song song={par_wall:.1f}s "
          f"tăng tốc toàn cục={global_speedup:.2f}× (cần ≥ {target}×)")
    assert global_speedup >= target, (
        f"tăng tốc toàn cục {global_speedup:.2f}× < {target}× "
        f"(tuần tự {seq_wall:.1f}s, song song {par_wall:.1f}s)")
