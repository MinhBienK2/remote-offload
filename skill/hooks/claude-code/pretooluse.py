#!/usr/bin/env python3
"""Hook `PreToolUse` của Claude Code — chặn lệnh nặng chạy ở máy local.

Cách chạy (Claude Code gọi tự động, matcher `Bash`):

    python3 <skill-dir>/hooks/claude-code/pretooluse.py   < hook_input.json

Input (stdin, JSON): ``{"tool_name": "Bash", "tool_input": {"command": "npm test"}}``.

Quy tắc (xem `docs/DESIGN.md` §5.11):

* ``tool_name`` khác ``Bash`` → cho qua, im lặng.
* Lệnh có ``ROFF_LOCAL=1`` → cho qua (cách thoát của người dùng).
* Lệnh khớp danh sách **nặng** mặc định (media / build / test / ml) →
  **chặn**, trả hướng dẫn dùng ``roff do``. Danh sách phủ nhiều hệ sinh thái:
  npm/python, go/rust, java, .NET, Swift, Bazel, Elixir, PHP, Deno…
* Lệnh có file/tham số cụ thể (``pytest path::test_x``, ``vitest run file``)
  → cho qua.
* Lệnh tương tác / cần server (``npm run dev``, ``vite``, ``http.server``) →
  **không chặn cứng**, chỉ gợi ý ``roff do`` + ``roff tunnel``.

**Không bao giờ tự viết lại lệnh** — thiết kế đã loại phương án rewrite vì dễ
làm sai ngữ nghĩa. Hook chỉ nói "chạy cái này ở máy khác", agent tự quyết.

Module này **không import roffkit** (hook phải chạy độc lập, không phụ thuộc
package chưa merge) và chỉ stdlib.
"""

from __future__ import annotations

import configparser
import json
import os
import re
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

HOOK_EVENT = "PreToolUse"
EXIT_ALLOW = 0
EXIT_BLOCK = 2

#: Ngưỡng file lớn để coi ``ffprobe`` là việc nặng (MB).
HEAVY_FILE_MB = 50

#: Lệnh chỉ đọc — không bao giờ chặn.
READONLY = {
    "ls", "cat", "head", "tail", "less", "more", "grep", "egrep", "fgrep", "rg",
    "ag", "ack", "find", "fd", "tree", "wc", "pwd", "echo", "printf", "which",
    "whereis", "type", "file", "stat", "du", "df", "basename", "dirname",
    "realpath", "readlink", "env", "printenv", "id", "whoami", "uname", "date",
    "cal", "jq", "yq", "diff", "cmp", "md5sum", "sha1sum", "sha256sum", "sort",
    "uniq", "cut", "tr", "column", "nl", "comm", "join", "paste", "awk", "sed",
    "man", "help", "ps", "pgrep", "history", "true", "false", "seq", "sleep",
    "clear", "git",
}

#: Script npm/pnpm/yarn/bun coi là nặng.
HEAVY_SCRIPTS = {
    "build", "compile", "bundle", "rebuild", "test", "tests", "e2e", "bench",
    "benchmark", "dist", "release",
}

#: Script npm/pnpm/yarn/bun coi là tương tác (server/dev server) — chỉ gợi ý.
INTERACTIVE_SCRIPTS = {
    "dev", "start", "serve", "watch", "preview", "storybook", "storybook:dev",
}

#: Cờ chỉ in thông tin / chạy thử — **không bao giờ chặn** (quy ước sẵn có:
#: ``mvn -v``, ``gradle -v``, ``blender -v``, ``make -n``… đều được cho qua).
INFO_FLAGS = {
    "--help", "-h", "--version", "-v", "-V", "--dry-run", "-n",
    "--just-print", "--recon",
}

#: Cờ lọc phạm vi test — chỉ chạy một phần nhỏ nên không chặn cứng.
FILTER_FLAGS = {"--grep", "-g", "--filter", "--tags", "--tag"}

#: Wrapper: `npx vitest run` → `vitest run`.
WRAPPER_SINGLE = {"npx", "bunx", "uvx", "pnpx"}
WRAPPER_PAIRS = {
    ("pnpm", "exec"),
    ("pnpm", "dlx"),
    ("yarn", "dlx"),
    ("uv", "run"),
    ("poetry", "run"),
    ("pipenv", "run"),
    ("bun", "x"),
}
#: Cờ của wrapper không mang ý nghĩa cho việc phân loại.
WRAPPER_FLAGS = {"--yes", "-y", "--no", "--quiet", "-q", "--silent", "--no-install"}

IMAGE_EXT = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".tif", ".tiff", ".bmp", ".svg",
    ".heic", ".avif",
)
FILE_EXT = (
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".rb", ".php", ".java",
    ".go", ".rs", ".c", ".cpp", ".sh",
)

_SPLIT_RE = re.compile(r"\|\||&&|\||;|\n")
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
#: ``python``, ``python3``, ``python3.11``… — đều là wrapper của ``python -m <module>``.
_PYTHON_RE = re.compile(r"^python(\d+(?:\.\d+)?)?$")


# --------------------------------------------------------------------------------------
# Kiểu dữ liệu
# --------------------------------------------------------------------------------------
@dataclass
class HeavyConfig:
    """Phần `[heavy]` của ``~/.config/roff/config.ini``."""

    add: list[str] = field(default_factory=list)
    allow: list[str] = field(default_factory=list)


@dataclass
class Decision:
    """Kết quả phân loại một lệnh."""

    action: str  # "allow" | "suggest" | "block"
    group: str = ""
    reason: str = ""
    command: str = ""

    @property
    def blocked(self) -> bool:
        return self.action == "block"


_RANK = {"allow": 0, "suggest": 1, "block": 2}


# --------------------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------------------
def config_path() -> str | None:
    """Đường dẫn config (ưu tiên ``$ROFF_CONFIG``, rồi ``~/.config/roff/config.ini``)."""
    env = os.environ.get("ROFF_CONFIG")
    if env:
        return env
    default = Path.home() / ".config" / "roff" / "config.ini"
    return str(default) if default.exists() else None


def _as_list(value) -> list[str]:
    """configparser trả về str hoặc list (khoá lặp) → list[str] sạch."""
    if value is None:
        return []
    if isinstance(value, str):
        items = value.splitlines()
    else:
        items = [str(v) for v in value]
    out: list[str] = []
    for item in items:
        item = item.strip()
        if not item or item.startswith("#") or item.startswith(";"):
            continue
        out.append(item)
    return out


def load_config(path: str | None = None) -> HeavyConfig:
    """Đọc ``[heavy] add`` / ``[heavy] allow``.

    Config hỏng **không được** chặn agent → fail-open (trả config rỗng).
    """
    target = path if path is not None else config_path()
    if not target or not os.path.exists(target):
        return HeavyConfig()
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    try:
        parser.read(target, encoding="utf-8")
    except (configparser.Error, OSError, UnicodeDecodeError):
        return HeavyConfig()
    if not parser.has_section("heavy"):
        return HeavyConfig()
    section = parser["heavy"]
    return HeavyConfig(
        add=_as_list(section.get("add")),
        allow=_as_list(section.get("allow")),
    )


# --------------------------------------------------------------------------------------
# Bóc tách lệnh
# --------------------------------------------------------------------------------------
def _split_top_level(raw: str) -> list[str]:
    """Tách lệnh ở ``&&``/``||``/``|``/``;``/newline — **không** cắt trong dấu nháy.

    ``bash -lc 'cd x && npm test'`` phải là **một** phần, không phải hai — nếu
    cắt sai thì phần ``npm test"`` mang theo dấu nháy và không còn khớp danh sách
    nặng ⇒ lệnh nặng lọt qua hook.
    """
    parts: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if quote:
            buf.append(ch)
            if ch == quote and not (quote == '"' and i and raw[i - 1] == "\\"):
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            buf.append(ch)
            buf.append(raw[i + 1])
            i += 2
            continue
        two = raw[i:i + 2]
        if two in ("&&", "||"):
            parts.append("".join(buf))
            buf = []
            i += 2
            continue
        if ch in ("|", ";", "\n"):
            parts.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return [p for p in parts if p.strip()]


def _segments(raw: str) -> list[list[str]]:
    """Tách lệnh dài thành các phần (theo ``&&``, ``||``, ``|``, ``;``, newline)."""
    segments: list[list[str]] = []
    for part in _split_top_level(raw):
        try:
            tokens = shlex.split(part, posix=True)
        except ValueError:
            tokens = part.split()
        if tokens:
            segments.append(tokens)
    return segments or [raw.split()]


def _strip_env(tokens: list[str]) -> list[str]:
    """Bỏ tiền tố ``VAR=value`` ở đầu lệnh."""
    out = list(tokens)
    while out and _ENV_ASSIGN.match(out[0]):
        out.pop(0)
    return out


def _strip_wrappers(tokens: list[str]) -> list[str]:
    """Bỏ wrapper: ``npx``, ``uv run``, ``pnpm exec``, ``python -m``, ``sudo``, ``env``…"""
    out = list(tokens)
    changed = True
    while changed and out:
        changed = False
        while out and out[0] in WRAPPER_FLAGS:
            out.pop(0)
            changed = True
        if not out:
            break
        if out[0] in WRAPPER_SINGLE:
            out.pop(0)
            changed = True
            continue
        if out[0] == "sudo":
            out.pop(0)
            # `sudo -u user cmd` / `sudo -E cmd` — bỏ cờ, và cả giá trị của nó.
            while out and out[0].startswith("-"):
                flag = out.pop(0)
                if flag in ("-u", "-g", "-p", "-C") and out:
                    out.pop(0)
            changed = True
            continue
        if out[0] == "env":
            out.pop(0)
            while out and _ENV_ASSIGN.match(out[0]):
                out.pop(0)
            changed = True
            continue
        if len(out) >= 2 and (out[0], out[1]) in WRAPPER_PAIRS:
            out = out[2:]
            changed = True
            continue
        if len(out) >= 3 and _PYTHON_RE.match(out[0]) and out[1] == "-m":
            out = out[2:]
            changed = True
            continue
    return out


def _is_file_arg(token: str) -> bool:
    """Token có giống đường dẫn file cụ thể không?"""
    if not token or token.startswith("-"):
        return False
    if "/" in token:
        return True
    return token.lower().endswith(FILE_EXT)


def _scoped(args: Iterable[str]) -> bool:
    """Lệnh có giới hạn vào file/test cụ thể không (→ không chặn cứng)."""
    for arg in args:
        if arg.startswith("-"):
            continue
        if "::" in arg:
            return True
        if arg.endswith("/"):
            continue  # thư mục → vẫn là cả suite
        if _is_file_arg(arg):
            return True
    return False


def _has_info_flag(args: Iterable[str]) -> bool:
    """Lệnh có cờ chỉ-in-thông-tin / chạy-thử không (``--help``, ``-v``, ``-n``…)?"""
    return any(a in INFO_FLAGS for a in args)


def _has_filter_flag(args: Iterable[str]) -> bool:
    """Lệnh có cờ lọc phạm vi test không (``--filter``, ``--grep``…)?"""
    return any(a in FILTER_FLAGS or a.startswith("--filter=") for a in args)


def _sbt_commands(args: Iterable[str]) -> set[str]:
    """Lệnh sbt trong tham số (``sbt 'compile test'`` → ``{"compile", "test"}``)."""
    out: set[str] = set()
    for arg in args:
        for part in re.split(r"[;\s]+", arg):
            part = part.strip().lower()
            if part:
                out.add(part)
    return out


def _matches_config(tokens: list[str], entries: Iterable[str]) -> bool:
    """Token đầu lệnh có khớp một entry trong config không (prefix, theo từ)."""
    for entry in entries:
        try:
            entry_tokens = shlex.split(entry)
        except ValueError:
            entry_tokens = entry.split()
        if not entry_tokens:
            continue
        if tokens[: len(entry_tokens)] == entry_tokens:
            return True
    return False


# --------------------------------------------------------------------------------------
# Danh sách lệnh nặng
# --------------------------------------------------------------------------------------
def _pm_script(base: str, args: list[str]) -> str | None:
    """Tên script của npm/pnpm/yarn/bun (``npm run build`` → ``build``)."""
    if not args:
        return None
    sub = args[0]
    if sub == "run" and len(args) > 1:
        return args[1]
    if base == "npm" and sub == "start":
        return "start"
    if base == "yarn" and sub in ("build", "start"):
        return sub
    if base == "bun" and sub == "start":
        return "start"
    return None


def _node_pm(base: str, args: list[str]) -> tuple[str, str] | None:
    """npm/pnpm/yarn/bun — lệnh nặng?"""
    if not args:
        return None
    sub = args[0]
    if sub in ("test", "t"):
        return ("test", f"{base} test")
    if sub in ("ci", "install", "i", "add", "update", "up"):
        return ("build", f"{base} {sub} — cài dependency")
    script = _pm_script(base, args)
    if script:
        name = script.lower()
        if name in HEAVY_SCRIPTS or name.startswith("build:") or name.startswith("test:"):
            return ("build", f"{base} run {script}")
    return None


def _pip(base: str, args: list[str]) -> tuple[str, str] | None:
    """pip install — nặng khi cài theo file hoặc nhiều gói."""
    if not args or args[0] not in ("install", "download"):
        return None
    packages = [a for a in args[1:] if not a.startswith("-")]
    if any(p.lower().endswith((".txt", ".in", ".lock")) for p in packages):
        return ("build", f"{base} {args[0]} theo file")
    if len(packages) >= 3:
        return ("build", f"{base} {args[0]} nhiều gói")
    if any(a in ("-U", "--upgrade") for a in args) and len(packages) >= 2:
        return ("build", f"{base} {args[0]} --upgrade")
    return None


def _ffmpeg_writes_output(tokens: list[str]) -> bool:
    """ffmpeg có ghi file ra không (``ffmpeg -i x.mp4`` chỉ là probe)."""
    positional = [t for t in tokens[1:] if not t.startswith("-")]
    return len(positional) >= 2


def _has_large_file(args: Iterable[str]) -> bool:
    """Có tham số nào là file lớn không?"""
    limit = HEAVY_FILE_MB * 1024 * 1024
    for arg in args:
        if arg.startswith("-"):
            continue
        try:
            if os.path.isfile(arg) and os.path.getsize(arg) >= limit:
                return True
        except OSError:
            continue
    return False


def _convert_is_image_work(args: list[str]) -> bool:
    """``convert`` của ImageMagick — có phải xử lý ảnh hàng loạt không?"""
    if any(a in ("-resize", "-thumbnail", "-crop", "-append", "-morph", "-composite")
           for a in args):
        return True
    return any(not a.startswith("-") and a.lower().endswith(IMAGE_EXT) for a in args)


def _python_is_training(args: Iterable[str]) -> bool:
    """``python train*.py`` — script huấn luyện."""
    for arg in args:
        if arg.startswith("-"):
            continue
        name = os.path.basename(arg)
        if not name.endswith(".py"):
            continue
        stem = name.lower()
        if stem.startswith(("train", "pretrain", "finetune", "distill", "fit")):
            return True
    return False


def _heavy(tokens: list[str]) -> tuple[str, str] | None:
    """Lệnh có nặng không → ``(nhóm, lý do)`` hoặc ``None``."""
    prog = tokens[0]
    args = tokens[1:]
    base = os.path.basename(prog)

    # --- media ---
    if base == "ffmpeg":
        if _ffmpeg_writes_output(tokens):
            return ("media", "ffmpeg encode")
        return None
    if base == "ffprobe":
        if _has_large_file(args):
            return ("media", "ffprobe trên file lớn")
        return None
    if base == "blender":
        if any(a in args for a in ("--version", "-v", "--help")):
            return None
        return ("media", "blender render")
    if base == "magick":
        if args and args[0] in ("identify", "-version", "version", "--version"):
            return None
        return ("media", "magick xử lý ảnh hàng loạt")
    if base == "convert":
        if _convert_is_image_work(args):
            return ("media", "convert xử lý ảnh hàng loạt")
        return None
    if base == "sox":
        if any(a in args for a in ("--version", "-h", "--help")):
            return None
        return ("media", "sox xử lý audio")

    # --- build ---
    if base == "cargo":
        if args and args[0] in ("build", "test", "bench", "check", "clippy",
                                "install", "publish", "fetch"):
            return ("build", f"cargo {args[0]}")
        return None
    if base == "go":
        if args and args[0] in ("build", "test"):
            return ("build", f"go {args[0]}")
        return None
    if base in ("mvn", "mvnw", "gradle", "gradlew"):
        if any(a in args for a in ("--version", "-v", "-h", "--help", "tasks", "help")):
            return None
        return ("build", f"{base} build toàn dự án")
    if base == "make":
        if any(a in args for a in ("-n", "--dry-run", "--just-print", "--recon")):
            return None
        return ("build", "make build toàn dự án")
    if base == "cmake":
        if "--build" in args:
            return ("build", "cmake --build")
        return None
    if base in ("npm", "pnpm", "yarn", "bun"):
        return _node_pm(base, args)
    if base == "tsc":
        if any(_is_file_arg(a) for a in args):
            return None
        return ("build", "tsc toàn project")
    if base == "next":
        if args and args[0] == "build":
            return ("build", "next build")
        return None
    if base == "vite":
        if args and args[0] == "build":
            return ("build", "vite build")
        return None
    if base in ("webpack", "rollup", "esbuild", "parcel"):
        return ("build", f"{base} build")
    if base in ("pip", "pip3", "pipx"):
        return _pip(base, args)
    if base == "docker":
        if args and args[0] in ("build", "buildx"):
            return ("build", "docker build")
        return None
    if base == "dotnet" and args and args[0] in ("build", "publish", "restore", "msbuild"):
        if _has_info_flag(args):
            return None
        return ("build", f"dotnet {args[0]}")
    if base == "swift" and args and args[0] == "build":
        if _has_info_flag(args):
            return None
        return ("build", "swift build")
    if base in ("bazel", "bazelisk") and args and args[0] == "build":
        if _has_info_flag(args):
            return None
        return ("build", f"{base} build")
    if base == "ninja":
        if _has_info_flag(args):
            return None
        if "-t" in args:
            return None  # chế độ truy vấn: `ninja -t targets`
        if _scoped(args):
            return None  # `ninja src/main.o` — chỉ một target
        return ("build", "ninja build toàn dự án")
    if base == "meson" and args and args[0] == "compile":
        if _has_info_flag(args):
            return None
        return ("build", "meson compile")
    if base == "sbt":
        # sbt vừa build vừa test → xử lý một chỗ, nhóm trả về theo lệnh.
        if _has_info_flag(args):
            return None
        cmds = _sbt_commands(args)
        if "testonly" in cmds:
            return None  # `sbt "testOnly com.foo.BarTest"` — một test class
        if "test" in cmds:
            return ("test", "sbt test")
        if cmds & {"compile", "package", "assembly"}:
            return ("build", "sbt compile")
        return None
    if base == "mix" and args and args[0] == "compile":
        if _has_info_flag(args):
            return None
        return ("build", "mix compile")
    if base == "deno" and args and args[0] == "compile":
        if _has_info_flag(args):
            return None
        return ("build", "deno compile")
    if base == "build":
        # `python -m build` — frontend PEP 517 (đã bóc wrapper `python -m`).
        if _has_info_flag(args):
            return None
        return ("build", "python -m build (PEP 517)")

    # --- test ---
    if base == "pytest":
        if _scoped(args):
            return None
        return ("test", "pytest chạy cả suite")
    if base == "jest":
        if _scoped(args):
            return None
        return ("test", "jest chạy cả suite")
    if base == "vitest":
        if _scoped(args):
            return None
        return ("test", "vitest chạy cả suite")
    if base == "playwright":
        if args and args[0] == "test":
            if any(a in args for a in ("--grep", "-g")):
                return None
            return ("test", "playwright test — khởi động browser")
        return None
    if base == "cypress":
        if args and args[0] == "run":
            return ("test", "cypress run — khởi động browser")
        return None
    if base in ("tox", "nox"):
        return ("test", f"{base} dựng env rồi chạy test")
    if base == "rspec":
        if _scoped(args):
            return None
        return ("test", "rspec chạy cả suite")
    if base == "unittest":
        # `python -m unittest` / `python -m unittest discover`.
        if _has_info_flag(args):
            return None
        if _scoped(args):
            return None
        return ("test", "unittest discover chạy cả suite")
    if base == "dotnet" and args and args[0] == "test":
        if _scoped(args[1:]) or _has_filter_flag(args[1:]):
            return None
        return ("test", "dotnet test chạy cả suite")
    if base == "swift" and args and args[0] == "test":
        if _scoped(args[1:]) or _has_filter_flag(args[1:]):
            return None
        return ("test", "swift test chạy cả suite")
    if base in ("bazel", "bazelisk") and args and args[0] == "test":
        # Nhãn bazel luôn chứa `/` ⇒ không dùng `_scoped` ở đây.
        if _has_info_flag(args):
            return None
        return ("test", f"{base} test chạy cả suite")
    if base == "meson" and args and args[0] == "test":
        if _has_info_flag(args):
            return None
        return ("test", "meson test chạy cả suite")
    if base == "mix" and args and args[0] == "test":
        if _scoped(args[1:]) or _has_filter_flag(args[1:]):
            return None
        return ("test", "mix test chạy cả suite")
    if base == "deno" and args and args[0] == "test":
        if _scoped(args[1:]) or _has_filter_flag(args[1:]):
            return None
        return ("test", "deno test chạy cả suite")
    if base == "deno" and args and args[0] == "task" and len(args) > 1:
        name = args[1].lower()
        if name in ("test", "tests", "e2e", "bench", "benchmark"):
            return ("test", f"deno task {name}")
        if name in HEAVY_SCRIPTS:
            return ("build", f"deno task {name}")
        return None
    if base == "phpunit":
        if _has_info_flag(args):
            return None
        if _scoped(args) or _has_filter_flag(args):
            return None
        return ("test", "phpunit chạy cả suite")
    if base == "php" and args and args[0] == "artisan" and len(args) > 1 \
            and args[1] == "test":
        if _scoped(args[2:]) or _has_filter_flag(args[2:]):
            return None
        return ("test", "php artisan test chạy cả suite")

    # --- ml ---
    if base in ("torchrun", "deepspeed", "horovodrun"):
        return ("ml", f"{base} phân tán train")
    if base == "accelerate":
        if args and args[0] == "launch":
            return ("ml", "accelerate launch train")
        return None
    if base == "torch.distributed.run":
        return ("ml", "torch.distributed.run train")
    if base in ("python", "python3"):
        if _python_is_training(args):
            return ("ml", "script huấn luyện")
    return None


def _interactive(tokens: list[str]) -> str | None:
    """Lệnh tương tác / cần server → chỉ gợi ý, không chặn cứng."""
    prog = tokens[0]
    args = tokens[1:]
    base = os.path.basename(prog)

    script = _pm_script(base, args)
    if script and script.lower() in INTERACTIVE_SCRIPTS:
        return "server / dev server — needs a live process"

    if base == "vite":
        return "dev server — needs a live process"
    if base == "next" and args and args[0] == "dev":
        return "dev server — needs a live process"
    if base == "nuxt" and args and args[0] == "dev":
        return "dev server — needs a live process"
    if base in ("http.server", "SimpleHTTPServer"):
        return "static http server — needs a live process"
    if base in ("jupyter", "jupyter-lab"):
        return "jupyter — needs a GUI"
    if base == "flask" and args and args[0] == "run":
        return "server — needs a live process"
    if base in ("uvicorn", "gunicorn", "hypercorn"):
        return f"{base} — server, needs a live process"
    if base == "rails" and args and args[0] in ("s", "server"):
        return "rails server — needs a live process"
    if base == "php" and args and args[0] == "-S":
        return "php built-in server — needs a live process"
    if base in ("serve", "http-server", "live-server"):
        return "static file server — needs a live process"
    if base == "cargo" and args and args[0] == "run":
        return "long-running process — needs a live process"
    if base == "dotnet" and args and args[0] in ("watch", "run"):
        return "dotnet app — needs a live process"
    if base == "swift" and args and args[0] == "run":
        return "swift app — needs a live process"
    if base in ("bazel", "bazelisk") and args and args[0] == "run":
        return "bazel app — needs a live process"
    if base == "mix" and args and args[0] == "phx.server":
        return "dev server — needs a live process"
    if base == "php" and args and args[0] == "artisan" and len(args) > 1 \
            and args[1] == "serve":
        return "dev server — needs a live process"
    if base == "sbt" and not args:
        return "sbt shell — needs a live process"
    if base == "docker" and args and args[0] == "compose" and "up" in args:
        return "long-running containers — needs a live process"
    if base == "docker" and args and args[0] == "run":
        return "long-running container — needs a live process"
    return None


# --------------------------------------------------------------------------------------
# Phân loại
# --------------------------------------------------------------------------------------
def _classify_segment(tokens: list[str], raw: str, cfg: HeavyConfig,
                     _depth: int = 0) -> Decision:
    tokens = _strip_env(tokens)
    if not tokens:
        return Decision("allow", command=raw)

    # 1. Config allow — miễn trừ tường minh, thắng mọi thứ.
    if _matches_config(tokens, cfg.allow):
        return Decision("allow", group="config-allow", reason="[heavy] allow", command=raw)

    # 2. Config add — lệnh nặng do người dùng tự khai.
    if _matches_config(tokens, cfg.add):
        return Decision("block", group="config-add", reason="[heavy] add", command=raw)

    stripped = _strip_wrappers(tokens)

    if _matches_config(stripped, cfg.allow):
        return Decision("allow", group="config-allow", reason="[heavy] allow", command=raw)
    if _matches_config(stripped, cfg.add):
        return Decision("block", group="config-add", reason="[heavy] add", command=raw)

    # 2b. Lệnh bọc trong shell: `bash -lc '…'`, `sh -c "…"`.
    # Phải phân loại **bên trong** script, nếu không `bash -lc "pytest"` lọt qua.
    script = _shell_script(tokens)
    if script:
        inner = classify(script, cfg, _depth=_depth + 1)
        if inner.action != "allow":
            return Decision(inner.action, group=inner.group,
                            reason=inner.reason, command=raw)
        return Decision("allow", command=raw)

    # 3. Lệnh chỉ đọc.
    if os.path.basename(stripped[0]) in READONLY:
        return Decision("allow", group="readonly", command=raw)

    # 4. Lệnh nặng.
    hit = _heavy(stripped)
    if hit:
        group, reason = hit
        return Decision("block", group=group, reason=reason, command=raw)

    # 5. Lệnh tương tác → chỉ gợi ý.
    hint = _interactive(stripped)
    if hint:
        return Decision("suggest", group="interactive", reason=hint, command=raw)

    return Decision("allow", command=raw)


#: Shell nào được coi là "bọc lệnh".
SHELL_WRAPPERS = {"sh", "bash", "zsh", "dash", "ksh", "ash", "busybox"}


def _shell_script(tokens: list[str]) -> str | None:
    """``sh -c '<script>'`` ⇒ trả về script bên trong; ``None`` nếu không phải.

    Nhận cả cụm cờ ngắn (``-c``, ``-lc``, ``-ic``, ``-euxc``…) vì
    ``bash -lc`` là cách agent hay dùng nhất.
    """
    if len(tokens) < 3:
        return None
    if tokens[0] not in SHELL_WRAPPERS:
        return None
    flag = tokens[1]
    if not flag.startswith("-") or "c" not in flag[1:]:
        return None
    if not all(ch in "liecxaup" for ch in flag[1:]):
        return None
    script = tokens[2]
    rest = tokens[3:]
    return " ".join([script, *rest]) if rest else script


def classify(command: str, cfg: HeavyConfig | None = None, _depth: int = 0) -> Decision:
    """Phân loại một lệnh Bash → :class:`Decision`.

    Lệnh dài (nhiều phần ``&&``/``;``/``|``) → lấy phần **nặng nhất**.
    """
    raw = (command or "").strip()
    if not raw:
        return Decision("allow", command=raw)
    if "ROFF_LOCAL=1" in raw:
        return Decision("allow", group="escape", reason="ROFF_LOCAL=1", command=raw)
    if _depth >= 3:
        # Chống lồng vô hạn: script lồng script lồng script…
        return Decision("allow", command=raw)
    config = cfg if cfg is not None else load_config()
    best = Decision("allow", command=raw)
    for segment in _segments(raw):
        decision = _classify_segment(segment, raw, config, _depth=_depth)
        if _RANK[decision.action] > _RANK[best.action]:
            best = decision
    return best


# --------------------------------------------------------------------------------------
# Thông điệp trả về cho agent
# --------------------------------------------------------------------------------------
def _quoted(command: str) -> str:
    return "'" + command.replace("'", "'\\''") + "'"


def block_message(decision: Decision) -> str:
    """Thông điệp gửi cho agent khi lệnh bị chặn."""
    lines = [
        "[remote-offload] Heavy command blocked — do not run it on this machine.",
        "",
        f"Command: {decision.command}",
        f"Group: {decision.group} ({decision.reason})",
        "",
        "Run it on a remote host instead:",
        f"  roff do --cmd {_quoted(decision.command)}",
        "Add --in <dir> for the files the command needs, and --out <dir> for artifacts.",
        "",
        "If `roff do` exits 10 (no usable host), report that and ask the user —",
        "do not quietly run it here.",
        "",
        "To run it locally on purpose (you own the load):",
        f"  ROFF_LOCAL=1 {decision.command}",
    ]
    return "\n".join(lines) + "\n"


def suggest_message(decision: Decision) -> str:
    """Thông điệp gợi ý (không chặn) cho lệnh tương tác."""
    lines = [
        f"[remote-offload] Hint: `{decision.command}` — {decision.reason}.",
        "To run it on a remote host:",
        f"  roff do --cmd {_quoted(decision.command)}",
        "To reach a service running there from this machine:",
        "  roff tunnel start <host> --lport <L> --rport <R>",
    ]
    return "\n".join(lines) + "\n"


def _json_deny(decision: Decision) -> dict:
    """Dạng hook output JSON (Claude Code mới hỗ trợ `permissionDecision`)."""
    return {
        "hookSpecificOutput": {
            "hookEventName": HOOK_EVENT,
            "permissionDecision": "deny",
            "permissionDecisionReason": block_message(decision).strip(),
        }
    }


# --------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------
#: Chuẩn bị thêm ``bash`` — đây là tool **shell** của Claude Code.
SHELL_TOOL_NAMES = {
    "bash",
    "shell",
    "localshell",
    "local_shell",
    "execcommand",
    "exec_command",
}

#: Biến môi trường để thêm tên tool shell của runtime khác (phân tách dấu phẩy).
TOOLS_ENV = "ROFF_HOOK_TOOLS"


def _is_shell_tool(name: str) -> bool:
    """Tool này có phải tool **chạy lệnh shell** không?

    Claude Code tên tool là ``Bash``. Codex (CLI 0.142) dùng tên dạng
    ``shell`` / ``local_shell`` / ``exec_command`` — cùng giao thức hook
    (``tool_name`` + ``tool_input.command`` + ``hookSpecificOutput``), nên
    **cùng một hook** dùng được cho cả hai.

    Tên tool lạ chưa biết thì thêm bằng ``$ROFF_HOOK_TOOLS``.
    """
    lowered = (name or "").strip().lower()
    if not lowered:
        return False
    if lowered in SHELL_TOOL_NAMES:
        return True
    extra = os.environ.get(TOOLS_ENV, "")
    if extra and lowered in {part.strip().lower() for part in extra.split(",") if part.strip()}:
        return True
    # Codex đặt tên tool shell theo mẫu chứa "shell" / "exec_command".
    if "shell" in lowered or "exec_command" in lowered or "execcommand" in lowered:
        return True
    return False


def main(argv: list[str] | None = None, stdin=None) -> int:
    """Điểm vào của hook.

    * exit 0 → cho qua (im lặng, hoặc chỉ gợi ý trên stderr);
    * exit 2 → chặn, lý do nằm trên stderr (Claude Code đưa stderr cho agent).

    Biến môi trường:

    * ``ROFF_HOOK_OFF=1`` — **tắt hook toàn cục** (cho qua im lặng, không chặn
      gì). Dùng khi muốn tự quyết định, hoặc khi đo "skill có tự bật được không
      mà không cần hook".
    * ``ROFF_HOOK_OUTPUT=json`` — Claude Code mới: trả JSON kèm quyết định
      deny thay vì exit 2.
    """
    if os.environ.get("ROFF_HOOK_OFF", "").strip().lower() not in ("", "0", "false"):
        return EXIT_ALLOW

    stream = stdin if stdin is not None else sys.stdin
    try:
        raw = stream.read()
    except (OSError, ValueError):
        return EXIT_ALLOW
    try:
        payload = json.loads(raw or "{}")
    except ValueError:
        return EXIT_ALLOW
    if not isinstance(payload, dict):
        return EXIT_ALLOW

    if not _is_shell_tool(str(payload.get("tool_name") or "")):
        return EXIT_ALLOW

    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if isinstance(command, list):
        # Codex truyền lệnh dạng **mảng argv**; ghép lại để phân loại được.
        command = " ".join(shlex.quote(str(part)) for part in command)
    if not isinstance(command, str) or not command.strip():
        return EXIT_ALLOW

    decision = classify(command)

    if decision.action == "block":
        if os.environ.get("ROFF_HOOK_OUTPUT", "").lower() == "json":
            # Claude Code mới: trả JSON + exit 0.
            sys.stdout.write(json.dumps(_json_deny(decision), ensure_ascii=False) + "\n")
            return EXIT_ALLOW
        sys.stderr.write(block_message(decision))
        return EXIT_BLOCK

    if decision.action == "suggest":
        sys.stderr.write(suggest_message(decision))

    return EXIT_ALLOW


if __name__ == "__main__":
    sys.exit(main())
