"""Unit test cho ``roffkit.policy`` — lọc cứng trước, chấm điểm sau."""

from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(_HERE))
_SKILL = os.path.join(_REPO, "skill")
if _SKILL not in sys.path:
    sys.path.insert(0, _SKILL)

from roffkit import policy as policy_mod  # noqa: E402
from roffkit.policy import Choice, Need  # noqa: E402
from roffkit.probe import Profile  # noqa: E402


class _HostCfg:
    """HostConfig tối thiểu (duck-typing)."""

    def __init__(self, name: str, labels=None, weight: float = 1.0, slots: int = 1):
        self.name = name
        self.labels = list(labels or [])
        self.weight = weight
        self.slots = slots
        self.mem_budget_mb = 0


class _Cfg:
    """Config tối thiểu (duck-typing, không phụ thuộc phần A)."""

    def __init__(self, hosts=None):
        self._hosts = dict(hosts or {})

    def host(self, name: str) -> _HostCfg:
        return self._hosts.get(name, _HostCfg(name))


def _profile(alias: str, **kw) -> Profile:
    """Profile mặc định là 'khoẻ' để chỉ test điều muốn."""
    base = dict(
        alias=alias,
        reachable=True,
        hostname="host-" + alias,
        boot_id="boot-" + alias,
        home="/root",
        layout_root="/home/hatch/.roff",
        durable=True,
        tools={"ffmpeg": "/usr/bin/ffmpeg", "git": "/usr/bin/git", "python3": "/usr/bin/python3"},
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
# lọc cứng
# --------------------------------------------------------------------------
def test_select_returns_none_when_all_unreachable():
    profiles = [_profile("a", reachable=False, error="timeout", detail="timed out")]
    assert policy_mod.select(profiles, Need(), _Cfg()) is None


def test_select_returns_none_on_empty_fleet():
    assert policy_mod.select([], Need(), _Cfg()) is None


def test_unreachable_host_is_rejected_with_reason():
    profiles = [_profile("a", reachable=False, error="permission_denied", detail="Permission denied (publickey).")]
    assert policy_mod.select(profiles, Need(), _Cfg()) is None
    reason = policy_mod.reject_reason(profiles[0], Need(), _Cfg(), 0)
    assert reason is not None
    assert "permission" in reason.lower()


def test_rejected_contains_alias_and_reason():
    profiles = [_profile("a", reachable=False, error="timeout", detail="ssh timed out")]
    # select trả None khi không host nào dùng được -> reason nằm ở hàm lọc
    assert policy_mod.select(profiles, Need(), _Cfg()) is None
    reasons = policy_mod.reject_reason(profiles[0], Need(), _Cfg(), 0)
    assert reasons is not None
    assert "timeout" in reasons


def test_missing_tool_is_rejected_and_reason_is_clear():
    profiles = [_profile("a", tools={"git": "/usr/bin/git"})]
    assert policy_mod.select(profiles, Need(tools=["ffmpeg"]), _Cfg()) is None
    reason = policy_mod.reject_reason(profiles[0], Need(tools=["ffmpeg"]), _Cfg(), 0)
    assert reason is not None
    assert "ffmpeg" in reason


def test_tool_present_is_accepted():
    profiles = [_profile("a", tools={"ffmpeg": "/usr/bin/ffmpeg"})]
    choice = policy_mod.select(profiles, Need(tools=["ffmpeg"]), _Cfg())
    assert choice is not None
    assert choice.host == "a"


def test_ram_floor_is_absolute_not_a_job_requirement():
    """Sàn RAM là sàn TUYỆT ĐỐI của host, không phải yêu cầu của job.

    Rule (đã đổi 23/11/2026 sau khi đo fleet thật): host phải còn
    ``max(256, need.mem_mb + mem_reserve_mb)`` MB. Host còn 400 MB thì job
    nhỏ vẫn chạy được; host còn 200 MB thì không job nào chạy được.
    Xem regression R21.
    """
    # 400 MB trống, job không cần RAM → chạy được
    profiles = [_profile("a", mem_available_mb=400)]
    assert policy_mod.select(profiles, Need(), _Cfg()) is not None

    # 200 MB trống → dưới sàn tuyệt đối 256 MB → loại
    profiles = [_profile("a", mem_available_mb=200)]
    assert policy_mod.select(profiles, Need(), _Cfg()) is None
    reason = policy_mod.reject_reason(profiles[0], Need(), _Cfg(), 0)
    assert reason is not None and "256" in reason


def test_ram_reserve_is_added_to_the_need():
    """Job cần N MB thì host phải còn N + reserve (mặc định 256)."""
    profiles = [_profile("a", mem_available_mb=800)]
    assert policy_mod.select(profiles, Need(mem_mb=1024), _Cfg()) is None
    profiles = [_profile("a", mem_available_mb=2000)]
    assert policy_mod.select(profiles, Need(mem_mb=1024), _Cfg()) is not None


def test_disk_floor():
    profiles = [_profile("a", disk_free_mb=100)]
    assert policy_mod.select(profiles, Need(disk_mb=1000), _Cfg()) is None
    reason = policy_mod.reject_reason(profiles[0], Need(disk_mb=1000), _Cfg(), 0)
    assert reason is not None and "disk" in reason.lower()


def test_full_slot_host_is_rejected():
    profiles = [_profile("a")]
    cfg = _Cfg({"a": _HostCfg("a", slots=1)})
    assert policy_mod.select(profiles, Need(), cfg, busy={"a": 1}) is None
    reason = policy_mod.reject_reason(profiles[0], Need(), cfg, 1)
    assert reason is not None and "slot" in reason.lower()


def test_free_slot_host_is_accepted():
    profiles = [_profile("a")]
    cfg = _Cfg({"a": _HostCfg("a", slots=2)})
    assert policy_mod.select(profiles, Need(), cfg, busy={"a": 1}) is not None


def test_internet_requirement():
    profiles = [_profile("a", internet=False)]
    assert policy_mod.select(profiles, Need(internet=True), _Cfg()) is None
    reason = policy_mod.reject_reason(profiles[0], Need(internet=True), _Cfg(), 0)
    assert reason is not None and "internet" in reason.lower()


def test_bridge_busy_host_is_rejected():
    profiles = [_profile("a", bridge_busy=True)]
    assert policy_mod.select(profiles, Need(), _Cfg()) is None
    reason = policy_mod.reject_reason(profiles[0], Need(), _Cfg(), 0)
    assert reason is not None and "bridge" in reason.lower()


# --------------------------------------------------------------------------
# chấm điểm
# --------------------------------------------------------------------------
def test_choice_has_host_score_reasons_rejected():
    profiles = [_profile("a"), _profile("b", reachable=False, error="timeout")]
    choice = policy_mod.select(profiles, Need(), _Cfg())
    assert isinstance(choice, Choice)
    assert choice.host == "a"
    assert isinstance(choice.score, float)
    assert isinstance(choice.reasons, list) and choice.reasons
    assert all(isinstance(r, str) and r for r in choice.reasons)
    assert "b" in choice.rejected


def test_prefer_hosts_gives_bonus_and_wins():
    profiles = [
        _profile("a", mem_available_mb=8000, cores=8),
        _profile("b", mem_available_mb=8000, cores=8),
    ]
    choice = policy_mod.select(profiles, Need(), _Cfg(), prefer_hosts=["b"])
    assert choice is not None
    assert choice.host == "b"
    assert any("b" in r for r in choice.reasons)


def test_prefer_hosts_only_first_present_alias_gets_bonus():
    profiles = [_profile("a"), _profile("b")]
    choice = policy_mod.select(profiles, Need(), _Cfg(), prefer_hosts=["missing", "a"])
    assert choice is not None
    assert choice.host == "a"


def test_score_durable_adds_15():
    p_durable = _profile("a", durable=True)
    p_soft = _profile("b", durable=False)
    s_durable, r_durable = policy_mod.score(p_durable, Need(), _Cfg(), 0)
    s_soft, _ = policy_mod.score(p_soft, Need(), _Cfg(), 0)
    assert s_durable - s_soft == pytest.approx(15.0)
    assert any("15" in r for r in r_durable)


def test_score_weight_multiplies():
    p = _profile("a")
    cfg = _Cfg({"a": _HostCfg("a", weight=2.0)})
    s1, _ = policy_mod.score(p, Need(), _Cfg(), 0)
    s2, _ = policy_mod.score(p, Need(), cfg, 0)
    assert s2 == pytest.approx(s1 * 2.0)


def test_score_busy_penalty_is_30_per_job():
    p = _profile("a")
    s0, _ = policy_mod.score(p, Need(), _Cfg(), 0)
    s2, reasons = policy_mod.score(p, Need(), _Cfg(), 2)
    assert s0 - s2 == pytest.approx(60.0)
    assert any("30" in r or "job" in r for r in reasons)


def test_score_health_prefers_idle_host():
    idle, _ = policy_mod.score(_profile("a", load1=0.0, cores=4), Need(), _Cfg(), 0)
    busy, _ = policy_mod.score(_profile("a", load1=4.0, cores=4), Need(), _Cfg(), 0)
    assert idle > busy


def test_score_label_bonus():
    p = _profile("a")
    cfg = _Cfg({"a": _HostCfg("a", labels=["ffmpeg", "shared"])})
    s_none, _ = policy_mod.score(p, Need(), _Cfg(), 0)
    s_match, reasons = policy_mod.score(p, Need(labels=["ffmpeg"]), cfg, 0)
    assert s_match - s_none == pytest.approx(25.0)
    assert any("ffmpeg" in r for r in reasons)


def test_score_warm_project_bonus():
    p = _profile("a", projects=["foo"])
    s_none, _ = policy_mod.score(p, Need(), _Cfg(), 0)
    s_warm, reasons = policy_mod.score(p, Need(project="foo"), _Cfg(), 0)
    assert s_warm - s_none == pytest.approx(40.0)
    assert any("foo" in r for r in reasons)


def test_score_warm_toolchain_bonus():
    p_cold = _profile("a", toolchains=[])
    p_warm = _profile("a", toolchains=["ffmpeg"])
    s_cold, _ = policy_mod.score(p_cold, Need(), _Cfg(), 0)
    s_warm, reasons = policy_mod.score(p_warm, Need(), _Cfg(), 0)
    assert s_warm - s_cold == pytest.approx(10.0)
    assert any("toolchain" in r.lower() for r in reasons)


def test_select_healthier_host_wins():
    profiles = [
        _profile("a", load1=3.5, cores=4, mem_available_mb=700),
        _profile("b", load1=0.1, cores=4, mem_available_mb=3000),
    ]
    choice = policy_mod.select(profiles, Need(), _Cfg())
    assert choice is not None
    assert choice.host == "b"


def test_select_returns_none_and_collects_all_reasons():
    profiles = [
        _profile("a", reachable=False, error="timeout"),
        _profile("b", tools={"git": "/usr/bin/git"}),
        _profile("c", mem_available_mb=10),
    ]
    choice = policy_mod.select(profiles, Need(tools=["ffmpeg"]), _Cfg())
    assert choice is None


def test_rejected_mapping_is_returned_for_all_filtered_hosts():
    profiles = [
        _profile("ok"),
        _profile("bad-tool", tools={}),
        _profile("bad-mem", mem_available_mb=1),
    ]
    choice = policy_mod.select(profiles, Need(tools=["ffmpeg"]), _Cfg())
    assert choice is not None
    assert "bad-tool" in choice.rejected
    assert "bad-mem" in choice.rejected
    assert "ok" not in choice.rejected


def test_reject_all_keeps_reasons_when_select_returns_none():
    """Khi không host nào dùng được, lý do loại phải còn lại được (S5/S13)."""
    profiles = [
        _profile("muse-pmb333", mem_available_mb=842),
        _profile("muse-tampt", mem_available_mb=450),
    ]
    assert policy_mod.select(profiles, Need(mem_mb=6144), _Cfg()) is None
    rejected = policy_mod.reject_all(profiles, Need(mem_mb=6144), _Cfg())
    assert set(rejected) == {"muse-pmb333", "muse-tampt"}
    assert "842" in rejected["muse-pmb333"]
    assert "6144" in rejected["muse-pmb333"]


def test_reject_all_omits_usable_hosts():
    profiles = [_profile("ok"), _profile("bad", mem_available_mb=10)]
    rejected = policy_mod.reject_all(profiles, Need(), _Cfg())
    assert "ok" not in rejected
    assert "bad" in rejected


def test_select_rejects_alias_of_same_physical_host():
    """2 alias cùng máy vật lý -> chỉ 1 alias được dùng (API-CONTRACT §15b.5)."""
    profiles = [
        _profile("muse-a", hostname="htch", boot_id="boot-1", instance_token="tok-1"),
        _profile("muse-b", hostname="htch", boot_id="boot-1", instance_token="tok-1"),
    ]
    choice = policy_mod.select(profiles, Need(), _Cfg())
    assert choice is not None
    assert choice.host in ("muse-a", "muse-b")
    other = "muse-b" if choice.host == "muse-a" else "muse-a"
    assert other in choice.rejected
    assert "cùng máy vật lý" in choice.rejected[other]


def test_select_keeps_aliases_of_different_machines():
    profiles = [
        _profile("muse-a", hostname="htch-1", boot_id="boot-1", instance_token="tok-1"),
        _profile("muse-b", hostname="htch-2", boot_id="boot-2", instance_token="tok-2"),
    ]
    choice = policy_mod.select(profiles, Need(), _Cfg())
    assert choice is not None
    assert choice.rejected == {}


def test_select_does_not_dedupe_without_identity_data():
    """Không có boot_id/token thì không được đoán là cùng máy."""
    profiles = [
        _profile("muse-a", hostname=None, boot_id=None, instance_token=None),
        _profile("muse-b", hostname=None, boot_id=None, instance_token=None),
    ]
    choice = policy_mod.select(profiles, Need(), _Cfg())
    assert choice is not None
    assert choice.rejected == {}


# --------------------------------------------------------------------------
# tích hợp với config thật của phần A
# --------------------------------------------------------------------------
def test_select_with_real_config_from_part_a(tmp_path):
    """Chạy được với Config thật (INI) của phần A."""
    config_mod = pytest.importorskip("roffkit.config")
    ini = tmp_path / "config.ini"
    ini.write_text(
        "[host.a]\nlabels = ffmpeg, shared\nweight = 2\nslots = 1\n"
        "[host.b]\nslots = 1\n",
        encoding="utf-8",
    )
    cfg = config_mod.Config.load(str(ini))
    profiles = [_profile("a"), _profile("b")]

    choice = policy_mod.select(profiles, Need(labels=["ffmpeg"]), cfg)
    assert choice is not None
    assert choice.host == "a"
    # host 'a' đã đầy slot -> phải bị loại, 'b' còn trống
    choice2 = policy_mod.select(profiles, Need(), cfg, busy={"a": 1})
    assert choice2 is not None
    assert choice2.host == "b"
    assert "a" in choice2.rejected


def test_score_uses_real_host_config_weight(tmp_path):
    config_mod = pytest.importorskip("roffkit.config")
    ini = tmp_path / "config.ini"
    ini.write_text("[host.a]\nweight = 3\n", encoding="utf-8")
    cfg = config_mod.Config.load(str(ini))
    base, _ = policy_mod.score(_profile("a"), Need(), _Cfg(), 0)
    weighted, _ = policy_mod.score(_profile("a"), Need(), cfg, 0)
    assert weighted == pytest.approx(base * 3.0)
