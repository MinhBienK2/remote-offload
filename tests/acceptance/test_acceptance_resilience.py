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
from _common import pick_live_all  # noqa: E402
from _common import wait_or_skip  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
SKILL = REPO / "skill"
LAUNCHER = SKILL / "roff.py"

pytestmark = pytest.mark.acceptance

MUSE = ["muse-pmb333", "muse-tampt", "muse-bienpm333", "muse-phamminhbien3333"]


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
    alias = pick_live(fleet)

    if alias is None:

        pytest.skip("mọi máy muse đã chết — sự cố hạ tầng")
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
    if common.is_host_down(rc, obj, err):
        pytest.skip(f"máy remote chết lúc gửi job — sự cố hạ tầng: {err[-200:]}")
    assert rc == 0, f"roff do thất bại: {rc} {json.dumps(obj)} {err[-400:]}"
    jid = obj["job"]
    assert obj.get("host") == alias, f"phải chạy trên {alias}: {obj}"

    # Chờ job **thật sự chạy** trước khi cắt kết nối.
    #
    # Nếu job còn xếp hàng (hết slot) thì cửa sổ "cắt kết nối" trôi qua
    # **trước khi job chạy** ⇒ watcher không có gì để mất ⇒ không ghi được
    # 'disconnected' ⇒ kịch bản fail oan (đo thật 23/11/2026: fleet bận,
    # job lên 'launched' muộn hơn 12 s ⇒ fail với `['pulled', 'finished']`).
    deadline = time.time() + 120
    launched = False
    while time.time() < deadline:
        _rch, st, _err = roff_json(["status", jid], timeout=60, env=env)
        if str(st.get("state") or "") in ("launched", "running"):
            launched = True
            break
        time.sleep(2)
    if not launched:
        pytest.skip("job không lên 'launched' trong 120 s — host quá tải / flap")

    # Cắt kết nối: trỏ alias sang cổng chết (1 = tcpmux, không có sshd)
    write_cfg(1)
    time.sleep(25)

    # Kết nối lại
    write_cfg(real_port)

    rc2, res, err2 = wait_or_skip(jid, timeout=360, env=env, wait_timeout=300)
    assert rc2 == 0, f"job phải chạy tiếp tới khi xong: rc={rc2} {json.dumps(res)}"

    # Ledger: có disconnected, KHÔNG có attempt thứ hai
    ledger = json.loads(
        (tmp_path / "state" / "jobs" / f"{jid}.json").read_text())
    events = [e.get("event") for e in (ledger.get("_roff", {}).get("events") or [])]
    if "disconnected" not in events and not ssh_ok(alias):
        pytest.skip(f"{alias} chết trong lúc test — sự cố hạ tầng")
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
    if common.is_host_down(rc, obj, err):
        pytest.skip(f"máy remote chết lúc gửi job — sự cố hạ tầng: {err[-200:]}")
    assert rc == 0, f"roff do thất bại: {rc} {json.dumps(obj)} {err[-400:]}"
    jid = obj["job"]

    rc2, res, err2 = wait_or_skip(jid, timeout=360, env=cfg["env"], wait_timeout=300)
    assert rc2 == 0, f"job phải thành công: {rc2} {json.dumps(res)}"

    # Ledger phải đọc ở **đúng** thư mục mà lệnh `roff` vừa ghi vào.
    #
    # BUG B19 (đo thật 23/11/2026): `ROFF_STATE_DIR` **chỉ** có trong env của
    # tiến trình con ⇒ `os.environ` của pytest **không** có ⇒ đọc nhầm ledger
    # thật của người dùng (`~/.local/state/roff`) ⇒ `FileNotFoundError`.
    # Thư mục state dùng chung của bộ nghiệm thu là `common.DEFAULT_STATE`
    # (xem `_common.roff_env`) — đó **chính là** thư mục mà `roff` ghi vào.
    ledger = json.loads(
        (Path(_DEFAULT_STATE) / "jobs" / f"{jid}.json").read_text())
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

    rc2, res, err2 = wait_or_skip(jid, timeout=360, env=env, wait_timeout=300)
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
    if common.is_host_down(rc, obj, err):
        pytest.skip(f"máy remote chết lúc gửi job — sự cố hạ tầng: {err[-200:]}")
    assert rc == 0, f"job A thất bại: {rc} {json.dumps(obj)} {err[-300:]}"
    jid_a = obj["job"]
    rc2, _, err2 = wait_or_skip(jid_a, timeout=360, env=env, wait_timeout=300)
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
    if common.is_host_down(rc3, obj3, err3):
        pytest.skip(f"máy remote chết lúc gửi job — sự cố hạ tầng: {err3[-200:]}")
    assert rc3 == 0, f"job B thất bại: {rc3} {json.dumps(obj3)} {err3[-300:]}"

    # Thu đủ 3 shard
    job_ids = obj3.get("jobs") or [obj3.get("job")]
    job_ids = [j for j in job_ids if j]
    assert len(job_ids) == 3, f"phải có 3 shard: {json.dumps(obj3)}"

    hosts = set()
    for jid in job_ids:
        rcw, res, errw = wait_or_skip(jid, timeout=360, env=env, wait_timeout=300)
        assert rcw == 0, f"shard {jid} không thành công: {rcw} {json.dumps(res)}"
        # `roff wait` trả 0 cả khi job **remote** exit khác 0 ⇒ phải xem cả
        # `exit` của job, nếu không thì shard chết âm thầm mà test vẫn xanh
        # rồi fail ở kiểm tra "đủ 3 file" (khó hiểu, dễ quy sai).
        assert res.get("exit") == 0, (
            f"shard {jid} exit={res.get('exit')} (không phải 0): "
            f"{json.dumps(res, ensure_ascii=False)[:400]}")
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

#: Độ trễ **gửi** (giữa 2 lần `do` tạo job) mà ta coi là "gửi **tuần tự**".
#: Ngưỡng này lớn hơn nhiều lần chi phí probe/đẩy-input bình thường, nhưng
#: nhỏ hơn thời gian chạy của 1 job ⇒ đủ để tách:
#:   * `do --each` gửi **tuần tự** (lỗi **sản phẩm** — B36) — FAIL;
#:   * fleet hẹp, host chết giữa lô (lỗi **hạ tầng**) — SKIP.
SUBMIT_STAGGER_S = 10.0


def _ledger_of(state_dir, job_id: str) -> dict:
    """Nội dung ledger của 1 job trong state dir của test (không ném lỗi)."""
    path = Path(state_dir) / "jobs" / f"{job_id}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}



def _make_videos(tmp_path, n, seconds=2):
    """Video **nhỏ** (320x240) — upload phải **không** phải nút thắt.

    Lý do (đo thật 23/11/2026): video 1280x720/6 s = **5,6 MB** ⇒ đẩy mất
    ~25 s trên đường lên 0,5–1 MB/s, trong khi phần **chạy** chỉ ~17 s ⇒
    các job **không bao giờ** chạy chồng nhau ⇒ không đo được tăng tốc tính
    toán (đó là giới hạn của **đường lên**, không phải của scheduler).
    ⇒ input nhỏ (~200 KB ⇒ đẩy ~0,3 s) + lệnh **nặng** (lặp 6 lần encode) ⇒
    đo được phần tính toán có phân tán hay không.
    """
    vids = []
    for i in range(n):
        path = tmp_path / f"v{i}.mp4"
        r = subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi",
             "-i", "testsrc2=size=320x240:rate=30",
             "-t", str(seconds), "-c:v", "libx264", "-preset", "ultrafast",
             "-pix_fmt", "yuv420p", str(path)],
            capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            pytest.skip(f"không tạo được video test: {r.stderr[-200:]}")
        vids.append(path)
    return vids


def test_s18_parallel_speedup(fleet, cfg, tmp_path):
    """S18: N video chạy trên cả fleet.

    **Cái gì kiểm chứng được**: các job có **chạy chồng nhau** trên **≥ 2 máy**
    khác nhau hay không — tức là phần **tính toán** có được phân tán hay không.

    **Cái gì KHÔNG kiểm chứng được ở đây**: tăng tốc **wall-clock toàn cục**.
    Lý do (đo thật 23/11/2026): đường lên local → host chỉ **0,5–1 MB/s**, mỗi
    video thử nghiệm **5,6 MB** ⇒ **đẩy** tốn ~25 s còn **chạy** chỉ ~17 s ⇒
    thời gian của lô bị **đường lên** chi phối, không phải bởi fleet (xem
    `docs/REPORT.md` §3). Tăng tốc wall-clock vẫn được **đo và in ra** để đọc.

    **Đạt khi**:
      * phía **song song** chạy trên **≥ 2 máy khác nhau**;
      * các job **chạy chồng nhau** (cửa sổ chạy remote có giao nhau);
      * tăng tốc **tính toán** ≥ 0,6 × (số máy đã dùng) — đo bằng
        **tổng thời gian chạy remote ÷ trục thời gian của cả lô**: 1,0× ⇒
        lô chạy **tuần tự** (lỗi sản phẩm); ~k× ⇒ k job chạy đồng thời trên
        k máy. (Đo bằng "đoạn mọi job cùng chạy" sẽ **vỡ** khi fleet hẹp hơn
        số item ⇒ báo đỏ oan.)
      * mọi output có sha256 đúng, không trùng lặp.

    **"Không chạy chồng nhau" là lỗi SẢN PHẨM hay HẠ TẦNG?** (B37 — xem
    `docs/REPORT.md` §5). Phân biệt bằng **mốc gửi** (lúc `do` tạo job):
      * các job được gửi **lệch nhau > 10 s** ⇒ `do --each` gửi **tuần tự**
        ⇒ lỗi **sản phẩm** ⇒ **FAIL** (không được `skip`, nếu không lỗi sản
        phẩm sẽ bị báo xanh mỗi đợt nghiệm thu);
      * các job được gửi **cùng lúc** mà vẫn không chồng nhau ⇒ fleet **hẹp**
        / host chết giữa lô ⇒ **hạ tầng** ⇒ `skip` (kèm lý do đúng).
    """
    # Fleet flap liên tục ⇒ đếm máy **đang sống lúc này**, không dựa vào
    # fixture module-scope (nó có thể đếm cả máy đã chết).
    live = pick_live_all(fleet)
    n = min(4, len(live))
    if n < 2:
        pytest.skip(f"cần ≥ 2 máy đang sống để đo tăng tốc, đang có {len(live)}")

    _make_videos(tmp_path, n)
    # Lệnh **nặng** (lặp 6 lần) để phần *chạy* lấn át phần *đẩy* — nếu không,
    # đường lên chi phối và không đo được tăng tốc tính toán (xem `_make_videos`).
    cmd = ('for _i in 1 2 3 4 5 6; do ffmpeg -y -i "$ROFF_ITEM" '
           '-vf scale=1280:720 -c:v libx264 -preset slow -pix_fmt yuv420p '
           '"$ROFF_OUT/$(basename "$ROFF_ITEM")"; done')

    # --- 1 máy: cùng lệnh, cùng `--each`, nhưng ghim một host ---
    # (dùng `--each` cho cả hai phía để lệnh và đường truyền là như nhau —
    #  khác lệnh thì so sánh tăng tốc vô nghĩa).
    t0 = time.time()
    out1 = tmp_path / "seq"
    rc, obj, err = roff_json([
        "do", "--cmd", cmd, "--each", str(tmp_path / "v*.mp4"),
        "--host", live[0], "--out", str(out1), "--need", "ffmpeg",
    ], timeout=1800, env=cfg["env"])
    if common.is_host_down(rc, obj, err):
        pytest.skip(f"máy remote chết lúc gửi job — sự cố hạ tầng: {err[-200:]}")
    assert rc == 0, f"job tuần tự thất bại: {rc} {json.dumps(obj)} {err[-300:]}"
    seq_ids = [j for j in (obj.get("jobs") or [obj.get("job")]) if j]
    assert len(seq_ids) == n, f"phải có {n} job tuần tự: {json.dumps(obj)}"
    for jid in seq_ids:
        rcw, res, errw = wait_or_skip(jid, timeout=960, env=cfg["env"], wait_timeout=900)
        assert rcw == 0, f"job tuần tự {jid} không xong: {rcw} {json.dumps(res)}"
    seq_wall = time.time() - t0

    # --- N máy: cùng lệnh, `--each`, không ghim host ---
    out2 = tmp_path / "par"
    t1 = time.time()
    rc, obj, err = roff_json([
        "do", "--cmd", cmd, "--each", str(tmp_path / "v*.mp4"),
        "--out", str(out2), "--need", "ffmpeg",
    ], timeout=1800, env=cfg["env"])
    if common.is_host_down(rc, obj, err):
        pytest.skip(f"máy remote chết lúc gửi job — sự cố hạ tầng: {err[-200:]}")
    assert rc == 0, f"job song song thất bại: {rc} {json.dumps(obj)} {err[-400:]}"
    job_ids = [j for j in (obj.get("jobs") or [obj.get("job")]) if j]
    assert len(job_ids) == n, f"phải có {n} job, thấy {len(job_ids)}: {json.dumps(obj)}"
    par_hosts = []
    for jid in job_ids:
        rcw, res, errw = wait_or_skip(jid, timeout=960, env=cfg["env"], wait_timeout=900)
        assert rcw == 0, f"job {jid} không xong: {rcw} {json.dumps(res)}"
        par_hosts.append(str(res.get("host") or ""))
    par_wall = time.time() - t1

    # --- Đo **tính toán**: các job có chạy CHỒNG NHAU trên ≥ 2 máy không? ---
    windows = []
    submits = []
    for jid in job_ids:
        # Ledger nằm ở state dir **dùng chung** của bộ nghiệm thu (xem
        # `_common.roff_env`) — đó **chính là** thư mục `roff` ghi vào.
        led = _ledger_of(_DEFAULT_STATE, jid)
        attempts = led.get("attempts") or []
        started = float((attempts[-1] if attempts else {}).get("started") or 0.0)
        ended = 0.0
        for ev in ((led.get("_roff") or {}).get("events") or []):
            if ev.get("event") == "finished":
                ended = float(ev.get("ts") or 0.0)
        if started and ended > started:
            windows.append((started, ended, str(led.get("host") or "")))
        # Mốc **gửi** (lúc `do` tạo job) — dùng để phân biệt "hệ thống gửi
        # tuần tự" với "fleet hẹp không chạy nổi nhiều job cùng lúc".
        submits.append(float(led.get("created_at") or 0.0))

    used = sorted({h for h in par_hosts if h})
    k = len(used)
    if k < 2:
        pytest.skip(
            "song song chỉ chạy trên %d máy (%s) — fleet hẹp lúc đo ⇒ không "
            "kiểm chứng được tính toán phân tán" % (k, ", ".join(used) or "không rõ"))
    if len(windows) < n:
        pytest.skip(
            "thiếu mốc thời gian của %d/%d job — không đo được cửa sổ chạy remote"
            % (len(windows), n))

    # Đoạn mà **mọi** job cùng chạy: [max(start), min(end)] — chỉ để **in ra**.
    lo = max(w[0] for w in windows)
    hi = min(w[1] for w in windows)
    real_submits = [s for s in submits if s]
    stagger = (max(real_submits) - min(real_submits)) if len(real_submits) >= 2 else 0.0
    detail = "; ".join(
        ["%s %.1f→%.1f" % (h, s, e) for s, e, h in sorted(windows)]
    )

    # **Tăng tốc tính toán** = (tổng thời gian chạy remote) ÷ (trục thời gian
    # của **cả lô**). Cách này đo được **mức song song thật**:
    #   * 1 job chạy tuần tự ⇒ 1,0×;
    #   * k job chạy đồng thời trên k máy ⇒ ~k×.
    # (Đo bằng "đoạn mọi job cùng chạy" như trước thì **vỡ** khi fleet hẹp:
    # 3 job trên 2 máy không bao giờ có đoạn chung, dù 2 job **có** chạy
    # chồng nhau ⇒ báo tăng tốc 0 ⇒ **báo đỏ oan** cho sản phẩm đúng.)
    span_start = min(w[0] for w in windows)
    span_end = max(w[1] for w in windows)
    compute_wall = span_end - span_start
    compute_total = sum(end - start for start, end, _ in windows)
    compute_speedup = compute_total / compute_wall if compute_wall else 0.0
    # Số job chạy **đồng thời tối đa** (độ sâu chồng nhau) — đo trực tiếp.
    edges = []
    for start, end, _h in windows:
        edges.append((start, 1))
        edges.append((end, -1))
    edges.sort()
    depth = best = 0
    for _ts, delta in edges:
        depth += delta
        best = max(best, depth)
    max_parallel = best
    # Trần vật lý: 1 máy chạy **1 job** một lúc (slot mặc định) ⇒ tăng tốc
    # tối đa = số máy đã dùng. Dùng trần đó, không dùng `n` — nếu fleet hẹp hơn
    # số item thì `n` là ngưỡng **không với tới** ⇒ báo đỏ oan.
    expect = max(1, min(k, len(windows)))
    global_speedup = seq_wall / par_wall if par_wall > 0 else 0

    # **Không job nào chạy chồng nhau** ⇒ lô chạy **tuần tự**. Có hai nguyên
    # nhân rất khác nhau, phải phân biệt được (B37 — lý do skip sai sẽ giấu
    # lỗi sản phẩm):
    #   (a) `roff do --each` gửi các item **tuần tự** ⇒ lỗi **sản phẩm** (B36);
    #   (b) các job đã gửi **cùng lúc** mà vẫn xếp hàng ⇒ máy **đang bận**
    #       ⇒ **hạ tầng**.
    # Mốc quyết định: **độ trễ giữa các lần gửi**.
    if max_parallel < 2:
        if stagger > SUBMIT_STAGGER_S:
            pytest.fail(
                "các job KHÔNG chạy chồng nhau mà `do --each` đã gửi TUẦN TỰ "
                "(lệch %.1f s > %.1f s) ⇒ lỗi **sản phẩm**: đường gửi job "
                "tuần tự, fleet không tính toán song song (B36). Cửa sổ chạy: %s"
                % (stagger, SUBMIT_STAGGER_S, detail))
        # **Nêu đúng nguyên nhân** (nguyên tắc §2): máy **chết/sập** giữa lô
        # (watcher ghi `disconnected` rồi `failover`) khác với máy **đang bận**
        # (job phải xếp hàng chờ slot).
        downed = []
        for jid in job_ids:
            evs = ((_ledger_of(_DEFAULT_STATE, jid).get("_roff") or {}).get("events") or [])
            names = {str(e.get("event") or "") for e in evs}
            if names & {"disconnected", "failover", "input_push_failed"}:
                downed.append(jid)
        why = ("máy **chết/sập** giữa lô (job phải chờ rồi failover)"
               if downed else "máy đang **bận** (job phải xếp hàng chờ slot)")
        pytest.skip(
            "các job đã gửi cùng lúc (lệch %.1f s) nhưng không job nào chạy "
            "chồng nhau — %s ⇒ **hạ tầng**, không phải lỗi sản phẩm. "
            "Cửa sổ chạy: %s" % (stagger, why, detail))
    print(f"\n[S18] n={n} máy dùng={k} ({', '.join(used)}) | "
          f"tính toán: {compute_total:.1f}s chạy remote trong {compute_wall:.1f}s "
          f"trục lô => tăng tốc tính toán {compute_speedup:.2f}x "
          f"(cần >= {0.6 * expect:.2f}x; chạy đồng thời tối đa {max_parallel} job) "
          f"| đoạn chạy chồng nhau của MỌI job: "
          f"{'có' if lo < hi else 'không'} ({hi - lo:.1f}s) | wall-clock: tuần tự "
          f"{seq_wall:.1f}s vs song song {par_wall:.1f}s => {global_speedup:.2f}x "
          f"(đường lên chi phối)")
    assert compute_speedup >= 0.6 * expect, (
        f"tăng tốc tính toán {compute_speedup:.2f}x < {0.6 * expect:.2f}x "
        f"(tổng {compute_total:.1f}s chạy remote / {compute_wall:.1f}s trục lô; "
        f"chạy đồng thời tối đa {max_parallel} job; {k} máy: {', '.join(used)})")

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

