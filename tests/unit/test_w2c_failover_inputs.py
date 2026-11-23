"""Hồi quy W2c — failover phải **dựng lại** input, không chỉ "đẩy lại path".

Sự cố: ``cli._cmd_do`` xử lý ``--in git:<url>`` bằng ``_clone_git_on_host`` và
``--in http:<url>`` bằng cách cho host tự tải — nhưng **URL không được ghi vào
ledger**. Khi watcher failover sang host mới, nó chỉ push lại các input dạng
path ⇒ job failover chạy với ``$ROFF_IN`` trỏ tới path **không tồn tại**.

Yêu cầu:

* ``options["input_specs"]`` ghi lại **mọi** input (kind + value);
* failover dựng lại input ``git:``/``http:`` trên host mới;
* input ``job:`` **không** dựng lại được ⇒ failover thất bại, job kết thúc
  ``lost`` với reason ``input_not_recreatable``.

Test không cần mạng: mọi hàm chạm host được monkeypatch.
"""

from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(_HERE))
_SKILL = os.path.join(_REPO, "skill")
if _SKILL not in sys.path:
    sys.path.insert(0, _SKILL)

from conftest import parse_json, run_cli  # noqa: E402

from roffkit import inputs as inputs_mod  # noqa: E402
from roffkit import job as job_mod  # noqa: E402
from roffkit import shared as shared_mod  # noqa: E402
from roffkit import watcher as watcher_mod  # noqa: E402

PROBE_OUTPUT = "\n".join(
    [
        "ROFF_HOSTNAME=htch-runtime",
        "ROFF_BOOT_ID=boot-1",
        "ROFF_HOME=/root",
        "ROFF_CORES=4",
        "ROFF_MEM_TOTAL_KB=8388608",
        "ROFF_MEM_AVAILABLE_KB=921600",
        "ROFF_LAYOUT_ROOT=/home/hatch/.roff",
        "ROFF_DURABLE=1",
        "ROFF_DISK_FREE_MB=102400",
        "ROFF_INSTANCE_TOKEN=tok-1",
        "ROFF_CGROUP_LIMIT_MB=",
        "ROFF_CGROUP_USED_MB=",
        "ROFF_TOOL=git=/usr/bin/git",
        "ROFF_INTERNET_CODE=200",
        "ROFF_INTERNET_RC=0",
        "ROFF_INTERNET_ERR=",
        "",
    ]
)

OLD_ROOT = "/old/root"
NEW_ROOT = "/new/root"
PROJECT = "demo"


class FakeProfile:
    """Profile giả của host mới."""

    alias = "muse-new"
    reachable = True
    bridge_busy = False
    error = None
    detail = None
    tools: dict = {}
    mem_available_mb = 4096
    disk_free_mb = 100000
    internet = True
    layout_root = NEW_ROOT
    home = "/root"
    boot_id = "boot-2"
    instance_token = "tok-2"
    projects: list = []
    toolchains: list = []
    cores = 4
    load1 = 0.0


class FakeChoice:
    host = "muse-new"
    score = 1.0
    reasons: list = []
    rejected: dict = {}


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("ROFF_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ROFF_CONFIG", str(tmp_path / "none.ini"))


@pytest.fixture
def host(monkeypatch):
    """Host giả: không có filesystem thật, không có mạng."""
    monkeypatch.setattr(watcher_mod, "cached_profiles", lambda *a, **k: [FakeProfile()])
    monkeypatch.setattr(watcher_mod, "select", lambda *a, **k: FakeChoice())
    monkeypatch.setattr(watcher_mod, "remote_state", lambda *a, **k: {"reachable": True})

    class Result:
        ok = True
        bytes_transferred = 1
        files: list = []
        error = None

    monkeypatch.setattr(
        shared_mod.transfer_mod, "push", lambda *a, **k: Result()
    )

    def fake_ensure(host_name, local_path, layout_root, **kwargs):
        key = shared_mod.shared_key(local_path)
        rel = shared_mod.shared_rel(local_path)
        return shared_mod.shared_remote_path(layout_root, key, rel)

    monkeypatch.setattr(shared_mod, "ensure_shared_input", fake_ensure)
    monkeypatch.setattr(
        shared_mod, "_run_shell", lambda *a, **k: (0, "", "")
    )
    return fake_ensure


@pytest.fixture
def launches(monkeypatch):
    """Ghi lại mỗi lần launch."""
    calls: list[dict] = []

    def fake_launch(host_name, job, env=None, cpus=0, mem_mb=0):
        calls.append({"host": host_name, "env": dict(env or {})})
        job.state = "launched"

    monkeypatch.setattr(watcher_mod, "launch", fake_launch)
    return calls


def make_job(tmp_path, **options):
    """Job + options như ``roff do`` đã ghi."""
    jid = "261123-101530-ab12"
    job = job_mod.Job(
        id=jid,
        cmd="true",
        host="muse-old",
        project=PROJECT,
        out_remote=f"{OLD_ROOT}/jobs/{jid}/out",
    )
    job_mod.save_job(job)

    base = {
        "workdir": f"{OLD_ROOT}/projects/{PROJECT}",
        "layout_root": OLD_ROOT,
        "needs": [],
        "retry": 1,
        "slot_host": "muse-old",
        "slot_ticket": "",
    }
    base.update(options)
    job_mod.set_job_options(jid, **base)
    return job


# --------------------------------------------------------------------------
# ledger phải ghi lại input_specs
# --------------------------------------------------------------------------
def _cli_env(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    config = tmp_path / "config.ini"
    config.write_text("[fleet]\ninclude = muse-*\n")
    ssh_config = tmp_path / "ssh_config"
    ssh_config.write_text("Host muse-a\n  HostName 127.0.0.1\n  Port 2222\n")
    env = dict(os.environ)
    env.update(
        {
            "ROFF_STATE_DIR": str(state),
            "ROFF_CONFIG": str(config),
            "ROFF_SSH_CONFIG": str(ssh_config),
            "ROFF_NO_WATCHER": "1",
        }
    )
    for key in ("ROFF_STATE_DIR", "ROFF_CONFIG", "ROFF_SSH_CONFIG"):
        monkeypatch.setenv(key, env[key])
    return env


def test_do_records_git_input_spec(fake_ssh, tmp_path, monkeypatch):
    """``--in git:<url>`` ⇒ ledger có ``input_specs`` (kind=git)."""
    fake_ssh(
        "muse-a",
        responses=[
            {"match": "nproc", "stdout": PROBE_OUTPUT},
            {"match": "git clone", "stdout": ""},
        ],
    )
    env = _cli_env(tmp_path, monkeypatch)

    rc, out, _err = run_cli(
        [
            "do",
            "--cmd", "make",
            "--in", "git:https://example.invalid/repo.git",
            "--name", "proj",
        ],
        env=env,
    )
    assert rc == 0, out
    job_id = parse_json(out)["job"]
    options = job_mod.job_options(job_id)
    specs = options.get("input_specs")
    assert specs, "ledger phải ghi input_specs"
    assert specs[0]["kind"] == "git"
    assert specs[0]["value"] == "https://example.invalid/repo.git"


def test_do_records_http_input_spec(fake_ssh, tmp_path, monkeypatch):
    """``--in http:<url>`` ⇒ ledger có ``input_specs`` (kind=http)."""
    fake_ssh(
        "muse-a",
        responses=[{"match": "nproc", "stdout": PROBE_OUTPUT}],
    )
    env = _cli_env(tmp_path, monkeypatch)

    rc, out, _err = run_cli(
        [
            "do",
            "--cmd", "make",
            "--in", "https://example.invalid/file.bin",
            "--name", "proj",
        ],
        env=env,
    )
    assert rc == 0, out
    job_id = parse_json(out)["job"]
    specs = job_mod.job_options(job_id).get("input_specs")
    assert specs and specs[0]["kind"] == "http"
    assert specs[0]["value"] == "https://example.invalid/file.bin"


def test_do_records_path_input_spec(fake_ssh, tmp_path, monkeypatch):
    """Input dạng path cũng được ghi (đầy đủ, không chỉ git/http)."""
    fake_ssh(
        "muse-a",
        responses=[{"match": "nproc", "stdout": PROBE_OUTPUT}],
    )
    env = _cli_env(tmp_path, monkeypatch)
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("x")

    rc, out, _err = run_cli(
        ["do", "--cmd", "make", "--in", str(src), "--name", "proj"], env=env
    )
    assert rc == 0, out
    job_id = parse_json(out)["job"]
    specs = job_mod.job_options(job_id).get("input_specs")
    assert specs and specs[0]["kind"] == "path"


# --------------------------------------------------------------------------
# failover: dựng lại input
# --------------------------------------------------------------------------
def test_failover_reclones_git_input(host, launches, monkeypatch, tmp_path):
    """Input ``git:`` phải được clone **lại** trên host mới."""
    clones: list[tuple] = []

    def fake_clone(host_name, url, remote_dir):
        clones.append((host_name, url, remote_dir))
        return f"{remote_dir}/repo"

    monkeypatch.setattr(inputs_mod, "clone_git_on_host", fake_clone)
    job = make_job(
        tmp_path,
        input_specs=[{"kind": "git", "value": "https://example.invalid/repo.git"}],
        input_remote=f"{OLD_ROOT}/projects/demo/repo",
        input_kind="git",
    )

    ok = watcher_mod._failover(job, "host_restarted")

    assert ok is True
    assert clones, "failover phải clone lại repo trên host mới"
    assert clones[0][0] == "muse-new"
    assert clones[0][1] == "https://example.invalid/repo.git"
    options = job_mod.job_options(job.id)
    assert options["input_remote"] == f"{NEW_ROOT}/projects/demo/repo"


def test_failover_recreates_http_input_with_internet(host, launches, monkeypatch,
                                                      tmp_path):
    """Host mới có internet ⇒ host tự tải, ``$ROFF_URL`` được đặt lại."""
    downloads: list[str] = []
    monkeypatch.setattr(
        inputs_mod, "download_http_locally", lambda url: downloads.append(url) or "/tmp/x"
    )
    job = make_job(
        tmp_path,
        input_specs=[{"kind": "http", "value": "https://example.invalid/f.bin"}],
        input_remote="https://example.invalid/f.bin",
        input_kind="http",
    )

    ok = watcher_mod._failover(job, "host_restarted")

    assert ok is True
    assert not downloads, "host có internet ⇒ KHÔNG tải qua local"
    env = job_mod.load_job_env(job.id)
    assert env.get("ROFF_URL") == "https://example.invalid/f.bin"
    options = job_mod.job_options(job.id)
    assert options["input_remote"] == "https://example.invalid/f.bin"


def test_failover_recreates_http_input_without_internet(host, launches, monkeypatch,
                                                         tmp_path):
    """Host mới **không** có internet ⇒ tải qua local rồi đẩy lên host."""
    monkeypatch.setattr(FakeProfile, "internet", False)
    downloads: list[str] = []
    monkeypatch.setattr(
        inputs_mod,
        "download_http_locally",
        lambda url: downloads.append(url) or "/tmp/folder",
    )
    pushed: list[tuple] = []
    monkeypatch.setattr(
        inputs_mod,
        "push_local_path",
        lambda host_name, local_path, remote_dir: pushed.append(
            (host_name, local_path, remote_dir)
        ) or f"{remote_dir}/f.bin",
    )
    job = make_job(
        tmp_path,
        input_specs=[{"kind": "http", "value": "https://example.invalid/f.bin"}],
        input_remote="https://example.invalid/f.bin",
        input_kind="http",
    )

    ok = watcher_mod._failover(job, "host_restarted")

    assert ok is True
    assert downloads == ["https://example.invalid/f.bin"], "phải tải qua local"
    assert pushed and pushed[0][0] == "muse-new", "phải đẩy lên host mới"
    options = job_mod.job_options(job.id)
    assert options["input_remote"].endswith("/f.bin")
    assert "https://example.invalid/f.bin" not in options["input_remote"]


def test_failover_job_input_is_not_recreatable(host, launches, tmp_path):
    """Input ``job:`` không dựng lại được ⇒ failover thất bại + event."""
    job = make_job(
        tmp_path,
        input_specs=[{"kind": "job", "value": "job-source"}],
        input_remote=f"{OLD_ROOT}/jobs/job-source/out",
        input_kind="job",
    )

    ok = watcher_mod._failover(job, "host_restarted")

    assert ok is False, "failover không được thành công với input job:"
    assert not launches, "không được launch với $ROFF_IN sai"
    events = [
        entry["event"]
        for entry in (job_mod.job_options(job.id).get("events") or [])
    ]
    assert "failover_input_unsupported" in events
    assert job.reason == "input_not_recreatable"


def test_failover_job_input_ends_lost_with_reason(host, launches, monkeypatch,
                                                   tmp_path):
    """Watcher: failover thất bại ⇒ job kết thúc ``lost`` (reason đúng)."""
    job = make_job(
        tmp_path,
        input_specs=[{"kind": "job", "value": "job-source"}],
        input_remote=f"{OLD_ROOT}/jobs/job-source/out",
        input_kind="job",
        needs_retry=True,
    )
    # job đã chạy trên host cũ (có attempt) ⇒ watcher theo dõi bình thường
    job.state = "running"
    job.attempts = [
        {"host": "muse-old", "pid": 4242, "boot_id": "boot-1",
         "instance_token": "tok-1"}
    ]
    job_mod.save_job(job)
    monkeypatch.setattr(
        watcher_mod,
        "remote_state",
        lambda *a, **k: {
            "reachable": True,
            "boot_id": "boot-2",
            "instance_token": "tok-2",
            "pid_alive": False,
            "state": "lost",
        },
    )

    def _boom(_seconds):
        raise AssertionError("watcher phải kết thúc, không ngủ")

    monkeypatch.setattr(watcher_mod, "_sleep", _boom)
    watcher_mod.watch(job.id)

    final = job_mod.load_job(job.id)
    assert final.state == "lost"
    assert final.reason == "input_not_recreatable"
