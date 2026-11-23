"""Tiện ích chung cho bộ nghiệm thu.

Vai trò: **phân biệt lỗi của `roff` với lỗi của hạ tầng**.

Fleet `muse-*` của người dùng flap liên tục (reverse tunnel rớt giữa chừng).
Một job có thể **mất** vì máy chết (exit 69, `reason=host_restarted`) — đó là
sự cố hạ tầng, **không** phải lỗi của `roff`. Nếu test coi nó là FAIL thì báo
cáo đo được **độ ổn định của đường truyền**, chứ không phải **chất lượng của
`roff`** — nguyên tắc đã chốt ở `docs/REPORT.md` §2.

⇒ Mọi chỗ chờ job trong bộ nghiệm thu đều đi qua `wait_or_skip()`.
"""
from __future__ import annotations

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
LAUNCHER = REPO / "skill" / "roff.py"

#: **Một** state dir tạm cho **toàn bộ** bộ nghiệm thu.
#:
#: Bộ nghiệm thu **không** được ghi vào ledger thật của người dùng
#: (``~/.local/state/roff``). Trước đây mỗi file test tự tạo state dir riêng
#: còn ``roff wait`` thì tra **ledger thật** ⇒ ``wait`` không thấy job do
#: ``do`` tạo ⇒ test treo hết timeout (bug B19, đo thật 23/11/2026).
#:
#: Vì vậy: **mọi** lệnh ``roff`` trong bộ nghiệm thu (kể cả ``wait``) phải
#: dùng **chính** thư mục này.
DEFAULT_STATE = tempfile.mkdtemp(prefix="roff-acceptance-state-")
atexit.register(shutil.rmtree, DEFAULT_STATE, True)


def roff_env(env=None) -> dict:
    """Env cho lệnh ``roff`` — **luôn** cô lập khỏi ledger thật.

    ``env`` rõ ràng (ví dụ ``{"ROFF_CONFIG": ...}``) được trộn vào **sau**,
    nên nó thắng — nhưng không ai được vô tình ghi đè ``ROFF_STATE_DIR``.
    """
    e = dict(os.environ)
    e["ROFF_STATE_DIR"] = DEFAULT_STATE
    if env:
        e.update(env)
    return e


def ledger_of(job_id):
    """Nội dung ledger của 1 job trong state dir của **bộ nghiệm thu**.

    Trả ``None`` nếu không có file (hoặc file hỏng) — không ném lỗi: test
    kiểm tra "ledger **không** ghi lỗi" thì thiếu file cũng là đạt.
    """
    path = Path(DEFAULT_STATE) / "jobs" / f"{job_id}.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def pick_live(hosts, timeout=15):
    """Host đầu tiên **đang** sống trong ``hosts`` (probe thật, không đọc cache).

    Fixture ``fleet`` là **module-scope** ⇒ nó tính "ai sống" **một** lần cho
    cả module. Fleet Muse flap liên tục ⇒ tới lúc test chạy, ``fleet[0]`` có
    thể đã chết ⇒ test đỏ với lý do **hạ tầng** (đo thật 23/11/2026).

    Vì vậy: chọn host **lúc chạy test**, không dựa vào fixture.
    """
    for host in hosts or []:
        try:
            r = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6",
                 str(host), "true"],
                capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.SubprocessError:
            continue
        if r.returncode == 0:
            return str(host)
    return None


def pick_live_all(hosts, timeout=15):
    """Tất cả host **đang** sống trong ``hosts``, theo thứ tự đã cho.

    Dùng cho kịch bản cần **nhiều** máy (S18 đo tăng tốc song song): đếm
    theo fixture module-scope sẽ tính cả máy đã chết ⇒ tưởng có 4 máy
    nhưng thực chỉ 1 ⇒ test đỏ vì lý do hạ tầng.
    """
    live = []
    for host in hosts or []:
        try:
            r = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6",
                 str(host), "true"],
                capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.SubprocessError:
            continue
        if r.returncode == 0:
            live.append(str(host))
    return live


#: Mã thoát khi artifact **không về được** (``pull_failed``, B23).
#:
#: Cố tình khai báo cục bộ (không import ``roffkit.errors``) để file này không
#: phụ thuộc package — giống :data:`JOB_LOST_REASONS` bên dưới.
EXIT_PULL_FAILED = 74

#: Lý do ⇒ **host chết / không dùng được** (lỗi hạ tầng, không phải lỗi roff).
HOST_DEATH_REASONS = {
    "no_host",
    "host_unreachable",
    "unknown_host",
    "empty_fleet",
}

#: Lý do ⇒ job đã chạy được nhưng **mất** vì máy chết giữa chừng.
JOB_LOST_REASONS = {
    "host_restarted",
    "host_wiped",
    "oom_killed",
    "vanished",
    # Lỗi **hạ tầng** phía host, không phải lỗi của job:
    # - `input_push_failed`: kho input dùng chung của host kẹt / host flap
    #   (B17) — watcher đã failover, nhưng nếu failover cũng hỏng thì job mất
    #   với lý do này ⇒ vẫn là sự cố hạ tầng, không phải lỗi roff.
    # - `workdir_missing`: host không tạo được thư mục làm việc (disk/host flap).
    "input_push_failed",
    "workdir_missing",
}


def roff_json(args, timeout=300, env=None, cwd=None):
    """Chạy `roff` và trả ``(rc, json, stderr)``; stdout **phải** là 1 JSON.

    Args:
        args: tham số lệnh.
        timeout: giới hạn giây.
        env: biến môi trường **thêm** (đã đặt `ROFF_STATE_DIR` tạm trước).
        cwd: thư mục làm việc của lệnh (dùng cho `--sync-back`: project local
            là thư mục này, và đường dẫn `--sync-back` được hiểu theo nó).
    """
    p = subprocess.run(
        [sys.executable, str(LAUNCHER), *[str(a) for a in args]],
        capture_output=True, text=True, timeout=timeout, env=roff_env(env),
        cwd=str(cwd) if cwd else None,
    )
    lines = [ln for ln in p.stdout.strip().splitlines() if ln.strip()]
    try:
        obj = json.loads(lines[-1]) if lines else {}
    except (json.JSONDecodeError, ValueError):
        obj = {}
    return p.returncode, obj, p.stderr


def wait_or_skip(jid, timeout=300, env=None, wait_timeout=None, extra=None):
    """Chờ job tới trạng thái cuối.

    * job **mất vì host chết** ⇒ ``pytest.skip`` — không phải lỗi của `roff`;
    * **artifact không về** vì máy chết (exit 74, ``pull_failed``) ⇒ ``pytest.skip``
      — cũng là sự cố hạ tầng (B23); nhưng nếu nguyên nhân **không** phải máy
      chết thì trả rc=74 để test **FAIL** (lỗi thật của `roff`);
    * còn lại ⇒ trả ``(rc, json, stderr)`` như bình thường (kể cả rc≠0).
    """
    secs = int(wait_timeout if wait_timeout is not None else timeout)
    args = ["wait", jid, "--timeout", str(secs)]
    if extra:
        args += list(extra)
    rc, obj, err = roff_json(args, timeout=timeout + 60, env=env)
    reason = str(obj.get("reason") or "")
    if rc == 69 or reason in JOB_LOST_REASONS:
        pytest.skip(
            "job %s mất vì host chết (reason=%s) — sự cố hạ tầng, không phải lỗi roff"
            % (jid, reason or "69")
        )
    if rc == EXIT_PULL_FAILED:
        detail = str(obj.get("pull_error") or obj.get("detail") or "")
        if is_host_down(255, {"detail": detail}, err):
            pytest.skip(
                "job %s chạy xong nhưng artifact không về vì máy chết (%s) — "
                "sự cố hạ tầng" % (jid, detail[:160])
            )
    return rc, obj, err


#: Dấu hiệu **máy chết / đường truyền đứt** trong lỗi ssh.
#:
#: Reverse tunnel của fleet Muse rớt liên tục (đo thật 23/11/2026: cả 4 máy
#: restart trong lúc đợt nghiệm thu chạy) ⇒ lệnh `roff` chết với rc=255 và
#: một trong các thông điệp dưới đây. Đó là **sự cố hạ tầng** — test phải
#: `skip`, không được `fail` (nguyên tắc `docs/REPORT.md` §2).
HOST_DOWN_MARKERS = (
    "connection refused",
    "connection timed out",
    "connection reset",
    "connection closed",
    "no route to host",
    "kex_exchange_identification",
    "banner exchange",
    "broken pipe",
    "host is down",
    "network is unreachable",
    # `subprocess.TimeoutExpired` in ra: "Command '[...]' timed out after 60
    # seconds" — dấu hiệu **tầng ssh bị treo**, không phải lỗi logic của roff.
    # Khoá theo cụm "timed out after" để **không** trùng với "job timeout"
    # (exit 124 — lỗi thật, phải FAIL).
    "timed out after",
    # `rc=255` — mã thoát **tầng ssh** theo hợp đồng exit-code
    # (`docs/API-CONTRACT.md` §2). `roff` tự báo "rc=255 khi mkdir …" khi
    # lệnh ssh giữa chừng chết ⇒ đó là máy chết/đường truyền đứt.
    "rc=255",
)


#: Số lần liên tiếp phải thấy ``lost`` mới kết luận job đã mất.
#:
#: ``remote_state()`` đọc ``kill -0 $PID`` trên host ⇒ nằm giữa lúc process
#: vừa sinh / vừa chết và lúc file ``exit`` được ghi, verdict có thể **sai**.
#: Đo thật 24/11/2026: job chạy **thành công** (``exit=0``) nhưng poll đọc
#: trúng khoảnh giữa lúc ghi ``pid`` và lúc ghi ``exit`` ⇒ báo ``vanished``.
#: ⇒ "chưa chắc chắn thì chờ và thử lại" (đúng quy tắc vàng của sản phẩm).
LOST_CONFIRMATIONS = 3


def _host_alive(alias: str) -> bool:
    """Host còn với được không (1 lần ssh rẻ)."""
    try:
        p = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", alias, "echo UP"],
            capture_output=True, text=True, timeout=20,
        )
    except Exception:
        return False
    return p.returncode == 0 and "UP" in p.stdout


def wait_state_settled(alias, job, timeout=240.0, poll=3.0):
    """Chờ trạng thái job **ổn định** trên host.

    ``exited``/``cancelled`` ⇒ kết luận ngay. ``lost`` ⇒ phải thấy
    **liên tiếp** :data:`LOST_CONFIRMATIONS` lần mới kết luận (xem
    :data:`LOST_CONFIRMATIONS`).

    Returns:
        dict trạng thái cuối (cùng kiểu ``remote_state()`` trả về).
    """
    from roffkit import job as job_mod

    deadline = time.monotonic() + timeout
    state: dict = {}
    lost_seen = 0
    while True:
        try:
            state = job_mod.remote_state(alias, job, timeout=30)
        except Exception:
            state = {}
        kind = state.get("state")
        if kind == "lost":
            lost_seen += 1
            if lost_seen >= LOST_CONFIRMATIONS:
                return state
        else:
            lost_seen = 0
            if kind in ("exited", "cancelled"):
                return state
        if not state and not _host_alive(alias):
            pytest.skip(f"host {alias} chết lúc chờ job — sự cố hạ tầng")
        if time.monotonic() >= deadline:
            return state
        time.sleep(poll)


def is_host_down(rc: int, obj: dict | None = None, err: str = "") -> bool:
    """Lệnh `roff` chết vì **máy remote chết** hay không?

    Args:
        rc: exit code của lệnh `roff`.
        obj: JSON trả về (nếu có).
        err: stderr của lệnh `roff`.

    Returns:
        ``True`` ⇒ sự cố **hạ tầng** ⇒ test phải ``pytest.skip``.
        ``False`` ⇒ lỗi **của roff** ⇒ test phải ``fail`` để lộ ra.
    """
    obj = obj or {}
    reason = str(obj.get("reason") or "")
    detail = " ".join(
        str(obj.get(k) or "") for k in ("detail", "error", "message")
    ) + " " + str(err or "")
    lowered = detail.lower()
    if any(marker in lowered for marker in HOST_DOWN_MARKERS):
        return True
    return rc == 255 and reason in ("host_unreachable", "no_host", "unknown_host")
