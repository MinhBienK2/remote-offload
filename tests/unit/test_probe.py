"""Unit test cho ``roffkit.probe``.

Không cần mạng thật: `_run_ssh` được monkeypatch để trả về output giả của
script probe (các dòng ``ROFF_*``).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(_HERE))
_SKILL = os.path.join(_REPO, "skill")
if _SKILL not in sys.path:
    sys.path.insert(0, _SKILL)

from roffkit import probe as probe_mod  # noqa: E402


# --------------------------------------------------------------------------
# dữ liệu giả
# --------------------------------------------------------------------------
FULL_OUTPUT = "\n".join(
    [
        "ROFF_HOSTNAME=htch-runtime",
        "ROFF_BOOT_ID=41bfee50-0000-1111-2222-333344445555",
        "ROFF_HOME=/root",
        "ROFF_CORES=4",
        "ROFF_MEM_TOTAL_KB=8114176",
        "ROFF_MEM_AVAILABLE_KB=514048",
        "ROFF_CGROUP_LIMIT_MB=",
        "ROFF_CGROUP_USED_MB=",
        "ROFF_LOAD1=0.81",
        "ROFF_LAYOUT_ROOT=/home/hatch/.roff",
        "ROFF_DURABLE=1",
        "ROFF_DISK_FREE_MB=153600",
        "ROFF_INSTANCE_TOKEN=abcdef0123456789abcdef0123456789",
        "ROFF_UPTIME_S=3600.5",
        "ROFF_TOOL=ffmpeg=/usr/bin/ffmpeg",
        "ROFF_TOOL=git=/usr/bin/git",
        "ROFF_PROJECT=foo",
        "ROFF_TOOLCHAIN=ffmpeg",
        "ROFF_INTERNET_CODE=200",
        "ROFF_INTERNET_RC=0",
        "ROFF_INTERNET_ERR=",
        "",
    ]
)


class _Cfg:
    """Config tối giểu cho test (duck-typing, không phụ thuộc phần A)."""

    def __init__(self, probe_cache_s: int = 60) -> None:
        self.probe_cache_s = probe_cache_s


def _profile(alias: str, **kw) -> "probe_mod.Profile":
    """Dựng Profile trực tiếp (không qua ssh) cho test nhận diện máy."""
    base = dict(
        alias=alias,
        reachable=True,
        hostname="htch",
        boot_id="boot-1",
        instance_token="tok-1",
        home="/root",
        layout_root="/home/hatch/.roff",
        durable=True,
        tools={},
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
    return probe_mod.Profile(**base)


def _fake_ssh(output: str = FULL_OUTPUT, rc: int = 0, stderr: str = ""):
    """Trả về hàm giả cho probe._run_ssh, có đếm số lần gọi."""

    calls: list[tuple[str, str]] = []

    def _run(alias: str, script: str, timeout: int):
        calls.append((alias, script))
        return rc, output, stderr

    _run.calls = calls  # type: ignore[attr-defined]
    return _run


# --------------------------------------------------------------------------
# script probe
# --------------------------------------------------------------------------
def test_script_is_valid_shell():
    """Script probe phải parse được bởi ``sh -n`` (không lỗi cú pháp)."""
    script = probe_mod.build_script()
    proc = subprocess.run(["sh", "-n"], input=script.encode(), capture_output=True)
    assert proc.returncode == 0, proc.stderr.decode()


def test_script_contains_every_probe():
    """Script phải gom hostname/boot_id/home/mem/cgroup/load/disk/tool/internet."""
    script = probe_mod.build_script()
    for needle in (
        "emit HOSTNAME",
        "emit BOOT_ID",
        "emit HOME",
        "emit CORES",
        "emit MEM_TOTAL_KB",
        "emit MEM_AVAILABLE_KB",
        "emit CGROUP_LIMIT_MB",
        "emit CGROUP_USED_MB",
        "emit LOAD1",
        "emit LAYOUT_ROOT",
        "emit DURABLE",
        "emit DISK_FREE_MB",
        "emit INSTANCE_TOKEN",
        "emit TOOL ",
        "emit INTERNET_CODE",
        "emit INTERNET_ERR",
    ):
        assert needle in script, needle


def test_script_lists_all_interesting_tools():
    script = probe_mod.build_script()
    for tool in (
        "ffmpeg", "ffprobe", "node", "npm", "python3", "pip3", "git", "rsync",
        "tar", "curl", "wget", "gcc", "make", "cmake", "go", "cargo", "rustc",
        "java", "mvn", "gradle", "docker", "podman", "deno", "bun", "pnpm",
        "yarn", "uv", "poetry", "dotnet", "playwright", "chromium",
        "google-chrome",
    ):
        assert tool in script, tool


def test_script_reads_cgroup_v1_and_v2():
    script = probe_mod.build_script()
    assert "memory.max" in script
    assert "memory.current" in script
    assert "memory.limit_in_bytes" in script
    assert "memory.usage_in_bytes" in script


# --------------------------------------------------------------------------
# parse output
# --------------------------------------------------------------------------
def test_script_runs_on_a_real_shell_and_parses(tmp_path):
    """Chạy script thật qua `sh -s` (không mạng) rồi parse ra Profile.

    HOME trỏ vào thư mục tạm để không đụng máy thật.
    """
    script = probe_mod.build_script()
    env = dict(os.environ)
    env["HOME"] = str(tmp_path)
    env["ROFF_PROBE_NO_NET"] = "1"
    proc = subprocess.run(
        ["sh", "-s"],
        input=script.encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr.decode()
    values = probe_mod._parse_output(proc.stdout.decode("utf-8", "replace"))
    assert values, "script không in dòng ROFF_ nào"
    profile = probe_mod._profile_from_values("local", values)
    assert profile.reachable is True
    assert profile.home == str(tmp_path)
    assert profile.cores >= 1
    assert profile.mem_total_mb > 0
    assert profile.disk_free_mb > 0
    assert profile.instance_token
    assert profile.internet is False
    # token phải được tạo trong $HOME (mode 600) để phát hiện wipe
    token_file = tmp_path / ".roff-instance-token"
    assert token_file.exists()
    assert oct(token_file.stat().st_mode)[-3:] == "600"


def test_token_not_reported_when_it_cannot_be_persisted(tmp_path):
    """Token không ghi được thì KHÔNG báo (token đổi mỗi lần = giả wipe)."""
    script = probe_mod.build_script()
    env = dict(os.environ)
    env["HOME"] = str(tmp_path / "khong-ton-tai")
    env["ROFF_PROBE_NO_NET"] = "1"
    proc = subprocess.run(
        ["sh", "-s"],
        input=script.encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr.decode()
    values = probe_mod._parse_output(proc.stdout.decode("utf-8", "replace"))
    profile = probe_mod._profile_from_values("local", values)
    assert profile.instance_token is None


def test_run_ssh_normalizes_dataclass_result(monkeypatch):
    """Runner dùng chung (sshx.RunResult) phải được chuẩn hoá về tuple."""
    sshx = pytest.importorskip("roffkit.sshx")

    def fake_run_shell(host, script, timeout=60.0, **kwargs):
        return sshx.RunResult(rc=0, stdout="ROFF_CORES=4\n", stderr="")

    monkeypatch.setattr(probe_mod, "_ssh_runner", lambda: fake_run_shell)
    rc, out, err = probe_mod._run_ssh("muse-pmb333", "script", 30)
    assert rc == 0
    assert "ROFF_CORES=4" in out
    assert err == ""


def test_probe_uses_shared_ssh_runner_when_available(monkeypatch):
    """Nếu roffkit.sshx có, probe phải đi qua đó (hợp đồng §15b.1)."""
    sshx = pytest.importorskip("roffkit.sshx")
    seen = {}

    def fake_run_shell(host, script, timeout=60.0, **kwargs):
        seen["host"] = host
        seen["script"] = script
        return sshx.RunResult(rc=0, stdout=FULL_OUTPUT, stderr="")

    monkeypatch.setattr(probe_mod, "_ssh_runner", lambda: fake_run_shell)
    profile = probe_mod.probe("muse-pmb333")
    assert seen["host"] == "muse-pmb333"
    assert "emit HOSTNAME" in seen["script"]
    assert profile.reachable is True
    assert profile.cores == 4


def test_parse_output_multivalue_keys():
    values = probe_mod._parse_output(FULL_OUTPUT)
    assert values["TOOL"] == ["ffmpeg=/usr/bin/ffmpeg", "git=/usr/bin/git"]
    assert values["PROJECT"] == ["foo"]
    assert values["TOOLCHAIN"] == ["ffmpeg"]


def test_parse_output_ignores_non_roff_lines():
    values = probe_mod._parse_output("welcome to the bridge\nROFF_CORES=8\nrandom text\n")
    assert values["CORES"] == ["8"]
    assert "random" not in values


# --------------------------------------------------------------------------
# probe()
# --------------------------------------------------------------------------
def test_probe_uses_exactly_one_ssh_round_trip(monkeypatch):
    fake = _fake_ssh()
    monkeypatch.setattr(probe_mod, "_run_ssh", fake)
    profile = probe_mod.probe("muse-pmb333")
    assert len(fake.calls) == 1
    assert profile.reachable is True
    assert profile.alias == "muse-pmb333"


def test_probe_fills_every_field(monkeypatch):
    monkeypatch.setattr(probe_mod, "_run_ssh", _fake_ssh())
    p = probe_mod.probe("muse-pmb333")
    assert p.hostname == "htch-runtime"
    assert p.boot_id == "41bfee50-0000-1111-2222-333344445555"
    assert p.home == "/root"
    assert p.layout_root == "/home/hatch/.roff"
    assert p.durable is True
    assert p.cores == 4
    assert p.mem_total_mb == 8114176 // 1024
    assert p.mem_available_mb == 514048 // 1024
    assert p.load1 == pytest.approx(0.81)
    assert p.disk_free_mb == 153600
    assert p.internet is True
    assert p.instance_token == "abcdef0123456789abcdef0123456789"
    assert p.probed_at > 0


def test_probe_records_only_existing_tools(monkeypatch):
    monkeypatch.setattr(probe_mod, "_run_ssh", _fake_ssh())
    p = probe_mod.probe("muse-pmb333")
    assert p.tools == {"ffmpeg": "/usr/bin/ffmpeg", "git": "/usr/bin/git"}
    assert "npm" not in p.tools


def test_probe_cgroup_unreadable_is_none(monkeypatch):
    """memory.max không đọc được (Muse) → None, KHÔNG suy đoán."""
    monkeypatch.setattr(probe_mod, "_run_ssh", _fake_ssh())
    p = probe_mod.probe("muse-pmb333")
    assert p.cgroup_mem_limit_mb is None
    assert p.cgroup_mem_used_mb is None


def test_probe_cgroup_max_is_none(monkeypatch):
    out = FULL_OUTPUT.replace("ROFF_CGROUP_LIMIT_MB=", "ROFF_CGROUP_LIMIT_MB=max")
    monkeypatch.setattr(probe_mod, "_run_ssh", _fake_ssh(out))
    p = probe_mod.probe("muse-pmb333")
    assert p.cgroup_mem_limit_mb is None


def test_probe_cgroup_numeric_is_read(monkeypatch):
    out = FULL_OUTPUT.replace("ROFF_CGROUP_LIMIT_MB=", "ROFF_CGROUP_LIMIT_MB=1024")
    out = out.replace("ROFF_CGROUP_USED_MB=", "ROFF_CGROUP_USED_MB=512")
    monkeypatch.setattr(probe_mod, "_run_ssh", _fake_ssh(out))
    p = probe_mod.probe("muse-pmb333")
    assert p.cgroup_mem_limit_mb == 1024
    assert p.cgroup_mem_used_mb == 512


def test_probe_gpu_none_when_absent(monkeypatch):
    monkeypatch.setattr(probe_mod, "_run_ssh", _fake_ssh())
    p = probe_mod.probe("muse-pmb333")
    assert p.gpu is None


def test_probe_gpu_dict_when_present(monkeypatch):
    out = FULL_OUTPUT + "ROFF_GPU_PRESENT=1\nROFF_GPU_NAME=NVIDIA A100\n"
    monkeypatch.setattr(probe_mod, "_run_ssh", _fake_ssh(out))
    p = probe_mod.probe("muse-pmb333")
    assert isinstance(p.gpu, dict)
    assert p.gpu.get("name") == "NVIDIA A100"


def test_probe_internet_429_is_bridge_busy_not_offline(monkeypatch):
    out = FULL_OUTPUT.replace("ROFF_INTERNET_CODE=200", "ROFF_INTERNET_CODE=429")
    out = out.replace(
        "ROFF_INTERNET_ERR=",
        "ROFF_INTERNET_ERR=curl: (22) The requested URL returned error: 429",
    )
    monkeypatch.setattr(probe_mod, "_run_ssh", _fake_ssh(out))
    p = probe_mod.probe("muse-pmb333")
    assert p.internet is False
    assert p.bridge_busy is True
    assert p.error and "bridge" in p.error.lower()
    assert "429" in (p.detail or "")


def test_probe_internet_already_active_is_bridge_busy(monkeypatch):
    out = FULL_OUTPUT.replace("ROFF_INTERNET_CODE=200", "ROFF_INTERNET_CODE=000")
    out = out.replace(
        "ROFF_INTERNET_ERR=",
        "ROFF_INTERNET_ERR=ssh: session already active for this host",
    )
    monkeypatch.setattr(probe_mod, "_run_ssh", _fake_ssh(out))
    p = probe_mod.probe("muse-pmb333")
    assert p.internet is False
    assert p.bridge_busy is True


def test_probe_internet_timeout_is_false(monkeypatch):
    out = FULL_OUTPUT.replace("ROFF_INTERNET_CODE=200", "ROFF_INTERNET_CODE=000")
    out = out.replace("ROFF_INTERNET_ERR=", "ROFF_INTERNET_ERR=curl: (28) Operation timed out")
    monkeypatch.setattr(probe_mod, "_run_ssh", _fake_ssh(out))
    p = probe_mod.probe("muse-pmb333")
    assert p.internet is False
    assert p.bridge_busy is False


def test_probe_ssh_failure_permission_denied(monkeypatch):
    monkeypatch.setattr(
        probe_mod,
        "_run_ssh",
        _fake_ssh(rc=255, stderr="muse-pmb333: Permission denied (publickey)."),
    )
    p = probe_mod.probe("muse-pmb333")
    assert p.reachable is False
    assert p.error
    assert "permission" in p.error.lower()
    # detail phải chứa nguyên văn lỗi ssh gốc
    assert "Permission denied (publickey)." in (p.detail or "")
    assert p.hostname is None


def test_probe_ssh_failure_connection_refused(monkeypatch):
    monkeypatch.setattr(
        probe_mod,
        "_run_ssh",
        _fake_ssh(rc=255, stderr="ssh: connect to host 127.0.0.1 port 2222: Connection refused"),
    )
    p = probe_mod.probe("muse-pmb333")
    assert p.reachable is False
    assert p.error and "permission" not in p.error.lower()
    assert "Connection refused" in (p.detail or "")


def test_probe_timeout_is_reported(monkeypatch):
    def _boom(alias, script, timeout):
        raise subprocess.TimeoutExpired(cmd="ssh", timeout=timeout)

    monkeypatch.setattr(probe_mod, "_run_ssh", _boom)
    p = probe_mod.probe("muse-pmb333")
    assert p.reachable is False
    assert p.error and "timeout" in p.error.lower()


def test_probe_empty_output_is_not_reachable(monkeypatch):
    monkeypatch.setattr(probe_mod, "_run_ssh", _fake_ssh(output=""))
    p = probe_mod.probe("muse-pmb333")
    assert p.reachable is False
    assert p.error


def test_probe_does_not_print(monkeypatch, capsys):
    monkeypatch.setattr(probe_mod, "_run_ssh", _fake_ssh())
    probe_mod.probe("muse-pmb333")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


# --------------------------------------------------------------------------
# probe_many()
# --------------------------------------------------------------------------
def test_probe_many_is_serial_by_default(monkeypatch):
    state = {"concurrent": 0, "max_concurrent": 0}
    lock = threading.Lock()

    def _run(alias, script, timeout):
        with lock:
            state["concurrent"] += 1
            state["max_concurrent"] = max(state["max_concurrent"], state["concurrent"])
        time.sleep(0.02)
        with lock:
            state["concurrent"] -= 1
        return 0, FULL_OUTPUT, ""

    monkeypatch.setattr(probe_mod, "_run_ssh", _run)
    profiles = probe_mod.probe_many(["a", "b", "c"])
    assert len(profiles) == 3
    assert state["max_concurrent"] == 1


def test_probe_many_parallel_option(monkeypatch):
    def _run(alias, script, timeout):
        return 0, FULL_OUTPUT, ""

    monkeypatch.setattr(probe_mod, "_run_ssh", _run)
    profiles = probe_mod.probe_many(["a", "b"], parallel=True)
    assert [p.alias for p in profiles] == ["a", "b"]


# --------------------------------------------------------------------------
# to_dict / from_dict
# --------------------------------------------------------------------------
def test_profile_roundtrip(monkeypatch):
    monkeypatch.setattr(probe_mod, "_run_ssh", _fake_ssh())
    p = probe_mod.probe("muse-pmb333")
    d = p.to_dict()
    assert json.loads(json.dumps(d)) == d  # JSON-safe
    p2 = probe_mod.Profile.from_dict(d)
    assert p2.to_dict() == d


def test_profile_from_dict_tolerates_missing_keys():
    p = probe_mod.Profile.from_dict({"alias": "x", "cores": 2})
    assert p.alias == "x"
    assert p.cores == 2
    assert p.reachable is False
    assert p.tools == {}


# --------------------------------------------------------------------------
# nhận diện máy vật lý + phát hiện flap (API-CONTRACT §15b.5)
# --------------------------------------------------------------------------
def test_physical_host_key_is_hostname_boot_token():
    p = _profile("a", hostname="htch", boot_id="boot-1", instance_token="tok-1")
    assert probe_mod.physical_host_key(p) == ("htch", "boot-1", "tok-1")


def test_same_physical_host_true_for_same_machine():
    a = _profile("alias-a", hostname="htch", boot_id="boot-1", instance_token="tok-1")
    b = _profile("alias-b", hostname="htch", boot_id="boot-1", instance_token="tok-1")
    assert probe_mod.same_physical_host(a, b) is True


def test_same_physical_host_false_for_different_token():
    a = _profile("alias-a", hostname="htch", boot_id="boot-1", instance_token="tok-1")
    b = _profile("alias-b", hostname="htch", boot_id="boot-1", instance_token="tok-2")
    assert probe_mod.same_physical_host(a, b) is False


def test_same_physical_host_false_for_different_boot_id():
    a = _profile("alias-a", hostname="htch", boot_id="boot-1", instance_token="tok-1")
    b = _profile("alias-b", hostname="htch", boot_id="boot-2", instance_token="tok-1")
    assert probe_mod.same_physical_host(a, b) is False


def test_same_physical_host_false_when_unknown():
    """Không đọc được boot_id/token thì KHÔNG kết luận là cùng máy."""
    a = _profile("alias-a", hostname=None, boot_id=None, instance_token=None)
    b = _profile("alias-b", hostname=None, boot_id=None, instance_token=None)
    assert probe_mod.same_physical_host(a, b) is False


def test_detect_flapping_finds_group_with_two_tokens():
    profiles = [
        _profile("muse-tampt", hostname="htch", boot_id="boot-1", instance_token="tok-1"),
        _profile("muse-tampt2", hostname="htch", boot_id="boot-1", instance_token="tok-2"),
        _profile("muse-other", hostname="htch2", boot_id="boot-9", instance_token="tok-9"),
    ]
    flaps = probe_mod.detect_flapping(profiles)
    assert len(flaps) == 1
    flap = flaps[0]
    assert flap["boot_id"] == "boot-1"
    assert flap["hostname"] == "htch"
    assert flap["aliases"] == ["muse-tampt", "muse-tampt2"]
    assert flap["tokens"] == {"muse-tampt": "tok-1", "muse-tampt2": "tok-2"}


def test_detect_flapping_empty_when_tokens_match():
    profiles = [
        _profile("a", hostname="htch", boot_id="boot-1", instance_token="tok-1"),
        _profile("b", hostname="htch", boot_id="boot-1", instance_token="tok-1"),
    ]
    assert probe_mod.detect_flapping(profiles) == []


def test_detect_flapping_ignores_unreachable_profiles():
    profiles = [
        _profile("a", hostname=None, boot_id=None, instance_token=None, reachable=False),
        _profile("b", hostname=None, boot_id=None, instance_token=None, reachable=False),
    ]
    assert probe_mod.detect_flapping(profiles) == []


def test_detect_flapping_empty_fleet():
    assert probe_mod.detect_flapping([]) == []


# --------------------------------------------------------------------------
# cache
# --------------------------------------------------------------------------
def test_cached_profiles_uses_cache_within_ttl(monkeypatch, tmp_path):
    fake = _fake_ssh()
    monkeypatch.setattr(probe_mod, "_run_ssh", fake)
    cfg = _Cfg(probe_cache_s=60)
    state_dir = str(tmp_path)

    first = probe_mod.cached_profiles(["muse-pmb333"], cfg, state_dir=state_dir)
    assert len(first) == 1
    assert len(fake.calls) == 1

    second = probe_mod.cached_profiles(["muse-pmb333"], cfg, state_dir=state_dir)
    assert len(fake.calls) == 1, "phải dùng cache, không probe lại"
    assert second[0].cores == first[0].cores


def test_cached_profiles_reprobes_after_ttl(monkeypatch, tmp_path):
    fake = _fake_ssh()
    monkeypatch.setattr(probe_mod, "_run_ssh", fake)
    cfg = _Cfg(probe_cache_s=0)
    state_dir = str(tmp_path)

    probe_mod.cached_profiles(["muse-pmb333"], cfg, state_dir=state_dir)
    probe_mod.cached_profiles(["muse-pmb333"], cfg, state_dir=state_dir)
    assert len(fake.calls) == 2


def test_cached_profiles_writes_json_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(probe_mod, "_run_ssh", _fake_ssh())
    probe_mod.cached_profiles(["muse-pmb333"], _Cfg(60), state_dir=str(tmp_path))
    cache_file = tmp_path / "probes.json"
    assert cache_file.exists()
    data = json.loads(cache_file.read_text(encoding="utf-8"))
    assert "muse-pmb333" in data
    assert data["muse-pmb333"]["ts"] > 0


def test_cached_profiles_keeps_order(monkeypatch, tmp_path):
    monkeypatch.setattr(probe_mod, "_run_ssh", _fake_ssh())
    profiles = probe_mod.cached_profiles(["a", "b"], _Cfg(60), state_dir=str(tmp_path))
    assert [p.alias for p in profiles] == ["a", "b"]
