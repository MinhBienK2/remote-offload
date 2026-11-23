"""Unit test cho hook `PreToolUse` của Claude Code (`hooks/claude-code/pretooluse.py`).

Hook phải chạy **độc lập** (không import roffkit) nên test nạp file trực tiếp
bằng importlib.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HOOK_PATH = os.path.join(ROOT, "skill", "hooks", "claude-code", "pretooluse.py")


def _load_hook():
    spec = importlib.util.spec_from_file_location("roff_pretooluse", HOOK_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["roff_pretooluse"] = module
    spec.loader.exec_module(module)
    return module


hook = _load_hook()


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """Không đọc config thật của người dùng."""
    cfg = tmp_path / "config.ini"
    monkeypatch.setenv("ROFF_CONFIG", str(cfg))
    return cfg


def run_hook(payload, raw=None):
    """Chạy hook với input JSON, trả (rc, stdout, stderr)."""
    data = raw if raw is not None else json.dumps(payload)
    stdin = io.StringIO(data)
    out, err = io.StringIO(), io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        rc = hook.main(stdin=stdin)
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    return rc, out.getvalue(), err.getvalue()


# --------------------------------------------------------------------------------------
# Bảng lệnh → kết quả
# --------------------------------------------------------------------------------------
BLOCKED = [
    # build
    "npm test",
    "npm run build",
    "npm ci",
    "npm install",
    "pnpm install",
    "yarn install",
    "bun test",
    "cargo build",
    "cargo test",
    "go build",
    "go test ./...",
    "mvn package",
    "gradle build",
    "make",
    "cmake --build .",
    "tsc",
    "next build",
    "vite build",
    "webpack",
    "pip install -r requirements.txt",
    "docker build .",
    # build — hệ sinh thái khác (dotnet / swift / bazel / ninja / meson / sbt / mix / deno)
    "dotnet build",
    "dotnet publish",
    "dotnet restore",
    "dotnet msbuild",
    "swift build",
    "bazel build //...",
    "ninja",
    "meson compile",
    "sbt compile",
    "sbt package",
    "mix compile",
    "deno compile",
    "deno task build",
    "python -m build",
    # test — ngôn ngữ khác
    "python -m unittest",
    "python -m unittest discover",
    "dotnet test",
    "swift test",
    "bazel test //...",
    "meson test",
    "sbt test",
    "mix test",
    "deno test",
    "deno task test",
    "phpunit",
    "php artisan test",
    # test
    "npx vitest run",
    "vitest run",
    "jest",
    "pytest",
    "playwright test",
    "cypress run",
    "tox",
    "nox",
    "rspec",
    # media
    "ffmpeg -i in.mp4 out.mp4",
    "blender -b scene.blend",
    "sox in.wav out.wav",
    "magick a.png b.png",
    "convert a.png b.png",
    # ml
    "python train.py",
    "python train_resnet.py",
    "accelerate launch train.py",
    "torchrun --nproc_per_node=4 train.py",
    "python -m torch.distributed.run train.py",
    # wrapper
    "uv run pytest",
    "poetry run pytest",
    "pnpm exec vitest run",
    "yarn dlx playwright test",
    "bunx vitest run",
    # lệnh ghép
    "cd /tmp/proj && npm test",
]

ALLOWED = [
    "ls",
    "ls -la",
    "cat foo.txt",
    "grep -r TODO src",
    "rg pattern",
    "git status",
    "git log --oneline",
    "git diff HEAD~1",
    "find . -name '*.py'",
    "echo hello",
    "pwd",
    # lệnh có file/tham số cụ thể
    "pytest tests/test_a.py::test_b",
    "pytest tests/test_a.py",
    "vitest run src/a.test.ts",
    "jest src/a.test.ts",
    "rspec spec/models/user_spec.rb",
    # lệnh giới hạn vào file/test cụ thể (ngôn ngữ khác)
    "phpunit tests/FooTest.php",
    "phpunit --filter FooTest",
    "php artisan test --filter FooTest",
    "deno test tests/a_test.ts",
    "deno test --filter 'a_test'",
    "mix test tests/foo_test.exs",
    "python -m unittest tests/test_a.py",
    "swift test --filter MyTestCase",
    "sbt 'testOnly com.foo.BarTest'",
    # cờ chỉ-in-thông-tin / chạy-thử
    "dotnet --version",
    "dotnet build --help",
    "swift --version",
    "bazel --version",
    "ninja -n",
    "ninja -t targets",
    "sbt --version",
    "mix --version",
    "deno --version",
    "phpunit --version",
    "meson --version",
    # make dry-run, tsc 1 file
    "make -n build",
    "tsc src/a.ts",
    # lệnh ghép
    "cd /tmp/proj && ls",
]

SUGGESTED = [
    "npm run dev",
    "npm start",
    "vite",
    "next dev",
    "python -m http.server 8000",
    "jupyter lab",
    # dev server / process sống — chỉ gợi ý, không chặn cứng
    "dotnet watch",
    "dotnet run",
    "mix phx.server",
    "php artisan serve",
    "swift run",
    "bazel run //app:app",
    "sbt",
]


@pytest.mark.parametrize("command", BLOCKED)
def test_heavy_commands_are_blocked(command):
    decision = hook.classify(command)
    assert decision.action == "block", f"{command!r} phải bị chặn, got {decision.action}"
    assert decision.group


@pytest.mark.parametrize("command", ALLOWED)
def test_light_commands_are_allowed(command):
    decision = hook.classify(command)
    assert decision.action == "allow", f"{command!r} phải qua, got {decision.action}"


@pytest.mark.parametrize("command", SUGGESTED)
def test_interactive_commands_only_suggest(command):
    decision = hook.classify(command)
    assert decision.action == "suggest", f"{command!r} phải gợi ý, got {decision.action}"


def test_roff_local_escape_hatch():
    decision = hook.classify("ROFF_LOCAL=1 npm test")
    assert decision.action == "allow"


def test_roff_local_escape_hatch_in_chain():
    decision = hook.classify("cd /tmp && ROFF_LOCAL=1 npm test")
    assert decision.action == "allow"


# --------------------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------------------
def test_config_add_makes_command_heavy(isolated_config):
    isolated_config.write_text("[heavy]\nadd = gradle\n")
    assert hook.classify("gradle assemble").action == "block"
    assert hook.classify("npm test").action == "block"  # mặc định vẫn chặn


def test_config_allow_exempts_command(isolated_config):
    isolated_config.write_text("[heavy]\nallow = npm test\n")
    assert hook.classify("npm test").action == "allow"
    assert hook.classify("npm run build").action == "block"


def test_config_allow_multi_word(isolated_config):
    isolated_config.write_text("[heavy]\nallow = make -n\n")
    assert hook.classify("make -n build").action == "allow"


def test_config_duplicate_keys_are_read(isolated_config):
    isolated_config.write_text("[heavy]\nadd = gradle\nadd = cargo\n")
    assert hook.classify("gradle assemble").action == "block"
    assert hook.classify("cargo build").action == "block"


def test_broken_config_fails_open(isolated_config):
    isolated_config.write_text("đây không phải ini [[[\n")
    assert hook.classify("npm test").action == "block"  # vẫn dùng danh sách mặc định
    assert hook.classify("ls").action == "allow"


def test_no_config_file_uses_defaults(isolated_config):
    assert not isolated_config.exists()
    assert hook.classify("npm test").action == "block"
    assert hook.classify("ls").action == "allow"


# --------------------------------------------------------------------------------------
# Hành vi hook (stdin/stdout/exit code)
# --------------------------------------------------------------------------------------
def test_non_bash_tool_passes_silently():
    rc, out, err = run_hook({"tool_name": "Read", "tool_input": {"file_path": "/tmp/x"}})
    assert rc == 0
    assert out == ""
    assert err == ""


def test_invalid_json_passes():
    rc, out, err = run_hook(None, raw="{không phải json")
    assert rc == 0
    assert err == ""


def test_missing_command_passes():
    rc, out, err = run_hook({"tool_name": "Bash", "tool_input": {}})
    assert rc == 0
    assert err == ""


def test_heavy_command_blocks_with_exit_2_and_stderr():
    rc, out, err = run_hook(
        {"tool_name": "Bash", "tool_input": {"command": "npm test"}}
    )
    assert rc == 2
    assert "roff do" in err
    assert "npm test" in err
    assert "ROFF_LOCAL=1" in err


def test_light_command_exits_0_without_output():
    rc, out, err = run_hook({"tool_name": "Bash", "tool_input": {"command": "ls"}})
    assert rc == 0
    assert out == ""
    assert err == ""


def test_interactive_command_exits_0_with_hint():
    rc, out, err = run_hook(
        {"tool_name": "Bash", "tool_input": {"command": "npm run dev"}}
    )
    assert rc == 0
    assert "roff tunnel" in err
    assert "roff do" in err


def test_hook_does_not_import_roffkit():
    source = open(HOOK_PATH, encoding="utf-8").read()
    assert not re.search(r"^\s*(?:from|import)\s+roffkit", source, re.M), (
        "hook phải chạy độc lập, không import roffkit"
    )


def test_hook_module_never_prints_on_its_own(capsys):
    hook.classify("npm test")
    hook.classify("ls")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


@pytest.mark.parametrize(
    "command",
    [
        "sudo npm test",
        "sudo -u node npm run build",
        "env NODE_ENV=production npm test",
        "sudo env FOO=1 pytest",
    ],
)
def test_system_wrappers_do_not_hide_heavy_commands(command):
    assert hook.classify(command).action == "block"


# --------------------------------------------------------------------------------------
# Regression: lệnh nặng bọc trong shell không được lọt qua hook
#
# Đo thật trên máy người dùng (23/11/2026): `bash -lc "pytest"` bị hook **cho
# qua** vì danh sách nặng khớp trên *tên lệnh đầu tiên* (`bash`), và cách tách
# lệnh cũ cắt ở `&&` **bên trong** dấu nháy. Hai lỗi cộng lại ⇒ lệnh nặng chạy
# thẳng trên máy local — đúng cái điều skill sinh ra để tránh.
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "command",
    [
        'sh -c "npm test"',
        'bash -c "pytest"',
        'bash -lc "npm test"',
        'sh -lc "npm test"',
        "bash -lc 'cd /tmp && pytest -q'",
        'bash -lc "cd x && npm test"',
        'bash -lc "pytest -q && npm run build"',
    ],
)
def test_shell_wrapped_heavy_command_is_blocked(command):
    assert hook.classify(command).action == "block", (
        f"lệnh nặng bọc trong shell phải bị chặn: {command!r}")


@pytest.mark.parametrize(
    "command",
    [
        'bash -lc "ls -la"',
        'sh -c "cat file.txt"',
        'bash -lc "echo hi | grep h"',
        'bash -c "true"',
    ],
)
def test_shell_wrapped_light_command_is_allowed(command):
    assert hook.classify(command).action == "allow", (
        f"lệnh nhẹ bọc trong shell không được chặn: {command!r}")


def test_roff_local_escape_works_through_shell_wrapper():
    assert hook.classify('ROFF_LOCAL=1 bash -lc "npm test"').action == "allow"


def test_quoted_separator_inside_shell_script_is_not_split():
    """`&&` trong dấu nháy là một phần, không phải hai phần."""
    parts = hook._split_top_level("bash -lc 'cd x && npm test'")
    assert len(parts) == 1, parts


def test_nested_shell_scripts_do_not_recurse_forever():
    decision = hook.classify('bash -lc "bash -lc \'bash -lc \\"ls\\"\'"')
    assert decision.action in ("allow", "block", "suggest")


# --------------------------------------------------------------------------------------
# Ngôn ngữ khác: dotnet / swift / bazel / ninja / meson / sbt / mix / deno / php
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "command,group",
    [
        ("python -m unittest", "test"),
        ("python3 -m unittest discover", "test"),
        ("python3.11 -m unittest", "test"),
        ("python -m build", "build"),
        ("python3 -m build", "build"),
    ],
)
def test_python_module_command_is_unwrapped_then_classified(command, group):
    """`python -m <module>` bóc wrapper còn lại tên module → phân loại theo module."""
    decision = hook.classify(command)
    assert decision.action == "block", f"{command!r} phải bị chặn, got {decision.action}"
    assert decision.group == group


@pytest.mark.parametrize(
    "command",
    [
        "python -m unittest tests/test_a.py",
        "phpunit tests/FooTest.php",
        "deno test tests/a_test.ts",
        "mix test tests/foo_test.exs",
        "swift test --filter MyTestCase",
        "php artisan test --filter FooTest",
        "sbt 'testOnly com.foo.BarTest'",
    ],
)
def test_new_test_runner_scoped_run_is_allowed(command):
    """Lệnh giới hạn vào file/test cụ thể thì không chặn cứng."""
    assert hook.classify(command).action == "allow", command


@pytest.mark.parametrize(
    "command",
    [
        "dotnet --version",
        "dotnet build --help",
        "swift --version",
        "bazel --version",
        "ninja -n",
        "ninja -t targets",
        "sbt --version",
        "mix --version",
        "deno --version",
        "phpunit --version",
        "meson --version",
    ],
)
def test_info_flags_are_never_blocked(command):
    assert hook.classify(command).action == "allow", command


@pytest.mark.parametrize(
    "command",
    [
        "dotnet watch",
        "dotnet run",
        "mix phx.server",
        "php artisan serve",
        "swift run",
        "bazel run //app:app",
    ],
)
def test_dev_servers_of_new_ecosystems_are_only_suggested(command):
    decision = hook.classify(command)
    assert decision.action == "suggest", f"{command!r} phải gợi ý, got {decision.action}"


def test_escape_hatch_applies_to_new_commands():
    assert hook.classify("ROFF_LOCAL=1 dotnet build").action == "allow"
    assert hook.classify("ROFF_LOCAL=1 python -m unittest").action == "allow"


def test_config_still_wins_for_new_commands(isolated_config):
    isolated_config.write_text("[heavy]\nallow = dotnet build\n")
    assert hook.classify("dotnet build").action == "allow"
    assert hook.classify("dotnet publish").action == "block"


def test_config_add_blocks_new_command(isolated_config):
    isolated_config.write_text("[heavy]\nadd = bazel build\n")
    assert hook.classify("bazel build //...").action == "block"
