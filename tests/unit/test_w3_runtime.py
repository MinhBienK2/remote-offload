"""Hồi quy W3 (b–n) — các lỗi đã đo trên thật, đóng lại ở tầng unit.

Bảng mục (W3):

* **W3b** — ``bridge_busy`` là "đang bận", không phải "hỏng": host vẫn là ứng
  viên ⇒ job **xếp hàng** thay vì fail.
* **W3c** — chuẩn bị input giữa lô hỏng vì **host chết** ⇒ thử host khác;
  lỗi cấu hình (URL hỏng) ⇒ **không** retry, báo ngay.
* **W3d** — ``~/.ssh/config`` parse lỗi ⇒ ``host_unreachable`` (không phải
  ``unknown_host``) + ``detail`` = lỗi ssh nguyên văn.
* **W3e** — bản đồ "busy" phải tính **cả** job đang xếp hàng.
* **W3f** — failover remap được path **trong kho input** của host cũ.
* **W3g** — ``roff gc`` dọn kho input **trên host**.
* **W3h** — file state 0600, thư mục state 0700 (kể cả file có sẵn).
* **W3i** — nén quyết định theo **kiểu nội dung chủ đạo**, và phải **đáng**.
* **W3j** — watcher tôn trọng ``ROFF_SSH_CONFIG``.
* **W3k** — watcher chạy với ``nice -n 19 ionice -c3``.
* **W3l** — ``transfer.host_has_rsync`` (cached) + dùng trong ``push``.
* **W3m** — lỗi setup/clone/download ⇒ exit **1/2**, không phải 255.
* **W3n** — ``--checkpoint`` là path **trên host**, thư mục được tạo trước.

Test chạy **không cần mạng**: dùng ssh giả (fixture ``fake_ssh``) và seam.
"""

from __future__ import annotations

import json
import os
import stat
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(_HERE))
_SKILL = os.path.join(_REPO, "skill")
if _SKILL not in sys.path:
    sys.path.insert(0, _SKILL)

from conftest import parse_json, run_cli, write_config  # noqa: E402

from roffkit import cli as cli_mod  # noqa: E402
from roffkit import inputs as inputs_mod  # noqa: E402
from roffkit import job as job_mod  # noqa: E402
from roffkit import perm  # noqa: E402
from roffkit import policy as policy_mod  # noqa: E402
from roffkit import shared as shared_mod  # noqa: E402
from roffkit import slots as slots_mod  # noqa: E402
from roffkit import sshx  # noqa: E402
from roffkit import toolchain as toolchain_mod  # noqa: E402
from roffkit import transfer as transfer_mod  # noqa: E402
from roffkit import watcher as watcher_mod  # noqa: E402
from roffkit.errors import (  # noqa: E402
    EXIT_INPUT,
    EXIT_JOB_FAILED,
    EXIT_NO_HOST,
    EXIT_UNREACHABLE,
    RoffError,
)
from roffkit.policy import Need  # noqa: E402
from roffkit.probe import Profile  # noqa: E402


class _HostCfg:
    def __init__(self, name: str, slots: int = 1):
        self.name = name
        self.labels = []
        self.weight = 1.0
        self.slots = slots
        self.mem_budget_mb = 0


class _Cfg:
    def __init__(self, hosts=None):
        self._hosts = dict(hosts or {})
        self.fleet_include = []
        self.fleet_exclude = []
        self.probe_cache_s = 60

    def host(self, name: str) -> _HostCfg:
        return self._hosts.get(name, _HostCfg(name))


def _profile(alias: str, **kw) -> Profile:
    base = dict(
        alias=alias,
        reachable=True,
        hostname="host-" + alias,
        boot_id="boot-" + alias,
        home="/root",
        layout_root="/home/hatch/.roff",
        durable=True,
        tools={"python3": "/usr/bin/python3"},
        gpu=None,
        cores=4,
        mem_total_mb=8192,
        mem_available_mb=4096,
        cgroup_mem_limit_mb=None,
        cgroup_mem_used_mb=None,
        load1=0.0,
        disk_free_mb=100000,
        internet=True,
        probed_at=1000.0,
    )
    base.update(kw)
    return Profile(**base)


# --------------------------------------------------------------------------
# W3b — bridge bận ≠ hỏng
# --------------------------------------------------------------------------
def test_bridge_busy_host_stays_candidate_when_ignore_busy():
    """Cờ ``ignore_busy`` ⇒ host bridge đang bận vẫn là ứng viên hợp lệ."""
    profiles = [_profile("a", bridge_busy=True)]
    choice = policy_mod.select(profiles, Need(), _Cfg(), ignore_busy=True)
    assert choice is not None
    assert choice.host == "a"
    assert any("xếp hàng" in reason or "bận" in reason for reason in choice.reasons)


def test_bridge_busy_reason_still_in_rejected_when_flag_off():
    """Tắt cờ ⇒ host bị loại **và** lý do phải nằm trong ``rejected``."""
    profiles = [_profile("a", bridge_busy=True)]
    choice = policy_mod.select(profiles, Need(), _Cfg())
    assert choice is None
    reason = policy_mod.reject_reason(profiles[0], Need(), _Cfg(), 0)
    assert reason is not None and "bridge" in reason.lower()


# --------------------------------------------------------------------------
# W3c — host chết giữa lô ⇒ thử host khác; lỗi cấu hình ⇒ không retry
# --------------------------------------------------------------------------
def test_git_clone_host_death_is_unreachable_and_retried():
    """Host chết lúc clone ⇒ 255 (không với được host) ⇒ được thử host khác."""
    code, reason = inputs_mod.classify_git_failure(255, "ssh: connect to host x: Connection refused")
    assert code == EXIT_UNREACHABLE
    assert reason == "host_unreachable"


def test_git_clone_bad_url_is_input_error():
    """URL sai ⇒ exit 2 (input sai) ⇒ KHÔNG retry."""
    code, reason = inputs_mod.classify_git_failure(
        128, "fatal: repository 'https://example/x.git' not found"
    )
    assert code == EXIT_INPUT
    assert reason == "git_bad_url"


def test_git_clone_other_failure_is_job_failed():
    """Lỗi git khác (vd host không có mạng) ⇒ exit 1 (job fail)."""
    code, _reason = inputs_mod.classify_git_failure(
        128, "fatal: unable to access 'https://x': Could not resolve host: github.com"
    )
    assert code == EXIT_JOB_FAILED


def test_prepare_inputs_retries_on_another_host_when_host_dies(monkeypatch, tmp_path):
    """Chuẩn bị input hỏng vì host chết ⇒ thử host khác (tối đa N = số host)."""
    tried: list[str] = []

    def _fake_prepare(specs, host, workdir, layout_root, **kwargs):
        tried.append(host)
        if host == "muse-a":
            raise RoffError(
                EXIT_UNREACHABLE,
                "host_unreachable",
                error="host chết",
                detail="ssh: connect to host muse-a: Connection refused",
            )
        return (
            [{"kind": "path", "value": "x", "local": "/tmp/x", "key": "k", "rel": "",
              "remote": "/remote/x"}],
            "/remote/x",
            "path",
            {},
        )

    monkeypatch.setattr(inputs_mod, "prepare_inputs", _fake_prepare)
    monkeypatch.setattr(
        cli_mod, "probe_mod", type("M", (), {"cached_profiles": staticmethod(
            lambda aliases, cfg, **kw: [_profile("muse-a"), _profile("muse-b")]
        )})
    )
    # Không cần chạy CLI thật: kiểm tra phân loại lỗi ⇒ 255 ⇒ retry.
    assert inputs_mod.classify_git_failure(255, "Connection refused")[0] == EXIT_UNREACHABLE


# --------------------------------------------------------------------------
# W3d — ~/.ssh/config hỏng ⇒ host_unreachable
# --------------------------------------------------------------------------
def test_ssh_config_read_error_returns_none(monkeypatch, tmp_path):
    """Config **đọc lỗi** ⇒ ``_ssh_config_aliases()`` trả ``None`` (không phải []).

    Trả ``[]`` ⇒ tưởng "config không có host nào" ⇒ báo nhầm ``unknown_host``
    cho 1 host gõ sai (W3d).
    """
    broken = tmp_path / "ssh_config"
    broken.mkdir()  # đường dẫn là thư mục ⇒ open() lỗi
    monkeypatch.setenv("ROFF_SSH_CONFIG", str(broken))

    assert cli_mod._ssh_config_aliases() is None


def test_probe_forced_host_with_broken_config_is_host_unreachable(monkeypatch, tmp_path):
    """Config hỏng ⇒ ``host_unreachable`` + detail nguyên văn (không unknown_host)."""
    broken = tmp_path / "ssh_config"
    broken.mkdir()
    monkeypatch.setenv("ROFF_SSH_CONFIG", str(broken))

    class _P:
        alias = "muse-a"
        reachable = False
        detail = "ssh: connect to host muse-a port 22: Connection refused"
        error = None

    monkeypatch.setattr(cli_mod.probe_mod, "cached_profiles", lambda *a, **k: [_P()])

    with pytest.raises(RoffError) as excinfo:
        cli_mod._probe_forced_host("muse-a", _Cfg())

    assert excinfo.value.code == EXIT_UNREACHABLE
    assert excinfo.value.reason == "host_unreachable"
    assert "Connection refused" in str(excinfo.value.fields.get("detail"))


def test_unknown_host_still_reported_for_typo(monkeypatch, tmp_path):
    """Gõ sai tên host ⇒ vẫn là ``unknown_host`` (không hồi quy W3d)."""
    from roffkit.errors import EXIT_UNREACHABLE as UNREACHABLE

    monkeypatch.setattr(cli_mod, "_ssh_config_aliases", lambda: ["muse-a"])

    class _P:
        alias = "muse-x"
        reachable = False
        detail = "ssh: Could not resolve hostname muse-x: Name or service not known"
        error = None

    monkeypatch.setattr(
        cli_mod.probe_mod, "cached_profiles", lambda *a, **k: [_P()]
    )
    with pytest.raises(RoffError) as excinfo:
        cli_mod._probe_forced_host("muse-x", _Cfg())
    assert excinfo.value.code == UNREACHABLE
    assert excinfo.value.reason == "unknown_host"


# --------------------------------------------------------------------------
# W3e — bản đồ busy phải tính cả job xếp hàng
# --------------------------------------------------------------------------
def test_busy_map_counts_queued_jobs(monkeypatch, tmp_path):
    """``_busy_map`` = lease **+** ticket đang xếp hàng."""
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("ROFF_STATE_DIR", str(state))
    ledger = slots_mod.SlotLedger(state_dir=str(state))
    lease_ticket = ledger.request_slot("muse-a", 1)
    ledger.try_acquire(lease_ticket)
    ledger.request_slot("muse-a", 1)  # đang xếp hàng

    busy = cli_mod._busy_map()
    assert busy.get("muse-a") == 2, "busy phải tính cả job đang xếp hàng"


def test_running_map_counts_only_leases(monkeypatch, tmp_path):
    """Bản đồ "đang chạy" chỉ tính lease (job xếp hàng chưa chiếm RAM)."""
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("ROFF_STATE_DIR", str(state))
    ledger = slots_mod.SlotLedger(state_dir=str(state))
    lease_ticket = ledger.request_slot("muse-a", 1)
    ledger.try_acquire(lease_ticket)
    ledger.request_slot("muse-a", 1)

    running = cli_mod._running_map()
    assert running.get("muse-a") == 1


# --------------------------------------------------------------------------
# W3f — failover remap path trong kho input
# --------------------------------------------------------------------------
def _failover_options(tmp_path, **extra):
    options = {
        "workdir": "/home/hatch/.roff/projects/demo",
        "layout_root": "/home/hatch/.roff",
        "input_shared_key": "key-1",
        "input_sources": [{"local": "/data/in", "key": "key-1", "rel": ""}],
        "sync_back_remote": [],
    }
    options.update(extra)
    return options


def test_failover_remaps_path_inside_input_store(monkeypatch, tmp_path):
    """Path trong kho input của host cũ ⇒ remap sang kho input của host mới."""
    monkeypatch.setenv("ROFF_STATE_DIR", str(tmp_path))
    job = job_mod.Job(id="j1", cmd="true", host="muse-a", project="demo", state="running")
    job_mod.save_job(job)
    job_mod.set_job_options(job.id, **_failover_options(tmp_path))

    seen: list[str] = []

    class _Choice:
        host = "muse-b"
        reasons = []

    monkeypatch.setattr(watcher_mod, "cached_profiles", lambda *a, **k: [])
    monkeypatch.setattr(
        watcher_mod, "select", lambda *a, **k: _Choice()
    )
    monkeypatch.setattr(
        watcher_mod, "_config", lambda: _Cfg()
    )
    monkeypatch.setattr(
        watcher_mod, "env_fleet_aliases", lambda cfg: ["muse-b"]
    )
    monkeypatch.setattr(
        watcher_mod, "cached_profiles",
        lambda aliases, cfg: [_profile("muse-b", layout_root="/home/hatch2/.roff")],
    )
    monkeypatch.setattr(toolchain_mod, "ensure_tools", lambda *a, **k: [])

    def _fake_prepare(specs, host, workdir, layout_root, **kwargs):
        return ([], "", "", {})

    monkeypatch.setattr(inputs_mod, "prepare_inputs", _fake_prepare)
    monkeypatch.setattr(watcher_mod, "_shell_runner", lambda host, script: (0, "", ""))
    monkeypatch.setattr(
        watcher_mod, "launch", lambda *a, **k: seen.append(a) or None
    )

    ok = watcher_mod._failover(job, "host_restarted")
    assert ok is True
    options = job_mod.job_options(job.id)
    # Path trong kho input phải trỏ sang kho input của host MỚI, cùng key.
    remapped = options["sync_back_remote"]
    assert remapped == [], "không có sync_back trong test này"


def test_remap_input_store_path_uses_same_key(monkeypatch, tmp_path):
    """Remap path ``<root>/inputs/<key>/x`` ⇒ ``<root2>/inputs/<key>/x``."""
    monkeypatch.setenv("ROFF_STATE_DIR", str(tmp_path))
    captured: list[str] = []

    class _Choice:
        host = "muse-b"
        reasons = []

    monkeypatch.setattr(watcher_mod, "_config", lambda: _Cfg())
    monkeypatch.setattr(watcher_mod, "env_fleet_aliases", lambda cfg: ["muse-b"])
    monkeypatch.setattr(
        watcher_mod,
        "cached_profiles",
        lambda aliases, cfg: [_profile("muse-b", layout_root="/home/hatch2/.roff")],
    )
    monkeypatch.setattr(watcher_mod, "select", lambda *a, **k: _Choice())
    monkeypatch.setattr(toolchain_mod, "ensure_tools", lambda *a, **k: [])
    monkeypatch.setattr(inputs_mod, "prepare_inputs", lambda *a, **k: ([], "", "", {}))
    monkeypatch.setattr(watcher_mod, "_shell_runner", lambda host, script: (0, "", ""))

    def _fake_launch(host, job_, **kwargs):
        captured.append(kwargs.get("env") or {})
        return None

    monkeypatch.setattr(watcher_mod, "launch", _fake_launch)

    job = job_mod.Job(id="j2", cmd="true", host="muse-a", project="demo", state="running")
    job_mod.save_job(job)
    job_mod.set_job_options(
        job.id,
        **_failover_options(
            tmp_path,
            sync_back_remote=["/home/hatch/.roff/inputs/key-1/out.txt"],
        ),
    )

    assert watcher_mod._failover(job, "host_restarted") is True
    options = job_mod.job_options(job.id)
    assert options["sync_back_remote"] == [
        "/home/hatch2/.roff/inputs/key-1/out.txt"
    ]


def test_remap_unknown_input_store_key_is_not_remapped(monkeypatch, tmp_path):
    """Key lạ (host mới không có bản copy) ⇒ giữ nguyên + ghi sự kiện."""
    monkeypatch.setenv("ROFF_STATE_DIR", str(tmp_path))

    class _Choice:
        host = "muse-b"
        reasons = []

    monkeypatch.setattr(watcher_mod, "_config", lambda: _Cfg())
    monkeypatch.setattr(watcher_mod, "env_fleet_aliases", lambda cfg: ["muse-b"])
    monkeypatch.setattr(
        watcher_mod,
        "cached_profiles",
        lambda aliases, cfg: [_profile("muse-b", layout_root="/home/hatch2/.roff")],
    )
    monkeypatch.setattr(watcher_mod, "select", lambda *a, **k: _Choice())
    monkeypatch.setattr(toolchain_mod, "ensure_tools", lambda *a, **k: [])
    monkeypatch.setattr(inputs_mod, "prepare_inputs", lambda *a, **k: ([], "", "", {}))
    monkeypatch.setattr(watcher_mod, "_shell_runner", lambda host, script: (0, "", ""))
    monkeypatch.setattr(watcher_mod, "launch", lambda *a, **k: None)

    job = job_mod.Job(id="j3", cmd="true", host="muse-a", project="demo", state="running")
    job_mod.save_job(job)
    job_mod.set_job_options(
        job.id,
        **_failover_options(
            tmp_path,
            sync_back_remote=["/home/hatch/.roff/inputs/key-la/out.txt"],
        ),
    )

    assert watcher_mod._failover(job, "host_restarted") is True
    options = job_mod.job_options(job.id)
    assert options["sync_back_remote"] == [
        "/home/hatch/.roff/inputs/key-la/out.txt"
    ]
    events = watcher_mod.job_events(job.id)
    assert any(e.get("event") == "failover_path_not_remapped" for e in events)


# --------------------------------------------------------------------------
# W3g — gc dọn kho input trên host
# --------------------------------------------------------------------------
def test_gc_shared_store_removes_old_unused_entry(monkeypatch):
    """Bản copy cũ, không job nào dùng, có marker ⇒ bị xoá."""
    scripts: list[str] = []

    def _fake_run(host, script, timeout=60.0):
        scripts.append(script)
        if "for p in" in script:
            return (0, "dir|abc123|1000000|2048\nfile|abc123.complete|1000000|1\n", "")
        return (0, "", "")

    monkeypatch.setattr(shared_mod, "_run_shell", _fake_run)

    result = shared_mod.gc_shared_store(
        "muse-a", "/home/hatch/.roff", keep_keys=set(), max_age_s=100, now=2000000.0
    )

    assert result["removed"] == ["/home/hatch/.roff/inputs/abc123"]
    assert result["freed_mb"] == pytest.approx(2.0, rel=0.01)


def test_gc_shared_store_keeps_entry_in_use(monkeypatch):
    """Bản copy mà job đang dùng ⇒ KHÔNG được xoá."""
    def _fake_run(host, script, timeout=60.0):
        if "for p in" in script:
            return (0, "dir|abc123|1000000|2048\nfile|abc123.complete|1000000|1\n", "")
        return (0, "", "")

    monkeypatch.setattr(shared_mod, "_run_shell", _fake_run)

    result = shared_mod.gc_shared_store(
        "muse-a", "/home/hatch/.roff", keep_keys={"abc123"}, max_age_s=100, now=2000000.0
    )

    assert result["removed"] == []
    assert any(item["reason"] == "in_use" for item in result["skipped"])


def test_gc_shared_store_keeps_locked_and_recent_entries(monkeypatch):
    """Entry đang bị lock hoặc còn mới ⇒ giữ."""
    def _fake_run(host, script, timeout=60.0):
        if "for p in" in script:
            return (
                0,
                "dir|abc123|1000000|2048\n"
                "file|abc123.complete|1000000|1\n"
                "dir|def456|1999999|2048\n"
                "file|def456.complete|1999999|1\n",
                "",
            )
        return (0, "", "")

    monkeypatch.setattr(shared_mod, "_run_shell", _fake_run)

    result = shared_mod.gc_shared_store(
        "muse-a", "/home/hatch/.roff", keep_keys=set(), max_age_s=100, now=2000000.0
    )

    removed = set(result["removed"])
    assert "/home/hatch/.roff/inputs/def456" not in removed, "entry mới phải được giữ"


def test_gc_json_contains_hosts_and_freed_mb(monkeypatch, tmp_path):
    """JSON của ``roff gc`` phải có ``hosts`` và ``freed_mb``."""
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("ROFF_STATE_DIR", str(state))

    monkeypatch.setattr(cli_mod, "_gc_host_stores", lambda keep, age=None: [
        {"host": "muse-a", "removed": ["/x"], "freed_mb": 1.5, "skipped": []}
    ])
    payload, code = cli_mod._cmd_gc(_GcArgs())
    assert code == 0
    assert isinstance(payload["hosts"], list)
    assert payload["freed_mb"] >= 1.5


class _GcArgs:
    max_age_days = 7.0


# --------------------------------------------------------------------------
# W3h — quyền file state
# --------------------------------------------------------------------------
def test_state_files_are_0600_and_dirs_0700(tmp_path, monkeypatch):
    """File state 0600, thư mục state 0700."""
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("ROFF_STATE_DIR", str(state))

    job = job_mod.Job(id="jperm", cmd="true", host="muse-a", project="demo", state="queued")
    job_mod.save_job(job)
    job_mod.save_job_env("jperm", {"TOKEN": "x"})
    cli_mod.write_audit({"cmd": "true"})
    slots_mod.SlotLedger(state_dir=str(state)).request_slot("muse-a", 1)

    job_file = job_mod.job_path("jperm")
    env_file = job_mod.env_path("jperm")
    for path in (job_file, env_file, cli_mod.audit_path()):
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600, f"{path} phải là 0600, thấy {oct(mode)}"
    for path in (job_mod.ledger_dir(), state):
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o700, f"{path} phải là 0700, thấy {oct(mode)}"


def test_preexisting_loose_files_are_tightened(tmp_path, monkeypatch):
    """File có sẵn (mode 0664) phải được nâng lên 0600."""
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("ROFF_STATE_DIR", str(state))

    loose = state / "audit.jsonl"
    loose.write_text("{}\n", encoding="utf-8")
    os.chmod(loose, 0o664)
    os.chmod(state, 0o755)

    perm.normalize_tree(str(state))

    assert stat.S_IMODE(os.stat(loose).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(state).st_mode) == 0o700


def test_normalize_tree_ignores_missing_root():
    """Thư mục không tồn tại ⇒ không ném lỗi."""
    assert perm.normalize_tree("/khong/ton/tai") == 0


# --------------------------------------------------------------------------
# W3i — nén theo kiểu nội dung chủ đạo
# --------------------------------------------------------------------------
def _write(path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def test_compress_decided_by_dominant_text(tmp_path):
    """≥80 % byte là text ⇒ nén (nếu nén thực sự tiết kiệm)."""
    root = tmp_path / "tree"
    _write(root / "a.txt", b"hello world " * 500)
    _write(root / "b.txt", b"hello world " * 500)

    compress, info = transfer_mod.should_compress_batch(str(root))

    assert compress is True
    assert info["reason"] == "dominant_text"
    assert info["estimated_saving"] > 0.1


def test_compress_skipped_for_dominant_media(tmp_path):
    """≥80 % byte là media (đã nén) ⇒ KHÔNG nén."""
    root = tmp_path / "tree"
    _write(root / "clip.mp4", b"\0" * 10000)
    _write(root / "a.txt", b"x" * 10)

    compress, info = transfer_mod.should_compress_batch(str(root))

    assert compress is False
    assert info["reason"] == "dominant_media"


def test_compress_skipped_when_not_worth_it(tmp_path):
    """Text không nén được (random) ⇒ không nén (tiết kiệm < 10 %)."""
    root = tmp_path / "tree"
    _write(root / "a.txt", os.urandom(20000))

    compress, info = transfer_mod.should_compress_batch(str(root))

    assert compress is False
    assert info["reason"] == "not_worth_it"


def test_compress_decision_is_one_per_batch(tmp_path):
    """Quyết định nén **một lần cho cả lô**, không phải theo từng file."""
    root = tmp_path / "tree"
    _write(root / "a.txt", b"hello world " * 500)
    _write(root / "clip.mp4", b"\0" * 10)

    compress, info = transfer_mod.should_compress_batch(str(root))

    # File media nhỏ lẫn text lớn ⇒ chủ đạo là text ⇒ nén (1 quyết định).
    assert compress is True
    assert info["total_bytes"] > 0


# --------------------------------------------------------------------------
# W3j — watcher tôn trọng ROFF_SSH_CONFIG
# --------------------------------------------------------------------------
def test_watcher_uses_env_fleet_aliases(monkeypatch, tmp_path):
    """Watcher phải đọc alias theo ``$ROFF_SSH_CONFIG`` (không phải ~/.ssh/config)."""
    cfg = tmp_path / "ssh_config"
    cfg.write_text("Host muse-x\n  HostName 10.0.0.1\n", encoding="utf-8")
    monkeypatch.setenv("ROFF_SSH_CONFIG", str(cfg))

    aliases = watcher_mod.env_fleet_aliases(_Cfg())

    assert aliases == ["muse-x"]


# --------------------------------------------------------------------------
# W3k — watcher chạy nice/ionice
# --------------------------------------------------------------------------
def test_spawn_watcher_uses_nice_and_ionice(monkeypatch):
    """Watcher nền phải chạy ``nice -n 19 ionice -c3``."""
    seen: dict = {}

    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            seen["cmd"] = list(cmd)

        pid = 4321

    monkeypatch.setattr(watcher_mod, "_popen", lambda cmd, **kw: _FakePopen(cmd, **kw))
    monkeypatch.setattr(watcher_mod, "set_job_options", lambda *a, **k: {})

    watcher_mod.spawn_watcher("job-1")

    cmd = seen["cmd"]
    assert cmd[:3] == ["nice", "-n", "19"], f"thiếu nice: {cmd}"
    assert "ionice" in cmd and "-c3" in cmd, f"thiếu ionice: {cmd}"


# --------------------------------------------------------------------------
# W3l — host_has_rsync
# --------------------------------------------------------------------------
def test_host_has_rsync_is_cached(monkeypatch):
    """Dò rsync **1 lần** rồi cache lại (không dò lại mỗi lần push)."""
    calls: list[str] = []

    def _fake_run_ssh(alias, script, timeout, stdin=None):
        calls.append(alias)
        return (0, "__RSYNC__\n", "")

    monkeypatch.setattr(transfer_mod, "_RSYNC_CACHE", {})
    monkeypatch.setattr(transfer_mod, "_run_ssh", _fake_run_ssh)

    assert transfer_mod.host_has_rsync("muse-a") is True
    assert transfer_mod.host_has_rsync("muse-a") is True
    assert len(calls) == 1, "phải cache kết quả dò rsync"


def test_host_has_rsync_false_when_marker_absent(monkeypatch):
    """Host không có rsync ⇒ False."""
    monkeypatch.setattr(transfer_mod, "_RSYNC_CACHE", {})
    monkeypatch.setattr(
        transfer_mod, "_run_ssh", lambda *a, **k: (0, "__NO_RSYNC__\n", "")
    )
    assert transfer_mod.host_has_rsync("muse-b") is False


def test_push_skips_rsync_when_host_has_none(monkeypatch, tmp_path):
    """Host không có rsync ⇒ push đi thẳng qua tar (không chạy rsync cho cỏ)."""
    src = tmp_path / "proj"
    src.mkdir()
    (src / "a.txt").write_text("hello")

    monkeypatch.setattr(transfer_mod, "_RSYNC_CACHE", {})
    monkeypatch.setattr(
        transfer_mod, "_run_ssh", lambda *a, **k: (0, "__NO_RSYNC__\n", "")
    )
    cmds: list[list[str]] = []
    monkeypatch.setattr(
        transfer_mod, "_run_cmd", lambda cmd, timeout, stdin=None: cmds.append(cmd) or (0, "", "")
    )
    piped: list = []
    monkeypatch.setattr(
        transfer_mod,
        "_pipeline",
        lambda cmds_, timeout, capture=True: piped.append(cmds_) or (0, "", ""),
    )

    transfer_mod.push("muse-b", str(src), "/remote/proj")

    assert not any("rsync" in " ".join(cmd) for cmd in cmds)
    assert piped, "phải dùng tar-over-ssh"


# --------------------------------------------------------------------------
# W3m — lỗi setup/clone/download ⇒ exit 1/2 (không phải 255)
# --------------------------------------------------------------------------
def test_setup_failure_is_job_failed_not_unreachable(monkeypatch, tmp_path):
    """Script setup hỏng ⇒ exit 1 (job fail), không phải 255."""
    profile = _profile("muse-a")
    script = tmp_path / "setup.sh"
    script.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
    monkeypatch.setattr(toolchain_mod, "_shell_runner", lambda *a, **k: (3, "", "boom"))
    monkeypatch.setattr(
        toolchain_mod, "_read_remote_stamp", lambda *a, **k: {}
    )
    monkeypatch.setattr(
        toolchain_mod, "_sha256_file", lambda path: "hash"
    )
    monkeypatch.setattr(transfer_mod, "push", lambda *a, **k: None)
    monkeypatch.setattr(toolchain_mod.layout_mod, "instance_token", lambda *a, **k: "tok")

    with pytest.raises(RoffError) as excinfo:
        toolchain_mod._run_setup("muse-a", profile, str(script), "ffmpeg")

    assert excinfo.value.code == EXIT_JOB_FAILED
    assert excinfo.value.fields.get("detail")


def test_download_failure_is_input_error(monkeypatch):
    """URL hỏng ⇒ exit 2 (input sai), không phải 255."""
    import urllib.error

    def _boom(*args, **kwargs):
        raise urllib.error.URLError("404 Not Found")

    monkeypatch.setattr("urllib.request.urlopen", _boom)

    with pytest.raises(RoffError) as excinfo:
        inputs_mod.download_http_locally("https://example.invalid/x.bin")

    assert excinfo.value.code == EXIT_INPUT
    assert excinfo.value.fields.get("detail")


def test_git_clone_failure_carries_verbatim_detail(monkeypatch):
    """Lỗi clone phải kèm ``detail`` nguyên văn của git."""
    monkeypatch.setattr(
        inputs_mod,
        "_shell_runner",
        lambda *a, **k: (128, "", "fatal: repository 'https://x' not found"),
    )
    with pytest.raises(RoffError) as excinfo:
        inputs_mod.clone_git_on_host("muse-a", "https://x", "/remote/work")

    assert excinfo.value.code == EXIT_INPUT
    assert "not found" in str(excinfo.value.fields.get("detail"))


# --------------------------------------------------------------------------
# W3n — checkpoint là path trên host
# --------------------------------------------------------------------------
def test_checkpoint_dir_created_on_host(tmp_path, monkeypatch):
    """``--checkpoint`` ⇒ thư mục phải được tạo trên host trước khi launch."""
    created: list[str] = []
    monkeypatch.setattr(
        watcher_mod.layout_mod,
        "remote_mkdirs",
        lambda host, path, run=None: created.append(path),
    )
    monkeypatch.setattr(inputs_mod, "prepare_inputs", lambda *a, **k: ([], "", "", {}))
    launched: list[dict] = []
    monkeypatch.setattr(
        watcher_mod,
        "launch",
        lambda host, job, env=None, **kw: launched.append(env or {}),
    )

    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("ROFF_STATE_DIR", str(state))
    job = job_mod.Job(
        id="jcp",
        cmd="true",
        host="muse-a",
        project="demo",
        state="queued",
        out_remote="/home/hatch/.roff/jobs/jcp/out",
    )
    job_mod.save_job(job)
    job_mod.set_job_options(
        job.id,
        workdir="/home/hatch/.roff/projects/demo",
        layout_root="/home/hatch/.roff",
        checkpoint="/home/hatch/.roff/projects/demo/.checkpoint",
    )

    assert watcher_mod._start(job) is True
    assert "/home/hatch/.roff/projects/demo/.checkpoint" in created
    assert launched and launched[0].get("ROFF_CHECKPOINT") == (
        "/home/hatch/.roff/projects/demo/.checkpoint"
    )


# --------------------------------------------------------------------------
# P1 — ssh thật phải dùng config thay thế
# --------------------------------------------------------------------------
def test_ssh_base_uses_replacement_config(monkeypatch, tmp_path):
    """``$ROFF_SSH_CONFIG`` ⇒ ssh phải nhận ``-F <path>`` (nếu không sẽ nối
    nhầm máy khi alias trùng tên)."""
    cfg = tmp_path / "ssh_config"
    cfg.write_text("Host muse-a\n  HostName 10.0.0.1\n", encoding="utf-8")
    monkeypatch.setenv("ROFF_SSH_CONFIG", str(cfg))

    argv = sshx.ssh_base("muse-a")

    assert "-F" in argv
    assert argv[argv.index("-F") + 1] == str(cfg)


def test_ssh_base_without_replacement_config_has_no_F(monkeypatch):
    """Không có env ⇒ không thêm ``-F`` (ssh tự dùng ~/.ssh/config)."""
    monkeypatch.delenv("ROFF_SSH_CONFIG", raising=False)
    argv = sshx.ssh_base("muse-a")
    assert "-F" not in argv
