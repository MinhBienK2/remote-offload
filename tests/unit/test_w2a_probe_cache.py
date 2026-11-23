"""Hồi quy W2a — cache probe phải **tự chữa**, ``--host`` phải probe mới.

Sự cố thật đã gặp trên fleet (23/11/2026): ``roff do --host muse-pmb333``
trả ``reason:"unknown_host"`` trong khi máy đó **đang sống** và có trong
``~/.ssh/config``.

Nguyên nhân: :func:`roffkit.probe.cached_profiles` đọc entry cache và dựng
``Profile.from_dict(entry["profile"])`` — nếu ``profile`` rỗng/hỏng thì
``from_dict`` cho ``alias=""`` (mặc định), sinh ra một "host ma" không tên;
``_select_host`` tìm ``p.alias == forced`` ⇒ không thấy ⇒ ``unknown_host``.

Các test ở đây **không cần mạng**: ``probe._run_ssh`` được monkeypatch.
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

from roffkit import cli as cli_mod  # noqa: E402
from roffkit import probe as probe_mod  # noqa: E402
from roffkit.errors import EXIT_UNREACHABLE, RoffError  # noqa: E402

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
        "ROFF_TOOL=python3=/usr/bin/python3",
        "ROFF_INTERNET_CODE=200",
        "ROFF_INTERNET_RC=0",
        "ROFF_INTERNET_ERR=",
        "",
    ]
)


class _Cfg:
    """Config tối giểu (duck-typing)."""

    def __init__(self, probe_cache_s: int = 60) -> None:
        self.probe_cache_s = probe_cache_s


def _fake_ssh(output: str = PROBE_OUTPUT, rc: int = 0, stderr: str = ""):
    """Runner ssh giả cho probe — có đếm số lần gọi."""
    calls: list[tuple[str, str]] = []

    def _run(alias: str, script: str, timeout: int):
        calls.append((alias, script))
        return rc, output, stderr

    _run.calls = calls  # type: ignore[attr-defined]
    return _run


def _write_cache(state_dir, alias: str, profile: dict, ts: float = 9e9) -> None:
    """Ghi 1 entry cache thủ công (mô phỏng cache cũ/hỏng)."""
    path = os.path.join(str(state_dir), "probes.json")
    os.makedirs(str(state_dir), exist_ok=True)
    data: dict = {}
    if os.path.isfile(path):
        try:
            data = json.loads(open(path, encoding="utf-8").read())
        except (OSError, ValueError):
            data = {}
    data[alias] = {"ts": ts, "profile": profile}
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle)


# --------------------------------------------------------------------------
# cache tự chữa
# --------------------------------------------------------------------------
def test_broken_cache_entry_is_reprobed(monkeypatch, tmp_path):
    """Cache chứa ``profile: {}`` ⇒ phải probe lại, không trả "host ma"."""
    fake = _fake_ssh()
    monkeypatch.setattr(probe_mod, "_run_ssh", fake)
    state_dir = str(tmp_path)
    _write_cache(tmp_path, "muse-pmb333", {})

    profiles = probe_mod.cached_profiles(
        ["muse-pmb333"], _Cfg(60), state_dir=state_dir
    )

    assert len(fake.calls) == 1, "entry cache hỏng phải được probe lại"
    assert profiles[0].alias == "muse-pmb333"
    assert profiles[0].reachable is True


def test_cache_entry_with_wrong_alias_is_reprobed(monkeypatch, tmp_path):
    """Cache chứa profile của alias **khác** ⇒ coi như hỏng, probe lại."""
    fake = _fake_ssh()
    monkeypatch.setattr(probe_mod, "_run_ssh", fake)
    state_dir = str(tmp_path)
    _write_cache(tmp_path, "muse-pmb333", {"alias": "muse-khác", "cores": 99})

    profiles = probe_mod.cached_profiles(
        ["muse-pmb333"], _Cfg(60), state_dir=state_dir
    )

    assert len(fake.calls) == 1
    assert profiles[0].alias == "muse-pmb333"
    assert profiles[0].cores == 4, "không được dùng profile của alias khác"


def test_cache_entry_with_empty_alias_is_reprobed(monkeypatch, tmp_path):
    """Profile trong cache có ``alias=""`` ⇒ probe lại (không sinh host ma)."""
    fake = _fake_ssh()
    monkeypatch.setattr(probe_mod, "_run_ssh", fake)
    state_dir = str(tmp_path)
    _write_cache(tmp_path, "muse-pmb333", {"alias": "", "cores": 8})

    profiles = probe_mod.cached_profiles(
        ["muse-pmb333"], _Cfg(60), state_dir=state_dir
    )

    assert len(fake.calls) == 1
    assert profiles[0].alias == "muse-pmb333"


def test_valid_cache_entry_is_still_used(monkeypatch, tmp_path):
    """Cache hợp lệ (alias khớp) ⇒ **không** probe lại (hành vi cũ giữ nguyên)."""
    fake = _fake_ssh()
    monkeypatch.setattr(probe_mod, "_run_ssh", fake)
    state_dir = str(tmp_path)

    probe_mod.cached_profiles(["muse-pmb333"], _Cfg(60), state_dir=state_dir)
    again = probe_mod.cached_profiles(["muse-pmb333"], _Cfg(60), state_dir=state_dir)

    assert len(fake.calls) == 1
    assert again[0].alias == "muse-pmb333"


# --------------------------------------------------------------------------
# force
# --------------------------------------------------------------------------
def test_force_bypasses_fresh_cache(monkeypatch, tmp_path):
    """``force=[alias]`` ⇒ probe mới dù cache còn hạn."""
    fake = _fake_ssh()
    monkeypatch.setattr(probe_mod, "_run_ssh", fake)
    state_dir = str(tmp_path)

    probe_mod.cached_profiles(["muse-pmb333"], _Cfg(60), state_dir=state_dir)
    assert len(fake.calls) == 1

    probe_mod.cached_profiles(
        ["muse-pmb333"], _Cfg(60), state_dir=state_dir, force=["muse-pmb333"]
    )
    assert len(fake.calls) == 2, "force phải bỏ qua cache"


def test_force_only_affects_listed_aliases(monkeypatch, tmp_path):
    """``force`` chỉ áp cho alias được nêu; alias khác vẫn dùng cache."""
    fake = _fake_ssh()
    monkeypatch.setattr(probe_mod, "_run_ssh", fake)
    state_dir = str(tmp_path)

    probe_mod.cached_profiles(["a", "b"], _Cfg(60), state_dir=state_dir)
    assert len(fake.calls) == 2

    probe_mod.cached_profiles(
        ["a", "b"], _Cfg(60), state_dir=state_dir, force=["a"]
    )
    assert len(fake.calls) == 3, "chỉ alias 'a' được probe lại"


def test_force_is_optional_and_keeps_old_behaviour(monkeypatch, tmp_path):
    """Không truyền ``force`` ⇒ hành vi cũ (cache còn hạn thì dùng)."""
    fake = _fake_ssh()
    monkeypatch.setattr(probe_mod, "_run_ssh", fake)
    state_dir = str(tmp_path)

    probe_mod.cached_profiles(["a"], _Cfg(60), state_dir=state_dir)
    probe_mod.cached_profiles(["a"], _Cfg(60), state_dir=state_dir)
    assert len(fake.calls) == 1


def test_cache_key_matches_profile_alias(monkeypatch, tmp_path):
    """Khoá cache = ``profile.alias`` thực tế ⇒ entry luôn tự nhất quán."""
    monkeypatch.setattr(probe_mod, "_run_ssh", _fake_ssh())
    state_dir = str(tmp_path)

    probe_mod.cached_profiles(["muse-pmb333"], _Cfg(60), state_dir=state_dir)
    data = json.loads(
        open(os.path.join(state_dir, "probes.json"), encoding="utf-8").read()
    )
    assert data["muse-pmb333"]["profile"]["alias"] == "muse-pmb333"


# --------------------------------------------------------------------------
# --host: probe mới + không bao giờ "unknown_host" cho host có thật
# --------------------------------------------------------------------------
def test_forced_host_is_probed_fresh_even_with_cache(monkeypatch, tmp_path):
    """``--host`` ⇒ bypass cache (cache cũ không được che host được ghim)."""
    fake = _fake_ssh()
    monkeypatch.setattr(probe_mod, "_run_ssh", fake)
    state_dir = str(tmp_path)

    # cache cũ, còn hạn, nhưng hỏng
    _write_cache(tmp_path, "muse-pmb333", {})

    profiles = probe_mod.cached_profiles(
        ["muse-pmb333"], _Cfg(600), state_dir=state_dir, force=["muse-pmb333"]
    )
    assert len(fake.calls) == 1
    assert profiles[0].alias == "muse-pmb333"


def test_probe_forced_host_uses_ssh_config_aliases(monkeypatch, tmp_path):
    """Host có thật trong ``~/.ssh/config`` nhưng chết ⇒ ``host_unreachable``."""
    monkeypatch.setattr(cli_mod, "_ssh_config_aliases", lambda: ["muse-pmb333"])
    monkeypatch.setattr(
        probe_mod,
        "cached_profiles",
        lambda *a, **k: [
            probe_mod.Profile(
                alias="muse-pmb333",
                reachable=False,
                error="timeout",
                detail="ssh: connect to host muse-pmb333 port 22: Connection timed out",
            )
        ],
    )
    with pytest.raises(RoffError) as excinfo:
        cli_mod._probe_forced_host("muse-pmb333", _Cfg(60))

    exc = excinfo.value
    assert exc.code == EXIT_UNREACHABLE
    assert exc.reason == "host_unreachable", "host có thật KHÔNG được báo unknown_host"
    assert "Connection timed out" in exc.payload()["detail"]


def test_probe_forced_host_unknown_alias_is_unknown_host(monkeypatch):
    """Alias không có trong ``~/.ssh/config`` ⇒ ``unknown_host`` + lỗi ssh gốc."""
    monkeypatch.setattr(cli_mod, "_ssh_config_aliases", lambda: ["muse-a"])
    monkeypatch.setattr(
        probe_mod,
        "cached_profiles",
        lambda *a, **k: [
            probe_mod.Profile(
                alias="khong-co",
                reachable=False,
                error="dns_error",
                detail="ssh: Could not resolve hostname khong-co: Name or service not known",
            )
        ],
    )
    with pytest.raises(RoffError) as excinfo:
        cli_mod._probe_forced_host("khong-co", _Cfg(60))

    exc = excinfo.value
    assert exc.code == EXIT_UNREACHABLE
    assert exc.reason == "unknown_host"
    assert "Could not resolve hostname" in exc.payload()["detail"]


def test_probe_forced_host_reachable_returns_profile(monkeypatch):
    """Host sống ⇒ trả về profile (không ném lỗi)."""
    monkeypatch.setattr(cli_mod, "_ssh_config_aliases", lambda: ["muse-pmb333"])
    monkeypatch.setattr(
        probe_mod,
        "cached_profiles",
        lambda *a, **k: [probe_mod.Profile(alias="muse-pmb333", reachable=True)],
    )
    profiles = cli_mod._probe_forced_host("muse-pmb333", _Cfg(60))
    assert profiles[0].alias == "muse-pmb333"
    assert profiles[0].reachable is True
