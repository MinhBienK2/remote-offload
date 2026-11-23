"""Unit test cho ``roffkit.cli`` — hợp đồng stdout/exit code của CLI.

Nguyên tắc được khoá lại ở đây:

* **mọi** lệnh in **đúng 1 JSON object** ra stdout (trừ ``roff logs`` — text);
* lỗi argparse → **stderr** + exit **2**, **không** in JSON;
* ``RoffError`` → JSON payload + exit code tương ứng;
* exit **124** (wait hết giờ) **không phải lỗi** (``ok: true``,
  ``state: "running"``);
* **không bao giờ** chạy ``--cmd`` trên máy local;
* secret đi qua ``--env``: **không** nằm trong argv, **không** nằm trong audit log.

Test chạy CLI như **tiến trình con** (subprocess) để kiểm tra hợp đồng
stdout/exit code thật — không mock. Mạng được thay bằng ``ssh`` giả trong
``tests/fake/`` (fixture ``fake_ssh``).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SKILL = REPO / "skill"
LAUNCHER = SKILL / "roff.py"

if str(SKILL) not in sys.path:
    sys.path.insert(0, str(SKILL))

from roffkit import job as job_mod  # noqa: E402


# --------------------------------------------------------------------------
# hạ tầng test
# --------------------------------------------------------------------------
PROBE_OUTPUT = "\n".join(
    [
        "ROFF_HOSTNAME=htch-runtime",
        "ROFF_BOOT_ID=boot-1",
        "ROFF_HOME=/root",
        "ROFF_CORES=4",
        "ROFF_UPTIME_S=100000",
        "ROFF_LOAD1=0.10",
        "ROFF_MEM_TOTAL_KB=8388608",
        "ROFF_MEM_AVAILABLE_KB=921600",
        "ROFF_LAYOUT_ROOT=/home/hatch/.roff",
        "ROFF_DURABLE=1",
        "ROFF_DISK_FREE_MB=102400",
        "ROFF_INSTANCE_TOKEN=tok-1",
        "ROFF_CGROUP_LIMIT_MB=",
        "ROFF_CGROUP_USED_MB=",
        "ROFF_TOOL=ffmpeg=/usr/bin/ffmpeg",
        "ROFF_TOOL=node=/usr/bin/node",
        "ROFF_TOOL=python3=/usr/bin/python3",
        "ROFF_INTERNET_CODE=200",
        "ROFF_INTERNET_RC=0",
        "ROFF_INTERNET_ERR=",
        "",
    ]
)

LAUNCH_OUTPUT = "\n".join(
    [
        "ROFF_PID=4242",
        "ROFF_BOOT_ID=boot-1",
        "ROFF_INSTANCE_TOKEN=tok-1",
        "",
    ]
)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Env cô lập cho CLI: state riêng, config riêng, ssh config riêng."""
    state = tmp_path / "state"
    state.mkdir()
    config = tmp_path / "config.ini"
    config.write_text(
        "[fleet]\n"
        "include = muse-*\n"
        "[local]\n"
        "cache_max_mb = 1\n"
        "cache_min_free_mb = 0\n"
    )
    ssh_config = tmp_path / "ssh_config"
    ssh_config.write_text(
        "Host muse-a\n  HostName 127.0.0.1\n  Port 2222\n"
        "Host muse-b\n  HostName 127.0.0.1\n  Port 2223\n"
    )
    monkeypatch.setenv("ROFF_STATE_DIR", str(state))
    monkeypatch.setenv("ROFF_CONFIG", str(config))
    monkeypatch.setenv("ROFF_SSH_CONFIG", str(ssh_config))
    # Không spawn watcher nền trong test (nếu không, watcher sẽ quay vòng vô hạn).
    monkeypatch.setenv("ROFF_NO_WATCHER", "1")
    # Chờ slot ngắn để test fail nhanh thay vì treo 15 phút.
    monkeypatch.setenv("ROFF_SLOT_WAIT_S", "5")
    return {
        "state": state,
        "config": config,
        "ssh_config": ssh_config,
        # ghi đè riêng cho từng test (đọc lúc chạy, sau khi fixture ssh giả set PATH)
        "extra": {},
    }


def run(args, env, timeout=60):
    """Chạy CLI như tiến trình con; trả (rc, stdout, stderr).

    Env của tiến trình con = ``os.environ`` hiện tại (đã có PATH của ssh giả do
    fixture ``fake_ssh`` set) + phần ghi đè riêng của test.
    """
    child = dict(os.environ)
    child.update(env.get("extra") or {})
    proc = subprocess.run(
        [sys.executable, str(LAUNCHER)] + [str(a) for a in args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=child,
    )
    return proc.returncode, proc.stdout, proc.stderr


def only_json(stdout):
    """Parse stdout thành đúng 1 JSON object (fail loud nếu không phải)."""
    text = stdout.strip()
    assert text, "stdout rỗng — lệnh phải in 1 JSON object"
    obj = json.loads(text)  # json.loads fail nếu có thêm text nào
    assert isinstance(obj, dict), "JSON phải là object"
    return obj


def good_host(fake_ssh):
    """Cấu hình 1 host giả trả lời đúng cho probe + launch."""
    fake_ssh(
        "muse-a",
        responses=[
            {"match": "nproc", "stdout": PROBE_OUTPUT},
            {"match": "launch.sh", "stdout": LAUNCH_OUTPUT},
        ],
    )


def make_job(job_id="261123-101530-ab12", **kwargs):
    """Tạo 1 job trong ledger local (dùng để test status/wait/result)."""
    base = dict(
        id=job_id,
        cmd="echo hello",
        host="muse-a",
        project="proj",
        created_at=time.time(),
        out_remote=f"/home/hatch/.roff/jobs/{job_id}/out",
    )
    base.update(kwargs)
    job = job_mod.Job(**base)
    job_mod.save_job(job)
    return job


# --------------------------------------------------------------------------
# hợp đồng chung của CLI
# --------------------------------------------------------------------------
def test_help_goes_to_stderr_stdout_stays_empty(env):
    """``--help`` không được in ra stdout (stdout chỉ dành cho JSON)."""
    rc, out, err = run(["--help"], env)
    assert rc == 0
    assert out.strip() == ""
    assert "do" in err


def test_unknown_command_is_exit_2_without_json(env):
    """Lệnh lạ ⇒ stderr + exit 2, stdout rỗng (không in JSON)."""
    rc, out, err = run(["khong-ton-tai"], env)
    assert rc == 2
    assert out.strip() == ""
    assert err.strip()


def test_no_command_is_exit_2(env):
    """Không có lệnh ⇒ usage ra stderr + exit 2."""
    rc, out, err = run([], env)
    assert rc == 2
    assert out.strip() == ""
    assert err.strip()


def test_do_requires_cmd_is_argparse_error(env):
    """``--cmd`` bắt buộc ⇒ lỗi argparse ⇒ stderr + exit 2, không JSON."""
    rc, out, err = run(["do"], env)
    assert rc == 2
    assert out.strip() == ""
    assert err.strip()


def test_do_zero_timeout_is_rejected(env):
    """R17: ``--timeout 0`` bị từ chối (exit 2, JSON lỗi)."""
    rc, out, _err = run(["do", "--cmd", "echo x", "--timeout", "0"], env)
    assert rc == 2
    payload = only_json(out)
    assert payload["ok"] is False
    assert payload["exit_code"] == 2
    assert payload["reason"] == "invalid_timeout"


def test_do_invalid_env_is_rejected(env):
    """``--env`` phải có dạng KEY=VALUE."""
    rc, out, _err = run(["do", "--cmd", "echo x", "--env", "KHONGCOEQU"], env)
    assert rc == 2
    payload = only_json(out)
    assert payload["ok"] is False
    assert payload["reason"] == "invalid_env"


def test_do_invalid_mem_is_rejected(env):
    """``--mem`` sai định dạng ⇒ exit 2."""
    rc, out, _err = run(["do", "--cmd", "echo x", "--mem", "khong-phai-so"], env)
    assert rc == 2
    payload = only_json(out)
    assert payload["reason"] == "invalid_size"


# --------------------------------------------------------------------------
# nguyên tắc 1: không bao giờ chạy --cmd trên máy local
# --------------------------------------------------------------------------
def test_do_without_usable_host_is_exit_10_and_lists_reasons(env, fake_ssh, tmp_path):
    """Không host nào dùng được ⇒ exit 10 + lý do loại từng host."""
    fake_ssh("muse-a", ssh_error="ssh: connect to host 127.0.0.1 port 2222: Connection refused")
    marker = tmp_path / "ran-locally"
    rc, out, _err = run(
        ["do", "--cmd", f"touch {marker}", "--name", "proj"], env
    )
    assert rc == 10
    payload = only_json(out)
    assert payload["ok"] is False
    assert payload["exit_code"] == 10
    assert payload["reason"] == "no_host"
    assert "muse-a" in payload["error"]
    assert "muse-a" in payload["rejected"]
    # KHÔNG được chạy lệnh trên máy local
    assert not marker.exists(), "lệnh --cmd đã bị chạy trên máy local!"


def test_do_success_never_runs_cmd_locally(env, fake_ssh, tmp_path):
    """Job chạy trên host ⇒ lệnh KHÔNG chạy trên máy local."""
    good_host(fake_ssh)
    marker = tmp_path / "ran-locally"
    rc, out, _err = run(
        ["do", "--cmd", f"touch {marker}", "--name", "proj"], env
    )
    assert rc == 0
    only_json(out)
    assert not marker.exists(), "lệnh --cmd đã bị chạy trên máy local!"


# --------------------------------------------------------------------------
# roff do — hợp đồng JSON
# --------------------------------------------------------------------------
def test_do_emits_full_json_contract(env, fake_ssh, tmp_path):
    """JSON của ``roff do`` có đủ khoá theo SPEC-CLI §1.2."""
    good_host(fake_ssh)
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("x")
    rc, out, _err = run(
        [
            "do",
            "--cmd", "ffmpeg -i $ROFF_IN out.mp4",
            "--in", str(src),
            "--out", str(tmp_path / "out"),
            "--need", "ffmpeg",
            "--name", "proj",
        ],
        env,
    )
    assert rc == 0
    payload = only_json(out)
    assert payload["ok"] is True
    assert payload["exit_code"] == 0
    assert payload["state"] in ("launched", "running", "queued")
    assert payload["host"] == "muse-a"
    assert isinstance(payload["host_reasons"], list) and payload["host_reasons"]
    assert payload["ledger"].endswith(".json")
    assert payload["next"] and "wait" in payload["next"][0]
    assert payload["job"]


def test_do_ledger_record_is_written(env, fake_ssh):
    """``roff do`` ghi job vào ledger local."""
    good_host(fake_ssh)
    rc, out, _err = run(["do", "--cmd", "echo hi", "--name", "proj"], env)
    assert rc == 0
    job_id = only_json(out)["job"]
    ledger = env["state"] / "jobs" / f"{job_id}.json"
    assert ledger.is_file()
    data = json.loads(ledger.read_text())
    assert data["id"] == job_id
    assert data["host"] == "muse-a"


def test_do_side_effects_disables_retry(env, fake_ssh):
    """``--side-effects`` ⇒ needs_retry=False (watcher không failover)."""
    good_host(fake_ssh)
    rc, out, _err = run(
        ["do", "--cmd", "echo hi", "--side-effects", "--name", "proj"], env
    )
    assert rc == 0
    job_id = only_json(out)["job"]
    data = json.loads((env["state"] / "jobs" / f"{job_id}.json").read_text())
    assert data["needs_retry"] is False


def test_do_forced_host_uses_that_host(env, fake_ssh):
    """``--host`` ép host, bỏ qua scheduler."""
    good_host(fake_ssh)
    rc, out, _err = run(
        ["do", "--cmd", "echo hi", "--host", "muse-a", "--name", "proj"], env
    )
    assert rc == 0
    payload = only_json(out)
    assert payload["host"] == "muse-a"
    assert any("ép" in reason for reason in payload["host_reasons"])


def test_do_unknown_forced_host_is_255(env, fake_ssh):
    """``--host`` không tồn tại ⇒ 255 kèm detail."""
    good_host(fake_ssh)
    rc, out, _err = run(
        ["do", "--cmd", "echo hi", "--host", "khong-co", "--name", "proj"], env
    )
    assert rc == 255
    payload = only_json(out)
    assert payload["ok"] is False
    assert payload["exit_code"] == 255
    assert "detail" in payload


# --------------------------------------------------------------------------
# nguyên tắc 2: secret qua --env, không lộ trong argv / audit log
# --------------------------------------------------------------------------
def test_do_secret_is_redacted_in_audit_log(env, fake_ssh):
    """Audit log: giá trị ``--env`` không bao giờ xuất hiện (kể cả lỡ tay nằm
    trong ``--cmd`` ⇒ bị redact thành ``***``)."""
    good_host(fake_ssh)
    secret = "s3cr3t-value-xyz"
    cmd = f"mytool --token {secret}"
    rc, out, _err = run(
        ["do", "--cmd", cmd, "--env", f"MY_SECRET={secret}", "--name", "proj"], env
    )
    assert rc == 0
    audit = env["state"] / "audit.jsonl"
    assert audit.is_file()
    text = audit.read_text()
    assert secret not in text, "audit log lộ secret"
    assert "mytool --token ***" in text
    assert "MY_SECRET" in text, "chỉ có TÊN biến, không có giá trị"


def test_do_audit_log_keeps_cmd_verbatim(env, fake_ssh):
    """``--cmd`` sạch được ghi **nguyên văn** vào audit log."""
    good_host(fake_ssh)
    cmd = "ffmpeg -i $ROFF_IN -c:v libx264 $ROFF_OUT/out.mp4"
    rc, out, _err = run(["do", "--cmd", cmd, "--name", "proj"], env)
    assert rc == 0
    text = (env["state"] / "audit.jsonl").read_text()
    assert cmd in text


def test_do_secret_stored_in_mode_600_env_file(env, fake_ssh):
    """Secret nằm trong file env mode 600, không nằm trong ``--cmd``."""
    good_host(fake_ssh)
    secret = "s3cr3t-value-xyz"
    rc, out, _err = run(
        ["do", "--cmd", "mytool", "--env", f"MY_SECRET={secret}", "--name", "proj"],
        env,
    )
    assert rc == 0
    job_id = only_json(out)["job"]
    env_file = env["state"] / "jobs" / f"{job_id}.env"
    assert env_file.is_file()
    assert oct(env_file.stat().st_mode)[-3:] == "600"
    assert secret in env_file.read_text()
    # ledger của job không được chứa secret trong ``cmd``
    data = json.loads((env["state"] / "jobs" / f"{job_id}.json").read_text())
    assert secret not in data["cmd"]


def test_do_output_never_contains_secret(env, fake_ssh):
    """JSON trả về không chứa giá trị secret."""
    good_host(fake_ssh)
    secret = "s3cr3t-value-xyz"
    rc, out, _err = run(
        ["do", "--cmd", "mytool", "--env", f"MY_SECRET={secret}", "--name", "proj"],
        env,
    )
    assert rc == 0
    assert secret not in out


# --------------------------------------------------------------------------
# roff wait — poll ledger cục bộ
# --------------------------------------------------------------------------
def test_wait_exited_zero(env):
    """Job xong với exit 0 ⇒ exit 0, state=exited."""
    make_job(job_id="job-ok", state="exited", exit_code=0)
    rc, out, _err = run(["wait", "job-ok", "--timeout", "5"], env)
    assert rc == 0
    payload = only_json(out)
    assert payload["ok"] is True
    assert payload["state"] == "exited"
    assert payload["exit"] == 0


def test_wait_exited_nonzero(env):
    """Job xong với exit khác 0 ⇒ exit 1, reason=job_failed."""
    make_job(job_id="job-fail", state="exited", exit_code=3)
    rc, out, _err = run(["wait", "job-fail", "--timeout", "5"], env)
    assert rc == 1
    payload = only_json(out)
    assert payload["ok"] is True
    assert payload["state"] == "exited"
    assert payload["exit"] == 3
    assert payload["reason"] == "job_failed"


def test_wait_lost_is_exit_69(env):
    """Job mất ⇒ exit 69 + reason (host_restarted…)."""
    make_job(job_id="job-lost", state="lost", reason="host_restarted")
    rc, out, _err = run(["wait", "job-lost", "--timeout", "5"], env)
    assert rc == 69
    payload = only_json(out)
    assert payload["ok"] is False
    assert payload["state"] == "lost"
    assert payload["reason"] == "host_restarted"


def test_wait_timeout_is_not_an_error(env):
    """Exit 124 **không phải lỗi**: ok=true, state=running."""
    make_job(job_id="job-run", state="running")
    rc, out, _err = run(["wait", "job-run", "--timeout", "1"], env)
    assert rc == 124
    payload = only_json(out)
    assert payload["ok"] is True
    assert payload["exit_code"] == 124
    assert payload["state"] == "running"


def test_wait_unknown_job_is_exit_2(env):
    """Job id không tồn tại ⇒ exit 2."""
    rc, out, _err = run(["wait", "khong-co-job"], env)
    assert rc == 2
    payload = only_json(out)
    assert payload["ok"] is False
    assert payload["reason"] == "job_not_found"


def test_wait_zero_timeout_is_exit_2(env):
    """``--timeout 0`` bị từ chối (R17)."""
    make_job(job_id="job-run2", state="running")
    rc, out, _err = run(["wait", "job-run2", "--timeout", "0"], env)
    assert rc == 2
    payload = only_json(out)
    assert payload["ok"] is False


# --------------------------------------------------------------------------
# status / result / jobs
# --------------------------------------------------------------------------
def test_status_reads_ledger(env):
    """``roff status`` đọc ledger 1 lần, exit 0."""
    make_job(job_id="job-st", state="running")
    rc, out, _err = run(["status", "job-st"], env)
    assert rc == 0
    payload = only_json(out)
    assert payload["job"] == "job-st"
    assert payload["state"] == "running"
    assert payload["host"] == "muse-a"


def test_status_unknown_job_is_exit_2(env):
    """Job id không tồn tại ⇒ exit 2."""
    rc, out, _err = run(["status", "khong-co"], env)
    assert rc == 2
    assert only_json(out)["ok"] is False


def test_result_has_full_json(env):
    """``roff result`` có state/exit/attempts/artifacts."""
    job = make_job(job_id="job-res", state="exited", exit_code=0)
    job.attempts = [
        {"host": "muse-a", "pid": 42, "boot_id": "boot-1", "instance_token": "tok-1",
         "started": 1.0, "failover": False}
    ]
    job_mod.save_job(job)
    job_mod.set_job_options(
        "job-res", manifest=[{"path": "a.mp4", "sha256": "abc", "bytes": 10}]
    )
    rc, out, _err = run(["result", "job-res"], env)
    assert rc == 0
    payload = only_json(out)
    assert payload["state"] == "exited"
    assert payload["exit"] == 0
    assert payload["attempts"][0]["host"] == "muse-a"
    assert payload["artifacts"][0]["sha256"] == "abc"


def test_jobs_lists_newest_first(env):
    """``roff jobs`` liệt kê mới nhất trước."""
    make_job(job_id="job-a", created_at=100.0)
    make_job(job_id="job-b", created_at=200.0)
    rc, out, _err = run(["jobs"], env)
    assert rc == 0
    payload = only_json(out)
    assert payload["count"] == 2
    assert payload["jobs"][0]["job"] == "job-b"


def test_jobs_filters_by_state(env):
    """``roff jobs --state`` lọc theo trạng thái."""
    make_job(job_id="job-c", state="running")
    make_job(job_id="job-d", state="exited", exit_code=0)
    rc, out, _err = run(["jobs", "--state", "running"], env)
    assert rc == 0
    payload = only_json(out)
    assert payload["count"] == 1
    assert payload["jobs"][0]["job"] == "job-c"


# --------------------------------------------------------------------------
# logs (lệnh duy nhất in text) / cancel
# --------------------------------------------------------------------------
def test_logs_prints_text_not_json(env, fake_ssh):
    """``roff logs`` in **text** ra stdout — không phải JSON."""
    good_host(fake_ssh)
    fake_ssh(
        "muse-a",
        responses=[
            {"match": "nproc", "stdout": PROBE_OUTPUT},
            {"match": "log", "stdout": "dòng 1\ndòng 2\n"},
        ],
    )
    make_job(job_id="job-log", state="running")
    rc, out, _err = run(["logs", "job-log", "--tail", "5"], env)
    assert rc == 0
    assert "dòng 1" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out.strip())


def test_logs_unknown_job_is_json_error(env):
    """Lệnh logs lỗi ⇒ JSON lỗi + exit 2 (hợp đồng chung)."""
    rc, out, _err = run(["logs", "khong-co"], env)
    assert rc == 2
    payload = only_json(out)
    assert payload["ok"] is False
    assert payload["reason"] == "job_not_found"


def test_cancel_kills_process_group(env, fake_ssh):
    """``roff cancel`` huỷ theo pid đã ghi nhận ⇒ exit 0."""
    good_host(fake_ssh)
    fake_ssh(
        "muse-a",
        responses=[
            {"match": "nproc", "stdout": PROBE_OUTPUT},
            {"match": "ROFF_KILLED", "stdout": "ROFF_PID=4242\nROFF_KILLED=1\n"},
        ],
    )
    job = make_job(job_id="job-cancel", state="running")
    job.attempts = [{"host": "muse-a", "pid": 4242}]
    job_mod.save_job(job)
    rc, out, _err = run(["cancel", "job-cancel"], env)
    assert rc == 0
    payload = only_json(out)
    assert payload["ok"] is True
    assert payload["state"] == "cancelled"
    assert payload["killed"] is True


def test_cancel_already_cancelled_is_exit_130(env):
    """Job vốn đã cancelled ⇒ exit 130."""
    make_job(job_id="job-c2", state="cancelled", exit_code=130)
    rc, out, _err = run(["cancel", "job-c2"], env)
    assert rc == 130
    payload = only_json(out)
    assert payload["state"] == "cancelled"


# --------------------------------------------------------------------------
# hosts / probe
# --------------------------------------------------------------------------
def test_hosts_probes_fleet(env, fake_ssh):
    """``roff hosts`` probe mọi host, JSON có hosts + count."""
    good_host(fake_ssh)
    rc, out, _err = run(["hosts"], env)
    assert rc == 0
    payload = only_json(out)
    # fleet trong test có 2 alias; chỉ muse-a trả lời được
    assert payload["count"] == 2
    host = payload["hosts"][0]
    assert host["alias"] == "muse-a"
    assert host["reachable"] is True
    assert host["mem_available_mb"] == 900
    assert host["layout_root"] == "/home/hatch/.roff"
    assert host["boot_id"] == "boot-1"
    assert "ffmpeg" in host["tools"]
    # host không có trong fake ssh ⇒ không reachable, có lý do
    other = payload["hosts"][1]
    assert other["alias"] == "muse-b"
    assert other["reachable"] is False
    assert other["error"]


def test_hosts_parallel_flag_accepted(env, fake_ssh):
    """``--parallel`` vẫn cho JSON cùng hình dạng."""
    good_host(fake_ssh)
    rc, out, _err = run(["hosts", "--parallel"], env)
    assert rc == 0
    payload = only_json(out)
    assert payload["count"] == 2
    assert payload["hosts"][0]["alias"] == "muse-a"


def test_probe_unreachable_is_exit_255_with_detail(env, fake_ssh):
    """Host không với được ⇒ exit 255 + ``detail`` (bắt buộc theo hợp đồng)."""
    fake_ssh("muse-a", ssh_error="ssh: Permission denied (publickey)")
    rc, out, _err = run(["probe", "muse-a"], env)
    assert rc == 255
    payload = only_json(out)
    assert payload["ok"] is False
    assert payload["exit_code"] == 255
    assert "Permission denied" in payload["detail"]


def test_probe_reachable_returns_profile(env, fake_ssh):
    """``roff probe`` trả JSON của host."""
    good_host(fake_ssh)
    rc, out, _err = run(["probe", "muse-a"], env)
    assert rc == 0
    payload = only_json(out)
    assert payload["alias"] == "muse-a"
    assert payload["reachable"] is True


# --------------------------------------------------------------------------
# push / pull
# --------------------------------------------------------------------------
def test_push_uploads_project(env, fake_ssh, tmp_path):
    """``roff push`` đẩy project lên host, JSON có host/remote/bytes/files."""
    good_host(fake_ssh)
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("x")
    rc, out, _err = run(["push", "muse-a", str(src), "--name", "proj"], env)
    assert rc == 0
    payload = only_json(out)
    assert payload["host"] == "muse-a"
    assert payload["remote"] == "/home/hatch/.roff/projects/proj"
    assert payload["bytes"] >= 0
    assert isinstance(payload["files"], list)


def test_push_requires_name(env, fake_ssh, tmp_path):
    """``--name`` là bắt buộc ⇒ lỗi argparse ⇒ stderr + exit 2, không JSON."""
    good_host(fake_ssh)
    src = tmp_path / "src"
    src.mkdir()
    rc, out, err = run(["push", "muse-a", str(src)], env)
    assert rc == 2
    assert out.strip() == ""
    assert err.strip()


def test_pull_fetches_artifacts(env, fake_ssh, tmp_path):
    """``roff pull`` kéo artifact về local."""
    good_host(fake_ssh)
    job = make_job(job_id="job-pull", state="exited", exit_code=0)
    rc, out, _err = run(["pull", "job-pull", "--local", str(tmp_path / "out")], env)
    assert rc == 0
    payload = only_json(out)
    assert payload["job"] == "job-pull"
    assert payload["host"] == "muse-a"
    assert isinstance(payload["files"], list)
    assert (tmp_path / "out").is_dir()


# --------------------------------------------------------------------------
# ensure — idempotent theo (hash script, boot_id, instance_token)
# --------------------------------------------------------------------------
def test_ensure_installs_then_skips(env, fake_ssh, tmp_path):
    """Lần đầu cài, lần sau báo skipped (stamp khớp hash+boot_id+token)."""
    setup = tmp_path / "setup.sh"
    setup.write_text("#!/bin/sh\necho 'setup ok'\n")
    digest = hashlib.sha256(setup.read_bytes()).hexdigest()

    # --- lần 1: chưa có stamp ⇒ phải cài ---
    fake_ssh(
        "muse-a",
        responses=[
            {"match": "nproc", "stdout": PROBE_OUTPUT},
            {"match": "setup.sh", "stdout": "setup ok\n"},
            {"match": "roff-instance-token", "stdout": "tok-1\n"},
        ],
    )
    rc, out, _err = run(
        ["ensure", "muse-a", "--setup", str(setup), "--name", "npm"], env
    )
    assert rc == 0
    payload = only_json(out)
    assert payload["ensured"] is True
    assert payload["skipped"] is False
    assert payload["host"] == "muse-a"
    assert payload["name"] == "npm"

    # --- lần 2: stamp đã khớp ⇒ skipped ---
    stamp = json.dumps(
        {
            "name": "npm",
            "script_sha256": digest,
            "boot_id": "boot-1",
            "instance_token": "tok-1",
        }
    )
    fake_ssh(
        "muse-a",
        responses=[
            {"match": "nproc", "stdout": PROBE_OUTPUT},
            {"match": "stamp.json", "stdout": stamp},
            {"match": "setup.sh", "stdout": "setup ok\n"},
            {"match": "roff-instance-token", "stdout": "tok-1\n"},
        ],
    )
    rc, out, _err = run(
        ["ensure", "muse-a", "--setup", str(setup), "--name", "npm"], env
    )
    assert rc == 0
    payload = only_json(out)
    assert payload["skipped"] is True
    assert payload["ensured"] is False


def test_ensure_missing_setup_file_is_exit_2(env, fake_ssh, tmp_path):
    """Script setup không tồn tại ⇒ exit 2."""
    good_host(fake_ssh)
    rc, out, _err = run(
        ["ensure", "muse-a", "--setup", str(tmp_path / "khong-co.sh"), "--name", "x"],
        env,
    )
    assert rc == 2
    assert only_json(out)["ok"] is False


# --------------------------------------------------------------------------
# gc — LRU + trần cache + sàn disk trống
# --------------------------------------------------------------------------
def test_gc_respects_cache_min_free_mb(env, tmp_path):
    """Disk trống < cache_min_free_mb ⇒ không xoá gì."""
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "old.bin").write_bytes(b"x" * 4096)
    env["extra"]["ROFF_CACHE_DIR"] = str(cache)
    env["extra"]["ROFF_CONFIG"] = str(
        _write_config(tmp_path, "[local]\ncache_max_mb = 0\ncache_min_free_mb = 999999999\n")
    )
    rc, out, _err = run(["gc"], env)
    assert rc == 0
    payload = only_json(out)
    assert payload["skipped"] is True
    assert payload["freed_mb"] == 0
    assert (cache / "old.bin").exists()


def test_gc_removes_oldest_first(env, tmp_path):
    """Vượt trần cache ⇒ xoá file cũ nhất trước (LRU)."""
    cache = tmp_path / "cache"
    cache.mkdir()
    old = cache / "old.bin"
    new = cache / "new.bin"
    old.write_bytes(b"x" * 1_500_000)
    new.write_bytes(b"y" * 1_500_000)
    os.utime(old, (1000, 1000))
    os.utime(new, (2000, 2000))
    env["extra"]["ROFF_CACHE_DIR"] = str(cache)
    env["extra"]["ROFF_CONFIG"] = str(
        _write_config(tmp_path, "[local]\ncache_max_mb = 2\ncache_min_free_mb = 0\n")
    )
    rc, out, _err = run(["gc"], env)
    assert rc == 0
    payload = only_json(out)
    assert payload["skipped"] is False
    assert not old.exists(), "file cũ nhất phải bị xoá trước"
    assert new.exists()
    assert payload["freed_mb"] > 0


def _write_config(tmp_path, body):
    """Ghi file config tạm và trả đường dẫn."""
    path = tmp_path / "gc-config.ini"
    path.write_text(body)
    return path


# --------------------------------------------------------------------------
# tunnel — module của phần D có thể chưa có
# --------------------------------------------------------------------------
def test_tunnel_list_never_crashes(env):
    """``roff tunnel list`` hoặc chạy được, hoặc báo lỗi rõ ràng (không traceback)."""
    rc, out, err = run(["tunnel", "list"], env)
    assert rc in (0, 2)
    payload = only_json(out)
    assert "ok" in payload and "exit_code" in payload
    if rc == 2:
        assert payload["ok"] is False
        assert payload["error"]
    assert "Traceback" not in err


def test_tunnel_start_failure_is_json_not_traceback(env, fake_ssh):
    """``roff tunnel start`` lỗi (host không với được) ⇒ JSON lỗi, không traceback."""
    fake_ssh("muse-a", ssh_error="ssh: connect to host 127.0.0.1 port 2222: Connection refused")
    rc, out, err = run(
        ["tunnel", "start", "--host", "muse-a", "--lport", "18000", "--rport", "8000"],
        env,
    )
    assert "Traceback" not in err
    payload = only_json(out)
    assert payload["exit_code"] == rc
    if rc != 0:
        assert payload["ok"] is False
        assert payload.get("error") or payload.get("detail")


# --------------------------------------------------------------------------
# lô: --each / --shards / --after
# --------------------------------------------------------------------------
def test_do_each_creates_one_job_per_item(env, fake_ssh, tmp_path):
    """``--each`` ⇒ 1 job mỗi item, mỗi job 1 host khác nhau."""
    for alias in ("muse-a", "muse-b"):
        fake_ssh(
            alias,
            responses=[
                {"match": "nproc", "stdout": PROBE_OUTPUT},
                {"match": "launch.sh", "stdout": LAUNCH_OUTPUT},
            ],
        )
    vids = tmp_path / "vids"
    vids.mkdir()
    (vids / "a.mp4").write_text("a")
    (vids / "b.mp4").write_text("b")
    rc, out, _err = run(
        [
            "do",
            "--cmd", "ffmpeg -i $ROFF_ITEM $ROFF_OUT/out.mp4",
            "--each", str(vids / "*.mp4"),
            "--name", "proj",
        ],
        env,
    )
    assert rc == 0
    payload = only_json(out)
    assert payload["count"] == 2
    assert len(payload["jobs"]) == 2
    for job_id in payload["jobs"]:
        data = json.loads((env["state"] / "jobs" / f"{job_id}.json").read_text())
        assert data["item"], "job phải ghi item"
        # Item nằm trong KHO DÙNG CHUNG của host (không nằm trong workdir
        # của job) ⇒ nhiều job trên cùng host không đẩy lại input.
        assert data["item"].startswith("/home/hatch/.roff/inputs/")
        assert data["item"].endswith(".mp4")


def test_do_shards_creates_one_job_per_shard(env, fake_ssh):
    """``--shards N`` ⇒ N job, mỗi job 1 shard ($ROFF_SHARD/$ROFF_SHARDS)."""
    for alias in ("muse-a", "muse-b"):
        fake_ssh(
            alias,
            responses=[
                {"match": "nproc", "stdout": PROBE_OUTPUT},
                {"match": "launch.sh", "stdout": LAUNCH_OUTPUT},
            ],
        )
    rc, out, _err = run(
        ["do", "--cmd", "echo hi", "--shards", "2", "--name", "proj"], env
    )
    assert rc == 0
    payload = only_json(out)
    assert payload["count"] == 2
    shards = []
    for job_id in payload["jobs"]:
        data = json.loads((env["state"] / "jobs" / f"{job_id}.json").read_text())
        shards.append((data["shard"], data["shards"]))
    assert sorted(shards) == [(0, 2), (1, 2)]


def test_do_after_waits_for_dependency_job(env, fake_ssh):
    """``--after job:X`` ⇒ chờ X xong (poll ledger) rồi mới chạy."""
    good_host(fake_ssh)
    make_job(job_id="job-dep", state="exited", exit_code=0)
    rc, out, _err = run(
        ["do", "--cmd", "echo hi", "--after", "job:job-dep", "--name", "proj"], env
    )
    assert rc == 0
    assert only_json(out)["job"]


def test_do_after_failed_dependency_is_exit_1(env, fake_ssh):
    """Job phụ thuộc fail ⇒ ``do`` fail với exit 1, KHÔNG tạo job mới."""
    good_host(fake_ssh)
    make_job(job_id="job-dep2", state="exited", exit_code=2)
    rc, out, _err = run(
        ["do", "--cmd", "echo hi", "--after", "job:job-dep2", "--name", "proj"], env
    )
    assert rc == 1
    payload = only_json(out)
    assert payload["ok"] is False
    assert payload["reason"] == "job_failed"
    assert not list((env["state"] / "jobs").glob("*.json")) or all(
        "job-dep2" in path.name for path in (env["state"] / "jobs").glob("*.json")
    )


# --------------------------------------------------------------------------
# hợp đồng: đúng 1 JSON object cho mọi lệnh
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "command",
    [
        ["status", "job-x"],
        ["wait", "job-x", "--timeout", "1"],
        ["result", "job-x"],
        ["jobs"],
        ["hosts"],
        ["probe", "muse-a"],
        ["gc"],
        ["tunnel", "list"],
    ],
)
def test_every_command_prints_exactly_one_json_object(env, fake_ssh, command):
    """Mọi lệnh in đúng 1 JSON object (parse được cả stdout)."""
    good_host(fake_ssh)
    make_job(job_id="job-x", state="running")
    rc, out, _err = run(command, env)
    payload = only_json(out)
    assert isinstance(payload.get("ok"), bool)
    assert isinstance(payload.get("exit_code"), int)
    assert payload["exit_code"] == rc


def test_do_output_is_exactly_one_json_object(env, fake_ssh):
    """``roff do`` cũng in đúng 1 JSON object."""
    good_host(fake_ssh)
    rc, out, _err = run(["do", "--cmd", "echo hi", "--name", "proj"], env)
    payload = only_json(out)
    assert payload["ok"] is True
    assert payload["exit_code"] == rc
