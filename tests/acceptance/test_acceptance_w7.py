"""S40–S42 — kho input dùng chung bị **kẹt** (sự cố B17).

Cả 3 kịch bản đều **cần fleet thật**. Test tự `skip` khi không đủ máy —
không được báo xanh khi không kiểm chứng được (nguyên tắc `docs/REPORT.md` §2).

Bối cảnh: ngày 23/11/2026 một job treo **28,5 phút** vì thư mục `<key>.lock`
của kho input dùng chung bị **mồ côi** ⇒ mọi lần đẩy sau đó fail
`shared_input_busy` ⇒ watcher quay vòng **vô hạn** trên cùng host ⇒ job treo
tới hết `timeout` (2 giờ 10 phút) mà **không** failover.

Mô tả đầy đủ: `docs/SCENARIOS.md` §12a.
"""
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as common  # noqa: E402

from _common import pick_live  # noqa: E402
from _common import roff_json  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
LAUNCHER = REPO / "skill" / "roff.py"

pytestmark = pytest.mark.acceptance

#: Lý do ⇒ job mất vì **máy chết** (sự cố hạ tầng) ⇒ `skip`, không FAIL.
#:
#: Cố ý **không** dùng `common.JOB_LOST_REASONS`: reason `input_push_failed` /
#: `workdir_missing` nằm trong đó, nhưng 2 reason đó **là lỗi của roff**
#: (kho input kẹt) ⇒ phải FAIL để lộ ra, không được `skip` cho qua.
HOST_DEATH = {"host_restarted", "host_wiped", "oom_killed", "vanished"}

#: Ngưỡng thời gian cho S40. Lý thuyết: lock mồ côi bị coi là chết sau
#: `LOCK_STALE_S` (30 s) ⇒ job phải xong trong ~1 phút. Trước W7 phải quay
#: vòng ~30 lần ≈ **30 phút**. Đặt ngưỡng 240 s để có dư địa cho băng thông
#: chậm mà vẫn bắt được đúng lỗi (treo 30 phút).
S40_MAX_SECONDS = 240


def ssh_out(alias, script, timeout=90):
    """Chạy script trên host; trả ``(rc, stdout, stderr)``."""
    try:
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
             alias, "sh -s"],
            input=script, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.SubprocessError as exc:
        return 255, "", str(exc)
    return r.returncode, r.stdout, r.stderr


MUSE = ["muse-pmb333", "muse-tampt", "muse-bienpm333", "muse-phamminhbien3333"]


@pytest.fixture(scope="module")
def fleet():
    """Các máy Muse **đang sống** lúc module bắt đầu (probe thật)."""
    live = []
    for alias in MUSE:
        rc, _out, _err = ssh_out(alias, "true", timeout=30)
        if rc == 0:
            live.append(alias)
    if not live:
        pytest.skip("không có máy muse nào sống — bật lại tunnel trước khi nghiệm thu")
    return live


def shared_key(path):
    """Khoá của 1 nguồn local — **giống hệt** `roffkit.shared.shared_key`."""
    absolute = os.path.abspath(os.path.expanduser(str(path)))
    return hashlib.sha256(absolute.encode("utf-8")).hexdigest()[:16]


def layout_root_of(alias, timeout=300):
    """`layout_root` (kho input dùng chung) của 1 host; ``""`` nếu hỏng."""
    rc, obj, err = roff_json(["hosts", "--json"], timeout=timeout)
    for host in obj.get("hosts") or []:
        if host.get("alias") == alias:
            return str(host.get("layout_root") or "")
    return ""


def wait_strict(jid, timeout=300, wait_timeout=None):
    """Chờ job tới trạng thái cuối — **không** skip vì lỗi của roff.

    Chỉ `skip` khi **máy chết** (`host_restarted`…). Lý do do roff sinh ra
    (`input_push_failed`, `workdir_missing`, `timeout`) phải **FAIL** để lộ.
    """
    secs = int(wait_timeout if wait_timeout is not None else timeout)
    rc, obj, err = roff_json(["wait", jid, "--timeout", str(secs)],
                             timeout=timeout + 60)
    reason = str(obj.get("reason") or "")
    if reason in HOST_DEATH:
        pytest.skip(f"host chết giữa lúc chạy (reason={reason}) — sự cố hạ tầng")
    if common.is_host_down(rc, obj, err):
        pytest.skip(f"máy chết / đường truyền đứt lúc chờ — sự cố hạ tầng: {err[-200:]}")
    return rc, obj, err


# ------------------------------------------------------------------ S40

def test_s40_orphan_shared_lock_is_cleaned_and_job_runs(fleet, tmp_path):
    """S40: kho input bị kẹt (lock mồ côi) ⇒ phải tự dọn, không treo.

    Đạt khi:
      * job **vẫn chạy được** (exit 0, có artifact);
      * xong trong < 240 s (trước W7: ~30 phút);
      * lock **không** còn sót sau khi job xong.
    """
    alias = pick_live(fleet)
    if alias is None:
        pytest.skip("mọi máy muse đã chết — sự cố hạ tầng")
    layout = layout_root_of(alias)
    if not layout:
        pytest.skip("không lấy được layout_root của host")

    src = tmp_path / "s40-input.txt"
    src.write_text("s40\n" * 500, encoding="utf-8")
    key = shared_key(src)
    root = f"{layout.rstrip('/')}/inputs"
    lock = f"{root}/{key}.lock"
    marker = f"{root}/{key}.complete"

    # Dọn trước (có thể còn sót từ lần chạy trước) rồi tạo lock mồ côi:
    # có `.lock`, **không** có `.complete` ⇒ đúng trạng thái B17.
    ssh_out(alias, f"rm -rf '{lock}' '{marker}'")
    rc, out, err = ssh_out(
        alias, f"mkdir -p '{root}' && mkdir '{lock}' && test -d '{lock}'")
    assert rc == 0, f"không tạo được lock mồ côi trên {alias}: {err[-300:]}"

    t0 = time.time()
    rc, obj, err = roff_json([
        "do", "--cmd", 'cp "$ROFF_IN" "$ROFF_OUT/copy.txt"',
        "--in", str(src), "--out", str(tmp_path / "out"), "--host", alias,
    ], timeout=300)
    elapsed = time.time() - t0
    if rc != 0 and str(obj.get("reason") or "") in HOST_DEATH:
        pytest.skip(
            f"host {alias} chết ngay lúc gửi job (reason={obj.get('reason')}) — "
            "sự cố hạ tầng")
    if rc != 0 and common.is_host_down(rc, obj, err):
        pytest.skip(
            f"host {alias} chết ngay lúc gửi job — sự cố hạ tầng: {err[-200:]}")
    assert rc == 0, (
        f"roff do thất bại khi kho input bị kẹt: rc={rc} "
        f"{json.dumps(obj, ensure_ascii=False)} {err[-400:]}")
    jid = obj.get("job")
    assert jid, f"thiếu job id: {json.dumps(obj, ensure_ascii=False)}"

    rc2, res, err2 = wait_strict(jid, timeout=360, wait_timeout=300)
    elapsed = time.time() - t0
    assert rc2 == 0, (
        f"job không thành công dù kho input đã được dọn: rc={rc2} "
        f"{json.dumps(res, ensure_ascii=False)}")
    assert elapsed < S40_MAX_SECONDS, (
        f"job mất {elapsed:.0f}s — quá lâu ⇒ lock kẹt chưa được dọn kịp "
        f"(ngưỡng {S40_MAX_SECONDS}s)")

    produced = tmp_path / "out" / "copy.txt"
    assert produced.exists(), f"thiếu artifact: {list((tmp_path / 'out').glob('*'))}"

    # Lock không được sót lại (kèm marker thì mới là bình thường).
    rc3, out3, _ = ssh_out(
        alias, f"if test -e '{lock}'; then echo LOCK_STILL_THERE; else echo GONE; fi")
    assert "GONE" in out3, f"lock vẫn còn sau khi job xong: {out3.strip()}"


# ------------------------------------------------------------------ S41

def test_s41_missing_local_input_ends_with_clear_reason(fleet, tmp_path):
    """S41: input local biến mất lúc watcher đẩy ⇒ `lost` + `input_not_found`.

    Cách làm **deterministic**: chiếm hết slot của host (job A `sleep`) ⇒
    job B **xếp hàng** ⇒ `roff do` **không** đẩy input (đẩy ở watcher) ⇒
    xoá file input ⇒ job B lên chạy thì không còn input ⇒ lỗi **của job**
    ⇒ kết thúc với lý do rõ ràng, **không** failover, **không** quay vòng.
    """
    alias = pick_live(fleet)
    if alias is None:
        pytest.skip("mọi máy muse đã chết — sự cố hạ tầng")

    # --- 1) job A chiếm slot duy nhất của host -----------------------------
    # `sleep 60` đủ để giữ slot lúc tạo job B (tạo job B mất vài giây), mà
    # không kéo dài cửa sổ mà host có thể flap.
    rc, obj, err = roff_json([
        "do", "--cmd", "sleep 60", "--host", alias,
        "--out", str(tmp_path / "hold"),
    ], timeout=180)
    if rc != 0 and str(obj.get("reason") or "") in HOST_DEATH:
        pytest.skip(f"host {alias} chết lúc tạo job — sự cố hạ tầng")
    assert rc == 0, f"không tạo được job giữ slot: {json.dumps(obj)} {err[-300:]}"
    hold = obj.get("job")
    assert hold, f"thiếu job id: {json.dumps(obj)}"

    deadline = time.time() + 90
    launched = False
    while time.time() < deadline:
        rch, st, _ = roff_json(["status", hold], timeout=60)
        if str(st.get("state") or "") in ("launched", "running"):
            launched = True
            break
        time.sleep(2)
    if not launched:
        pytest.skip("job giữ slot không lên 'launched' — host quá tải / flap")

    # --- 2) job B: cùng host ⇒ phải xếp hàng (hết slot) -------------------
    src = tmp_path / "s41-input.txt"
    src.write_text("s41\n", encoding="utf-8")
    rc, obj, err = roff_json([
        "do", "--cmd", 'cat "$ROFF_IN" > "$ROFF_OUT/copy.txt"',
        "--in", str(src), "--out", str(tmp_path / "out"), "--host", alias,
    ], timeout=180)
    if rc != 0 and str(obj.get("reason") or "") in HOST_DEATH:
        pytest.skip(f"host {alias} chết lúc tạo job — sự cố hạ tầng")
    assert rc == 0, f"không tạo được job thứ 2: {json.dumps(obj)} {err[-300:]}"
    jid = obj.get("job")
    assert jid, f"thiếu job id: {json.dumps(obj)}"
    state = str(obj.get("state") or "")
    if state not in ("queued", "pending"):
        pytest.skip(f"job thứ 2 không xếp hàng (state={state}) — slot không bị chiếm")

    # --- 3) xoá input local ⇒ watcher sẽ không tìm thấy khi đẩy ----------
    os.unlink(src)

    # Chờ job B tới trạng thái cuối, nhưng **canh** job giữ slot: nếu nó chết
    # (host flap) thì slot không bao giờ được trả ⇒ job B treo ⇒ đó là sự cố
    # hạ tầng, không phải lỗi roff.
    deadline = time.time() + 300
    while time.time() < deadline:
        rc2, res, err2 = roff_json(["wait", jid, "--timeout", "15"],
                                    timeout=90)
        reason = str(res.get("reason") or "")
        if res.get("state") in ("lost", "exited", "cancelled", "failed"):
            break
        rch, hold_st, _ = roff_json(["status", hold], timeout=60)
        hold_state = str(hold_st.get("state") or "")
        if hold_state in ("lost", "cancelled", "failed"):
            pytest.skip(
                f"job giữ slot đã chết (state={hold_state}, "
                f"reason={hold_st.get('reason')}) — host flap, không phải lỗi roff")
        if str(hold_st.get("host") or "") != alias:
            # Job giữ slot đã **failover** sang máy khác ⇒ host ghim đã chết
            # ⇒ slot của máy cũ không bao giờ được trả ⇒ job B treo vì hạ
            # tầng, không phải lỗi roff.
            pytest.skip(
                f"host {alias} chết (job giữ slot failover sang "
                f"{hold_st.get('host')}) — sự cố hạ tầng")
        time.sleep(5)
    else:
        pytest.skip("job thứ 2 không kết thúc trong 300 s — slot không được trả (hạ tầng)")

    reason = str(res.get("reason") or "")
    assert res.get("state") == "lost", (
        f"job phải kết thúc ở 'lost': state={res.get('state')} "
        f"reason={reason} {json.dumps(res, ensure_ascii=False)}")
    assert reason == "input_not_found", (
        f"sai lý do: {reason!r} (phải là 'input_not_found') — "
        f"{json.dumps(res, ensure_ascii=False)}")

    # Không được failover: **job B** phải vẫn ở host đã ghim, và chưa từng
    # được launch lại ở máy khác (`attempts` chỉ có 1 lần chạy).
    rc3, st, _ = roff_json(["status", jid], timeout=120)
    assert str(st.get("host") or "") == alias, (
        f"job B đã bị chuyển sang máy khác: host={st.get('host')} (ghim {alias})")
    ledger = Path(common.DEFAULT_STATE) / "jobs" / f"{jid}.json"
    attempts = []
    if ledger.is_file():
        try:
            attempts = (json.loads(ledger.read_text(encoding="utf-8"))
                        .get("attempts") or [])
        except ValueError:
            attempts = []
    assert len(attempts) <= 1, (
        f"job B đã được launch lại {len(attempts)} lần ⇒ đã failover: {attempts}")


# ------------------------------------------------------------------ S42

def test_s42_pinned_host_unreachable_fails_loud_without_failover(tmp_path):
    """S42: job **ghim** host mà host đó không với được ⇒ fail to.

    Đạt khi:
      * exit 255 với lý do rõ ràng (`host_unreachable` / `unknown_host`);
      * **không** có gì chạy ở máy local (marker phải không tồn tại);
      * **không** có job nào được tạo trên máy khác (không failover).
    """
    # Alias **chắc chắn** không với được: trỏ về cổng SSH không có gì
    # nghe. Dùng config riêng để không đụng config thật của người dùng.
    ssh_cfg = tmp_path / "ssh_config"
    ssh_cfg.write_text(
        "Host roff-dead-host\n"
        "    HostName 127.0.0.1\n"
        "    Port 2299\n"
        "    User nobody\n"
        "    BatchMode yes\n",
        encoding="utf-8",
    )
    cfg = tmp_path / "config.ini"
    cfg.write_text(
        "[fleet]\ninclude = roff-dead-host\n",
        encoding="utf-8",
    )
    env = {
        "ROFF_SSH_CONFIG": str(ssh_cfg),
        "ROFF_CONFIG": str(cfg),
    }

    marker = tmp_path / "LOCAL_RAN"

    # Ledger là **chung** cho cả người dùng và mọi lần chạy trước ⇒ chỉ
    # quan tâm job **mới** sinh ra trong lúc test này chạy.
    rc0, before, _ = roff_json(["jobs"], timeout=120, env=env)
    before_ids = {j.get("job") for j in (before.get("jobs") or [])}

    rc, obj, err = roff_json([
        "do", "--cmd", f"touch {marker}; echo nope",
        "--host", "roff-dead-host", "--out", str(tmp_path / "out"),
    ], timeout=180, env=env)

    assert rc == 255, (
        f"phải exit 255 (không với được host), thấy rc={rc}: "
        f"{json.dumps(obj, ensure_ascii=False)} {err[-300:]}")
    assert not marker.exists(), "LỆNH ĐÃ CHẠY Ở MÁY LOCAL — vi phạm nguyên tắc fail-loud"
    reason = str(obj.get("reason") or "")
    assert reason in ("host_unreachable", "unknown_host"), (
        f"sai lý do: {reason!r} — {json.dumps(obj, ensure_ascii=False)}")

    # Không được tạo job nào trên máy khác (không failover âm thầm).
    rc2, jobs, _ = roff_json(["jobs"], timeout=120, env=env)
    mine = [j for j in (jobs.get("jobs") or [])
            if j.get("job") not in before_ids
            and j.get("host") and j.get("host") != "roff-dead-host"]
    assert not mine, f"đã failover sang máy khác: {mine}"
