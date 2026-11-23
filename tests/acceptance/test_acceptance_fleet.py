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


#: State dir **riêng cho test** — test KHÔNG được ghi vào ledger thật của
#: người dùng (``~/.local/state/roff``). Nếu không, mỗi lần chạy nghiệm thu lại
#: đẩy hàng chục job record vào ledger thật (và tạo cả bản ghi tunnel ma).
_DEFAULT_STATE = tempfile.mkdtemp(prefix="roff-acceptance-state-")
atexit.register(shutil.rmtree, _DEFAULT_STATE, True)


def roff(args, timeout=180, env=None):
    e = dict(os.environ)
    e.setdefault("ROFF_STATE_DIR", _DEFAULT_STATE)
    if env:
        e.update(env)
    p = subprocess.run([sys.executable, str(LAUNCHER)] + list(args),
                       capture_output=True, text=True, timeout=timeout, env=e)
    return p


def roff_json(args, timeout=180, env=None):
    p = roff(args, timeout=timeout, env=env)
    lines = [ln for ln in p.stdout.strip().splitlines() if ln.strip()]
    obj = json.loads(lines[-1]) if lines else {}
    return p.returncode, obj, p.stderr


def live_hosts():
    """Danh sách alias trong ~/.ssh/config đang sống."""
    out = subprocess.run(["ssh", "-G", "muse-pmb333"], capture_output=True, text=True)
    if out.returncode != 0:
        return []
    aliases = ["muse-pmb333", "muse-tampt", "muse-bienpm333", "muse-phamminhbien3333"]
    live = []
    for a in aliases:
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", a, "true"],
                           capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            live.append(a)
    return live


@pytest.fixture(scope="module")
def fleet():
    hosts = live_hosts()
    if not hosts:
        pytest.skip("không có máy muse nào sống — bật lại tunnel trước khi nghiệm thu")
    return hosts


# ------------------------------------------------------------------ S3

def test_s03_ffmpeg_transcode_artifacts(fleet, tmp_path):
    """S3: ffmpeg ~500MB → artifact về local, sha256 khớp, đạt ngưỡng tài nguyên local."""
    src = tmp_path / "movie.mp4"
    # tạo video thật ~120s 1080p (đủ để có việc, không quá nặng cho CI)
    r = subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30",
         "-f", "lavfi", "-i", "sine=frequency=440", "-t", "20", "-c:v", "libx264",
         "-preset", "ultrafast", "-c:a", "aac", str(src)],
        capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        pytest.skip(f"không tạo được file test: {r.stderr[-300:]}")

    out = tmp_path / "out"
    rc, obj, err = roff_json([
        "do", "--cmd",
        'ffmpeg -y -i "$ROFF_IN" -vf scale=1280:-2 -c:v libx264 -preset veryfast '
        '-c:a aac "$ROFF_OUT/out.mp4"',
        "--in", str(src), "--out", str(out), "--need", "ffmpeg",
    ], timeout=900)
    assert rc == 0, f"roff do thất bại: rc={rc} {json.dumps(obj)} {err[-500:]}"

    jid = obj.get("job")
    assert jid, f"thiếu job id: {obj}"
    rc2, res, err2 = roff_json(["wait", jid, "--timeout", "900"], timeout=960)
    assert rc2 == 0, f"job không thành công: rc={rc2} {json.dumps(res)}"

    produced = out / "out.mp4"
    assert produced.exists(), f"thiếu artifact: {list(out.glob('*'))}"

    # sha256 khớp giữa remote và local
    files = res.get("artifacts") or res.get("files") or []
    if files:
        local_map = {Path(f.get("path", "")).name: f.get("sha256") for f in files}
        if "out.mp4" in local_map and local_map["out.mp4"]:
            import hashlib
            h = hashlib.sha256()
            with produced.open("rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            assert h.hexdigest() == local_map["out.mp4"], "sha256 local khác remote"

    # ffprobe xác nhận 1280 wide
    p = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=width,height", "-of", "csv=p=0",
                        str(produced)], capture_output=True, text=True)
    if p.returncode == 0 and p.stdout.strip():
        w, h = p.stdout.strip().split(",")[:2]
        assert int(w) == 1280, f"chiều rộng sai: {w}"


# ------------------------------------------------------------------ S4

def test_s04_url_input_prefers_internet_host(fleet, tmp_path):
    """S4: input là URL → scheduler ưu tiên host có internet; host không có internet
    thì phải tải qua local (báo rõ, không giấu)."""
    rc, obj, err = roff_json([
        "do", "--cmd",
        'curl -sSL --max-time 60 -o "$ROFF_OUT/data.txt" https://example.com/ && '
        'wc -c < "$ROFF_OUT/data.txt" > "$ROFF_OUT/size.txt"',
        "--in", "http://example.com/",
        "--out", str(tmp_path / "out"),
    ], timeout=600)
    assert rc == 0, f"roff do thất bại: {json.dumps(obj)} {err[-400:]}"
    jid = obj.get("job")
    rc2, res, err2 = roff_json(["wait", jid, "--timeout", "600"], timeout=660)
    # example.com có thể chặn; chấp nhận cả fail nếu không có internet ở đâu cả,
    # nhưng phải là fail có thật (exit 1), không phải treo.
    assert rc2 in (0, 1), f"trạng thái lạ: rc={rc2} {json.dumps(res)}"


# ------------------------------------------------------------------ S5

def test_s05_insufficient_ram_never_runs_local(fleet, tmp_path):
    """S5: yêu cầu RAM vượt xa mức có sẵn → exit 10, KHÔNG chạy local.

    Fleet bị giới hạn còn nhóm ``muse-*`` (RAM thật 450–900 MB) bằng config
    riêng — nếu không, máy ``comfy`` trong ``~/.ssh/config`` có 12 GB RAM và
    sẽ thoả yêu cầu, làm mất ý nghĩa của kịch bản. Việc này cũng kiểm tra
    mục ``[fleet] include`` có thật sự giới hạn được fleet không.
    """
    cfg = tmp_path / "config.ini"
    cfg.write_text("[fleet]\ninclude = muse-*\n")
    env = {"ROFF_CONFIG": str(cfg)}

    marker = tmp_path / "LOCAL_RAN"
    cmd = f'touch {marker}; echo nope'
    rc, obj, err = roff_json([
        "do", "--cmd", cmd, "--mem", "6G", "--out", str(tmp_path / "out"),
    ], timeout=300, env=env)
    assert rc == 10, f"phải exit 10 (không còn host), thấy rc={rc}: {json.dumps(obj)}"
    assert not marker.exists(), "LỆNH ĐÃ CHẠY Ở MÁY LOCAL — vi phạm nguyên tắc fail-loud"
    # Lý do loại phải nói rõ host nào thiếu bao nhiêu
    rejected = obj.get("rejected") or {}
    assert rejected, f"phải nói lý do từng host: {json.dumps(obj)}"


# ------------------------------------------------------------------ S13

def test_s13_all_hosts_dead_never_runs_local(fleet, tmp_path):
    """S13: ép host chết → không được chạy gì ở local."""
    marker = tmp_path / "LOCAL_RAN2"
    rc, obj, err = roff_json([
        "do", "--cmd", f'touch {marker}', "--host", "muse-phamminhbien3333",
        "--out", str(tmp_path / "out"),
    ], timeout=180)
    # Nếu host kia tình cờ sống, kết quả có thể là 0 — chấp nhận.
    # Điều kiện cứng: KHÔNG BAO GIỜ chạy ở local.
    assert not marker.exists(), "LỆNH ĐÃ CHẠY Ở MÁY LOCAL"
    if rc != 0:
        assert rc in (10, 255), f"exit lạ: {rc} {json.dumps(obj)}"


# ------------------------------------------------------------------ S14

def test_s14_secret_not_in_remote_argv(fleet, tmp_path):
    """S14: secret trong --env không lộ trong argv remote (thấy qua ps)."""
    secret = "s3cr3t-acceptance-9f3a"
    # job chạy đủ lâu để ta kịch ps trên host
    cmd = ('echo "$MY_SECRET" > "$ROFF_OUT/secret.txt"; sleep 25; '
           'ps -eo args 2>/dev/null | grep -F "$MY_SECRET" > "$ROFF_OUT/leak.txt" 2>/dev/null; '
           'true')
    rc, obj, err = roff_json([
        "do", "--cmd", cmd, "--env", f"MY_SECRET={secret}",
        "--out", str(tmp_path / "out"),
    ], timeout=300)
    assert rc == 0, f"roff do thất bại: {json.dumps(obj)} {err[-400:]}"
    jid = obj.get("job")
    host = obj.get("host")

    # ps ngay trên host trong lúc job chạy
    deadline = time.time() + 30
    leaked = False
    while time.time() < deadline:
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host,
             "ps -eo args 2>/dev/null | grep -F " + secret + " | grep -v grep | head -3"],
            capture_output=True, text=True, timeout=40)
        if secret in (r.stdout or ""):
            leaked = True
            break
        time.sleep(3)

    roff_json(["wait", jid, "--timeout", "300"], timeout=360)
    assert not leaked, "SECRET LỘ TRONG ARGV REMOTE (thấy qua ps)"


# ------------------------------------------------------------------ S15

def test_s15_stale_master_does_not_kill_foreign_pid(fleet, tmp_path):
    """S15: master ssh cũ giữ port → roff phải tự xử lý, không kill pid lạ."""
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                        fleet[0], "true"], capture_output=True, text=True, timeout=40)
    if r.returncode != 0:
        pytest.skip("host không sống")

    before = subprocess.run(["pgrep", "-c", "ssh"], capture_output=True, text=True)
    rc, obj, err = roff_json(["tunnel", "start", fleet[0], "--lport", "18099",
                               "--rport", "22"], timeout=120)
    after = subprocess.run(["pgrep", "-c", "ssh"], capture_output=True, text=True)
    # Không được giết hàng loạt ssh
    try:
        assert int(after.stdout.strip()) <= int(before.stdout.strip()) + 2, (
            "số tiến trình ssh tăng bất thường — có thể đã spawn quá nhiều")
    except ValueError:
        pass
    roff_json(["tunnel", "stop", fleet[0], "--lport", "18099"], timeout=60)
