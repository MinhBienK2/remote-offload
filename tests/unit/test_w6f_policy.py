"""W6b + W6f — ``roffkit.policy.select()``: ưu tiên host có tool suy ra (W6b)
và **loại** alias trùng máy vật lý (W6f, bug B12).

Không test nào ở đây cần mạng.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

_SKILL_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "skill"
)
if _SKILL_DIR not in sys.path:
    sys.path.insert(0, _SKILL_DIR)

from roffkit import policy as policy_mod  # noqa: E402
from roffkit.policy import Need  # noqa: E402
from roffkit.probe import Profile  # noqa: E402


class _HostCfg:
    """HostConfig tối thiểu (duck-typing)."""

    def __init__(self, name: str, labels=None, weight: float = 1.0, slots: int = 1):
        self.name = name
        self.labels = list(labels or [])
        self.weight = weight
        self.slots = slots
        self.mem_budget_mb = 0
        self.mem_reserve_mb = 256


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
        instance_token="tok-" + alias,
        home="/root",
        layout_root="/home/hatch/.roff",
        durable=True,
        tools={
            "ffmpeg": "/usr/bin/ffmpeg",
            "git": "/usr/bin/git",
            "node": "/usr/bin/node",
            "python3": "/usr/bin/python3",
        },
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


# ==========================================================================
# W6f — loại alias trùng máy vật lý
# ==========================================================================
def test_same_physical_host_alias_is_really_removed():
    """2 profile cùng ``boot_id`` + ``instance_token`` ⇒ **chỉ 1** còn lại.

    Bug B12 (đo thật 23/11/2026): trước đây alias trùng chỉ được **ghi chú**
    vào ``rejected`` *sau khi* đã chọn winner ⇒ 2 job chạy song song trên
    cùng 1 máy vật lý.
    """
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
    # Host bị loại **không** còn là ứng viên: chỉ 1 alias được giữ.
    kept = [a for a in ("muse-a", "muse-b") if a not in choice.rejected]
    assert kept == [choice.host]


def test_dedupe_happens_before_scoring_for_a_losing_group():
    """Nhóm alias trùng máy bị loại **dù nhóm đó không chứa winner**.

    Trước đây vòng ghi chú chỉ so với winner ⇒ nhóm thua điểm vẫn giữ cả 2
    alias ⇒ 2 job có thể chạy song song trên 1 máy.
    """
    profiles = [
        _profile("a", hostname="htch-1", boot_id="boot-1", instance_token="tok-1"),
        _profile("b", hostname="htch-1", boot_id="boot-1", instance_token="tok-1"),
        _profile("c", hostname="htch-2", boot_id="boot-2", instance_token="tok-2",
                 mem_available_mb=8000),
    ]
    choice = policy_mod.select(profiles, Need(), _Cfg())
    assert choice is not None
    assert choice.host == "c"
    # Đúng 1 trong {a, b} bị loại (không phải 0, không phải 2).
    assert len({"a", "b"} & set(choice.rejected)) == 1
    assert "c" not in choice.rejected


def test_group_of_three_keeps_one_alias():
    """3 alias cùng 1 máy ⇒ giữ đúng 1, loại 2 (luôn giữ ≥ 1 alias/nhóm)."""
    profiles = [
        _profile("a", hostname="htch", boot_id="boot-1", instance_token="tok-1"),
        _profile("b", hostname="htch", boot_id="boot-1", instance_token="tok-1"),
        _profile("c", hostname="htch", boot_id="boot-1", instance_token="tok-1"),
    ]
    choice = policy_mod.select(profiles, Need(), _Cfg())
    assert choice is not None
    assert len(choice.rejected) == 2
    assert choice.host not in choice.rejected


def test_preferred_alias_of_a_duplicated_machine_wins():
    """Hồi quy: ``prefer_hosts`` trỏ alias **thứ 2** của 1 nhóm trùng máy.

    Trước W6f: winner là alias-b (điểm cao hơn). Sau W6f (giữ alias **đầu
    tiên** trong nhóm): alias-b bị loại ⇒ mất cả +50 của prefer ⇒ **sai**.
    Fix: alias user prefer được giữ lại trong nhóm.
    """
    profiles = [
        _profile("alias-a", hostname="htch", boot_id="boot-1", instance_token="tok-1",
                 mem_available_mb=4096),
        _profile("alias-b", hostname="htch", boot_id="boot-1", instance_token="tok-1",
                 mem_available_mb=8000),
    ]
    choice = policy_mod.select(profiles, Need(), _Cfg(), prefer_hosts=["alias-b"])
    assert choice is not None
    assert choice.host == "alias-b"
    assert "alias-a" in choice.rejected
    assert "cùng máy vật lý" in choice.rejected["alias-a"]
    assert any("alias-b" in r for r in choice.reasons)


def test_duplicated_machine_keeps_the_highest_scoring_alias():
    """Không có prefer ⇒ giữ alias **điểm cao hơn** trong nhóm."""
    profiles = [
        _profile("alias-a", hostname="htch", boot_id="boot-1", instance_token="tok-1",
                 mem_available_mb=4096),
        _profile("alias-b", hostname="htch", boot_id="boot-1", instance_token="tok-1",
                 mem_available_mb=8000),
    ]
    choice = policy_mod.select(profiles, Need(), _Cfg())
    assert choice is not None
    assert choice.host == "alias-b"
    assert "alias-a" in choice.rejected


def test_preferred_alias_of_a_duplicated_machine_is_not_dropped_white():
    """Alias user prefer bị loại vì trùng máy ⇒ được **thay bằng đồng đội**.

    Nghĩa là: nhóm vẫn còn 1 alias ứng viên (không mất host khỏi fleet) và
    alias prefer không bị loại trắng.
    """
    profiles = [
        _profile("alias-a", hostname="htch", boot_id="boot-1", instance_token="tok-1",
                 mem_available_mb=4096),
        _profile("alias-b", hostname="htch", boot_id="boot-1", instance_token="tok-1",
                 mem_available_mb=8000),
    ]
    choice = policy_mod.select(profiles, Need(), _Cfg(), prefer_hosts=["alias-b"])
    assert choice is not None
    # Đúng 1 alias bị loại ⇒ máy vật lý vẫn còn 1 ứng viên.
    assert len(choice.rejected) == 1
    assert "alias-b" not in choice.rejected


def test_different_boot_id_keeps_both_candidates():
    """2 profile khác ``boot_id`` ⇒ cả 2 vẫn là ứng viên (không loại nhầm)."""
    profiles = [
        _profile("muse-a", hostname="htch-1", boot_id="boot-1", instance_token="tok-1"),
        _profile("muse-b", hostname="htch-2", boot_id="boot-2", instance_token="tok-2"),
    ]
    choice = policy_mod.select(profiles, Need(), _Cfg())
    assert choice is not None
    assert choice.rejected == {}


def test_missing_identity_data_does_not_dedupe():
    """Không đọc được ``boot_id``/``instance_token`` ⇒ không loại nhầm."""
    profiles = [
        _profile("muse-a", hostname=None, boot_id=None, instance_token=None),
        _profile("muse-b", hostname=None, boot_id=None, instance_token=None),
    ]
    choice = policy_mod.select(profiles, Need(), _Cfg())
    assert choice is not None
    assert choice.rejected == {}


def test_same_physical_host_returning_none_does_not_dedupe(monkeypatch):
    """``same_physical_host`` trả ``None`` ⇒ coi là **khác máy**, không loại."""
    monkeypatch.setattr(policy_mod, "same_physical_host", lambda a, b: None)
    profiles = [
        _profile("muse-a", hostname="htch", boot_id="boot-1", instance_token="tok-1"),
        _profile("muse-b", hostname="htch", boot_id="boot-1", instance_token="tok-1"),
    ]
    choice = policy_mod.select(profiles, Need(), _Cfg())
    assert choice is not None
    assert choice.rejected == {}


def test_same_physical_host_raising_does_not_dedupe(monkeypatch):
    """Hàm thay thế hỏng (ném lỗi) ⇒ không được loại nhầm."""

    def _boom(a, b):
        raise RuntimeError("probe hỏng")

    monkeypatch.setattr(policy_mod, "same_physical_host", _boom)
    profiles = [
        _profile("muse-a", hostname="htch", boot_id="boot-1", instance_token="tok-1"),
        _profile("muse-b", hostname="htch", boot_id="boot-1", instance_token="tok-1"),
    ]
    choice = policy_mod.select(profiles, Need(), _Cfg())
    assert choice is not None
    assert choice.rejected == {}


def test_hard_rejected_alias_still_reports_its_own_reason():
    """Alias bị loại cứng (RAM) giữ lý do của nó; alias trùng máy vẫn bị loại."""
    profiles = [
        _profile("a", hostname="htch", boot_id="boot-1", instance_token="tok-1",
                 mem_available_mb=8000),
        _profile("b", hostname="htch", boot_id="boot-1", instance_token="tok-1",
                 mem_available_mb=10),
    ]
    choice = policy_mod.select(profiles, Need(), _Cfg())
    assert choice is not None
    assert choice.host == "a"
    assert "b" in choice.rejected
    assert "RAM" in choice.rejected["b"] or "ram" in choice.rejected["b"].lower()


# ==========================================================================
# W6b — ưu tiên host có tool suy ra (tiêu chí MỀM)
# ==========================================================================
def test_host_with_inferred_tool_is_preferred():
    """Host **có** tool suy ra được ưu tiên (đứng trước trong thứ tự ưu tiên)."""
    profiles = [
        _profile("no-tool", tools={"git": "/usr/bin/git"}, mem_available_mb=8000),
        _profile("has-tool", tools={"ffmpeg": "/usr/bin/ffmpeg"},
                 mem_available_mb=800),
    ]
    choice = policy_mod.select(
        profiles, Need(), _Cfg(), inferred_needs=["ffmpeg"]
    )
    assert choice is not None
    assert choice.host == "has-tool"
    assert any("ffmpeg" in r for r in choice.reasons)


def test_host_without_inferred_tool_is_not_rejected():
    """Host thiếu tool suy ra ⇒ **vẫn là ứng viên hợp lệ** (không bị loại)."""
    profiles = [_profile("no-tool", tools={"git": "/usr/bin/git"})]
    choice = policy_mod.select(
        profiles, Need(), _Cfg(), inferred_needs=["ffmpeg"]
    )
    assert choice is not None
    assert choice.host == "no-tool"
    assert choice.rejected == {}


def test_tool_unknown_to_every_host_changes_nothing():
    """Tool **không host nào có** ⇒ "không rõ" ⇒ hành vi không đổi."""
    profiles = [
        _profile("a", mem_available_mb=8000),
        _profile("b", mem_available_mb=3000),
    ]
    plain = policy_mod.select(profiles, Need(), _Cfg())
    with_needs = policy_mod.select(
        profiles, Need(), _Cfg(), inferred_needs=["zzz-cong-cu-khong-ton-tai"]
    )
    assert plain is not None and with_needs is not None
    assert plain.host == with_needs.host
    assert with_needs.rejected == {}


def test_partial_match_beats_no_match():
    """Host có **một phần** tool suy ra ⇒ đứng trước host không có tool nào."""
    profiles = [
        _profile("none", tools={"git": "/usr/bin/git"}, mem_available_mb=8000),
        _profile("some", tools={"ffmpeg": "/usr/bin/ffmpeg"},
                 mem_available_mb=800),
    ]
    choice = policy_mod.select(
        profiles, Need(), _Cfg(), inferred_needs=["ffmpeg", "maven"]
    )
    assert choice is not None
    assert choice.host == "some"


def test_inferred_need_matches_alias_family():
    """Tool chuẩn ``maven`` khớp host chỉ có ``mvn`` (probe đo ``mvn``)."""
    profiles = [
        _profile("no-mvn", tools={"git": "/usr/bin/git"}, mem_available_mb=8000),
        _profile("has-mvn", tools={"mvn": "/usr/bin/mvn"}, mem_available_mb=800),
    ]
    choice = policy_mod.select(profiles, Need(), _Cfg(), inferred_needs=["maven"])
    assert choice is not None
    assert choice.host == "has-mvn"


def test_empty_inferred_needs_keeps_old_behaviour():
    """Không truyền (hoặc rỗng) ⇒ hành vi **không đổi**."""
    profiles = [
        _profile("a", mem_available_mb=8000),
        _profile("b", mem_available_mb=3000),
    ]
    plain = policy_mod.select(profiles, Need(), _Cfg())
    empty = policy_mod.select(profiles, Need(), _Cfg(), inferred_needs=[])
    assert plain is not None and empty is not None
    assert plain.host == empty.host
    assert plain.score == empty.score
    assert plain.rejected == empty.rejected


def test_explicit_need_stays_a_hard_criterion():
    """Tool ghi rõ bằng ``--need`` vẫn là tiêu chí **cứng** như trước."""
    profiles = [_profile("a", tools={"git": "/usr/bin/git"})]
    assert policy_mod.select(profiles, Need(tools=["ffmpeg"]), _Cfg()) is None
    assert (
        policy_mod.select(
            profiles, Need(tools=["ffmpeg"]), _Cfg(), inferred_needs=["ffmpeg"]
        )
        is None
    )


def test_select_never_shells_out_for_inferred_needs(monkeypatch):
    """W6c: suy ra/so khớp tool **không** được gọi ssh thêm lần nào."""

    def _boom(*args, **kwargs):  # pragma: no cover - chỉ chạy khi lỗi
        raise AssertionError("policy.select không được gọi subprocess/ssh")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)
    monkeypatch.setattr(subprocess, "check_output", _boom)
    profiles = [_profile("a", tools={"ffmpeg": "/usr/bin/ffmpeg"})]
    choice = policy_mod.select(
        profiles, Need(), _Cfg(), inferred_needs=["ffmpeg"]
    )
    assert choice is not None
    assert choice.host == "a"


def test_inferred_needs_accepts_any_sequence():
    """Tham số nhận list/tuple/any iterable ⇒ không lỗi."""
    profiles = [_profile("a", tools={"ffmpeg": "/usr/bin/ffmpeg"})]
    for value in (("ffmpeg",), ["ffmpeg"], ["ffmpeg", "ffmpeg"], [" ffmpeg "]):
        choice = policy_mod.select(
            profiles, Need(), _Cfg(), inferred_needs=value
        )
        assert choice is not None
        assert choice.host == "a"


@pytest.mark.parametrize("value", [None, [], ()])
def test_inferred_needs_none_like(value):
    profiles = [_profile("a")]
    choice = policy_mod.select(profiles, Need(), _Cfg(), inferred_needs=value)
    assert choice is not None
    assert choice.host == "a"
