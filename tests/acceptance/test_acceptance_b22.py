"""S43 — `roff gc` phải dọn **lock mồ côi** trên host thật (sự cố B22).

Bối cảnh: `roff gc` chỉ **giữ** mọi entry đang lock ⇒ thư mục `<key>.lock`
mồ côi (tiến trình chết giữa lúc đẩy) **tích tụ mãi** trên máy remote.
Kiểm kê thật 23/11/2026: `muse-pmb333` còn **3** lock mồ côi (tuổi 4207–5515 s),
`muse-phamminhbien3333` còn **1**.

Kịch bản này **cần fleet thật**. Test tự `skip` khi không đủ máy — không
được báo xanh khi không kiểm chứng được (nguyên tắc `docs/REPORT.md` §2).

Mô tả đầy đủ: `docs/SCENARIOS.md` §12b.
"""
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import is_host_down  # noqa: E402
from _common import pick_live  # noqa: E402
from _common import roff_json  # noqa: E402

pytestmark = pytest.mark.acceptance

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


def layout_root_of(alias, timeout=300):
    """`layout_root` (kho input dùng chung) của 1 host; ``""`` nếu hỏng."""
    rc, obj, err = roff_json(["hosts", "--json"], timeout=timeout)
    for host in obj.get("hosts") or []:
        if host.get("alias") == alias:
            return str(host.get("layout_root") or "")
    return ""


def remote_path_state(alias, path, timeout=60):
    """``True`` = có, ``False`` = không có, ``None`` = **không xác định** (host chết)."""
    rc, out, _err = ssh_out(
        alias, f"if test -e '{path}'; then echo YES; else echo NO; fi", timeout=timeout)
    if rc != 0:
        return None
    return "YES" in out


# ------------------------------------------------------------------ S43

def test_s43_gc_removes_orphan_lock_but_keeps_live_lock(fleet):
    """S43: `roff gc` dọn lock **mồ côi**, nhưng **không** đụng lock còn sống.

    Đạt khi:
      * lock mồ côi (không có file ``owner``, mtime cũ) ⇒ **bị xoá**;
      * lock **còn chủ** (owner là tiến trình đang sống) ⇒ **được giữ**;
      * JSON của `roff gc` báo `removed_locks` chứa key của lock đã dọn.
    """
    alias = pick_live(fleet)
    if alias is None:
        pytest.skip("mọi máy muse đã chết — sự cố hạ tầng")
    layout = layout_root_of(alias)
    if not layout:
        pytest.skip("không lấy được layout_root của host")

    root = f"{layout.rstrip('/')}/inputs"
    stamp = uuid.uuid4().hex[:12]
    orphan_key = f"b22orphan{stamp}"
    live_key = f"b22live{stamp}"
    orphan_lock = f"{root}/{orphan_key}.lock"
    live_lock = f"{root}/{live_key}.lock"

    # --- tạo 2 lock trên host -------------------------------------------
    # (1) lock **mồ côi**: không có file `owner` ⇒ không ai giữ.
    # (2) lock **còn sống**: `owner` là tiến trình **đang chạy** của máy này.
    owner = json.dumps({
        "id": uuid.uuid4().hex,
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "started": time.time(),
    }, ensure_ascii=False)
    rc, _out, err = ssh_out(alias, f"""
set -e
mkdir -p '{root}'
rm -rf '{orphan_lock}' '{live_lock}'
mkdir '{orphan_lock}'
touch -d '-600 seconds' '{orphan_lock}'
mkdir '{live_lock}'
printf '%s' '{owner}' > '{live_lock}/owner'
touch -d '-600 seconds' '{live_lock}'
ls -d '{orphan_lock}' '{live_lock}'
""")
    if is_host_down(rc, {}, err):
        pytest.skip(f"host {alias} chết lúc tạo lock — sự cố hạ tầng: {err[-200:]}")
    assert rc == 0, f"không tạo được lock trên {alias}: {err[-300:]}"

    try:
        rc, obj, err = roff_json(["gc"], timeout=600)
        if is_host_down(rc, obj, err):
            pytest.skip(f"host {alias} chết lúc gc — sự cố hạ tầng: {err[-200:]}")
        assert rc == 0, f"roff gc lỗi: rc={rc} {json.dumps(obj, ensure_ascii=False)} {err[-300:]}"

        removed = [str(x) for x in (obj.get("removed_locks") or [])]

        state = remote_path_state(alias, orphan_lock)
        if state is None:
            pytest.skip(f"host {alias} chết lúc đo — sự cố hạ tầng")
        assert state is False, f"lock mồ côi vẫn còn sau gc: {orphan_lock}"
        assert any(orphan_key in item for item in removed), (
            f"JSON của gc không báo lock đã dọn: {removed}")

        state = remote_path_state(alias, live_lock)
        if state is None:
            pytest.skip(f"host {alias} chết lúc đo — sự cố hạ tầng")
        assert state is True, (
            f"gc đã xoá lock **còn chủ** ({live_lock}) — vi phạm nguyên tắc "
            "không xoá khoá đang dùng")
    finally:
        # Dọn đúng những gì test tạo (không đụng gì khác của người dùng).
        ssh_out(alias, f"rm -rf '{orphan_lock}' '{live_lock}'")
