"""Unit test W5a — ``roff doctor`` (chẩn đoán, **chỉ báo**).

Ràng buộc chính:

* mỗi check có ``name``/``ok``/``detail``; ``problems`` = số check ``ok=false``;
  ``ok`` = (problems == 0); exit code = 1 khi có vấn đề, 0 khi sạch;
* check lỗi **không** làm hỏng lệnh (bọc try/except);
* mặc định **không** probe mạng (đọc cache) ⇒ chạy nhanh;
* ``doctor`` **không ghi gì** — kể cả không chuẩn hoá quyền file state.
"""

from __future__ import annotations

import os
import sys
import time

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(_HERE))
_SKILL = os.path.join(_REPO, "skill")
if _SKILL not in sys.path:
    sys.path.insert(0, _SKILL)

from conftest import parse_json, run_cli  # noqa: E402
from roffkit import doctor as doctor_mod  # noqa: E402
from roffkit import probe as probe_mod  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Env cô lập: state dir + config rỗng + ssh config rỗng (không mạng)."""
    state = tmp_path / "state"
    state.mkdir()
    ssh_cfg = tmp_path / "ssh_config"
    ssh_cfg.write_text("")
    monkeypatch.setenv("ROFF_STATE_DIR", str(state))
    monkeypatch.setenv("ROFF_CONFIG", str(tmp_path / "config.ini"))
    monkeypatch.setenv("ROFF_SSH_CONFIG", str(ssh_cfg))
    monkeypatch.setenv("CLAUDE_SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setenv("ROFF_BIN_DIR", str(tmp_path / "bin"))
    return {"state": state, "root": tmp_path}


# --------------------------------------------------------------------------
# Hình dạng payload
# --------------------------------------------------------------------------
def test_checks_have_name_ok_detail(env):
    payload = doctor_mod.run_checks(state_dir=str(env["state"]))
    assert isinstance(payload["checks"], list) and payload["checks"]
    for check in payload["checks"]:
        assert "name" in check and "ok" in check and "detail" in check, check
        assert isinstance(check["ok"], bool), check
        assert isinstance(check["detail"], str), check


def test_required_check_names_present(env):
    payload = doctor_mod.run_checks(state_dir=str(env["state"]))
    names = {check["name"] for check in payload["checks"]}
    assert {"config", "fleet", "state_perms"} <= names, names


def test_problems_count_matches_checks(env):
    payload = doctor_mod.run_checks(state_dir=str(env["state"]))
    problems = sum(1 for check in payload["checks"] if not check["ok"])
    assert payload["problems"] == problems
    assert payload["ok"] is (problems == 0)
    assert payload["exit_code"] == (1 if problems else 0)


def test_exit_code_is_zero_when_clean(env, monkeypatch):
    """Ép mọi check thành ``ok`` ⇒ exit 0."""
    for name in doctor_mod.CHECK_NAMES:
        monkeypatch.setattr(
            doctor_mod, f"check_{name}",
            lambda *a, **k: {"name": name, "ok": True, "detail": "ok"},
        )
    payload = doctor_mod.run_checks(state_dir=str(env["state"]))
    assert payload["problems"] == 0
    assert payload["ok"] is True
    assert payload["exit_code"] == 0


def test_exit_code_is_one_when_problem(env, monkeypatch):
    """Có 1 check lỗi ⇒ exit 1, ``ok=false``."""
    monkeypatch.setattr(
        doctor_mod, "check_disk_local",
        lambda: {"name": "disk_local", "ok": False, "detail": "hết chỗ"},
    )
    payload = doctor_mod.run_checks(state_dir=str(env["state"]))
    assert payload["problems"] >= 1
    assert payload["ok"] is False
    assert payload["exit_code"] == 1


def test_failing_check_does_not_break_command(env, monkeypatch):
    """Check ném exception ⇒ vẫn ra payload đầy đủ, check đó ``ok=false``."""

    def boom():
        raise RuntimeError("hỏng")

    monkeypatch.setattr(doctor_mod, "check_disk_local", boom)
    payload = doctor_mod.run_checks(state_dir=str(env["state"]))
    names = [check["name"] for check in payload["checks"]]
    assert len(names) == len(set(names)), "check lỗi phải được ghi lại"
    disk = [c for c in payload["checks"] if c["name"] == "disk_local"]
    assert disk and disk[0]["ok"] is False
    assert "hỏng" in disk[0]["detail"]


# --------------------------------------------------------------------------
# Mặc định KHÔNG probe mạng
# --------------------------------------------------------------------------
def test_default_does_not_probe_network(env, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("doctor mặc định không được probe mạng")

    monkeypatch.setattr(probe_mod, "probe_many", boom)
    payload = doctor_mod.run_checks(state_dir=str(env["state"]))
    assert payload["checks"]


def test_probe_flag_probes(env, monkeypatch):
    calls = []

    class _Profile:
        def __init__(self, alias, reachable):
            self.alias = alias
            self.reachable = reachable

    def fake_probe_many(aliases, timeout=30):
        calls.append(list(aliases))
        return [_Profile("muse-a", True)]

    monkeypatch.setattr(probe_mod, "probe_many", fake_probe_many)
    doctor_mod.run_checks(probe_now=True, state_dir=str(env["state"]))
    assert calls, "--probe phải probe thật"


def test_fleet_check_reads_cache(env, monkeypatch):
    """Không có cache ⇒ host "không biết" (không phải "chết")."""
    ssh_cfg = env["root"] / "ssh_config"
    ssh_cfg.write_text("Host muse-a\n    HostName 127.0.0.1\n    Port 22\n")
    payload = doctor_mod.run_checks(state_dir=str(env["state"]))
    fleet = [c for c in payload["checks"] if c["name"] == "fleet"][0]
    assert fleet["ok"] is True, fleet
    assert fleet.get("unknown_hosts") == ["muse-a"], fleet
    assert fleet.get("dead_hosts") == [], fleet


# --------------------------------------------------------------------------
# CLI: JSON + exit code + KHÔNG ghi gì
# --------------------------------------------------------------------------
def test_cli_doctor_json_and_exit_code(env):
    rc, out, _err = run_cli(["doctor"])
    assert rc in (0, 1), out
    payload = parse_json(out)
    assert isinstance(payload["checks"], list) and payload["checks"]
    assert isinstance(payload["problems"], int)
    problems = sum(1 for check in payload["checks"] if not check["ok"])
    assert payload["problems"] == problems
    assert payload["ok"] is (problems == 0)
    assert rc == (1 if problems else 0)
    names = {check["name"] for check in payload["checks"]}
    assert {"config", "fleet", "state_perms"} <= names


def test_cli_doctor_writes_nothing(env):
    state = env["state"]
    run_cli(["doctor"])
    before = sorted(str(p.relative_to(state)) for p in state.rglob("*"))
    run_cli(["doctor"])
    after = sorted(str(p.relative_to(state)) for p in state.rglob("*"))
    assert before == after, f"doctor đã ghi file: {set(after) - set(before)}"


def test_cli_doctor_does_not_normalize_permissions(env):
    """``doctor`` chỉ **báo** ⇒ không được đổi quyền file state."""
    state = env["state"]
    target = state / "keeps-perm.txt"
    target.write_text("x")
    os.chmod(target, 0o644)
    run_cli(["doctor"])
    mode = os.stat(target).st_mode & 0o777
    assert mode == 0o644, f"doctor đã đổi quyền: {oct(mode)}"


def test_cli_doctor_is_fast(env):
    t0 = time.time()
    run_cli(["doctor"])
    dt = time.time() - t0
    assert dt < 5.0, f"doctor mất {dt:.1f}s — có vẻ đã probe mạng"


def test_state_perms_check_reports_bad_mode(env):
    """File state sai quyền ⇒ check ``state_perms`` phải báo lỗi."""
    state = env["state"]
    bad = state / "bad.txt"
    bad.write_text("x")
    os.chmod(bad, 0o644)
    payload = doctor_mod.run_checks(state_dir=str(state))
    check = [c for c in payload["checks"] if c["name"] == "state_perms"][0]
    assert check["ok"] is False, check
    assert str(bad) in (check.get("bad_files") or []), check
