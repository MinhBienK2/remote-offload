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
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

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

_VALUE_OPTIONS = frozenset({"hostname", "port", "user", "proxycommand",
                            "identityfile", "proxyjump"})
"""Option mà module này giữ lại (mọi option khác bị bỏ qua).

``identityfile``/``proxyjump`` thêm vào ở W5c: chúng quyết định **kết nối tới
đâu** (qua cổng nào, bằng key nào, qua máy trung gian nào) ⇒ phải nằm trong
fingerprint của alias."""

#: Option quyết định "kết nối tới đâu" — dùng để tính fingerprint (B14).
CONNECTION_KEYS: tuple[str, ...] = (
    "hostname",
    "port",
    "user",
    "identityfile",
    "proxycommand",
    "proxyjump",
)
"""Những option của ``ssh_config`` quyết định **kết nối tới máy nào**."""

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
    identity_file: str | None = None
    """``IdentityFile`` (nhiều dòng ⇒ nối bằng ``,``) — W5c."""
    proxy_jump: str | None = None
    """``ProxyJump`` — W5c."""


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


def _user_config_dir() -> str:
    """Thư mục gốc cho ``Include`` **tương đối** — ``$HOME/.ssh``.

    Luật của OpenSSH: file config *người dùng* (ta luôn truyền bằng ``-F``)
    coi đường dẫn không tuyệt đối là nằm trong ``~/.ssh`` — đo bằng ``strace``:
    ``Include inc.conf`` với ``-F /tmp/x/config`` ⇒ ssh mở ``$HOME/.ssh/inc.conf``,
    **không** phải ``/tmp/x/inc.conf``.

    Không dùng ``dirname(file config)``: file config có thể nằm ngoài
    ``$HOME/.ssh`` (vd ``$ROFF_SSH_CONFIG=/tmp/x.conf``) ⇒ lệch với ssh ⇒ roff
    thấy host mà ssh **không** thấy ⇒ fingerprint lệch ⇒ có thể nối nhầm máy
    (B25 ở mức *nội dung* config).
    """
    return os.path.join(os.path.expanduser("~"), ".ssh")


def _expand_include(pattern: str, base_dir: str) -> list[str]:
    """Mở rộng một pattern của directive ``Include`` thành danh sách file.

    ``~`` mở rộng theo ``$HOME`` — **đúng như OpenSSH đang làm trên máy này**
    (đo bằng ``strace``: với ``HOME=/tmp/x``, ``Include ~/inc.conf`` ⇒ ssh mở
    ``/tmp/x/inc.conf``). Lưu ý: luật này **khác** với luật của
    ``IdentityFile ~`` (ssh dùng *passwd home* cho nó) — nhưng fleet giữ
    nguyên chuỗi gốc của ``IdentityFile`` trong fingerprint ⇒ không lệch.

    Đường dẫn **tương đối** resolve theo ``base_dir`` (= ``$HOME/.ssh``, xem
    :func:`parse_ssh_config`) — cũng theo đúng luật của ssh.
    """
    pat = os.path.expanduser(pattern)
    if not os.path.isabs(pat):
        pat = os.path.join(base_dir, pat)
    if not glob.has_magic(pat):
        return [pat] if os.path.isfile(pat) else []
    return [match for match in sorted(glob.glob(pat)) if os.path.isfile(match)]


def _add_option(options: dict[str, Any], key: str, args: str) -> None:
    """Ghi 1 option vào khối (giá trị đầu tiên thắng).

    ``IdentityFile`` được phép lặp nhiều lần ⇒ gom thành **list**.
    """
    if key == "identityfile":
        values = options.get(key)
        if not isinstance(values, list):
            values = [] if values is None else [values]
            options[key] = values
        if args not in values:
            values.append(args)
        return
    options.setdefault(key, args)


def _parse_file(
    path: str,
    base_dir: str,
    blocks: list[_Block],
    visited: set[str],
    preamble: dict[str, Any],
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
                _add_option(preamble, key, args)
            else:
                _add_option(current.options, key, args)
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
    # Thư mục gốc cho ``Include`` tương đối — xem :func:`_user_config_dir`.
    base_dir = _user_config_dir()

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
    preamble: dict[str, Any] = {}
    _parse_file(path, base_dir, blocks, set(), preamble)
    return _merge_blocks(blocks, preamble)


def _merge_blocks(blocks: list[_Block], preamble: dict[str, Any]) -> list[SshHost]:
    """Gộp các block thành list :class:`SshHost` (giá trị đầu tiên thắng)."""
    defaults: dict[str, Any] = dict(preamble)
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
        identity = options.get("identityfile")
        if isinstance(identity, list):
            identity = ",".join(str(item) for item in identity) or None
        hosts.append(
            SshHost(
                alias=alias,
                hostname=options.get("hostname"),
                port=_parse_port(options.get("port")),
                user=options.get("user"),
                proxy_command=options.get("proxycommand"),
                identity_file=identity,
                proxy_jump=options.get("proxyjump"),
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


# --------------------------------------------------------------------------
# W5c — fingerprint tham số ssh (B14: đổi config ⇒ nói chuyện với nhầm máy)
# --------------------------------------------------------------------------
def ssh_config_file() -> str:
    """File ssh config mà roff dùng — **nguồn sự thật duy nhất** (B25).

    Ưu tiên ``$ROFF_SSH_CONFIG`` (``~`` mở rộng theo ``$HOME``, thành đường dẫn
    tuyệt đối); không có thì ``~/.ssh/config`` **theo ``$HOME``**.

    Luôn trả về một **đường dẫn** (kể cả khi file không tồn tại) vì
    fingerprint (B14) phải ổn định: đổi file ⇒ đổi fingerprint ⇒ đổi socket
    ControlMaster ⇒ không tái dùng master của config cũ.

    Xem :func:`ssh_config_path` cho phần "đường dẫn nào phải truyền cho ssh".
    """
    env = os.environ.get("ROFF_SSH_CONFIG")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return os.path.join(os.path.expanduser("~"), ".ssh", "config")


def ssh_config_path() -> str:
    """Đường dẫn ssh-config phải truyền cho ssh bằng ``-F`` (rỗng nếu không có).

    Nguồn sự thật duy nhất cho **cả hai** vế của B25: phía roff (fleet đọc
    file này để fingerprint) và phía ssh (đọc đúng file đó qua ``-F``).

    Vì sao phải truyền ``-F`` **tường minh**:

    * OpenSSH mở rộng ``~`` theo **passwd entry** (``getpwuid``), **KHÔNG**
      theo ``$HOME`` ⇒ ssh mặc định đọc ``~/.ssh/config`` của *passwd home*,
      trong khi :func:`ssh_config_file` đọc ``$HOME/.ssh/config``.
    * Hai file khác nhau ⇒ ssh đọc **file A** còn roff fingerprint **file B**
      ⇒ đổi config **không** đổi fingerprint ⇒ roff tái dùng ControlMaster cũ
      ⇒ nói chuyện với **máy cũ** (đúng bug B14 mà B25 chặn).

    Quy tắc tồn tại:

    * ``$ROFF_SSH_CONFIG`` được set ⇒ **luôn** trả đường dẫn đó, kể cả khi
      file không tồn tại: ``ssh -F <file thiếu>`` chết với *"Can't open user
      config file"* ⇒ **thất bại rõ ràng**, an toàn hơn lặng lẽ rơi về file
      khác (đó chính là lớp bug B25).
    * Không có env ⇒ trả ``$HOME/.ssh/config`` nếu file tồn tại, ngược lại
      ``""`` (không có file config nào ⇒ không truyền ``-F``, để ssh tự lo).
    """
    path = ssh_config_file()
    if os.environ.get("ROFF_SSH_CONFIG"):
        return path
    return path if os.path.isfile(path) else ""


def ssh_config_args() -> list[str]:
    """Tham số ``-F <path>`` cho ssh (rỗng nếu không có config nào).

    Nguồn sự thật duy nhất cho **mọi** lệnh ssh của roff: mọi nơi dựng argv
    ssh đều phải đưa qua đây — không được dựng tay rồi bỏ qua ``-F``, vì ssh
    đọc ``~/.ssh/config`` theo *passwd home* chứ không theo ``$HOME``
    (xem :func:`ssh_config_path`).
    """
    path = ssh_config_path()
    return ["-F", path] if path else []


def _block_matches(block: _Block, alias: str) -> bool:
    """Block ``Host ...`` này có áp cho ``alias`` không?

    Theo luật của ssh: token phủ định (``!foo``) loại alias ra khỏi block,
    token dương (``*``, ``muse-*``) chọn alias vào.
    """
    positive = [tok for tok in block.raw_aliases if not tok.startswith("!")]
    negative = [tok[1:] for tok in block.raw_aliases if tok.startswith("!")]
    for token in negative:
        if fnmatch.fnmatch(alias, token):
            return False
    if not positive:
        return False
    return any(fnmatch.fnmatch(alias, token) for token in positive)


def ssh_host_params(alias: str, path: str | None = None) -> dict:
    """Tham số ssh **đã resolve** cho một alias — đọc file config, KHÔNG gọi ssh.

    Trả dict với khoá: ``hostname``, ``port``, ``user``, ``identityfile``
    (list), ``proxycommand``, ``proxyjump``. Giá trị rỗng = không khai báo ⇒
    ssh tự dùng mặc định của nó.

    Resolve theo đúng thứ tự file: option lấy được **đầu tiên** thắng, block
    ``Host *`` và block có pattern (``Host muse-*``) cũng áp cho alias.
    """
    file_path = os.path.abspath(os.path.expanduser(path or ssh_config_file()))
    blocks: list[_Block] = []
    preamble: dict[str, Any] = {}
    # Cùng luật ``Include`` với :func:`parse_ssh_config` — nếu hai bên lệch,
    # fingerprint tính theo nội dung khác với ssh ⇒ nối nhầm máy (B25).
    _parse_file(file_path, _user_config_dir(), blocks, set(), preamble)

    raw: dict[str, Any] = {}
    identity: list[str] = []

    def take(key: str, value: Any) -> None:
        if key == "identityfile":
            items = value if isinstance(value, list) else [value]
            for item in items:
                if str(item) not in identity:
                    identity.append(str(item))
        elif key not in raw:
            raw[key] = value

    for key, value in preamble.items():
        take(key, value)
    for block in blocks:
        if not _block_matches(block, alias):
            continue
        for key, value in block.options.items():
            take(key, value)

    port = raw.get("port")
    try:
        port_value = int(str(port).strip())
    except (TypeError, ValueError):
        port_value = DEFAULT_PORT
    return {
        "hostname": str(raw.get("hostname")) if raw.get("hostname") else None,
        "port": port_value,
        "user": str(raw.get("user")) if raw.get("user") else None,
        "identityfile": identity,
        "proxycommand": str(raw.get("proxycommand")) if raw.get("proxycommand") else None,
        "proxyjump": str(raw.get("proxyjump")) if raw.get("proxyjump") else None,
    }


def ssh_fingerprint(alias: str, path: str | None = None) -> str:
    """Fingerprint (8 hex) của tham số ssh đã resolve — dùng cho ControlPath.

    **B14**: ``control_path`` trước đây khoá socket ControlMaster theo *alias*
    ⇒ đổi ``~/.ssh/config`` (đổi port, thay máy) vẫn tái dùng master của máy
    cũ ⇒ roff nói chuyện với **nhầm máy**. Fingerprint tính từ **file config
    local** (không gọi ssh ⇒ không tốn round-trip).

    Đổi config ⇒ fingerprint đổi ⇒ socket khác ⇒ master mới.
    """
    file_path = os.path.abspath(os.path.expanduser(path or ssh_config_file()))
    params = ssh_host_params(alias, file_path)
    payload = json.dumps(
        {"config": file_path, "alias": alias, "params": params},
        sort_keys=True, ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8]
