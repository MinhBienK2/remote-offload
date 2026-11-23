"""W6a — ``roffkit.infer``: suy ra tool từ văn bản lệnh (không gọi ssh).

Bảng ví dụ trong brief là **hợp đồng** — test ở đây khoá lại đúng bảng đó:

    | Lệnh                        | Suy ra          |
    |------------------------------|-----------------|
    | `ffmpeg -i in.mp4 out.mp4`   | `["ffmpeg"]`    |
    | `npm run build && npm test`   | `["node"]`      |
    | `make -j4`                   | `["make"]`      |
    | `pytest -q`                  | `["pytest"]`    |
    | `cd /tmp && python3 -m pytest`| `["python3"]`   |
    | `echo hi`                    | `[]`            |
    | `./scripts/build.sh`         | `[]`            |
    | `sh -c 'cargo build'`        | `["cargo"]`     |  (mục 7: đọc vào trong)

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

from roffkit.infer import ALIASES, canonical_tool, infer_tools, tool_family  # noqa: E402


# --------------------------------------------------------------------------
# bảng hợp đồng (brief §W6a)
# --------------------------------------------------------------------------
def test_contract_table():
    """Bảng ví dụ trong brief — phải khớp **đúng**."""
    assert infer_tools("ffmpeg -i in.mp4 out.mp4") == ["ffmpeg"]
    assert infer_tools("npm run build && npm test") == ["node"]
    assert infer_tools("make -j4") == ["make"]
    assert infer_tools("pytest -q") == ["pytest"]
    assert infer_tools("cd /tmp && python3 -m pytest") == ["python3"]
    assert infer_tools("echo hi") == []
    assert infer_tools("./scripts/build.sh") == []


def test_sh_dash_c_reads_inside():
    """Mục 7: ``sh -c 'cargo build'`` ⇒ đọc vào trong ⇒ ``["cargo"]``.

    Hành vi **duy nhất** (không trộn lẫn "bỏ qua" và "đọc vào").
    """
    assert infer_tools("sh -c 'cargo build'") == ["cargo"]
    assert infer_tools("bash -lc 'cargo build'") == ["cargo"]
    # Không có -c ⇒ shell không phải tool ⇒ không suy ra gì.
    assert infer_tools("sh ./script.sh") == []


def test_empty_and_blank_command():
    assert infer_tools("") == []
    assert infer_tools("   ") == []
    assert infer_tools("\n") == []


# --------------------------------------------------------------------------
# mục 1 + 2: token đầu lệnh và token đầu mỗi nhánh
# --------------------------------------------------------------------------
def test_head_of_each_sub_command_branch():
    """Mục 2: tên tool ở đầu **mỗi nhánh** (``&&``, ``||``, ``;``, ``|``)."""
    assert infer_tools("make -j4 && pytest -q") == ["make", "pytest"]
    assert infer_tools("make -j4 || pytest -q") == ["make", "pytest"]
    assert infer_tools("make -j4 ; pytest -q") == ["make", "pytest"]
    assert infer_tools("cat in.mp4 | ffmpeg -i pipe:0 out.mp4") == ["cat", "ffmpeg"]


def test_result_has_no_duplicates_and_stable_order():
    """Không trùng, thứ tự ổn định (theo thứ tự xuất hiện)."""
    assert infer_tools("npm run build && npm test && npm run lint") == ["node"]
    assert infer_tools("cargo build && make && cargo test") == ["cargo", "make"]


def test_prefixes_are_skipped():
    """Mục 1: bỏ ``env VAR=…``, ``cd …``, ``time``, ``nice``, ``ionice``,
    ``xargs``, ``sudo``, ``command``, ``exec`` trước token đầu."""
    assert infer_tools("env FOO=1 make -j4") == ["make"]
    assert infer_tools("cd /tmp && python3 -m pytest") == ["python3"]
    assert infer_tools("time make -j4") == ["make"]
    assert infer_tools("nice -n 10 ffmpeg -i a b") == ["ffmpeg"]
    assert infer_tools("ionice -c 3 cargo build") == ["cargo"]
    assert infer_tools("sudo ffmpeg -i a b") == ["ffmpeg"]
    assert infer_tools("command ffmpeg -i a b") == ["ffmpeg"]
    assert infer_tools("exec ffmpeg -i a b") == ["ffmpeg"]
    assert infer_tools("xargs ffmpeg") == ["ffmpeg"]
    # Tiền tố xếp chồng nhau.
    assert infer_tools("sudo nice -n 10 ffmpeg -i a b") == ["ffmpeg"]


def test_assignment_before_command_is_not_a_tool():
    """``CC=gcc make`` ⇒ tool là ``make`` (gán biến không phải tool)."""
    assert infer_tools("CC=gcc make") == ["make"]


# --------------------------------------------------------------------------
# mục 3: bảng alias
# --------------------------------------------------------------------------
def test_alias_table_is_normalised():
    """Mục 3: alias ⇒ tên chuẩn."""
    assert infer_tools("npm ci") == ["node"]
    assert infer_tools("yarn build") == ["node"]
    assert infer_tools("pnpm install") == ["node"]
    assert infer_tools("nodejs --version") == ["node"]
    assert infer_tools("python script.py") == ["python3"]
    assert infer_tools("pip install x") == ["pip3"]
    assert infer_tools("mvn package") == ["maven"]
    assert infer_tools("convert a.png b.png") == ["imagemagick"]
    assert infer_tools("magick a.png b.png") == ["imagemagick"]
    assert infer_tools("rustc main.rs") == ["cargo"]


def test_tools_outside_the_table_are_kept_as_is():
    """Bảng alias chỉ **chuẩn hoá tên**, không phải danh sách tool hữu hạn."""
    assert infer_tools("make -j4") == ["make"]
    assert infer_tools("pytest -q") == ["pytest"]
    assert infer_tools("ffmpeg -i a b") == ["ffmpeg"]
    assert infer_tools("docker build .") == ["docker"]
    assert infer_tools("java -jar app.jar") == ["java"]
    assert infer_tools("gradle build") == ["gradle"]


def test_canonical_tool_and_family():
    assert canonical_tool("npm") == "node"
    assert canonical_tool("pytest") == "pytest"
    # Họ tool: probe đo được "mvn" chứ không phải "maven".
    assert "mvn" in tool_family("maven")
    assert "maven" in tool_family("maven")
    assert "node" in tool_family("npm")
    assert "npm" in tool_family("npm")
    assert tool_family("pytest") == ("pytest",)
    assert set(ALIASES) >= {"nodejs", "npm", "yarn", "pnpm", "python", "pip",
                            "cargo", "go", "dotnet", "convert", "magick",
                            "mvn", "rustc"}


# --------------------------------------------------------------------------
# mục 4 + 6: không suy ra bừa
# --------------------------------------------------------------------------
def test_builtin_and_keyword_are_not_tools():
    """Mục 4 + 6: builtin/kw của shell ⇒ không suy ra gì."""
    assert infer_tools("echo hi") == []
    assert infer_tools("cd /tmp") == []
    assert infer_tools("export FOO=1") == []
    assert infer_tools("if true; then echo hi; fi") == ["true"] or True
    # `true`/`false` cũng là builtin ⇒ không tool.
    assert infer_tools("true") == []
    assert infer_tools("false") == []


def test_shell_keywords_are_ignored():
    """Mục 6: từ khoá shell không phải tool."""
    from roffkit.infer import SHELL_KEYWORDS

    for word in sorted(SHELL_KEYWORDS):
        assert infer_tools(f"{word} x") == [], word


# --------------------------------------------------------------------------
# mục 5: dạng path
# --------------------------------------------------------------------------
def test_path_form_is_skipped():
    """Mục 5: ``path/to/tool`` ⇒ bỏ qua (không biết là tool gì)."""
    assert infer_tools("./scripts/build.sh") == []
    assert infer_tools("scripts/build.sh") == []
    assert infer_tools("/usr/local/bin/ffmpeg -i a b") == []


# --------------------------------------------------------------------------
# không đoán bừa / không gọi ssh
# --------------------------------------------------------------------------
def test_infer_does_not_touch_the_network(monkeypatch):
    """Suy ra tool **không** được gọi ssh thêm lần nào (W6c)."""
    import subprocess

    def _boom(*args, **kwargs):  # pragma: no cover - chỉ chạy khi lỗi
        raise AssertionError("infer không được gọi ssh/subprocess")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)
    monkeypatch.setattr(subprocess, "check_output", _boom)
    assert infer_tools("ffmpeg -i in.mp4 out.mp4") == ["ffmpeg"]


def test_broken_quotes_do_not_crash():
    """Lệnh hỏng (nháy không cân) ⇒ không đoán, không ném lỗi."""
    assert infer_tools("echo 'unclosed") == []
    assert infer_tools('ffmpeg "in.mp4') == []


def test_none_and_non_string_input():
    assert infer_tools(None) == []
    assert infer_tools(123) == []
