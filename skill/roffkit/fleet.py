"""Đọc ``~/.ssh/config``: liệt kê alias, theo ``Include``, bỏ qua từ khoá option.

Hai bẫy đã biết (bug v1) mà module này chặn:

* **R14** — ``~/.ssh/config`` có directive ``Include`` (glob, đệ quy): phải đọc
  cả các file include, nếu không sẽ bỏ sót alias.
* **R15** — từ khoá option (``HostName``, ``Port``, ``User``, ``ProxyCommand``,
  ``IdentityFile``…) **không phải** tên host: parser cũ nhầm ``HostName`` thành
  ``Host`` + ``Name`` ⇒ sinh ra host giả. Ở đây parser theo cấu trúc: chỉ dòng
  ``Host <pattern>...`` mới tạo host.

Module này không in gì ra stdout.
"""

from __future__ import annotations

import fnmatch
import glob
import os
import re
from dataclasses import dataclass, field

try:  # tuỳ cách import: package `roffkit` hoặc module chạy rời
    from roffkit.config import Config
except ImportError:  # pragma: no cover
    from .config import Config

DEFAULT_PORT = 22
"""Port SSH mặc định khi block không khai báo ``Port``."""

OPTION_KEYWORDS = frozenset(
    {
        "addkeystoagent",
        "batchmode",
        "bindaddress",
        "canonicaldomains",
        "canonicalizehostname",
        "casignaturealgorithms",
        "certificatefile",
        "channeltimeout",
        "checkhostip",
        "ciphers",
        "clearallforwardings",
        "compression",
        "connecttimeout",
        "controlmaster",
        "controlpath",
        "controlpersist",
        "dynamicforward",
        "enableescapecommandline",
        "escapechar",
        "exitonforwardfailure",
        "forkafterauthentication",
        "forwardagent",
        "forwardx11",
        "forwardx11trusted",
        "gatewayports",
        "globalknownhostsfile",
        "gssapiauthentication",
        "hashknownhosts",
        "host",
        "hostname",
        "hostbasedauthentication",
        "hostkeyalgorithms",
        "hostkeyalias",
        "identityfile",
        "identitiesonly",
        "ignoreunknown",
        "include",
        "ipqos",
        "kbdinteractiveauthentication",
        "kbdinteractivedevices",
        "kexalgorithms",
        "knownhostscommand",
        "localcommand",
        "localforward",
        "loglevel",
        "macs",
        "nohostauthenticationforlocalhost",
        "numberofpasswordprompts",
        "passwordauthentication",
        "permitlocalcommand",
        "pkcs11provider",
        "port",
        "preferredauthentications",
        "proxycommand",
        "proxyjump",
        "proxyusefdpass",
        "pubkeyacceptedalgorithms",
        "pubkeyacceptedkeytypes",
        "pubkeyauthentication",
        "rekeylimit",
        "remotecommand",
        "remoteforward",
        "requesttty",
        "revokedhostkeys",
        "sendenv",
        "serveralivecountmax",
        "serveraliveinterval",
        "sessiontype",
        "setenv",
        "stricthostkeychecking",
        "tunnel",
        "tunneldevice",
        "updatehostkeys",
        "user",
        "userknownhostsfile",
        "verifyhostkeydns",
        "visualhostkey",
    }
)
"""Mọi từ khoá option của ``ssh_config`` — không bao giờ được hiểu là tên host."""

_VALUE_OPTIONS = frozenset({"hostname", "port", "user", "proxycommand"})
"""Option mà module này giữ lại (mọi option khác bị bỏ qua)."""

_INLINE_COMMENT_RE = re.compile(r"(?:^|\s)#")
"""Comment inline: ``#`` đứng đầu dòng hoặc sau khoảng trắng → cắt phần từ đó đi,
**giữ lại** phần trước (vd ``Host muse-a # note`` → ``Host muse-a``)."""


@dataclass
class SshHost:
    """Một alias trong ``~/.ssh/config``."""

    alias: str
    hostname: str | None = None
    port: int | None = DEFAULT_PORT
    user: str | None = None
    proxy_command: str | None = None


@dataclass
class _Block:
    """Một block ``Host ...`` đã đọc (giữ pattern thô + option đã thu)."""

    raw_aliases: list[str] = field(default_factory=list)
    options: dict[str, str] = field(default_factory=dict)


def _is_pattern(token: str) -> bool:
    """Token có chứa ký tự wildcard của ssh không (``*``, ``?``, ``[...]``)."""
    return any(ch in token for ch in "*?[")


def _concrete_aliases(raw_aliases: list[str]) -> list[str]:
    """Lọc pattern thô ra thành alias thật.

    Bỏ qua: token phủ định (``!foo``), catch-all ``*``, mọi pattern có wildcard
    (không phải host thật) và mọi từ khoá option.
    """
    out: list[str] = []
    for token in raw_aliases:
        if token.startswith("!"):
            continue
        if token == "*" or _is_pattern(token):
            continue
        if token.lower() in OPTION_KEYWORDS:
            continue
        if token not in out:
            out.append(token)
    return out


def _parse_port(value: str | None) -> int | None:
    """Port trong config (``None`` nếu khai báo sai)."""
    if value is None:
        return DEFAULT_PORT
    try:
        return int(value.strip())
    except ValueError:
        return None


def _expand_include(pattern: str, base_dir: str) -> list[str]:
    """Mở rộng một pattern của directive ``Include`` thành danh sách file."""
    pat = os.path.expanduser(pattern)
    if not os.path.isabs(pat):
        pat = os.path.join(base_dir, pat)
    if not glob.has_magic(pat):
        return [pat] if os.path.isfile(pat) else []
    return [match for match in sorted(glob.glob(pat)) if os.path.isfile(match)]


def _parse_file(
    path: str,
    base_dir: str,
    blocks: list[_Block],
    visited: set[str],
    preamble: dict[str, str],
) -> None:
    """Parse một file config (đệ quy theo ``Include``)."""
    real = os.path.realpath(path)
    if real in visited:
        return
    visited.add(real)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            content = handle.read()
    except OSError:
        return

    current: _Block | None = None
    for raw in content.splitlines():
        line = raw.strip()
        if not line:
            continue
        # comment inline: cắt từ '#' (đầu dòng hoặc sau khoảng trắng)
        comment = _INLINE_COMMENT_RE.search(line)
        if comment is not None:
            line = line[: comment.start()].strip()
        if not line:
            continue
        parts = line.split(None, 1)
        key = parts[0].lower()
        args = parts[1].strip() if len(parts) > 1 else ""

        if key == "host":
            current = _Block(raw_aliases=args.split(), options={})
            blocks.append(current)
        elif key == "include":
            for pattern in args.split():
                for included in _expand_include(pattern, base_dir):
                    _parse_file(included, base_dir, blocks, visited, preamble)
        elif key in _VALUE_OPTIONS:
            if not args:
                continue
            if current is None:
                # option nằm ngoài mọi block Host → áp cho mọi host
                preamble.setdefault(key, args)
            else:
                current.options.setdefault(key, args)
        # option khác: bỏ qua, nhưng KHÔNG bao giờ coi là tên host


def parse_ssh_config(path: str | None = None) -> list[SshHost]:
    """Đọc ``~/.ssh/config``, theo ``Include`` (dùng glob, đệ quy, chống lặp).

    Bỏ qua các từ khoá option (``HostName``, ``Port``, ``User``,
    ``ProxyCommand``, ``IdentityFile``…). Block ``Host a b`` sinh 2 alias;
    block ``Host *`` không sinh host nào (chỉ làm mặc định chung) nhưng option
    của nó được kế thừa cho các host khác.

    File không tồn tại → trả list rỗng.
    """
    if path is None:
        path = os.path.join(os.path.expanduser("~"), ".ssh", "config")
    path = os.path.expanduser(path)
    base_dir = os.path.dirname(os.path.abspath(path))

    # File **đọc lỗi** (không có quyền, là thư mục…) ⇒ ném lỗi thật thay vì
    # trả list rỗng: "không đọc được config" khác hẳn "config không có host
    # nào" (W3d) — nếu nhầm thì host gõ sai bị báo ``unknown_host`` khi thực
    # ra chỉ là lỗi cấu hình.
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                handle.read(1)
        except OSError:
            raise

    blocks: list[_Block] = []
    preamble: dict[str, str] = {}
    _parse_file(path, base_dir, blocks, set(), preamble)
    return _merge_blocks(blocks, preamble)


def _merge_blocks(blocks: list[_Block], preamble: dict[str, str]) -> list[SshHost]:
    """Gộp các block thành list :class:`SshHost` (giá trị đầu tiên thắng)."""
    defaults: dict[str, str] = dict(preamble)
    for block in blocks:
        if "*" in block.raw_aliases:
            for key, value in block.options.items():
                defaults.setdefault(key, value)

    alias_options: dict[str, dict[str, str]] = {}
    order: list[str] = []
    for block in blocks:
        for alias in _concrete_aliases(block.raw_aliases):
            merged = dict(defaults)
            merged.update(block.options)
            target = alias_options.setdefault(alias, {})
            for key, value in merged.items():
                target.setdefault(key, value)
            if alias not in order:
                order.append(alias)

    hosts: list[SshHost] = []
    for alias in order:
        options = alias_options[alias]
        hosts.append(
            SshHost(
                alias=alias,
                hostname=options.get("hostname"),
                port=_parse_port(options.get("port")),
                user=options.get("user"),
                proxy_command=options.get("proxycommand"),
            )
        )
    return hosts


def fleet_aliases(cfg: Config, aliases: list[str] | None = None) -> list[str]:
    """Lọc alias theo ``fleet_include``/``fleet_exclude`` (fnmatch, hỗ trợ ``muse-*``).

    ``aliases=None`` → đọc từ ``~/.ssh/config``. ``exclude`` thắng ``include``.

    Config **đọc lỗi** ⇒ fleet rỗng (không phải lỗi nội bộ) — người dùng sẽ
    thấy "fleet rỗng" thay vì traceback.
    """
    if aliases is None:
        try:
            aliases = [host.alias for host in parse_ssh_config()]
        except OSError:
            aliases = []
    includes = list(cfg.fleet_include) or ["*"]
    excludes = list(cfg.fleet_exclude)
    out: list[str] = []
    for alias in aliases:
        if not any(fnmatch.fnmatch(alias, pattern) for pattern in includes):
            continue
        if any(fnmatch.fnmatch(alias, pattern) for pattern in excludes):
            continue
        if alias in out:
            continue
        out.append(alias)
    return out


def env_fleet_aliases(cfg: Config) -> list[str]:
    """Alias của fleet theo ``$ROFF_SSH_CONFIG`` (seam cho test).

    ``cli`` và ``watcher`` phải dùng **cùng** danh sách alias: nếu watcher đọc
    ``~/.ssh/config`` còn ``roff do`` đọc file khác thì failover sang **host
    sai** (W3j).
    """
    ssh_config = os.environ.get("ROFF_SSH_CONFIG")
    if ssh_config:
        try:
            aliases = [host.alias for host in parse_ssh_config(ssh_config)]
        except OSError:
            aliases = []
        return fleet_aliases(cfg, aliases)
    return fleet_aliases(cfg)
