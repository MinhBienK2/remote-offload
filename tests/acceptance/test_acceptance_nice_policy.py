"""S48/S49 — chính sách ``nice``/``ionice``: **một** nguồn sự thật (B34) và
**ưu tiên job trên máy remote** (C9).

Bối cảnh (đo thật 23/11/2026):

* **B34** — chính sách ``nice``/``ionice`` bị **copy 5 lần** (probe / sshx /
  tunnel / job / transfer) với **3 nguồn sự thật** (hằng / env / config) ⇒
  ``ROFF_NICE`` **không có tác dụng** với rsync/tar, còn sửa ``[local] nice``
  trong ``~/.config/roff/config.ini`` cũng **không đổi được gì** ⇒ nút cấu
  hình chết.
* **C9** — đo thật: job chạy trên ``muse-phamminhbien3333`` có
  ``remote_nice = 0`` ⇒ lệnh của người dùng chạy **ngang với**
  ``postgres``/``telegram-bot``/``cloudflared`` của chính người dùng.

Sau khi sửa:

* 5 module dùng **cùng** một hàm ⇒ cùng env + cùng config ⇒ **5 kết quả
  bằng nhau** (S48);
* lệnh job trên host chạy ở ``nice 19`` + I/O ``idle`` (S49), đo được bằng
  ``awk '{print $19}' /proc/self/stat`` (trường 19 của ``/proc/self/stat`` là
  nice) — **không** phụ thuộc ``nice`` có sẵn trên host hay không;
* ``nice = 0`` ⇒ **không** bọc ``nice`` (S49b);
* host **không có** ``nice`` ⇒ job vẫn chạy, exit code vẫn đúng (S49c).

Mô tả đầy đủ: `docs/SCENARIOS.md` §12f.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SKILL = REPO / "skill"
if str(SKILL) not in sys.path:
    sys.path.insert(0, str(SKILL))

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as common  # noqa: E402

from roffkit.errors import EXIT_UNREACHABLE, RoffError  # noqa: E402

pytestmark = pytest.mark.acceptance

ALIASES = ["muse-pmb333", "muse-bienpm333", "muse-tampt", "muse-phamminhbien3333"]

#: Lệnh in ra nice của chính nó — trường 19 của ``/proc/self/stat`` (1-based:
#: pid=1, comm=2, state=3, …, nice=19). Không phụ thuộc ``nice`` có sẵn.
PRINT_NICE = "awk '{print $19}' /proc/self/stat"


def _ssh_run(alias: str, script: str, timeout: int = 60):
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", alias, script],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


@pytest.fixture(scope="module")
def host_and_profile():
    """Host Muse **đang sống** đầu tiên (probe thật)."""
    from roffkit.probe import probe

    for alias in ALIASES:
        prof = probe(alias, timeout=30)
        if prof.reachable:
            return alias, prof
    pytest.skip(
        "không có máy muse nào sống — sự cố hạ tầng (đường hầm ngược đang flap), "
        "KHÔNG phải lỗi của roff"
    )


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    """Ledger job cô lập — không đụng ``~/.local/state/roff`` thật."""
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("ROFF_STATE_DIR", str(state))
    return state


# --------------------------------------------------------------------------
# S48 — 5 module, 1 nguồn sự thật (KHÔNG cần fleet)
# --------------------------------------------------------------------------
def test_s48_nice_policy_has_one_source_of_truth(tmp_path, monkeypatch):
    """S48: 5 module, cùng env + cùng config ⇒ **5 kết quả phải bằng nhau**.

    Trước B34 chúng **khác nhau**: ``probe`` dùng hằng, ``sshx``/``tunnel``
    dùng env, ``job`` hardcode, ``transfer`` đọc config ⇒ ``ROFF_NICE`` không
    có tác dụng với rsync/tar và ``[local] nice`` chỉ có tác dụng với đường
    truyền.

    Kịch bản này **không** cần fleet thật ⇒ vẫn kiểm chứng được khi cả 4 máy
    Muse đang chết.
    """
    from roffkit import config as config_mod
    from roffkit import job as job_mod
    from roffkit import probe, sshx, transfer, tunnel

    prefix_fns = {
        "probe": probe._prefix_nice,
        "sshx": sshx._prefix,
        "tunnel": tunnel._prefix,
        "job": job_mod._prefix_nice,
        "transfer": transfer._prefix_nice,
    }

    cases = [
        ({}, None),
        ({"ROFF_NICE": "5"}, None),
        ({"ROFF_NICE": "5"}, "[local]\nnice = 7\n"),
        ({}, "[local]\nnice = 7\n"),
        ({"ROFF_NICE": "0"}, None),
        ({"ROFF_IONICE": "none"}, None),
        ({"ROFF_IONICE": "realtime"}, None),
        ({"ROFF_IONICE": "best-effort"}, None),
    ]

    for env, body in cases:
        for name in ("ROFF_NICE", "ROFF_IONICE"):
            monkeypatch.delenv(name, raising=False)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        cfg = tmp_path / "config.ini"
        if body is None:
            cfg.unlink(missing_ok=True)
        else:
            cfg.write_text(body, encoding="utf-8")
        monkeypatch.setenv("ROFF_CONFIG", str(cfg))

        results = {name: fn() for name, fn in prefix_fns.items()}
        distinct = {tuple(v) for v in results.values()}
        assert len(distinct) == 1, (
            f"env={env} config={body!r}: 5 module cho ra {len(distinct)} kết quả "
            f"khác nhau ⇒ chính sách nice/ionice lại bị copy: {results}"
        )
        assert results["probe"] == config_mod.local_nice_prefix()


# --------------------------------------------------------------------------
# S49 — job trên máy remote chạy ở nice đã hạ
# --------------------------------------------------------------------------
def _launch_job(alias: str, layout_root: str, cmd: str, project: str):
    """Launch 1 job thật lên host; trả ``(job, jobdir, workdir)``."""
    from roffkit import job as job_mod

    workdir = f"{layout_root}/projects/{project}"
    prep = _ssh_run(alias, f"mkdir -p '{workdir}' && echo READY")
    assert prep.returncode == 0 and "READY" in prep.stdout, (
        f"không tạo được workdir trên {alias}: {prep.stderr[-300:]}")

    jid = job_mod.new_job_id()
    j = job_mod.Job(id=jid, cmd=cmd, host=alias, project=project)
    j.out_remote = f"{layout_root}/jobs/{jid}/out"
    job_mod.save_job(j)
    job_mod.set_job_options(jid, workdir=workdir, layout_root=layout_root, input_dir="")
    try:
        job_mod.launch(alias, j, env={}, cpus=1, mem_mb=64)
    except RoffError as exc:
        # Host chết **đúng lúc launch** ⇒ 255 (tầng ssh) ⇒ **hạ tầng** ⇒
        # `skip`, không `fail` (nguyên tắc `docs/REPORT.md` §2). Đo thật
        # 24/11/2026: `muse-tampt` chết giữa chừng ⇒ S49c đỏ oan; chạy lại
        # ⇒ PASS. Lỗi **khác** 255 (vd 97 = thiếu workdir) ⇒ lỗi thật ⇒ đỏ.
        if exc.code == EXIT_UNREACHABLE:
            pytest.skip(f"host {alias} chết lúc launch job — sự cố hạ tầng: {exc}")
        raise
    return j, os.path.dirname(j.out_remote), workdir


def _wait_job(alias: str, job, timeout: int = 240) -> dict:
    """Chờ job kết thúc; **skip** nếu host chết trong lúc chờ.

    Host chết giữa chừng là **hạ tầng** ⇒ phải `skip`, không `fail` (nguyên
    tắc `docs/REPORT.md` §2). Đo thật 24/11/2026: `muse-tampt` chết giữa
    lúc ⇒ S49c đỏ oan (chạy lại ⇒ PASS).

    Trạng thái ``lost`` phải thấy **liên tiếp** vài lần mới kết luận — xem
    :func:`_common.wait_state_settled` (lý do: ``remote_state()`` đọc
    ``kill -0 $PID`` trên host ⇒ đọc trúng khoảnh giữa lúc ghi ``pid`` và
    lúc ghi ``exit`` thì verdict **sai**; đo thật 24/11/2026: job chạy
    **thành công** (``exit=0``, ``log=19``) nhưng poll báo ``vanished``).
    """
    return common.wait_state_settled(alias, job, timeout=timeout, poll=3.0)


def _job_log(alias: str, jobdir: str) -> str:
    out = _ssh_run(alias, f"cat '{jobdir}/log' 2>/dev/null")
    return out.stdout


def test_s49_remote_job_runs_at_nice_19(host_and_profile, ledger, tmp_path):
    """S49: lệnh job trên máy remote phải chạy ở **nice 19**.

    Đo bằng chính lệnh của job (``awk '{print $19}' /proc/self/stat``) ⇒ đo
    **trên host**, không suy đoán từ code.

    Đạt khi: giá trị nice job in ra trên host = **19**, và job có exit code 0
    ⇒ ``$JOBDIR/exit`` được ghi (cấm dùng ``exec``).
    """
    alias, prof = host_and_profile
    layout_root = prof.layout_root or "/root/.local/share/roff"

    job, jobdir, _workdir = _launch_job(
        alias, layout_root, PRINT_NICE, "acceptance-nice"
    )
    state = _wait_job(alias, job)
    assert state.get("state") == "exited", f"job không kết thúc: {state}"
    assert int(state.get("exit", -1)) == 0, f"exit code sai: {state}"

    log = _job_log(alias, jobdir)
    measured = [ln.strip() for ln in log.splitlines() if ln.strip()]
    assert measured, f"log rỗng — job không in ra gì: {log!r}"
    assert measured[-1] == "19", (
        f"nice của job trên host = {measured[-1]!r} (mong đợi '19') — "
        f"lệnh của người dùng vẫn chạy ngang với postgres/telegram-bot"
    )


def test_s49b_remote_job_nice_zero_is_not_wrapped(host_and_profile, ledger, tmp_path, monkeypatch):
    """S49b: ``nice = 0`` ⇒ **không** bọc ``nice`` ⇒ job chạy ở nice 0."""
    alias, prof = host_and_profile
    layout_root = prof.layout_root or "/root/.local/share/roff"
    monkeypatch.setenv("ROFF_REMOTE_NICE", "0")

    job, jobdir, _workdir = _launch_job(
        alias, layout_root, PRINT_NICE, "acceptance-nice-0"
    )
    state = _wait_job(alias, job)
    assert state.get("state") == "exited", f"job không kết thúc: {state}"
    assert int(state.get("exit", -1)) == 0, f"exit code sai: {state}"

    log = _job_log(alias, jobdir)
    measured = [ln.strip() for ln in log.splitlines() if ln.strip()]
    assert measured and measured[-1] == "0", (
        f"nice của job = {measured[-1] if measured else None!r} (mong đợi '0')"
    )


def test_s49c_remote_job_runs_when_host_has_no_nice(host_and_profile, ledger, tmp_path):
    """S49c: host **không có** ``nice`` ⇒ job vẫn chạy, exit code vẫn đúng.

    Giả lập bằng cách chạy lại **đúng** ``run.sh`` đã ship lên host với
    ``PATH`` chỉ chứa các lệnh script cần (``sh``, ``cat``, ``dirname``,
    ``awk``) — **không** có ``nice``/``ionice``.
    """
    alias, prof = host_and_profile
    layout_root = prof.layout_root or "/root/.local/share/roff"

    job, jobdir, workdir = _launch_job(
        alias, layout_root, PRINT_NICE, "acceptance-nice-nonice"
    )
    state = _wait_job(alias, job)
    assert state.get("state") == "exited", f"job không kết thúc: {state}"

    script = (
        f"d='{jobdir}/bin-khong-nice'; mkdir -p \"$d\"; "
        "for t in sh cat dirname awk; do p=$(command -v $t); "
        '[ -n "$p" ] && ln -sf "$p" "$d/$t"; done; '
        f"cd '{jobdir}' && ROFF_WORK='{workdir}' PATH=\"$d\" /bin/sh '{jobdir}/run.sh'; "
        'echo "rc=$?"'
    )
    out = _ssh_run(alias, script, timeout=120)
    assert out.returncode == 0, f"ssh lỗi: {out.stderr[-300:]}"
    assert "rc=0" in out.stdout, (
        f"job phải chạy được khi host không có nice — stdout: {out.stdout!r} "
        f"stderr: {out.stderr[-300:]!r}"
    )
    measured = [ln.strip() for ln in out.stdout.splitlines() if ln.strip().isdigit()]
    assert measured and measured[-1] == "0", (
        f"nice của job khi host không có nice = {measured[-1:]!r} (mong đợi '0')"
    )
    exit_file = _ssh_run(alias, f"cat '{jobdir}/exit' 2>/dev/null")
    assert exit_file.stdout.strip() == "0", (
        f"$JOBDIR/exit phải được ghi (cấm dùng exec): {exit_file.stdout!r}"
    )
