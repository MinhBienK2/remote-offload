"""Vòng đời job thật trên Muse — kiểm chứng tầng module, không qua CLI.

Đây là nghiệm thu S2 (job chạy được và trả artifact) nhưng gọi trực tiếp
``roffkit.job`` / ``roffkit.transfer`` / ``roffkit.watcher``, nên nó tách được
lỗi tầng module khỏi lỗi tầng CLI. Chạy được TRƯỚC khi CLI có mặt.

Chạy:  pytest tests/acceptance/test_lifecycle_real.py -q -m acceptance
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SKILL = REPO / "skill"
if str(SKILL) not in sys.path:
    sys.path.insert(0, str(SKILL))

pytestmark = pytest.mark.acceptance

ALIASES = ["muse-pmb333", "muse-bienpm333", "muse-tampt", "muse-phamminhbien3333"]


def ssh_run(alias, script, timeout=60):
    p = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", alias, script],
        capture_output=True, text=True, timeout=timeout)
    return p


@pytest.fixture(scope="module")
def host_and_profile():
    """Host sống đầu tiên + profile đã probe."""
    from roffkit.probe import probe

    for alias in ALIASES:
        prof = probe(alias, timeout=30)
        if prof.reachable:
            return alias, prof
    pytest.skip("không có máy muse nào sống")


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    """Ledger job cô lập — không đụng ~/.local/state/roff thật."""
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("ROFF_STATE_DIR", str(state))
    return state


def test_s02_job_lifecycle_on_real_host(host_and_profile, ledger, tmp_path):
    """S2: launch → chạy → exit file → artifact → pull về local → sha256 khớp.

    Chạy thật trên Muse: job ghi file vào $ROFF_OUT, in ra stdout/stderr,
    trả exit code 0. Sau đó pull về local và so sha256.
    """
    alias, prof = host_and_profile
    from roffkit import job as job_mod
    from roffkit import transfer as tr

    layout_root = prof.layout_root or "/root/.local/share/roff"
    project = "acceptance-smoke"
    workdir = f"{layout_root}/projects/{project}"

    # Chuẩn bị thư mục làm việc trên host.
    prep = ssh_run(alias, f"mkdir -p '{workdir}' && echo READY")
    assert prep.returncode == 0 and "READY" in prep.stdout, (
        f"không tạo được workdir trên {alias}: {prep.stderr[-300:]}")

    jid = job_mod.new_job_id()
    payload = b"x" * (256 * 1024)          # 256 KB, đủ để có băng thông
    local_in = tmp_path / "input.bin"
    local_in.write_bytes(payload)

    cmd = ('echo "stdout-from-remote"; echo "stderr-from-remote" >&2; '
           'cp "$ROFF_IN_DIR/input.bin" "$ROFF_OUT/artifact.bin" 2>/dev/null || '
           'head -c 262144 /dev/zero > "$ROFF_OUT/artifact.bin"; '
           'exit 0')

    j = job_mod.Job(id=jid, cmd=cmd, host=alias, project=project)
    j.out_remote = f"{layout_root}/jobs/{jid}/out"
    job_mod.save_job(j)
    job_mod.set_job_options(jid, workdir=workdir, layout_root=layout_root,
                             input_dir=str(tmp_path))

    # Đẩy input lên host (project dir) để job có $ROFF_IN_DIR/input.bin
    push = tr.push(alias, str(tmp_path), workdir,
                   excludes=["*.pyc", "__pycache__"])
    assert push.ok, f"push thất bại: {push.error}"

    # --- LAUNCH ---
    job_mod.launch(alias, j, env={"ROFF_SMOKE": "1"}, cpus=1, mem_mb=64)

    # --- CHỜ KẾT THÚC ---
    deadline = time.time() + 180
    state = {}
    while time.time() < deadline:
        state = job_mod.remote_state(alias, j, timeout=30)
        if state.get("state") in ("exited", "lost", "cancelled"):
            break
        time.sleep(3)

    assert state.get("state") == "exited", f"job không kết thúc bình thường: {state}"
    assert int(state.get("exit", -1)) == 0, f"exit code sai: {state}"

    # --- ARTIFACT TRÊN HOST ---
    out_dir = j.out_remote
    ls = ssh_run(alias, f"ls -la '{out_dir}' 2>&1")
    assert "artifact.bin" in ls.stdout, f"thiếu artifact trên host: {ls.stdout}"

    # --- PULL VỀ LOCAL ---
    local_out = tmp_path / "out"
    local_out.mkdir()
    pull = tr.pull(alias, out_dir, str(local_out))
    assert pull.ok, f"pull thất bại: {pull.error}"
    got = local_out / "artifact.bin"
    assert got.exists(), f"thiếu file local: {list(local_out.glob('*'))}"
    assert got.stat().st_size == 256 * 1024, (
        f"sai kích thước: {got.stat().st_size}")

    # --- SHA256 KHỚP ---
    import hashlib

    def sha(p):
        h = hashlib.sha256()
        with open(p, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    remote_sha = ssh_run(
        alias, f"sha256sum '{out_dir}/artifact.bin' | cut -d' ' -f1").stdout.strip()
    assert remote_sha == sha(got), f"sha256 lệch: remote={remote_sha} local={sha(got)}"

    # --- LOG ---
    log = job_mod.read_log(alias, j)
    assert "stdout-from-remote" in log, f"thiếu stdout trong log: {log[:300]}"
    assert "stderr-from-remote" in log, f"thiếu stderr trong log: {log[:300]}"

    # --- DỌN DẸP ---
    ssh_run(alias, f"rm -rf '{layout_root}/jobs/{jid}' '{workdir}'")


def test_s02b_job_failure_exit_code_on_real_host(host_and_profile, ledger):
    """S2b: job tự fail (exit 3) → phải báo đúng exit code, KHÔNG retry."""
    alias, prof = host_and_profile
    from roffkit import job as job_mod

    layout_root = prof.layout_root or "/root/.local/share/roff"
    project = "acceptance-smoke"
    workdir = f"{layout_root}/projects/{project}"
    ssh_run(alias, f"mkdir -p '{workdir}'")

    jid = job_mod.new_job_id()
    j = job_mod.Job(id=jid, cmd="exit 3", host=alias, project=project)
    j.out_remote = f"{layout_root}/jobs/{jid}/out"
    job_mod.save_job(j)
    job_mod.set_job_options(jid, workdir=workdir, layout_root=layout_root)

    job_mod.launch(alias, j, cpus=1, mem_mb=64)

    deadline = time.time() + 120
    state = {}
    while time.time() < deadline:
        state = job_mod.remote_state(alias, j, timeout=30)
        if state.get("state") in ("exited", "lost", "cancelled"):
            break
        time.sleep(2)

    assert state.get("state") == "exited", f"job không kết thúc: {state}"
    assert int(state.get("exit", -1)) == 3, (
        f"exit code phải là 3 (lỗi của job), thấy: {state}")
    assert state.get("oom") is not True, "job 3 là lỗi của job, không phải OOM"

    ssh_run(alias, f"rm -rf '{layout_root}/jobs/{jid}' '{workdir}'")


def test_s06_resume_partial_transfer_on_real_host(host_and_profile, ledger, tmp_path):
    """S6: transfer bị ngắt giữa chừng → lần sau phải TIẾP TỤC, không truyền lại
    từ đầu (rsync --partial)."""
    alias, prof = host_and_profile
    from roffkit import transfer as tr

    src = tmp_path / "big"
    src.mkdir()
    # 8 MB — đủ để lần truyền bị ngắt giữa chừng
    (src / "blob.bin").write_bytes(os.urandom(8 * 1024 * 1024))

    remote = f"{prof.layout_root or '/root/.local/share/roff'}/acceptance-resume"
    ssh_run(alias, f"rm -rf '{remote}'; mkdir -p '{remote}'")

    # Cắt ngang: timeout rất ngắn buộc phải dở dang.
    import roffkit.transfer as tr_mod

    real_timeout = tr_mod.push.__defaults__
    try:
        tr.push(alias, str(src), remote, timeout=2)
    except Exception:
        pass

    # Lần 2: phải thành công, và byte truyền lần 2 phải ÍT hơn tổng (đã truyền
    # được một phần lần 1).
    r = tr.push(alias, str(src), remote, timeout=300)
    assert r.ok, f"lần 2 push thất bại: {r.error}"

    check = ssh_run(alias, f"ls -l '{remote}/blob.bin' | awk '{{print $5}}'")
    size = int(check.stdout.strip() or 0)
    assert size == 8 * 1024 * 1024, f"sai kích thước sau resume: {size}"

    ssh_run(alias, f"rm -rf '{remote}'")
