"""W8c — ``roffkit.infer``: lệnh **chắc chắn không phải job nặng**.

W8c: thêm một tập nhỏ lệnh tầm thường vào ``NOT_TOOLS`` (``sleep``,
``date``, ``pwd`` …) ⇒ chúng khỏi làm **nhiễu** lý do "không có tool suy ra
từ lệnh: …".

Cố ý **KHÔNG** gồm ``cat``/``cp``/``mv``/``rm``/``tar``/``sort``/``grep``
— chúng **có** thể nặng khi dữ liệu lớn.

Không test nào ở đây cần mạng.
"""

from __future__ import annotations

import os
import sys

_SKILL_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "skill"
)
if _SKILL_DIR not in sys.path:
    sys.path.insert(0, _SKILL_DIR)

from roffkit.infer import NOT_TOOLS, TRIVIAL_COMMANDS, infer_tools  # noqa: E402


#: Lệnh **chắc chắn không nặng** (brief §W8c).
TRIVIAL: tuple[str, ...] = (
    "sleep", "wait", "true", "false", "exit", "pwd", "whoami", "id",
    "date", "hostname", "basename", "dirname", "printf",
)

#: Lệnh **có** thể nặng (dữ liệu lớn) ⇒ **không** được loại.
MAYBE_HEAVY: tuple[str, ...] = (
    "cat", "cp", "mv", "rm", "tar", "sort", "grep", "rsync", "gzip",
    "ffmpeg", "pytest", "cargo", "make", "docker",
)


# --------------------------------------------------------------------------
# lệnh tầm thường ⇒ không suy ra gì
# --------------------------------------------------------------------------
def test_trivial_commands_are_not_tools():
    """Lệnh tầm thường ⇒ ``[]``."""
    for cmd in TRIVIAL:
        assert infer_tools(cmd) == [], cmd
        assert infer_tools(f"{cmd} x") == [], cmd


def test_trivial_commands_are_in_not_tools():
    """Tập :data:`TRIVIAL_COMMANDS` nằm trong ``NOT_TOOLS``."""
    assert TRIVIAL_COMMANDS <= NOT_TOOLS
    for cmd in TRIVIAL:
        assert cmd in NOT_TOOLS, cmd


def test_sleep_disappears_from_a_realistic_command():
    """``sleep 120; cp a b`` ⇒ ``sleep`` biến mất (chỉ còn ``cp``)."""
    assert infer_tools("sleep 120; cp a b") == ["cp"]
    assert "sleep" not in infer_tools("sleep 120")


def test_trivial_command_does_not_hide_the_next_branch():
    """Nhánh tầm thường không được che nhánh nặng đứng sau."""
    assert infer_tools("sleep 30 && pytest -q") == ["pytest"]
    assert infer_tools("date; ffmpeg -i a b") == ["ffmpeg"]


def test_trivial_command_with_prefixes():
    """Tiền tố lệnh vẫn bị bỏ đúng, không làm lệ lệch."""
    assert infer_tools("nice -n 10 sleep 5") == []
    assert infer_tools("cd /tmp && sleep 5") == []
    assert infer_tools("env X=1 sleep 5") == []


# --------------------------------------------------------------------------
# lệnh CÓ THỂ nặng ⇒ vẫn được suy ra
# --------------------------------------------------------------------------
def test_maybe_heavy_commands_are_still_inferred():
    """``cat``/``cp``/``tar`` … **vẫn** được suy ra (dữ liệu lớn)."""
    for cmd in MAYBE_HEAVY:
        assert infer_tools(cmd) == [cmd], cmd


def test_cat_and_tar_in_a_pipeline():
    assert infer_tools("cat in.mp4 | tar xf -") == ["cat", "tar"]


# --------------------------------------------------------------------------
# bảng hợp đồng (brief §W8c + §kiểm chứng)
# --------------------------------------------------------------------------
def test_contract_table():
    """Bảng ví dụ trong brief — phải khớp **đúng**."""
    assert infer_tools("pytest -q") == ["pytest"]
    assert infer_tools("convert a.png b.jpg") == ["imagemagick"]
    assert infer_tools("ffmpeg -i in.mp4 out.mp4") == ["ffmpeg"]
    assert infer_tools("sleep 120") == []
    assert infer_tools("echo hi") == []


def test_lint_and_typecheck_tools_are_inferred():
    """Tool kiểm tra/lint (W8a) suy ra được từ lệnh."""
    assert infer_tools("mypy .") == ["mypy"]
    assert infer_tools("ruff check .") == ["ruff"]
    assert infer_tools("eslint .") == ["eslint"]
    assert infer_tools("tsc --noEmit") == ["tsc"]
    assert infer_tools("jest") == ["jest"]


def test_echo_is_still_a_builtin():
    """``echo`` đã là builtin từ trước ⇒ vẫn ``[]``."""
    assert infer_tools("echo hi") == []
    assert "echo" in NOT_TOOLS
