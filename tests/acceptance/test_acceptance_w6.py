"""Nghiệm thu W6b — **tự suy ra tool** từ câu lệnh (không cần ``--need``).

S37 — ``roff do`` **không** có ``--need`` vẫn phải **suy ra** được tool mà lệnh
cần (``ffmpeg …`` ⇒ ``ffmpeg``) và báo ra trong JSON, để người đọc thấy máy
được chọn có lý do gì.

S38 — suy ra là tiêu chí **MỀM**: tool mà **không host nào có** thì:
  * **không** được loại host (vẫn chạy được);
  * **không** được kích hoạt bước *cài* tool (``ensured`` phải rỗng) — vì suy ra
    có thể sai, cài tool theo suy ra là rủi ro;
  * vẫn phải xuất hiện trong JSON (minh bạch).

S39 — lệnh vô nghĩa (``echo``) ⇒ **không** suy ra gì.

Hợp đồng JSON (do người nghiệm thu định nghĩa, agent triển khai):
``{"inferred_needs": ["ffmpeg", …]}`` — danh sách tool **đã chuẩn hoá**, thứ tự
ổn định, không trùng.

Các test **bỏ qua** (skip) nếu fleet không đủ máy — không đánh dấu xanh khi
không kiểm chứng được.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as common  # noqa: E402
_DEFAULT_STATE = common.DEFAULT_STATE  # dùng chung (xem _common.py)

from _common import wait_or_skip  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
SKILL = REPO / "skill"
LAUNCHER = SKILL / "roff.py"

pytestmark = pytest.mark.acceptance


ALIASES = ["muse-pmb333", "muse-tampt", "muse-bienpm333", "muse-phamminhbien3333"]

#: Tool **không host nào** có — dùng để kiểm chứng tiêu chí mềm.
FAKE_TOOL = "zzz-roff-khong-ton-tai-xyz"


def roff(args, timeout=300, env=None):
    e = common.roff_env(env)
    return subprocess.run(
        [sys.executable, str(LAUNCHER), *args],
        capture_output=True, text=True, timeout=timeout, env=e,
    )


def roff_json(args, timeout=300, env=None):
    p = roff(args, timeout=timeout, env=env)
    lines = [ln for ln in p.stdout.strip().splitlines() if ln.strip()]
    try:
        obj = json.loads(lines[-1]) if lines else {}
    except json.JSONDecodeError:
        obj = {}
    return p.returncode, obj, p.stderr


def ssh_ok(alias, cmd="true", timeout=30):
    try:
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", alias, cmd],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return 255, ""
    return r.returncode, r.stdout


def live_aliases(minimum=1):
    """Các alias **đang** sống (probe thật, không đọc cache)."""
    out = []
    for alias in ALIASES:
        rc, _ = ssh_ok(alias)
        if rc == 0:
            out.append(alias)
    return out if len(out) >= minimum else []


@pytest.fixture(scope="module")
def fleet():
    live = live_aliases(minimum=1)
    if not live:
        pytest.skip("không có máy muse nào sống — bật lại tunnel trước khi nghiệm thu")
    return live


@pytest.fixture
def cfg(tmp_path, fleet):
    """Config giới hạn fleet còn các máy muse đang sống."""
    path = tmp_path / "config.ini"
    path.write_text("[fleet]\ninclude = muse-*\n")
    return {"path": path, "env": {"ROFF_CONFIG": str(path)}, "live": fleet}


def _inferred(obj) -> list:
    """Danh sách tool suy ra trong JSON của ``roff do``."""
    value = obj.get("inferred_needs")
    if value is None:
        return []
    assert isinstance(value, list), f"inferred_needs phải là list: {value!r}"
    return [str(v) for v in value]


def _cancel(job_id, env):
    """Huỷ job để không để lại rác trên host."""
    if job_id:
        roff_json(["cancel", job_id], timeout=120, env=env)


# ==========================================================================
# S37 — suy ra tool từ lệnh
# ==========================================================================
def test_s37_infers_tool_from_command(cfg):
    """Lệnh ``ffmpeg …`` ⇒ JSON phải báo ``inferred_needs`` có ``ffmpeg``."""
    rc, obj, err = roff_json([
        "do", "--cmd", "ffmpeg -version",
        "--out", str(cfg["path"].parent / "out37"),
    ], timeout=600, env=cfg["env"])
    if common.is_host_down(rc, obj, err):
        pytest.skip(f"máy remote chết lúc gửi job — sự cố hạ tầng: {err[-200:]}")
    assert rc == 0, f"roff do thất bại: rc={rc} {json.dumps(obj, ensure_ascii=False)} {err[-300:]}"
    inferred = _inferred(obj)
    assert "ffmpeg" in inferred, f"phải suy ra được ffmpeg: {inferred}"
    # Danh sách không trùng, thứ tự ổn định.
    assert len(inferred) == len(set(inferred)), f"inferred_needs bị trùng: {inferred}"
    _cancel(obj.get("job"), cfg["env"])


def test_s37_explicit_need_is_not_guessed_away(cfg):
    """``--need`` ghi rõ vẫn là tiêu chí **cứng** (không bị suy ra ghi đè)."""
    rc, obj, err = roff_json([
        "do", "--cmd", "ffmpeg -version", "--need", "ffmpeg",
        "--out", str(cfg["path"].parent / "out37b"),
    ], timeout=600, env=cfg["env"])
    if common.is_host_down(rc, obj, err):
        pytest.skip(f"máy remote chết lúc gửi job — sự cố hạ tầng: {err[-200:]}")
    assert rc == 0, f"roff do thất bại: rc={rc} {json.dumps(obj, ensure_ascii=False)} {err[-300:]}"
    # Suy ra vẫn được báo (minh bạch) nhưng **không** thay thế `--need`.
    assert "ffmpeg" in _inferred(obj)
    _cancel(obj.get("job"), cfg["env"])


# ==========================================================================
# S38 — suy ra là tiêu chí MỀM
# ==========================================================================
def test_s38_inferred_tool_never_rejects_or_installs(cfg):
    """Tool suy ra mà **không host nào có** ⇒ không loại host, không cài tool."""
    cmd = f"{FAKE_TOOL} --version"
    rc, obj, err = roff_json([
        "do", "--cmd", cmd,
        "--out", str(cfg["path"].parent / "out38"),
    ], timeout=600, env=cfg["env"])

    # 0) Máy chết ⇒ sự cố hạ tầng, không phải lỗi roff.
    if common.is_host_down(rc, obj, err):
        pytest.skip(f"máy remote chết lúc gửi job — sự cố hạ tầng: {err[-200:]}")

    # 1) Suy ra vẫn xuất hiện (minh bạch).
    assert FAKE_TOOL in _inferred(obj), f"phải báo tool suy ra: {json.dumps(obj, ensure_ascii=False)}"

    # 2) **Không** loai host vì thiếu tool suy ra.
    if rc == 10:
        rejected = json.dumps(obj.get("rejected") or {}, ensure_ascii=False)
        assert FAKE_TOOL not in rejected, (
            f"tool **suy ra** không được dùng để loại host: {rejected}")
        pytest.skip("fleet đã đầy — không kiểm chứng được việc tool thiếu không loại host")

    assert rc == 0, f"roff do thất bại: rc={rc} {json.dumps(obj, ensure_ascii=False)} {err[-300:]}"

    # 3) **Không** kích hoạt bước cài tool (suy ra có thể sai — cài là rủi ro).
    ensured = obj.get("ensured")
    assert not ensured, f"suy ra không được dẫn tới việc cài tool: {ensured!r}"

    _cancel(obj.get("job"), cfg["env"])


# ==========================================================================
# S39 — lệnh không cần tool
# ==========================================================================
def test_s39_builtin_command_infers_nothing(cfg):
    """``echo`` là builtin của shell ⇒ không suy ra tool nào."""
    rc, obj, err = roff_json([
        "do", "--cmd", "echo roff-acceptance",
        "--out", str(cfg["path"].parent / "out39"),
    ], timeout=600, env=cfg["env"])
    if common.is_host_down(rc, obj, err):
        pytest.skip(f"máy remote chết lúc gửi job — sự cố hạ tầng: {err[-200:]}")
    assert rc == 0, f"roff do thất bại: rc={rc} {json.dumps(obj, ensure_ascii=False)} {err[-300:]}"
    assert _inferred(obj) == [], f"echo không được suy ra tool: {_inferred(obj)}"
    _cancel(obj.get("job"), cfg["env"])


def test_s39_waits_for_inferred_job_to_finish(cfg):
    """Job dùng tool suy ra vẫn chạy và **kết thúc bình thường**."""
    rc, obj, err = roff_json([
        "do", "--cmd", "ffmpeg -version",
        "--out", str(cfg["path"].parent / "out39b"),
    ], timeout=600, env=cfg["env"])
    if common.is_host_down(rc, obj, err):
        pytest.skip(f"máy remote chết lúc gửi job — sự cố hạ tầng: {err[-200:]}")
    assert rc == 0, f"roff do thất bại: rc={rc} {json.dumps(obj, ensure_ascii=False)} {err[-300:]}"
    jid = obj.get("job")
    assert jid, f"phải có job id: {json.dumps(obj, ensure_ascii=False)}"
    rcw, res, errw = wait_or_skip(jid, timeout=960, env=cfg["env"], wait_timeout=900)
    assert rcw == 0, f"job {jid} không xong: rc={rcw} {json.dumps(res, ensure_ascii=False)}"
