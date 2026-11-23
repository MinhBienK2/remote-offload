"""W8a — ``probe.DEFAULT_TOOLS``: dò đủ tool hay dùng cho **job nặng**.

Bug B18 (đo thật 23/11/2026): ``DEFAULT_TOOLS`` là danh sách **cố định 33
tool** ⇒ ``pytest``/``mypy``/``ruff``/``eslint``/``tsc``/``jest``/``convert``/
``magick`` **không bao giờ** có trong ``Profile.tools`` ⇒ tool suy ra ra loại
đó không host nào khớp ⇒ ưu tiên "host có tool" là **no-op**.

Không test nào ở đây cần mạng: chỉ **render** script probe rồi grep, không
chạy ssh thật.
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

from roffkit import probe as probe_mod  # noqa: E402


#: Tool **mới** (brief §W8a).
NEW_TOOLS: tuple[str, ...] = (
    "pytest", "mypy", "ruff", "eslint", "tsc", "jest", "convert", "magick",
)

#: Tool cũ — phải **giữ nguyên** (và giữ nguyên thứ tự tương đối).
OLD_TOOLS: tuple[str, ...] = (
    "ffmpeg", "ffprobe", "node", "npm", "python3", "pip3", "git", "rsync",
    "tar", "curl", "wget", "gcc", "make", "cmake", "go", "cargo", "rustc",
    "java", "mvn", "gradle", "docker", "podman", "deno", "bun", "pnpm",
    "yarn", "uv", "poetry", "dotnet", "playwright", "chromium",
    "google-chrome",
)


def _script_tools() -> list[str]:
    """Tool **thực sự** được dò trong script probe đã render."""
    script = probe_mod.build_script()
    for line in script.splitlines():
        stripped = line.strip()
        if stripped.startswith("for t in "):
            inner = stripped[len("for t in "):]
            inner = inner.split(";", 1)[0]
            return inner.split()
    raise AssertionError("không tìm thấy vòng lặp dò tool trong script probe")


# --------------------------------------------------------------------------
# DEFAULT_TOOLS
# --------------------------------------------------------------------------
def test_default_tools_contains_new_tools():
    """Tool mới phải nằm trong ``DEFAULT_TOOLS`` (bug B18)."""
    for tool in NEW_TOOLS:
        assert tool in probe_mod.DEFAULT_TOOLS, tool


def test_default_tools_still_contains_old_tools():
    """Tool cũ **không** được bỏ (thêm, không thay)."""
    for tool in OLD_TOOLS:
        assert tool in probe_mod.DEFAULT_TOOLS, tool


def test_default_tools_has_no_duplicate():
    """Không trùng tên (dò 2 lần là lãng phí)."""
    assert len(set(probe_mod.DEFAULT_TOOLS)) == len(probe_mod.DEFAULT_TOOLS)


def test_default_tools_count():
    """33 tool cũ + 8 tool mới = 41."""
    assert len(probe_mod.DEFAULT_TOOLS) == len(OLD_TOOLS) + len(NEW_TOOLS)


def test_old_tools_keep_their_relative_order():
    """Thứ tự **tương đối** của phần cũ không đổi (cache cũ vẫn đọc được)."""
    got = [t for t in probe_mod.DEFAULT_TOOLS if t in set(OLD_TOOLS)]
    assert got == list(OLD_TOOLS)


# --------------------------------------------------------------------------
# script probe (render + grep — KHÔNG chạy ssh)
# --------------------------------------------------------------------------
def test_probe_script_probes_new_tools():
    """Script probe phải **có lệnh dò** từng tool mới."""
    probed = _script_tools()
    for tool in NEW_TOOLS:
        assert tool in probed, tool


def test_probe_script_still_probes_old_tools():
    """Script probe vẫn dò **đủ** tool cũ."""
    probed = set(_script_tools())
    for tool in OLD_TOOLS:
        assert tool in probed, tool


def test_probe_script_uses_command_v():
    """Dò tool bằng ``command -v`` (rẻ, không chạy tool)."""
    script = probe_mod.build_script()
    assert 'command -v "$t"' in script


def test_probe_script_is_valid_shell():
    """Script render ra phải parse được bởi ``sh -n``."""
    script = probe_mod.build_script()
    proc = subprocess.run(["sh", "-n"], input=script.encode(), capture_output=True)
    assert proc.returncode == 0, proc.stderr.decode()


def test_probe_script_emits_tool_lines():
    """Tool có thật mới được ghi ra (``emit TOOL``)."""
    script = probe_mod.build_script()
    assert "emit TOOL" in script


# --------------------------------------------------------------------------
# hành vi probe khác KHÔNG đổi (TTL, cache, fingerprint…)
# --------------------------------------------------------------------------
def test_probe_cache_key_unchanged():
    """Tên khoá cache probe không đổi ⇒ cache cũ vẫn dùng được."""
    assert probe_mod._cache_path("/tmp/roff-w8a").endswith("probes.json")


def test_probe_does_not_probe_more_than_one_round_trip():
    """Thêm tool **không** được làm probe gửi nhiều round-trip hơn.

    Script probe gom **mọi** thông tin trong 1 lần chạy (1 round-trip ssh):
    script được gửi qua stdin ⇒ không có lệnh ``ssh`` nào bên trong script.
    """
    script = probe_mod.build_script()
    assert "ssh " not in script
    # Lệnh ssh gửi script qua stdin (`ssh <alias> sh -s`) ⇒ đúng 1 lần gọi.
    cmd = probe_mod.ssh_command("muse")
    assert cmd[-2:] == ["sh", "-s"]
    assert cmd.count("ssh") == 1


def test_default_tools_are_valid_shell_words():
    """Tên tool phải là "từ" shell hợp lệ (script dán thẳng vào ``for t in``)."""
    for tool in probe_mod.DEFAULT_TOOLS:
        assert tool.replace("-", "").replace(".", "").replace("+", "").isalnum(), tool
