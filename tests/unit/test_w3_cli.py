"""Hồi quy W3 ở **tầng CLI** — hành vi người dùng thấy.

* ticket mồ côi không được kẹt host (W3a) — ``roff do`` vẫn chạy, ``roff gc``
  dọn và **báo cáo** trong JSON;
* host chết lúc chuẩn bị input ⇒ thử host khác (W3c);
* ``~/.ssh/config`` hỏng ⇒ ``host_unreachable`` + ``detail`` nguyên văn (W3d);
* ``roff gc`` dọn kho input **trên host** (W3g);
* ``--checkpoint`` là path **trên host** và thư mục được tạo (W3n).

Test chạy **không cần mạng**: ssh giả (fixture ``fake_ssh``).
"""

from __future__ import annotations

import json
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(_HERE))
_SKILL = os.path.join(_REPO, "skill")
if _SKILL not in sys.path:
    sys.path.insert(0, _SKILL)

from conftest import parse_json, run_cli  # noqa: E402

from roffkit import job as job_mod  # noqa: E402
from roffkit import slots as slots_mod  # noqa: E402

EXIT_UNREACHABLE = 255


def _probe_out(hostname="htch-a", boot="boot-a", token="tok-a", cores=4) -> str:
    return "\n".join(
        [
            f"ROFF_HOSTNAME={hostname}",
            f"ROFF_BOOT_ID={boot}",
            "ROFF_HOME=/root",
            f"ROFF_CORES={cores}",
            "ROFF_MEM_TOTAL_KB=8388608",
            "ROFF_MEM_AVAILABLE_KB=921600",
            "ROFF_LAYOUT_ROOT=/home/hatch/.roff",
            "ROFF_DURABLE=1",
            "ROFF_DISK_FREE_MB=102400",
            f"ROFF_INSTANCE_TOKEN={token}",
            "ROFF_CGROUP_LIMIT_MB=",
            "ROFF_CGROUP_USED_MB=",
            "ROFF_TOOL=python3=/usr/bin/python3",
            "ROFF_INTERNET_CODE=200",
            "ROFF_INTERNET_RC=0",
            "ROFF_INTERNET_ERR=",
            "",
        ]
    )


def _cli_env(tmp_path, monkeypatch=None, hosts=("muse-a",)):
    """Env cô lập cho CLI (state/config/ssh-config riêng)."""
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    config = tmp_path / "config.ini"
    config.write_text("[fleet]\ninclude = muse-*\n", encoding="utf-8")
    ssh_config = tmp_path / "ssh_config"
    ssh_config.write_text(
        "".join(f"Host {name}\n  HostName 127.0.0.1\n" for name in hosts),
        encoding="utf-8",
    )
    env = dict(os.environ)
    env.update(
        {
            "ROFF_STATE_DIR": str(state),
            "ROFF_CONFIG": str(config),
            "ROFF_SSH_CONFIG": str(ssh_config),
            "ROFF_NO_WATCHER": "1",
        }
    )
    if monkeypatch is not None:
        for key in ("ROFF_STATE_DIR", "ROFF_CONFIG", "ROFF_SSH_CONFIG"):
            monkeypatch.setenv(key, env[key])
    return env


def _job_states(tmp_path) -> list[str]:
    jobs_dir = tmp_path / "state" / "jobs"
    if not jobs_dir.is_dir():
        return []
    out = []
    for path in sorted(jobs_dir.glob("*.json")):
        out.append(json.loads(path.read_text()).get("state"))
    return out


# --------------------------------------------------------------------------
# W3a — ticket mồ côi không kẹt host
# --------------------------------------------------------------------------
def test_do_runs_even_when_an_orphan_ticket_blocks_the_host(fake_ssh, tmp_path):
    """Ticket mồ côi ở đầu hàng ⇒ reconcile dọn ⇒ job mới **vẫn chạy**."""
    fake_ssh("muse-a", responses=[{"match": "nproc", "stdout": _probe_out()}])
    env = _cli_env(tmp_path)

    ledger = slots_mod.SlotLedger(state_dir=env["ROFF_STATE_DIR"])
    orphan = ledger.request_slot("muse-a", 1, owner="job-gone")
    assert ledger.queue_length() == 1

    rc, out, _err = run_cli(["do", "--cmd", "echo hi", "--name", "proj"], env=env)

    assert rc == 0, out
    payload = parse_json(out)
    assert payload["state"] in ("launched", "queued")
    assert ledger.queue_length() <= 1
    assert ledger.position(orphan) == 0 or payload["state"] == "launched"


def test_do_reclaims_orphan_ticket_before_requesting_slot(fake_ssh, tmp_path):
    """Reconcile chạy **trước** khi xin slot ⇒ ticket mồ côi bị gỡ."""
    fake_ssh("muse-a", responses=[{"match": "nproc", "stdout": _probe_out()}])
    env = _cli_env(tmp_path)

    ledger = slots_mod.SlotLedger(state_dir=env["ROFF_STATE_DIR"])
    ledger.request_slot("muse-a", 1, owner="job-gone")

    rc, out, _err = run_cli(["do", "--cmd", "echo hi", "--name", "proj"], env=env)

    assert rc == 0
    payload = parse_json(out)
    assert payload["state"] == "launched", "job phải được launch (slot đã dọn)"


def test_gc_reports_orphan_ticket_cleanup(fake_ssh, tmp_path):
    """``roff gc`` dọn ticket mồ côi **và** báo cáo trong JSON."""
    fake_ssh("muse-a", responses=[{"match": "nproc", "stdout": _probe_out()}])
    env = _cli_env(tmp_path)

    ledger = slots_mod.SlotLedger(state_dir=env["ROFF_STATE_DIR"])
    ledger.request_slot("muse-a", 1, owner="job-gone")

    rc, out, _err = run_cli(["gc"], env=env)

    assert rc == 0, out
    payload = parse_json(out)
    assert payload["ok"] is True
    assert isinstance(payload.get("hosts"), list)
    assert "freed_mb" in payload
    removed = json.dumps(payload)
    assert "slot" in removed.lower() or payload["hosts"], "phải báo cáo việc dọn slot"
    assert ledger.queue_length() == 0, "ticket mồ côi phải bị dọn"


def test_gc_keeps_ticket_of_queued_job(fake_ssh, tmp_path):
    """Ticket của job **đang xếp hàng** ⇒ không được dọn."""
    fake_ssh("muse-a", responses=[{"match": "nproc", "stdout": _probe_out()}])
    env = _cli_env(tmp_path, monkeypatch=None)

    ledger = slots_mod.SlotLedger(state_dir=env["ROFF_STATE_DIR"])
    job = job_mod.Job(
        id="job-queued", cmd="true", host="muse-a", project="demo", state="queued"
    )
    job_mod.save_job(job, state_dir=env["ROFF_STATE_DIR"])
    ticket = ledger.request_slot("muse-a", 1, owner="job-queued")

    rc, out, _err = run_cli(["gc"], env=env)

    assert rc == 0
    assert ledger.queue_length() == 1, "ticket của job còn sống phải được giữ"


# --------------------------------------------------------------------------
# W3c — host chết lúc chuẩn bị input ⇒ thử host khác
# --------------------------------------------------------------------------
def test_do_retries_other_host_when_input_prep_fails(fake_ssh, tmp_path):
    """Host chết lúc **clone git** ⇒ thử host khác (W3c), không fail cả lệnh."""
    fake_ssh(
        "muse-a",
        responses=[
            {"match": "nproc", "stdout": _probe_out("htch-a", "boot-a", "tok-a")},
            {
                "match": "git clone",
                "exit": 255,
                "stderr": "ssh: connect to host muse-a port 22: Connection refused",
            },
        ],
    )
    fake_ssh(
        "muse-b",
        responses=[{"match": "nproc", "stdout": _probe_out("htch-b", "boot-b", "tok-b")}],
    )
    env = _cli_env(tmp_path, hosts=("muse-a", "muse-b"))

    rc, out, _err = run_cli(
        ["do", "--cmd", "echo hi", "--name", "proj", "--in", "git:https://example.invalid/x.git"],
        env=env,
    )

    assert rc == 0, f"không được fail cả lệnh: {out}"
    payload = parse_json(out)
    assert payload["ok"] is True
    assert payload["host"] == "muse-b", "phải thử host khác sau khi host chết"
    assert payload["state"] in ("launched", "queued")
    assert "lost" not in _job_states(tmp_path)


def test_do_does_not_retry_on_bad_input(fake_ssh, tmp_path):
    """URL hỏng ⇒ **không** retry, báo lỗi ngay (exit 2 — W3c + W3m)."""
    fake_ssh(
        "muse-a",
        responses=[
            {"match": "nproc", "stdout": _probe_out("htch-a", "boot-a", "tok-a")},
            {
                "match": "git clone",
                "exit": 128,
                "stderr": "fatal: repository 'https://example.invalid/x.git' not found",
            },
        ],
    )
    fake_ssh(
        "muse-b",
        responses=[{"match": "nproc", "stdout": _probe_out("htch-b", "boot-b", "tok-b")}],
    )
    env = _cli_env(tmp_path, hosts=("muse-a", "muse-b"))

    rc, out, _err = run_cli(
        ["do", "--cmd", "echo hi", "--name", "proj", "--in", "git:https://example.invalid/x.git"],
        env=env,
    )

    assert rc == 2, f"URL hỏng ⇒ exit 2 (input sai), không phải 255: {out}"
    payload = parse_json(out)
    assert payload["ok"] is False
    assert payload["exit_code"] == 2
    assert payload.get("detail"), "phải có detail nguyên văn của git"


# --------------------------------------------------------------------------
# W3d — ~/.ssh/config hỏng ⇒ host_unreachable (không phải unknown_host)
# --------------------------------------------------------------------------
def test_do_with_broken_ssh_config_reports_host_unreachable(fake_ssh, tmp_path):
    """Config hỏng ⇒ phải probe thật và báo ``host_unreachable`` + ``detail``."""
    fake_ssh("muse-a", ssh_error="ssh: connect to host muse-a port 22: Connection refused")
    env = _cli_env(tmp_path)
    # Config **đọc lỗi** (ở đây là một thư mục) ⇒ parser ném lỗi ⇒
    # ``_ssh_config_aliases()`` trả None ⇒ KHÔNG kết luận unknown_host.
    broken = tmp_path / "ssh_config_broken"
    broken.mkdir()
    env["ROFF_SSH_CONFIG"] = str(broken)

    rc, out, _err = run_cli(
        ["do", "--cmd", "echo hi", "--host", "muse-a", "--name", "proj"], env=env
    )

    assert rc == EXIT_UNREACHABLE, out
    payload = parse_json(out)
    assert payload["ok"] is False
    assert payload["exit_code"] == EXIT_UNREACHABLE
    assert payload.get("detail"), "255 luôn phải có detail (lỗi ssh nguyên văn)"
    assert "refused" in str(payload["detail"]).lower() or "ssh" in str(
        payload["detail"]
    ).lower()


# --------------------------------------------------------------------------
# W3g — gc dọn kho input trên host
# --------------------------------------------------------------------------
def test_gc_cleans_host_input_store(fake_ssh, tmp_path):
    """``roff gc`` dọn kho input trên host và báo ``hosts`` + ``freed_mb``."""
    listing = (
        "dir|abc123|1000000|4096\n"
        "file|abc123.complete|1000000|1\n"
    )
    fake_ssh(
        "muse-a",
        responses=[
            {"match": "nproc", "stdout": _probe_out()},
            {"match": "for p in", "stdout": listing},
        ],
    )
    env = _cli_env(tmp_path)

    rc, out, _err = run_cli(["gc", "--max-age-days", "0"], env=env)

    assert rc == 0, out
    payload = parse_json(out)
    assert isinstance(payload.get("hosts"), list)
    hosts = payload["hosts"]
    assert hosts, "phải có kết quả cho từng host"
    assert hosts[0]["host"] == "muse-a"
    assert "freed_mb" in payload


def test_gc_never_deletes_store_used_by_running_job(fake_ssh, tmp_path):
    """Kho mà job đang chạy/xếp hàng dùng ⇒ KHÔNG được xoá."""
    listing = (
        "dir|abc123|1000000|4096\n"
        "file|abc123.complete|1000000|1\n"
    )
    fake_ssh(
        "muse-a",
        responses=[
            {"match": "nproc", "stdout": _probe_out()},
            {"match": "for p in", "stdout": listing},
        ],
    )
    env = _cli_env(tmp_path)

    # Job đang chạy dùng key abc123 (key = sha256(local path)[:16]).
    key = slots_mod.__name__ and __import__("hashlib").sha256(
        os.path.abspath("/data/in").encode("utf-8")
    ).hexdigest()[:16]
    job = job_mod.Job(
        id="job-running", cmd="true", host="muse-a", project="demo", state="running"
    )
    job_mod.save_job(job, state_dir=env["ROFF_STATE_DIR"])
    job_mod.set_job_options(
        job.id,
        state_dir=env["ROFF_STATE_DIR"],
        input_shared_key=key,
    )

    rc, out, _err = run_cli(["gc", "--max-age-days", "0"], env=env)

    assert rc == 0
    payload = parse_json(out)
    removed = payload["hosts"][0].get("removed") or []
    assert not any(str(key) in item for item in removed), (
        "kho input của job đang chạy KHÔNG được xoá"
    )


# --------------------------------------------------------------------------
# W3n — checkpoint là path trên host
# --------------------------------------------------------------------------
def test_do_checkpoint_is_host_path_and_created(fake_ssh, tmp_path):
    """``--checkpoint`` ⇒ ``ROFF_CHECKPOINT`` là path trên host, thư mục có sẵn."""
    fake_ssh(
        "muse-a",
        responses=[{"match": "nproc", "stdout": _probe_out()}],
    )
    env = _cli_env(tmp_path)

    # Bật ghi log lệnh ssh của ssh giả (để xác nhận "mkdir -p ... .checkpoint"
    # thực sự được gửi tới host).
    spec_path = tmp_path / "ssh_spec.json"
    spec = json.loads(spec_path.read_text())
    log_path = tmp_path / "ssh.log"
    spec["log"] = str(log_path)
    spec_path.write_text(json.dumps(spec))

    rc, out, _err = run_cli(
        ["do", "--cmd", "echo hi", "--name", "proj", "--checkpoint", "ckpt"],
        env=env,
    )

    assert rc == 0, out
    payload = parse_json(out)
    assert payload["ok"] is True
    job_id = payload["job"]
    options = job_mod.job_options(job_id, state_dir=env["ROFF_STATE_DIR"])
    checkpoint = options.get("checkpoint") or ""
    assert checkpoint.startswith("/"), "checkpoint phải là path tuyệt đối phía HOST"
    assert ".checkpoint" in checkpoint
    assert checkpoint.startswith("/home/hatch/.roff/"), (
        "checkpoint phải nằm dưới layout_root của host"
    )
    log = log_path.read_text() if log_path.exists() else ""
    assert "mkdir -p" in log and ".checkpoint" in log, (
        "phải tạo thư mục checkpoint trên host"
    )
