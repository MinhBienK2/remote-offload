"""W6b — nối ``roffkit.infer`` vào **đường chạy thật** của ``roff do``.

Suy ra tool là **phỏng đoán** (``sh build.sh`` không ai biết nó cần gì) ⇒
tiêu chí **MỜM**:

* host **có** tool suy ra ⇒ được **ưu tiên**;
* host **thiếu** tool suy ra ⇒ **vẫn** là ứng viên hợp lệ (không bị loại);
* tool suy ra **không** được cài — chỉ ``--need`` mới được cài;
* ``--need`` vẫn là tiêu chí **CỨNG** (thiếu tool ⇒ exit 10).

Kiểm chứng (không cần mạng):

* end-to-end: ssh giả (fixture ``fake_ssh``) ⇒ JSON của ``roff do``;
* in-process: :func:`roffkit.cli._cmd_do` với seam giả (bắt được đúng tham số
  truyền xuống :func:`roffkit.policy.select`).

Hợp đồng JSON: khoá cấp cao nhất ``inferred_needs`` — ``list[str]`` (tên tool
**đã chuẩn hoá**), không trùng, thứ tự ổn định, ``[]`` khi không suy ra được.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(_HERE))
_SKILL = os.path.join(_REPO, "skill")
if _SKILL not in sys.path:
    sys.path.insert(0, _SKILL)

from conftest import parse_json, run_cli  # noqa: E402

from roffkit import cli as cli_mod  # noqa: E402
from roffkit import infer as infer_mod  # noqa: E402
from roffkit import job as job_mod  # noqa: E402
from roffkit import policy as policy_mod  # noqa: E402
from roffkit import probe as probe_mod  # noqa: E402
from roffkit import sshx  # noqa: E402

#: Tool **không host nào có** và **không** có script setup ⇒ dùng để kiểm
#: chứng tiêu chí mềm (không loại host) và ``--need`` vẫn là tiêu chí cứng.
MISSING_TOOL = "zzz-roff-khong-ton-tai-xyz"


# --------------------------------------------------------------------------
# ssh giả (end-to-end)
# --------------------------------------------------------------------------
def _probe_out(hostname="htch-a", boot="boot-a", token="tok-a", cores=4) -> str:
    """Output giả của lệnh probe (giống seam của test W3)."""
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


def _cli_env(tmp_path, hosts=("muse-a",)) -> dict:
    """Env cô lập cho CLI (state/config/ssh-config riêng — không đụng ledger thật)."""
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
    return env


def _do(env, *args, timeout=120):
    """Chạy ``roff do`` (end-to-end) và trả ``(rc, payload, stderr)``."""
    rc, out, err = run_cli(["do", *args], env=env, timeout=timeout)
    return rc, parse_json(out), err


# --------------------------------------------------------------------------
# 1-3 — JSON của `roff do` báo tool suy ra
# --------------------------------------------------------------------------
def test_do_reports_inferred_tool_for_ffmpeg(fake_ssh, tmp_path):
    """``ffmpeg -version`` ⇒ ``inferred_needs == ["ffmpeg"]``."""
    fake_ssh("muse-a", responses=[{"match": "nproc", "stdout": _probe_out()}])
    env = _cli_env(tmp_path)

    rc, payload, err = _do(env, "--cmd", "ffmpeg -version", "--name", "proj")

    assert rc == 0, f"{payload} {err}"
    assert payload["inferred_needs"] == ["ffmpeg"]
    # Không trùng, thứ tự ổn định.
    assert len(payload["inferred_needs"]) == len(set(payload["inferred_needs"]))


def test_do_reports_empty_inferred_needs_for_builtin(fake_ssh, tmp_path):
    """``echo hi`` ⇒ ``inferred_needs == []`` (``echo`` là builtin của shell)."""
    fake_ssh("muse-a", responses=[{"match": "nproc", "stdout": _probe_out()}])
    env = _cli_env(tmp_path)

    rc, payload, err = _do(env, "--cmd", "echo hi", "--name", "proj")

    assert rc == 0, f"{payload} {err}"
    assert payload["inferred_needs"] == []


def test_do_reports_canonical_tool_for_npm(fake_ssh, tmp_path):
    """``npm test`` ⇒ ``inferred_needs == ["node"]`` (tên đã **chuẩn hoá**)."""
    fake_ssh("muse-a", responses=[{"match": "nproc", "stdout": _probe_out()}])
    env = _cli_env(tmp_path)

    rc, payload, err = _do(env, "--cmd", "npm test", "--name", "proj")

    assert rc == 0, f"{payload} {err}"
    assert payload["inferred_needs"] == ["node"]


def test_do_reports_inferred_needs_for_every_job_of_a_batch(fake_ssh, tmp_path):
    """Lô ``--each`` ⇒ mọi job đều **dùng chung** 1 kết quả suy ra (tính 1 lần)."""
    fake_ssh("muse-a", responses=[{"match": "nproc", "stdout": _probe_out()}])
    env = _cli_env(tmp_path)
    items = tmp_path / "items"
    items.mkdir()
    for name in ("a.mp4", "b.mp4"):
        (items / name).write_text(name)

    rc, payload, err = _do(
        env,
        "--cmd",
        "ffmpeg -i $ROFF_ITEM $ROFF_OUT/out.mp4",
        "--each",
        str(items / "*.mp4"),
        "--name",
        "proj",
    )

    assert rc == 0, f"{payload} {err}"
    assert payload["inferred_needs"] == ["ffmpeg"]
    assert payload.get("count") == 2


# --------------------------------------------------------------------------
# 4 — suy ra KHÔNG gọi ssh (đường chạy nóng)
# --------------------------------------------------------------------------
def _boom(*args, **kwargs):
    raise AssertionError("suy ra tool không được gọi subprocess/ssh")


def test_infer_tools_never_shells_out(monkeypatch):
    """``infer_tools`` chỉ đọc văn bản lệnh ⇒ không subprocess nào được gọi."""
    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)
    monkeypatch.setattr(subprocess, "check_output", _boom)
    monkeypatch.setattr(subprocess, "check_call", _boom)

    assert infer_mod.infer_tools("ffmpeg -version") == ["ffmpeg"]
    assert infer_mod.infer_tools("npm test") == ["node"]
    assert infer_mod.infer_tools("echo hi") == []


# --------------------------------------------------------------------------
# seam in-process: bắt được đúng thứ được truyền xuống
# --------------------------------------------------------------------------
def _args(cmd: str, need=None, each=None, shards=1) -> argparse.Namespace:
    """``argparse.Namespace`` giống hệt ``roff do`` (thiếu option ⇒ mặc định)."""
    return argparse.Namespace(
        cmd=cmd,
        inputs=[],
        out=None,
        need=list(need or []),
        cpus=0,
        mem=None,
        disk=None,
        each=each,
        shards=shards,
        after=None,
        name="proj",
        sync_back=[],
        checkpoint=None,
        env=[],
        retry=1,
        side_effects=False,
        timeout=600,
        priority="normal",
        prefer_same_host=True,
        host=None,
    )


def _patch_seams(monkeypatch, tmp_path, ensure=None):
    """Bịt mọi đường ra ssh/subprocess của :func:`cli_mod._cmd_do`."""
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    config = tmp_path / "config.ini"
    config.write_text("[fleet]\ninclude = muse-*\n", encoding="utf-8")
    monkeypatch.setenv("ROFF_STATE_DIR", str(state))
    monkeypatch.setenv("ROFF_CONFIG", str(config))
    monkeypatch.setenv("ROFF_NO_WATCHER", "1")

    profile = probe_mod.Profile(
        alias="muse-a",
        reachable=True,
        cores=4,
        mem_available_mb=4096,
        disk_free_mb=100000,
        layout_root="/home/hatch/.roff",
        home="/root",
        boot_id="boot-1",
        instance_token="tok-1",
        internet=True,
        tools={"python3": "/usr/bin/python3"},
    )
    monkeypatch.setattr(cli_mod, "_fleet_aliases", lambda cfg: ["muse-a"])
    monkeypatch.setattr(probe_mod, "cached_profiles", lambda aliases, cfg: [profile])
    monkeypatch.setattr(
        sshx,
        "run_shell",
        lambda host, script, timeout=60: sshx.RunResult(rc=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(
        job_mod,
        "_run_ssh",
        lambda *a, **k: (
            0,
            "ROFF_PID=1\nROFF_BOOT_ID=boot-1\nROFF_INSTANCE_TOKEN=tok-1\n",
            "",
        ),
    )
    if ensure is not None:
        monkeypatch.setattr(cli_mod, "_ensure_tools", ensure)
    return profile


def test_do_runs_even_when_subprocess_is_forbidden(monkeypatch, tmp_path):
    """Suy ra **không** gọi subprocess ⇒ ``roff do`` vẫn chạy được (seam giả)."""
    _patch_seams(monkeypatch, tmp_path)
    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)
    monkeypatch.setattr(subprocess, "check_output", _boom)
    monkeypatch.setattr(subprocess, "check_call", _boom)

    payload, code = cli_mod._cmd_do(_args("ffmpeg -version"))

    assert code == 0
    assert payload["inferred_needs"] == ["ffmpeg"]


def test_inferred_needs_reach_policy_select(monkeypatch, tmp_path):
    """Tool suy ra đi **xuyên suốt** tới :func:`policy.select` (tiêu chí mềm)."""
    _patch_seams(monkeypatch, tmp_path)
    captured: dict = {}
    real_select = policy_mod.select

    def spy(*args, **kwargs):
        captured["inferred_needs"] = kwargs.get("inferred_needs")
        captured["need"] = args[1] if len(args) > 1 else kwargs.get("need")
        return real_select(*args, **kwargs)

    monkeypatch.setattr(policy_mod, "select", spy)

    payload, code = cli_mod._cmd_do(_args("ffmpeg -version"))

    assert code == 0
    assert payload["inferred_needs"] == ["ffmpeg"]
    assert captured["inferred_needs"] == ["ffmpeg"]
    # Tool suy ra **không** được trộn vào ``Need.tools`` (tiêu chí cứng).
    assert captured["need"].tools == []


def test_empty_inferred_needs_keeps_old_behaviour(monkeypatch, tmp_path):
    """Lệnh không cần tool ⇒ ``inferred_needs=[]`` ⇒ policy không đổi hành vi."""
    _patch_seams(monkeypatch, tmp_path)
    captured: dict = {}
    real_select = policy_mod.select

    def spy(*args, **kwargs):
        captured["inferred_needs"] = kwargs.get("inferred_needs")
        return real_select(*args, **kwargs)

    monkeypatch.setattr(policy_mod, "select", spy)

    payload, code = cli_mod._cmd_do(_args("echo hi"))

    assert code == 0
    assert payload["inferred_needs"] == []
    assert captured["inferred_needs"] == []


# --------------------------------------------------------------------------
# 5 — tool suy ra KHÔNG dẫn tới bước cài tool
# --------------------------------------------------------------------------
def test_inferred_tool_is_never_installed(monkeypatch, tmp_path):
    """Bước *ensure* chỉ nhận tool của ``--need`` — không có tool suy ra."""
    _patch_seams(monkeypatch, tmp_path)
    ensured_calls: list[list[str]] = []

    def fake_ensure(host, profile, tools):
        ensured_calls.append(list(tools or []))
        return []

    monkeypatch.setattr(cli_mod, "_ensure_tools", fake_ensure)

    payload, code = cli_mod._cmd_do(_args("ffmpeg -version"))

    assert code == 0
    assert ensured_calls == [[]], f"tool suy ra không được cài: {ensured_calls}"
    assert not payload.get("ensured")


def test_only_need_tools_are_installed(monkeypatch, tmp_path):
    """``--need`` **vẫn** được cài (tiêu chí cứng); tool suy ra thì không.

    ``npm`` có script setup (nên host không bị loại), còn tool **suy ra** từ
    lệnh (``npm test`` ⇒ ``node``) không được lọt vào bước *ensure*.
    """
    _patch_seams(monkeypatch, tmp_path)
    ensured_calls: list[list[str]] = []

    def fake_ensure(host, profile, tools):
        ensured_calls.append(list(tools or []))
        return []

    monkeypatch.setattr(cli_mod, "_ensure_tools", fake_ensure)

    payload, code = cli_mod._cmd_do(_args("npm test", need=["npm"]))

    assert code == 0
    assert ensured_calls == [["npm"]], f"chỉ --need mới được cài: {ensured_calls}"
    assert payload["inferred_needs"] == ["node"]


def test_inferred_tool_does_not_reject_host(monkeypatch, tmp_path):
    """Tool suy ra mà host không có ⇒ host **vẫn** là ứng viên hợp lệ."""
    _patch_seams(monkeypatch, tmp_path)

    payload, code = cli_mod._cmd_do(_args(f"{MISSING_TOOL} --version"))

    assert code == 0, f"tool suy ra không được loại host: {payload}"
    assert payload["inferred_needs"] == [MISSING_TOOL]


def test_need_is_still_a_hard_criterion(monkeypatch, tmp_path):
    """``--need`` (tool không có script setup) ⇒ host bị loại ⇒ exit 10."""
    _patch_seams(monkeypatch, tmp_path)
    from roffkit.errors import EXIT_NO_HOST, RoffError

    with pytest.raises(RoffError) as excinfo:
        cli_mod._cmd_do(_args("echo hi", need=[MISSING_TOOL]))

    assert excinfo.value.code == EXIT_NO_HOST
    rejected = json.dumps(excinfo.value.fields.get("rejected") or {})
    assert MISSING_TOOL in rejected, rejected


# --------------------------------------------------------------------------
# JSON end-to-end: `--need` cứng + tool suy ra mềm
# --------------------------------------------------------------------------
def test_do_need_still_rejects_host_end_to_end(fake_ssh, tmp_path):
    """``--need`` thiếu tool ⇒ exit 10 (không phải exit 0)."""
    fake_ssh("muse-a", responses=[{"match": "nproc", "stdout": _probe_out()}])
    env = _cli_env(tmp_path)

    rc, payload, err = _do(
        env, "--cmd", "echo hi", "--need", MISSING_TOOL, "--name", "proj"
    )

    assert rc == 10, f"{payload} {err}"
    rejected = str(payload.get("rejected") or {})
    assert MISSING_TOOL in rejected, rejected


def test_do_inferred_tool_does_not_install_end_to_end(fake_ssh, tmp_path):
    """Tool suy ra (không ai có) ⇒ ``ensured`` rỗng — không cài tool."""
    fake_ssh("muse-a", responses=[{"match": "nproc", "stdout": _probe_out()}])
    env = _cli_env(tmp_path)

    rc, payload, err = _do(
        env, "--cmd", f"{MISSING_TOOL} --version", "--name", "proj"
    )

    assert rc == 0, f"{payload} {err}"
    assert payload["inferred_needs"] == [MISSING_TOOL]
    assert not payload.get("ensured"), "suy ra không được dẫn tới việc cài tool"
