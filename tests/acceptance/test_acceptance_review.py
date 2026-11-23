"""Nghiệm thu các lỗi nghiệm thu phát hiện trong wave W3 — S31, S32.

Chạy:  pytest tests/acceptance/test_acceptance_review.py -q -m acceptance

Hai kịch bản này sinh ra từ **review code** của W3, không phải từ lỗi đã lộ
trên fleet — nhưng cả hai đều là loại lỗi nguy hiểm *âm thầm* (không báo lỗi,
chỉ làm hỏng việc):

* **S31** — `ROFF_SSH_CONFIG` được tài liệu hoá là "config ``~/.ssh/config``
  thay thế" nhưng trước đó **ssh không hề dùng nó**: `fleet` liệt kê host theo
  config thay thế, còn lệnh ssh thật vẫn đọc `~/.ssh/config`. Alias trùng tên
  ⇒ roff lên kế hoạch trên máy A nhưng lệnh chạy trên máy B. S31 chứng minh
  bằng **ssh thật**: một config thay thế trỏ alias về `127.0.0.1:<port đóng>`
  ⇒ lỗi phải là *connection refused*, không phải *không phân giải được tên*.
* **S32** — `reconcile_slots` dọn slot mồ côi. Nếu ledger job **đọc lỗi**,
  danh sách "owner còn sống" phải là *"không biết"* (`None`), không phải *"không
  ai còn sống"* (`set()` rỗng) — nếu là `set()` rỗng thì **mọi** ticket đang xếp
  hàng bị dọn oan ⇒ job xếp hàng chết âm thầm, không ai báo lỗi.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as common  # noqa: E402
_DEFAULT_STATE = common.DEFAULT_STATE  # dùng chung (xem _common.py)



#: State dir **riêng cho test** — test KHÔNG được ghi vào ledger thật của
#: người dùng (``~/.local/state/roff``). Nếu không, mỗi lần chạy nghiệm thu lại

REPO = Path(__file__).resolve().parents[2]
SKILL = REPO / "skill"
LAUNCHER = SKILL / "roff.py"

pytestmark = pytest.mark.acceptance

#: Cổng chắc chắn không có gì lắng nghe trên loopback.
DEAD_PORT = 59999


def roff_json(args, timeout=300, env=None):
    e = common.roff_env(env)
    p = subprocess.run([sys.executable, str(LAUNCHER)] + list(args),
                       capture_output=True, text=True, timeout=timeout, env=e)
    lines = [ln for ln in p.stdout.strip().splitlines() if ln.strip()]
    try:
        obj = json.loads(lines[-1]) if lines else {}
    except ValueError:
        obj = {}
    return p.returncode, obj, p.stderr


# --------------------------------------------------------------------------
# S31 — ssh phải dùng config thay thế
# --------------------------------------------------------------------------
def test_s31_ssh_actually_uses_replacement_ssh_config(tmp_path):
    """`ROFF_SSH_CONFIG` phải tới tay `ssh`, không chỉ tới tay fleet.

    Bằng chứng: config thay thế khai báo alias ``roff-s31-check`` →
    ``127.0.0.1:59999`` (cổng đóng). Nếu ssh đọc config thay thế ⇒ lỗi
    *"connection refused"*; nếu ssh vẫn đọc ``~/.ssh/config`` ⇒ lỗi
    *"could not resolve hostname roff-s31-check"*.
    """
    fake = tmp_path / "ssh_config"
    fake.write_text(
        "Host roff-s31-check\n"
        "    HostName 127.0.0.1\n"
        f"    Port {DEAD_PORT}\n"
        "    User nobody\n",
        encoding="utf-8",
    )
    rc, obj, err = roff_json(
        ["probe", "roff-s31-check"],
        timeout=120,
        env={"ROFF_SSH_CONFIG": str(fake)},
    )
    blob = (json.dumps(obj, ensure_ascii=False) + "\n" + err).lower()

    assert rc != 0, f"probe phải thất bại (host giả), rc={rc} obj={obj}"
    # ssh ĐÃ đọc config thay thế ⇒ đã thử nối 127.0.0.1:59999 ⇒ refused.
    assert "refused" in blob or "connection" in blob, (
        "ssh không dùng config thay thế — lỗi không nhắc tới kết nối: " + blob[:600]
    )
    # Và KHÔNG được phải là lỗi "không phân giải được tên" (tức config thay thế
    # bị bỏ qua).
    assert "resolve" not in blob or "could not resolve hostname" not in blob, (
        "ssh bỏ qua ROFF_SSH_CONFIG (vẫn dùng ~/.ssh/config): " + blob[:600]
    )


def test_s31b_without_replacement_config_alias_is_unknown(tmp_path):
    """Ngược lại: **không** có `ROFF_SSH_CONFIG` ⇒ alias lạ phải là lỗi.

    Đây là mốc đối chiếu cho S31: cùng một lệnh, khác môi trường, khác kết luận.
    """
    rc, obj, err = roff_json(["probe", "roff-s31-khong-ton-tai-xyz"], timeout=120)
    blob = (json.dumps(obj, ensure_ascii=False) + "\n" + err).lower()
    assert rc != 0
    assert "resolve" in blob or "unknown" in blob or "không" in blob, blob[:600]


# --------------------------------------------------------------------------
# S32 — ledger hỏng thì KHÔNG được dọn hàng đợi
# --------------------------------------------------------------------------
def test_s32_broken_ledger_does_not_wipe_the_slot_queue(tmp_path, monkeypatch):
    """Ledger đọc lỗi ⇒ hàng đợi slot phải **giữ nguyên**.

    Mô phỏng "ledger hỏng" theo cách thật: thư mục ``jobs/`` không đọc được
    (``chmod 000``) ⇒ ``os.listdir`` ném ``PermissionError`` ⇒
    ``list_jobs()`` lỗi ⇒ ``reconcile_slots`` **không được** coi là
    "không owner nào sống".
    """
    sys.path.insert(0, str(SKILL))
    try:
        from roffkit import slots as slots_mod
    finally:
        sys.path.pop(0)

    if os.geteuid() == 0:
        pytest.skip("root đọc được mọi thứ — không mô phỏng được ledger hỏng")

    state_dir = tmp_path / "state"
    (state_dir / "jobs").mkdir(parents=True)
    # Ledger job phải nằm **cùng** state dir với sổ slot — nếu không, đọc
    # ledger vẫn thành công (và còn là thư mục thật của người dùng!).
    monkeypatch.setenv("ROFF_STATE_DIR", str(state_dir))

    ledger = slots_mod.SlotLedger(state_dir=str(state_dir))
    ticket = ledger.request_slot("muse-s32-check", 1, "normal", owner="job-s32")
    before = slots_mod.SlotLedger(state_dir=str(state_dir))._read_state()
    assert ticket in [str(e.get("ticket")) for e in before.get("queue", [])], \
        "ticket phải nằm trong hàng đợi trước khi làm ledger hỏng"

    # Làm ledger đọc lỗi.
    jobs_dir = state_dir / "jobs"
    mode = jobs_dir.stat().st_mode
    os.chmod(jobs_dir, 0o000)
    try:
        result = slots_mod.reconcile_slots(state_dir=str(state_dir))
    finally:
        os.chmod(jobs_dir, mode)

    after = slots_mod.SlotLedger(state_dir=str(state_dir))._read_state()
    queue = [str(e.get("ticket")) for e in after.get("queue", [])]
    assert ticket in queue, (
        "hàng đợi bị dọn oan khi ledger hỏng — job xếp hàng chết âm thầm: "
        f"removed={result.get('removed')}"
    )
