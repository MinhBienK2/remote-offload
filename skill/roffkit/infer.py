"""Suy ra tool mà 1 câu lệnh cần — **chỉ đọc văn bản lệnh, KHÔNG gọi ssh**.

Kết quả dùng để **ưu tiên host** (mềm), KHÔNG dùng để *cài* tool: suy ra có
thể sai, nên host thiếu tool **không** bị loại — xem
:func:`roffkit.policy.select` (tham số ``inferred_needs``).

Quy tắc (cố ý giữ **đơn giản**, có thể đoán sai):

1. Token đầu tiên của câu lệnh (sau khi bỏ ``env VAR=…``, ``cd …``, ``time``,
   ``nice``, ``ionice``, ``xargs``, ``sudo``, ``command``, ``exec``) là tool
   cần.
2. Tên tool ở đầu **mỗi nhánh** lệnh con (``&&``, ``||``, ``;``, ``|``) —
   vd ``make -j4 && pytest -q`` ⇒ ``["make", "pytest"]``.
3. Tên được **chuẩn hoá** qua bảng :data:`ALIASES` (``npm`` ⇒ ``node`` …).
4. Token mà **không** phải tool (builtin/kw của shell) ⇒ **không** suy ra gì
   — không đoán bừa.
5. Lệnh dạng ``path/to/tool`` (``./scripts/build.sh``) ⇒ **bỏ qua** (không
   biết nó là tool gì ⇒ coi như không có).
6. Từ khoá shell (``if then else fi …``) ⇒ bỏ qua.
7. ``sh -c 'cargo build'`` ⇒ **đọc vào trong**: kết quả ``["cargo"]``.
8. Lệnh **chắc chắn không phải job nặng** (``sleep``, ``date``, ``pwd`` …) ⇒
   **không** suy ra gì (xem :data:`TRIVIAL_COMMANDS`).

Vì sao token lạ vẫn được coi là tool (mục 1 vs mục 4): bảng :data:`ALIASES`
chỉ **chuẩn hoá tên**, KHÔNG phải danh sách tool hữu hạn — ``make``,
``pytest``, ``ffmpeg`` đều là tool thật mà không có trong bảng. Vì vậy mọi
token đầu lệnh đều có thể là 1 tool; chỉ có **builtin/kw của shell** mới bị
loại (mục 4). Tool suy ra được mà **không host nào có** ⇒ coi như "không
rõ" (không ưu tiên, không loại) — không gọi ssh thêm để kiểm tra.
"""

from __future__ import annotations

import re
import shlex

__all__ = [
    "ALIASES",
    "NOT_TOOLS",
    "SHELL_KEYWORDS",
    "SHELL_BUILTINS",
    "SHELLS",
    "TRIVIAL_COMMANDS",
    "canonical_tool",
    "infer_tools",
    "tool_family",
]


# --------------------------------------------------------------------------
# bảng chuẩn hoá tên
# --------------------------------------------------------------------------
ALIASES: dict[str, str] = {
    "nodejs": "node",
    "npm": "node",
    "yarn": "node",
    "pnpm": "node",
    "python": "python3",
    "pip": "pip3",
    "cargo": "cargo",
    "go": "go",
    "dotnet": "dotnet",
    "convert": "imagemagick",
    "magick": "imagemagick",
    "mvn": "maven",
    "rustc": "cargo",
}
"""Bảng ``alias → tool chuẩn``.

Chỉ **chuẩn hoá tên**, KHÔNG phải danh sách tool hữu hạn: ``pytest``,
``make``, ``ffmpeg`` … không có trong bảng nhưng vẫn là tool thật (mục 1).
"""

#: Tool chuẩn của 1 tên (tên không có trong bảng ⇒ giữ nguyên).
def canonical_tool(name: str) -> str:
    """Chuẩn hoá tên tool qua :data:`ALIASES` (không có trong bảng ⇒ giữ nguyên)."""
    return ALIASES.get(name, name)


def tool_family(name: str) -> tuple[str, ...]:
    """Các tên khác nhau của **cùng 1 tool** (chuẩn + alias trỏ tới nó).

    Dùng để hỏi "host này có tool ``maven`` không?": probe đo được ``mvn``
    chứ không phải ``maven``, nên phải hỏi **mọi** tên trong họ.
    """
    canonical = canonical_tool(name)
    family = [canonical]
    for alias, target in ALIASES.items():
        if target == canonical and alias not in family:
            family.append(alias)
    return tuple(family)


# --------------------------------------------------------------------------
# token KHÔNG phải tool
# --------------------------------------------------------------------------
SHELL_KEYWORDS: frozenset[str] = frozenset(
    {
        "if", "then", "else", "elif", "fi", "for", "while", "until", "do",
        "done", "case", "esac", "function", "return", "export", "local",
        "break", "continue", "select", "in",
    }
)
"""Từ khoá shell (mục 6) — không phải tool."""

SHELL_BUILTINS: frozenset[str] = frozenset(
    {
        ".", ":", "alias", "bg", "bind", "break", "builtin", "cd", "command",
        "compgen", "complete", "compopt", "continue", "declare", "dirs",
        "disown", "echo", "enable", "eval", "exec", "exit", "export", "false",
        "fc", "fg", "getopts", "hash", "help", "history", "jobs", "kill",
        "let", "local", "logout", "mapfile", "popd", "printf", "pushd", "pwd",
        "read", "readarray", "readonly", "return", "set", "shift", "shopt",
        "source", "suspend", "test", "times", "trap", "true", "type",
        "typeset", "ulimit", "umask", "unalias", "unset", "wait", "whence",
    }
)
"""Builtin của shell — có sẵn ở **mọi** máy ⇒ coi như "không cần tool".

``echo`` nằm ở đây nên ``echo hi`` ⇒ ``[]``.
"""

SHELLS: frozenset[str] = frozenset(
    {"sh", "bash", "zsh", "dash", "ksh", "mksh", "ash", "fish", "csh", "tcsh"}
)
"""Tên shell — không phải "tool cần", trừ khi đi kèm ``-c`` (mục 7)."""

#: Lệnh **chắc chắn không phải job nặng** (W8c) — không suy ra tool.
#:
#: Chúng tốn thời gian **không đáng kể** ⇒ suy ra chúng chỉ làm **nhiễu**:
#: ``roff do --cmd 'sleep 120; cp a b'`` ⇒ lý do "không có tool suy ra từ
#: lệnh: sleep, cp" lặp ở mọi host mà **không giúp ích ai**.
#:
#: Cố ý **KHÔNG** gồm ``cat``/``cp``/``mv``/``rm``/``tar``/``sort``/``grep``
#: … — chúng **có** thể nặng khi dữ liệu lớn.
TRIVIAL_COMMANDS: frozenset[str] = frozenset(
    {
        "sleep", "wait", "true", "false", "exit", "pwd", "whoami", "id",
        "date", "hostname", "basename", "dirname", "printf",
    }
)
"""Lệnh tầm thường — **không** phải job nặng ⇒ không suy ra tool (W8c).

``echo`` đã là builtin (:data:`SHELL_BUILTINS`) ⇒ đã loại sẵn từ trước.
"""

NOT_TOOLS: frozenset[str] = (
    SHELL_KEYWORDS | SHELL_BUILTINS | SHELLS | TRIVIAL_COMMANDS
)
"""Mọi token **không** phải tool (mục 4 + 6 + W8c)."""


# --------------------------------------------------------------------------
# tiền tố lệnh (bỏ qua để tìm token đầu)
# --------------------------------------------------------------------------
#: Lệnh tiền tố: đứng trước lệnh thật ⇒ bỏ qua, xem tiếp token sau nó.
_PREFIX_COMMANDS: frozenset[str] = frozenset(
    {"env", "cd", "time", "nice", "ionice", "xargs", "sudo", "command", "exec"}
)

#: Cờ của tiền tố **có** giá trị đi kèm (``nice -n 10``, ``sudo -u root`` …).
_PREFIX_VALUE_FLAGS: dict[str, frozenset[str]] = {
    "env": frozenset(),
    "cd": frozenset(),
    "time": frozenset({"-o", "-f", "-p"}),
    "nice": frozenset({"-n"}),
    "ionice": frozenset({"-c", "-n"}),
    "xargs": frozenset({"-n", "-I", "-P", "-d", "-s", "-E", "-a", "-L"}),
    "sudo": frozenset({"-u", "-g", "-p", "-C", "-h", "-r", "-t"}),
    "command": frozenset(),
    "exec": frozenset(),
}

#: Token toán tử ⇒ hết 1 nhánh lệnh con.
_OPERATORS: frozenset[str] = frozenset({"&&", "||", ";;", ";", "|", "|&", "&"})

#: Mở nhóm ( ``(`` ``{`` ) — bỏ qua khi tìm token đầu nhánh.
_OPENERS: frozenset[str] = frozenset({"(", "{", "!"})

#: Sâu tối đa khi đọc lệnh bên trong ``sh -c '…'`` (chống lặp vô hạn).
_MAX_DEPTH = 3

_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
"""Token dạng ``VAR=VALUE`` (gán biến) — không phải tên tool."""

_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.+-]*$")
"""Tên hợp lệ của 1 tool (không có ``/``, không phải cờ, không phải ``VAR=VAL``)."""


# --------------------------------------------------------------------------
# token hoá
# --------------------------------------------------------------------------
def _tokenize(text: str) -> list[str]:
    """Tách lệnh thành token (nhận biết toán tử ``&&``, ``|``, ``;`` …).

    Dùng :mod:`shlex` nên dấu nháy được giữ đúng (``sh -c 'cargo build'`` ⇒
    token ``"cargo build"``). Lệnh hỏng (nháy không cân) ⇒ ``[]`` (không đoán).
    """
    try:
        lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        return list(lexer)
    except ValueError:
        return []


def _split_branches(tokens: list[str]) -> list[list[str]]:
    """Chia token thành các nhánh lệnh con (theo ``&&``, ``||``, ``;``, ``|``)."""
    branches: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token in _OPERATORS:
            branches.append(current)
            current = []
            continue
        current.append(token)
    branches.append(current)
    return [b for b in branches if b]


def _is_assignment(token: str) -> bool:
    """Token có dạng ``VAR=VALUE`` không?"""
    return bool(_ASSIGN_RE.match(token))


def _strip_prefixes(tokens: list[str]) -> list[str]:
    """Bỏ tiền tố lệnh (``env VAR=…``, ``cd …``, ``sudo``, ``nice -n 10`` …).

    Lặp: ``sudo nice ffmpeg`` ⇒ bỏ cả ``sudo`` lẫn ``nice``.
    """
    index = 0
    while index < len(tokens):
        head = tokens[index]
        if head not in _PREFIX_COMMANDS:
            break
        index += 1
        if head == "cd":
            # `cd <thư mục>` — bỏ luôn thư mục.
            if index < len(tokens):
                index += 1
            continue
        if head == "env":
            # `env [-i] [VAR=VAL …] <lệnh>` — bỏ cờ và VAR=VAL.
            while index < len(tokens) and (
                tokens[index].startswith("-") or _is_assignment(tokens[index])
            ):
                index += 1
            continue
        value_flags = _PREFIX_VALUE_FLAGS.get(head, frozenset())
        while index < len(tokens) and tokens[index].startswith("-"):
            flag = tokens[index]
            index += 1
            if flag in value_flags and index < len(tokens):
                index += 1
        continue
    return tokens[index:]


def _skip_assignments(tokens: list[str]) -> list[str]:
    """Bỏ token ``VAR=VALUE`` ở đầu nhánh (``CC=gcc make`` ⇒ ``make``).

    Cùng ý với ``env VAR=…`` của mục 1: gán biến không phải tool.
    """
    index = 0
    while index < len(tokens) and _is_assignment(tokens[index]):
        index += 1
    return tokens[index:]


def _shell_inner(tokens: list[str]) -> str | None:
    """Nội dung lệnh của ``<shell> -c '<lệnh>'`` (mục 7), hoặc ``None``.

    ``None`` ⇒ không phải dạng ``-c`` ⇒ shell đó **không** phải tool.
    """
    if not tokens or tokens[0] not in SHELLS:
        return None
    for index in range(1, len(tokens)):
        token = tokens[index]
        if not token.startswith("-"):
            return None
        if "c" in token[1:]:
            if index + 1 < len(tokens):
                return tokens[index + 1]
            return ""
    return None


def _tool_name(token: str) -> str | None:
    """Tên tool của token đầu nhánh, hoặc ``None`` (không suy ra gì)."""
    if not token:
        return None
    if token in NOT_TOOLS:
        return None
    if "/" in token:
        # `path/to/tool` (vd `./scripts/build.sh`) — không biết là tool gì.
        return None
    if token.startswith("-") or _is_assignment(token):
        return None
    if not _NAME_RE.match(token):
        return None
    if not any(ch.isalpha() for ch in token):
        # Toàn số ("123") ⇒ không phải tên tool.
        return None
    return canonical_tool(token)


# --------------------------------------------------------------------------
# API chính
# --------------------------------------------------------------------------
def infer_tools(cmd: str) -> list[str]:
    """Tool mà câu lệnh cần — suy ra từ văn bản lệnh.

    Trả về danh sách tool **đã chuẩn hoá** (tên như ``ffmpeg``, ``node``,
    ``cargo``), không trùng, thứ tự ổn định (theo thứ tự xuất hiện).

    Hàm này **không** gọi ssh, **không** đọc profile ⇒ chạy được trong đường
    chạy nóng (W6c). Kết quả chỉ dùng để *ưu tiên* host, không dùng để *cài*
    tool.

    Mục 7 (hành vi **duy nhất**, không trộn lẫn): ``sh -c 'cargo build'`` ⇒
    **đọc vào trong** ⇒ ``["cargo"]``. Lệnh ``sh script.sh`` (không có ``-c``)
    ⇒ ``[]`` (không đoán).

    Examples:
        >>> infer_tools("ffmpeg -i in.mp4 out.mp4")
        ['ffmpeg']
        >>> infer_tools("npm run build && npm test")
        ['node']
        >>> infer_tools("cd /tmp && python3 -m pytest")
        ['python3']
        >>> infer_tools("echo hi")
        []
        >>> infer_tools("./scripts/build.sh")
        []
        >>> infer_tools("sh -c 'cargo build'")
        ['cargo']
    """
    return _infer(cmd, depth=0)


def _infer(cmd: str, depth: int) -> list[str]:
    """Bản thân của :func:`infer_tools` (có tham số đệ quy cho ``sh -c``)."""
    if not cmd or not str(cmd).strip():
        return []
    if depth > _MAX_DEPTH:
        return []
    tokens = _tokenize(str(cmd))
    if not tokens:
        return []

    found: list[str] = []
    for branch in _split_branches(tokens):
        found.extend(_infer_branch(branch, depth))

    # Bỏ trùng, giữ thứ tự xuất hiện.
    seen: set[str] = set()
    out: list[str] = []
    for name in found:
        if name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def _infer_branch(tokens: list[str], depth: int) -> list[str]:
    """Tool của 1 nhánh lệnh con (đã bỏ tiền tố)."""
    index = 0
    while index < len(tokens) and tokens[index] in _OPENERS:
        index += 1
    rest = _skip_assignments(tokens[index:])
    rest = _strip_prefixes(rest)
    rest = _skip_assignments(rest)
    if not rest:
        return []

    # Mục 7: `<shell> -c '<lệnh>'` ⇒ đọc lệnh bên trong.
    inner = _shell_inner(rest)
    if inner is not None:
        return _infer(inner, depth + 1)

    name = _tool_name(rest[0])
    return [name] if name else []
