"""S45 — job chạy xong nhưng **artifact không về** thì phải **báo lỗi**, không báo
thành công (sự cố B23).

Bối cảnh (đo thật 23/11/2026): đợt nghiệm thu cuối có 1 trong 3 shard của kịch
bản S11 **mất file** (``['s0.txt','s2.txt']``, thiếu ``s1.txt``) trong khi
``roff wait`` trả **rc 0** cho cả 3 shard. Fleet Muse restart/flap giữa lúc
chạy ⇒ lần pull của shard đó hỏng ⇒ watcher **nuốt** lỗi pull ⇒ ``wait`` báo
"thành công" ⇒ agent tin là có artifact và đọc ra thư mục **rỗng**.

Cách làm **deterministic** (không cần đụng mạng, không cần giành chỗ trên
fleet): thư mục đích local **không ghi được** ⇒ ``pull`` chắc chắn hỏng.

Kịch bản này **cần fleet thật** (phải có máy để chạy job). Test tự ``skip``
khi không đủ máy — không được báo xanh khi không kiểm chứng được (nguyên tắc
`docs/REPORT.md` §2).

Mô tả đầy đủ: `docs/SCENARIOS.md` §12c.
"""
import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as common  # noqa: E402

from _common import pick_live  # noqa: E402
from _common import roff_json  # noqa: E402

pytestmark = pytest.mark.acceptance

MUSE = ["muse-pmb333", "muse-tampt", "muse-bienpm333", "muse-phamminhbien3333"]

#: Exit code của `roff wait` khi **job chạy xong** nhưng artifact **chưa về**.
#:
#: Cố ý **không** dùng `1` (exit code của "job fail"): agent phải phân biệt
#: * job fail (1) → **đừng** chạy lại job;
#: * artifact chưa về (74) → kéo lại bằng `roff pull`, **không** cần chạy lại.
EXIT_PULL_FAILED = 74


@pytest.fixture(scope="module")
def fleet():
    """Các máy Muse **đang sống** lúc module bắt đầu (probe thật)."""
    live = common.pick_live_all(MUSE)
    if not live:
        pytest.skip("không có máy muse nào sống — bật lại tunnel trước khi nghiệm thu")
    return live


def test_s45_pull_failure_is_reported_not_swallowed(fleet, tmp_path):
    """S45: pull hỏng ⇒ `roff wait` phải **báo lỗi**, không báo thành công.

    Đạt khi:
      * job **remote** chạy xong (`exit == 0`) — công việc trên máy đã xong;
      * `roff wait` trả **exit ≠ 0** với `reason == "pull_failed"`,
        `pulled == false`, `pull_error` khác rỗng;
      * sau khi mở lại quyền ghi, `roff pull` **kéo lại được** artifact ⇒
        có đường cứu, không phải mất data.
    """
    alias = pick_live(fleet)
    if alias is None:
        pytest.skip("mọi máy muse đã chết — sự cố hạ tầng")

    # Thư mục đích **không ghi được** ⇒ `pull` chắc chắn hỏng (PermissionError).
    blocked = tmp_path / "ro"
    blocked.mkdir()
    os.chmod(blocked, 0o500)
    out = blocked / "out"

    try:
        rc, obj, err = roff_json([
            "do", "--cmd", 'echo hi > "$ROFF_OUT/x.txt"',
            "--out", str(out),
        ], timeout=300)
        if common.is_host_down(rc, obj, err):
            pytest.skip(f"máy remote chết lúc gửi job — sự cố hạ tầng: {err[-200:]}")
        assert rc == 0, f"roff do thất bại: {rc} {json.dumps(obj, ensure_ascii=False)} {err[-300:]}"
        jid = obj.get("job")
        assert jid, f"thiếu job id: {json.dumps(obj, ensure_ascii=False)}"

        # Chờ job tới trạng thái cuối. Watcher thử pull lại nhiều lần ⇒ cần đủ thời gian.
        rcw, res, errw = roff_json(
            ["wait", jid, "--timeout", "300"], timeout=420)
        if rcw == 69:
            pytest.skip("host chết giữa lúc chạy — sự cố hạ tầng")

        # --- 1) phải BÁO LỖI, không được báo thành công ---------------------
        assert rcw == EXIT_PULL_FAILED, (
            f"pull hỏng mà `roff wait` trả rc={rcw} — phải báo lỗi "
            f"(exit {EXIT_PULL_FAILED}), không được báo thành công: "
            f"{json.dumps(res, ensure_ascii=False)}")
        assert res.get("ok") is False, (
            f"ok phải là false: {json.dumps(res, ensure_ascii=False)}")
        assert res.get("reason") == "pull_failed", (
            f"sai lý do: {res.get('reason')!r} — "
            f"{json.dumps(res, ensure_ascii=False)}")
        assert res.get("pulled") is False, (
            f"pulled phải là false: {json.dumps(res, ensure_ascii=False)}")
        assert str(res.get("pull_error") or ""), (
            f"phải có pull_error: {json.dumps(res, ensure_ascii=False)}")
        # Job **remote** vẫn thành công ⇒ không được báo nhầm là job fail.
        assert res.get("exit") == 0, (
            f"job remote chạy xong nên exit phải là 0: "
            f"{json.dumps(res, ensure_ascii=False)}")

        # --- 2) phải có ĐƯỜNG CỨU: `roff pull` kéo lại được -----------------
        os.chmod(blocked, 0o700)
        deadline = time.time() + 120
        rcp = 1
        objp = {}
        while time.time() < deadline:
            rcp, objp, errp = roff_json(["pull", jid, "--local", str(out)],
                                        timeout=180)
            if rcp == 0:
                break
            time.sleep(5)
        assert rcp == 0, (
            f"roff pull phải kéo lại được artifact: rc={rcp} "
            f"{json.dumps(objp, ensure_ascii=False)}")
        assert (out / "x.txt").exists(), (
            f"artifact không có sau khi pull lại: {list(out.glob('*'))}")
    finally:
        # Mở lại quyền để pytest dọn tmp_path (và để lần sau chạy được).
        try:
            os.chmod(blocked, 0o700)
        except OSError:
            pass
