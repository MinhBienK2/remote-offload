"""W8b — ``policy.select()``: lý do "tool suy ra từ lệnh" chỉ kê khi **có ích**.

Đo thật 23/11/2026: ``roff do --cmd 'sleep 120; cp "$ROFF_IN/x" "$ROFF_OUT/x"'``
cho ``host_reasons`` chứa dòng::

    "không có tool suy ra từ lệnh: sleep, cp (vẫn dùng được — ưu tiên là mềm)"

Dòng đó lặp ở **mọi** host mà không giúp ích ai ⇒ chỉ kê khi **có** host nào
thực sự có tool đó.

Không test nào ở đây cần mạng.
"""

from __future__ import annotations

import os
import subprocess
import sys

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
        tools={"git": "/usr/bin/git"},
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


def _tool_reasons(choice) -> list[str]:
    """Các dòng lý do nói về tool suy ra từ lệnh."""
    if choice is None:
        return []
    return [r for r in choice.reasons if "tool suy ra từ lệnh" in r]


# ==========================================================================
# không host nào có tool ⇒ KHÔNG kê lý do tool
# ==========================================================================
def test_no_host_has_tool_no_tool_reason_at_all():
    """Không host nào có tool suy ra ⇒ **không** có lý do tool trong reasons."""
    profiles = [
        _profile("a", tools={"git": "/usr/bin/git"}, mem_available_mb=8000),
        _profile("b", tools={"git": "/usr/bin/git"}, mem_available_mb=3000),
    ]
    choice = policy_mod.select(
        profiles, Need(), _Cfg(), inferred_needs=["pytest"]
    )
    assert choice is not None
    assert _tool_reasons(choice) == []


def test_unknown_tool_does_not_change_the_choice():
    """Tool không host nào có ⇒ hành vi chọn host **không đổi**."""
    profiles = [
        _profile("a", mem_available_mb=8000),
        _profile("b", mem_available_mb=3000),
    ]
    plain = policy_mod.select(profiles, Need(), _Cfg())
    with_needs = policy_mod.select(
        profiles, Need(), _Cfg(), inferred_needs=["pytest"]
    )
    assert plain is not None and with_needs is not None
    assert plain.host == with_needs.host
    assert plain.score == with_needs.score
    assert plain.rejected == with_needs.rejected


def test_empty_inferred_needs_keeps_old_behaviour():
    """Không truyền (hoặc rỗng) ⇒ kết quả **y hệt** trước W8."""
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
    assert plain.reasons == empty.reasons


def test_no_tool_reason_when_no_needs_at_all():
    """Không có tool suy ra nào ⇒ không có dòng nào về tool."""
    profiles = [_profile("a", tools={"pytest": "/usr/bin/pytest"})]
    choice = policy_mod.select(profiles, Need(), _Cfg())
    assert choice is not None
    assert _tool_reasons(choice) == []


# ==========================================================================
# có host nào có tool ⇒ CÓ lý do tool
# ==========================================================================
def test_host_with_tool_gets_reason():
    """Host có tool suy ra ⇒ có lý do "có tool suy ra từ lệnh: …"."""
    profiles = [
        _profile("a", tools={"pytest": "/usr/bin/pytest"}, mem_available_mb=8000),
    ]
    choice = policy_mod.select(
        profiles, Need(), _Cfg(), inferred_needs=["pytest"]
    )
    assert choice is not None
    reasons = _tool_reasons(choice)
    assert len(reasons) == 1
    assert "có tool suy ra từ lệnh: pytest" in reasons[0]
    assert "(ưu tiên)" in reasons[0]


def test_tool_presence_still_decides_the_winner():
    """Host có tool suy ra vẫn **thắng** host không có (dù điểm thấp hơn)."""
    profiles = [
        _profile("no-tool", tools={"git": "/usr/bin/git"}, mem_available_mb=8000),
        _profile("has-tool", tools={"pytest": "/usr/bin/pytest"},
                 mem_available_mb=800),
    ]
    choice = policy_mod.select(
        profiles, Need(), _Cfg(), inferred_needs=["pytest"]
    )
    assert choice is not None
    assert choice.host == "has-tool"


def test_every_inferred_tool_is_reported_when_useful():
    """Khi lý do tool **có ích** ⇒ báo **mọi** tool suy ra (có + thiếu)."""
    profiles = [
        _profile("a", tools={"pytest": "/usr/bin/pytest"}, mem_available_mb=8000),
    ]
    choice = policy_mod.select(
        profiles, Need(), _Cfg(), inferred_needs=["pytest", "mypy"]
    )
    assert choice is not None
    joined = " ".join(_tool_reasons(choice))
    assert "pytest" in joined
    assert "mypy" in joined


def test_partial_match_reports_missing_tool():
    """Host có 1/2 tool ⇒ báo tool có **và** tool thiếu (2 dòng)."""
    profiles = [
        _profile("a", tools={"pytest": "/usr/bin/pytest"}, mem_available_mb=8000),
    ]
    choice = policy_mod.select(
        profiles, Need(), _Cfg(), inferred_needs=["pytest", "mypy"]
    )
    assert choice is not None
    reasons = _tool_reasons(choice)
    assert any("có tool suy ra từ lệnh: pytest" in r for r in reasons)
    assert any(
        "không có tool suy ra từ lệnh: mypy" in r for r in reasons
    )


def test_alias_family_counts_as_having_the_tool():
    """Tool chuẩn khớp host chỉ có **tên khác** trong cùng họ (vd ``convert``)."""
    profiles = [
        _profile("no-magick", tools={"git": "/usr/bin/git"}, mem_available_mb=8000),
        _profile("has-magick", tools={"convert": "/usr/bin/convert"},
                 mem_available_mb=800),
    ]
    choice = policy_mod.select(
        profiles, Need(), _Cfg(), inferred_needs=["imagemagick"]
    )
    assert choice is not None
    assert choice.host == "has-magick"
    assert _tool_reasons(choice)


def test_tool_reason_does_not_reject_the_host():
    """Lý do tool là **mềm**: host thiếu tool vẫn là ứng viên hợp lệ."""
    profiles = [_profile("a", tools={"git": "/usr/bin/git"})]
    choice = policy_mod.select(
        profiles, Need(), _Cfg(), inferred_needs=["pytest"]
    )
    assert choice is not None
    assert choice.rejected == {}


def test_explicit_need_stays_a_hard_criterion():
    """``--need`` (tiêu chí cứng) **không** bị đổi bởi W8b."""
    profiles = [_profile("a", tools={"git": "/usr/bin/git"})]
    assert policy_mod.select(profiles, Need(tools=["pytest"]), _Cfg()) is None
    assert (
        policy_mod.select(
            profiles, Need(tools=["pytest"]), _Cfg(), inferred_needs=["pytest"]
        )
        is None
    )


def test_select_never_shells_out_for_inferred_needs(monkeypatch):
    """Suy ra/so khớp tool **không** được gọi ssh thêm lần nào."""

    def _boom(*args, **kwargs):  # pragma: no cover - chỉ chạy khi lỗi
        raise AssertionError("policy.select không được gọi subprocess/ssh")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)
    monkeypatch.setattr(subprocess, "check_output", _boom)
    profiles = [_profile("a", tools={"pytest": "/usr/bin/pytest"})]
    choice = policy_mod.select(
        profiles, Need(), _Cfg(), inferred_needs=["pytest"]
    )
    assert choice is not None
    assert choice.host == "a"
