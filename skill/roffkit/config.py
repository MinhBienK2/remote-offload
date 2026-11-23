"""Đọc config INI của roff (``~/.config/roff/config.ini``).

Vì Python 3.10 **không có** ``tomllib``, config dùng INI (``configparser``).

Cấu trúc (xem docs/DESIGN.md §5.10 và docs/API-CONTRACT.md §3)::

    [fleet]
    include = muse-*
    include = qwen1-direct      # lặp lại được, cộng dồn
    exclude = colab-comfy
    probe_cache_s = 60
    max_parallel_transfers = 2  # chấp nhận cả ở [local]

    [local]
    max_parallel_transfers = 2
    nice = 19
    ionice = idle
    cache_max_mb = 5120
    cache_min_free_mb = 3072

    [remote]
    nice = 19
    ionice = idle

    [heavy]
    add = gradle
    allow = git

    [host.muse-pmb333]
    labels = ffmpeg, shared
    weight = 1
    slots = 1
    mem_budget_mb = 700
    nice = 19          # ghi đè [remote] nice cho riêng host này

Quy tắc:
  * không có file → :meth:`Config.load` trả config mặc định, ``path=None``;
  * file sai cú pháp INI (hoặc giá trị sai kiểu) → ``RoffError(EXIT_CONFIG)``;
  * khoá lặp lại trong cùng section (``include = ...`` nhiều dòng) được **cộng
    dồn** thành danh sách; giá trị cũng chấp nhận dạng CSV và dạng nhiều dòng;
  * **B27**: người dùng **chỉ định** một file config (qua ``ROFF_CONFIG``) mà
    file đó **không tồn tại** ⇒ ``strict=True`` ném ``RoffError(EXIT_CONFIG,
    "config_missing")``. Bỏ qua im lặng sẽ **nới rộng fleet** thành MỌI host
    trong ``~/.ssh/config`` (và làm mất luôn ``exclude`` của người dùng).

Module này cũng là **nguồn sự thật duy nhất** cho chính sách ``nice``/
``ionice`` (B34): :func:`local_nice_prefix` cho tiến trình local và
:func:`remote_nice_policy` cho lệnh job chạy trên máy remote (C9). Thứ tự
ưu tiên: **biến môi trường > config > mặc định**.
"""

from __future__ import annotations

import configparser
import os
import re
import shutil
from dataclasses import dataclass, field

try:  # tuỳ cách import: package `roffkit` hoặc module chạy rời
    from roffkit.errors import EXIT_CONFIG, RoffError
except ImportError:  # pragma: no cover
    from .errors import EXIT_CONFIG, RoffError

DEFAULT_CONFIG_PATH = os.path.join("~", ".config", "roff", "config.ini")

#: Cảnh báo ngắn (tiếng Việt) khi fleet **không** bị giới hạn (B27).
FLEET_UNRESTRICTED_WARNING = (
    "config không giới hạn fleet ⇒ mọi host trong ~/.ssh/config đều được chọn, "
    "gồm cả host bạn không muốn dùng"
)

_LIST_OPTION_KEYS = frozenset({"include", "exclude", "add", "allow", "labels"})

#: Regex option giống configparser (option / delimiter / value).
_OPT_RE = re.compile(r"^(?P<option>.*?)\s*(?P<vi>[=:])\s*(?P<value>.*)$")


@dataclass
class HostConfig:
    """Cấu hình riêng cho một alias host (section ``[host.<alias>]``)."""

    name: str
    labels: list[str] = field(default_factory=list)
    weight: float = 1.0
    slots: int = 1
    mem_budget_mb: int = 0
    """0 = dùng ``mem_available`` thật của host."""
    mem_reserve_mb: int = 256
    """RAM chừa lại cho dịch vụ production của chính host.

    Muse chạy postgres, telegram-bot, art-ex, cloudflared — tức dịch vụ của
    chính người dùng. RAM trống đo được lúc probe chỉ là khoảng trống tức thời;
    các dịch vụ này dao động. Reserve là khoảng chừa để job của roff không
    OOM-kill chúng.
    """
    nice: int | None = None
    """Ghi đè ``[remote] nice`` cho riêng host này (``None`` = không ghi đè).

    Chỉ áp cho **lệnh job chạy trên host** (C9) — không phải tiến trình
    local của roff.
    """
    ionice: str | None = None
    """Ghi đè ``[remote] ionice`` cho riêng host này (``None`` = không ghi đè)."""


@dataclass
class Config:
    """Toàn bộ config của roff."""

    path: str | None = None
    fleet_include: list[str] = field(default_factory=lambda: ["*"])
    fleet_exclude: list[str] = field(default_factory=list)
    probe_cache_s: int = 60
    max_parallel_transfers: int = 2
    nice: int = 19
    ionice: str = "idle"
    remote_nice: int = 19
    """Mức ``nice`` cho **lệnh job chạy trên máy remote** (C9)."""
    remote_ionice: str = "idle"
    """Lớp I/O cho **lệnh job chạy trên máy remote** (C9)."""
    cache_max_mb: int = 5120
    cache_min_free_mb: int = 3072
    heavy_add: list[str] = field(default_factory=list)
    heavy_allow: list[str] = field(default_factory=list)
    hosts: dict[str, HostConfig] = field(default_factory=dict)

    @staticmethod
    def load(path: str | None = None, *, strict: bool = False) -> "Config":
        """Nạp config từ ``path`` (INI).

        ``path=None`` → ``~/.config/roff/config.ini`` nếu tồn tại, nếu không thì
        trả :class:`Config` mặc định (``path=None``).

        File tồn tại nhưng sai cú pháp INI (hoặc giá trị sai kiểu) →
        ``raise RoffError(EXIT_CONFIG)`` với text lỗi rõ ràng.

        Args:
            path: đường dẫn file config; ``None`` ⇒ đường dẫn mặc định.
            strict: ``True`` ⇒ **file bị chỉ định rõ mà không tồn tại** thì
                ném ``RoffError(EXIT_CONFIG, "config_missing")``.

        Vì sao có ``strict`` (B27):

        * ``path=None`` (không chỉ định) ⇒ thiếu file **không** phải lỗi:
          đây là hành vi "cài xong chạy được ngay" ⇒ trả config mặc định.
        * ``path`` **tường minh** + ``strict=True`` + file không tồn tại ⇒
          **lỗi**. Người dùng đã chọn một config ⇒ bỏ qua im lặng là sai:
          config rỗng ⇒ ``fleet_include = ["*"]`` ⇒ fleet lấy **MỌI** host
          trong ``~/.ssh/config``, kể cả host ngoài ý muốn, và ``exclude``
          của người dùng bị bỏ qua.
        * ``strict=False`` (mặc định) ⇒ giữ hành vi cũ (config mặc định) —
          dùng cho các đường nội bộ đọc config *sau khi* job đã ghim host
          (watcher/transfer): ném lỗi ở đó sẽ làm watcher chết ⇒ job mất
          im lặng, tệ hơn hẳn việc dùng cấu hình mặc định.
        """
        explicit = path is not None
        if path is None:
            path = os.path.expanduser(DEFAULT_CONFIG_PATH)
        else:
            path = os.path.expanduser(path)
        if not os.path.isfile(path):
            if strict and explicit:
                raise RoffError(
                    EXIT_CONFIG,
                    "config_missing",
                    error=(
                        f"không có config tại {path} — ROFF_CONFIG trỏ tới file "
                        "không tồn tại; nếu bỏ qua, fleet sẽ lấy MỌI host trong "
                        "~/.ssh/config, gồm cả host bạn không muốn dùng"
                    ),
                    detail=f"config không tồn tại: {path}",
                    path=path,
                )
            return Config(path=None)

        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError as exc:
            raise RoffError(
                EXIT_CONFIG, error=f"không đọc được config {path}: {exc}"
            ) from exc

        parser = configparser.ConfigParser(
            allow_no_value=True,
            strict=False,
            interpolation=None,
        )
        try:
            parser.read_string(_merge_duplicate_list_options(text), source=path)
        except configparser.Error as exc:
            raise RoffError(
                EXIT_CONFIG, error=f"config sai cú pháp: {path}: {exc}"
            ) from exc

        return _build_config(parser, path)

    def host(self, name: str) -> HostConfig:
        """Trả về :class:`HostConfig` của alias, hoặc config mặc định nếu alias
        không có trong file config."""
        found = self.hosts.get(name)
        if found is None:
            return HostConfig(name=name)
        return found

    # ------------------------------------------------------------------
    # fleet có bị giới hạn không (B27)
    # ------------------------------------------------------------------
    def effective_include(self) -> list[str]:
        """Danh sách ``include`` **hiệu lực** (rỗng ⇒ ``["*"]``).

        Giống hệt cách :func:`roffkit.fleet.fleet_aliases` dùng: ``includes =
        list(cfg.fleet_include) or ["*"]``.
        """
        includes = [str(item).strip() for item in (self.fleet_include or [])]
        includes = [item for item in includes if item]
        return includes or ["*"]

    def fleet_unrestricted(self) -> bool:
        """``True`` khi include hiệu lực là ``["*"]`` ⇒ fleet **không giới hạn**.

        Khi đó :func:`roffkit.fleet.fleet_aliases` chọn **mọi** host trong
        ``~/.ssh/config`` — kể cả host người dùng không muốn dùng. Đây là
        trạng thái "fleet đang mở" (B27) và phải **thấy được bằng máy**
        (cờ JSON ``fleet_unrestricted`` + ``fleet_warning``).

        ``include = muse-*`` ⇒ **giới hạn** ⇒ ``False``.
        """
        return any(item == "*" for item in self.effective_include())

    def fleet_warning(self) -> str:
        """Cảnh báo ngắn (tiếng Việt) khi fleet không giới hạn (B27)."""
        text = FLEET_UNRESTRICTED_WARNING
        excludes = [
            str(item).strip() for item in (self.fleet_exclude or []) if str(item).strip()
        ]
        if excludes:
            text += " (trừ exclude: " + ", ".join(excludes) + ")"
        return text


# --------------------------------------------------------------------------
# Chính sách ``nice``/``ionice`` — MỘT nguồn sự thật (B34)
# --------------------------------------------------------------------------
#
# Trước B34 chính sách này bị **copy 5 lần** (probe/sshx/tunnel/job/transfer)
# với **3 nguồn sự thật** (hằng / env / config) ⇒ ``ROFF_NICE`` không có tác
# dụng với rsync/tar, còn sửa ``[local] nice`` trong ``config.ini`` cũng
# không đổi được gì ⇒ **nút cấu hình chết**. Mọi module giờ gọi
# :func:`local_nice_prefix` / :func:`remote_nice_policy` ở đây.

#: Mức ``nice`` mặc định cho tiến trình **local** của roff (thấp nhất).
DEFAULT_NICE = 19

#: Lớp ``ionice`` mặc định cho tiến trình **local** của roff.
DEFAULT_IONICE = "idle"

#: Tên lớp I/O → số lớp mà ``ionice -c`` hiểu.
#:
#: ``none`` **không** có trong bảng: nó có nghĩa "**không** bọc ``ionice``"
#: (giống giá trị rỗng) — không phải lớp số 0.
IONICE_CLASSES = {
    "idle": "3",
    "best-effort": "2",
    "be": "2",
    "realtime": "1",
}

#: Trần ``nice`` trên Linux (19 = thấp nhất).
NICE_MAX = 19

#: Dấu hiệu "biến có mặt nhưng **không** dùng" (khác với "không có biến").
_UNSET = object()


@dataclass(frozen=True)
class NicePolicy:
    """Chính sách ưu tiên CPU/I-O đã chốt — dùng chung cho mọi module (B34)."""

    nice: int = DEFAULT_NICE
    """Mức ``nice``; ``0`` ⇒ **không** bọc ``nice`` (ưu tiên cao, không hạ)."""
    ionice: str | None = None
    """Lớp I/O (``"3"``/``"2"``/``"1"``/``"0"``); ``None`` ⇒ **không** bọc ``ionice``."""

    def prefix(self) -> list[str]:
        """Tiền tố argv cho tiến trình con.

        Thứ tự: ``nice`` **trước** ``ionice`` — đúng dạng
        ``nice -n 19 ionice -c3`` mà tài liệu (và 3/5 module cũ: probe, job,
        transfer) dùng. **Thứ tự không phải hợp đồng**: `nice` và `ionice`
        đặt hai thuộc tính độc lập (CPU / I-O), cả hai đều được kế thừa qua
        fork+exec ⇒ đổi thứ tự **không** đổi hành vi. Trước B34, `sshx` và
        `tunnel` dùng thứ tự ngược lại ⇒ đó là **hệ quả của việc copy-paste
        chính sách, không phải ý định thiết kế**.
        """
        parts: list[str] = []
        if self.nice > 0 and shutil.which("nice"):
            parts += ["nice", "-n", str(self.nice)]
        if self.ionice and shutil.which("ionice"):
            parts += ["ionice", "-c" + str(self.ionice)]
        return parts


def load_config() -> Config | None:
    """Config hiện tại, hoặc ``None`` nếu **không đọc được**.

    Nguyên tắc B27: ở những đường *sau khi* job đã ghim host, đọc config
    **không được** làm chết tiến trình ⇒ ``strict=False`` + nuốt lỗi ⇒ dùng
    cấu hình mặc định an toàn. Xem :func:`roffkit.transfer._config`.
    """
    try:
        return Config.load(os.environ.get("ROFF_CONFIG") or None, strict=False)
    except Exception:
        return None


def ionice_class(value: str | None) -> str | None:
    """Tên lớp I/O → số lớp mà ``ionice -c`` hiểu.

    * rỗng / ``none`` ⇒ ``None`` ⇒ **không** bọc ``ionice``;
    * ``idle``/``best-effort``/``be``/``realtime`` ⇒ ``3``/``2``/``2``/``1``;
    * số ``0``–``3`` ⇒ giữ nguyên (``ionice -c0`` = không đặt ưu tiên I/O);
    * giá trị không hiểu được ⇒ ``None`` (không bọc) — an toàn.
    """
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text or text == "none":
        return None
    if text in IONICE_CLASSES:
        return IONICE_CLASSES[text]
    if text in ("0", "1", "2", "3"):
        return text
    return None


def _clamp_nice(value: int) -> int:
    """Chuẩn hoá mức ``nice``.

    * ``<= 0`` ⇒ ``0`` ⇒ **không** bọc ``nice``. Mức âm cần quyền đặc biệt
      để nâng ưu tiên; roff không tự ý nâng ưu tiên ⇒ coi như ``0``.
    * ``> 19`` ⇒ ``19`` (trần của Linux).
    """
    if value <= 0:
        return 0
    return min(value, NICE_MAX)


def _env_nice(name: str = "ROFF_NICE") -> int | None:
    """Mức ``nice`` từ biến môi trường; ``None`` nếu không có / không hợp lệ."""
    raw = os.environ.get(name)
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        value = int(text)
    except ValueError:
        return None
    return _clamp_nice(value)


def _env_ionice(name: str = "ROFF_IONICE") -> object:
    """Lớp I/O từ biến môi trường.

    Trả về ``_UNSET`` nếu biến **không có** (hoặc rác ⇒ bỏ qua, đọc tiếp
    config), ``None`` nếu biến nói rõ "**không** bọc ``ionice``" (rỗng hoặc
    ``none``), còn lại là số lớp.
    """
    raw = os.environ.get(name)
    if raw is None:
        return _UNSET
    text = raw.strip()
    if not text or text.lower() == "none":
        return None
    value = ionice_class(text)
    return value if value is not None else _UNSET


def _config_ionice(value: str | None) -> str | None:
    """Lớp I/O từ **config**.

    Rỗng / ``none`` ⇒ **không** bọc; giá trị rác ⇒ mặc định (``idle``) —
    không bỏ im lặng luôn (giữ được cam kết "máy local phải nhẹ").
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() == "none":
        return None
    return ionice_class(text) or ionice_class(DEFAULT_IONICE)


def local_nice_policy(config: Config | None = None) -> NicePolicy:
    """Chính sách ``nice``/``ionice`` cho tiến trình **local** của roff (B34).

    Thứ tự ưu tiên: ``ROFF_NICE``/``ROFF_IONICE`` (env) > ``[local]``
    (config) > mặc định (``19`` / ``idle``).

    * ``nice = 0`` ⇒ **không** bọc ``nice`` (người dùng muốn ưu tiên cao);
    * ``ionice`` rỗng hoặc ``none`` ⇒ **không** bọc ``ionice``;
    * config hỏng / không đọc được ⇒ **mặc định**, **không** raise (B27).
    """
    cfg = config if config is not None else load_config()
    nice = _env_nice()
    if nice is None and cfg is not None:
        nice = _clamp_nice(int(cfg.nice))
    if nice is None:
        nice = DEFAULT_NICE
    ionice = _env_ionice()
    if ionice is _UNSET and cfg is not None:
        ionice = _config_ionice(cfg.ionice)
    if ionice is _UNSET:
        ionice = ionice_class(DEFAULT_IONICE)
    return NicePolicy(nice=nice, ionice=ionice)


def remote_nice_policy(
    host: str | None = None, config: Config | None = None
) -> NicePolicy:
    """Chính sách ``nice``/``ionice`` cho **lệnh job chạy trên máy remote** (C9).

    Thứ tự ưu tiên: ``ROFF_REMOTE_NICE``/``ROFF_REMOTE_IONICE`` (env) >
    ``[host.<alias>]`` > ``[remote]`` > mặc định (``19`` / ``idle``).

    Mặc định ``19``/``idle`` là **an toàn**: job của roff không được cạnh
    tranh với ``postgres``/``telegram-bot``/``cloudflared`` của chính người
    dùng đang chạy trên cùng máy đó.
    """
    cfg = config if config is not None else load_config()
    nice = _env_nice("ROFF_REMOTE_NICE")
    if nice is None and cfg is not None:
        value: object = None
        if host:
            value = cfg.host(host).nice
        if value is None:
            value = cfg.remote_nice
        nice = _clamp_nice(int(value))
    if nice is None:
        nice = DEFAULT_NICE
    ionice = _env_ionice("ROFF_REMOTE_IONICE")
    if ionice is _UNSET and cfg is not None:
        value = None
        if host:
            value = cfg.host(host).ionice
        if value is None:
            value = cfg.remote_ionice
        ionice = _config_ionice(value)
    if ionice is _UNSET:
        ionice = ionice_class(DEFAULT_IONICE)
    return NicePolicy(nice=nice, ionice=ionice)


def local_nice_prefix(config: Config | None = None) -> list[str]:
    """Tiền tố argv (``ionice``/``nice``) cho tiến trình **local** (B34).

    Đây là hàm mà **mọi** module gọi — :mod:`roffkit.probe`,
    :mod:`roffkit.sshx`, :mod:`roffkit.tunnel`, :mod:`roffkit.job`,
    :mod:`roffkit.transfer` — nên 5 module **luôn** cho ra cùng một kết quả
    với cùng env + cùng config.
    """
    return local_nice_policy(config).prefix()


def remote_nice_args(
    host: str | None = None, config: Config | None = None
) -> tuple[str, str]:
    """Placeholder cho script chạy **trên host**: ``(nice, ionice)``.

    Chuỗi rỗng ⇒ **không** bọc. Giá trị được điền từ **local** (giống
    ``__CPUS__``/``__MEMMB__`` của ``launch.sh``) vì máy remote có thể không
    có ``nice``/``ionice`` — script trên host tự kiểm tra bằng ``command -v``.
    """
    policy = remote_nice_policy(host, config)
    nice = str(policy.nice) if policy.nice > 0 else ""
    return nice, (str(policy.ionice) if policy.ionice else "")


# ------------------------------------------------------------------ helpers


def _merge_duplicate_list_options(text: str) -> str:
    """Gộp khoá lặp lại (vd ``include``) trong cùng section thành giá trị nhiều dòng.

    ``configparser`` mặc định ném ``DuplicateOptionError`` khi một option lặp
    trong cùng section, nhưng tài liệu roff cho phép ``include`` lặp lại nhiều
    dòng. Ta dọn trước: dòng thứ hai trở đi của một khoá "danh sách" được đổi
    thành dòng nối tiếp (indent) → ``configparser`` đọc thành một giá trị nhiều
    dòng.
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith(("#", ";")):
            out.append(raw)
            continue
        if raw[:1] in (" ", "\t"):
            # dòng nối tiếp của option trước → giữ nguyên
            out.append(raw)
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            seen = set()
            out.append(raw)
            continue
        match = _OPT_RE.match(raw)
        if match is None or not match.group("option").strip():
            out.append(raw)
            continue
        key = match.group("option").strip().lower()
        value = match.group("value")
        if key in _LIST_OPTION_KEYS and key in seen:
            out.append("\t" + value if value.strip() else "\t")
            continue
        seen.add(key)
        out.append(raw)
    return "\n".join(out) + "\n"


def _get(parser: configparser.ConfigParser, section: str, option: str) -> str | None:
    """Giá trị của ``option`` trong ``section`` (``None`` nếu không có)."""
    if not parser.has_section(section):
        return None
    if not parser.has_option(section, option):
        return None
    value = parser.get(section, option)
    return value if isinstance(value, str) else None


def _as_list(value: str | None) -> list[str]:
    """Chuỗi (nhiều dòng và/hoặc CSV) → danh sách, bỏ dòng rỗng."""
    if value is None:
        return []
    items: list[str] = []
    for line in value.splitlines():
        for part in line.split(","):
            part = part.strip()
            if part:
                items.append(part)
    return items


def _as_int(value: str | None, where: str) -> int | None:
    """Chuỗi → int; giá trị sai → ``RoffError(EXIT_CONFIG)``."""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return int(float(text))
    except ValueError:
        raise RoffError(
            EXIT_CONFIG, error=f"config sai: {where}={text!r} (không phải số nguyên)"
        ) from None


def _as_float(value: str | None, where: str) -> float | None:
    """Chuỗi → float; giá trị sai → ``RoffError(EXIT_CONFIG)``."""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        raise RoffError(
            EXIT_CONFIG, error=f"config sai: {where}={text!r} (không phải số)"
        ) from None


def _build_config(parser: configparser.ConfigParser, path: str) -> Config:
    """Dựng :class:`Config` từ parser đã đọc."""
    cfg = Config(path=path)

    include = _as_list(_get(parser, "fleet", "include"))
    cfg.fleet_include = include if include else ["*"]
    cfg.fleet_exclude = _as_list(_get(parser, "fleet", "exclude"))

    probe_cache_s = _as_int(_get(parser, "fleet", "probe_cache_s"), "fleet.probe_cache_s")
    if probe_cache_s is not None:
        cfg.probe_cache_s = probe_cache_s

    # `max_parallel_transfers`: chấp nhận ở [local] (đặc tả §3) và ở [fleet]
    # (ví dụ trong DESIGN.md §5.10) — [local] thắng nếu cả hai cùng có.
    max_parallel = _as_int(
        _get(parser, "local", "max_parallel_transfers"), "local.max_parallel_transfers"
    )
    if max_parallel is None:
        max_parallel = _as_int(
            _get(parser, "fleet", "max_parallel_transfers"),
            "fleet.max_parallel_transfers",
        )
    if max_parallel is not None:
        cfg.max_parallel_transfers = max_parallel

    nice = _as_int(_get(parser, "local", "nice"), "local.nice")
    if nice is not None:
        cfg.nice = nice
    ionice = _get(parser, "local", "ionice")
    if ionice is not None and ionice.strip():
        cfg.ionice = ionice.strip()

    # [remote] — chính sách cho **lệnh job chạy trên máy remote** (C9).
    remote_nice = _as_int(_get(parser, "remote", "nice"), "remote.nice")
    if remote_nice is not None:
        cfg.remote_nice = remote_nice
    remote_ionice = _get(parser, "remote", "ionice")
    if remote_ionice is not None and remote_ionice.strip():
        cfg.remote_ionice = remote_ionice.strip()

    cache_max_mb = _as_int(_get(parser, "local", "cache_max_mb"), "local.cache_max_mb")
    if cache_max_mb is not None:
        cfg.cache_max_mb = cache_max_mb
    cache_min_free_mb = _as_int(
        _get(parser, "local", "cache_min_free_mb"), "local.cache_min_free_mb"
    )
    if cache_min_free_mb is not None:
        cfg.cache_min_free_mb = cache_min_free_mb

    cfg.heavy_add = _as_list(_get(parser, "heavy", "add"))
    cfg.heavy_allow = _as_list(_get(parser, "heavy", "allow"))

    for section in parser.sections():
        if not section.lower().startswith("host."):
            continue
        alias = section[len("host.") :].strip()
        if not alias:
            continue
        labels = _as_list(_get(parser, section, "labels"))
        weight = _as_float(_get(parser, section, "weight"), f"host.{alias}.weight")
        slots = _as_int(_get(parser, section, "slots"), f"host.{alias}.slots")
        mem_budget = _as_int(
            _get(parser, section, "mem_budget_mb"), f"host.{alias}.mem_budget_mb"
        )
        mem_reserve = _as_int(
            _get(parser, section, "mem_reserve_mb"), f"host.{alias}.mem_reserve_mb"
        )
        host_nice = _as_int(_get(parser, section, "nice"), f"host.{alias}.nice")
        host_ionice = _get(parser, section, "ionice")
        if host_ionice is not None and not host_ionice.strip():
            host_ionice = None
        cfg.hosts[alias] = HostConfig(
            name=alias,
            labels=labels,
            weight=1.0 if weight is None else weight,
            slots=1 if slots is None else slots,
            mem_budget_mb=0 if mem_budget is None else mem_budget,
            mem_reserve_mb=256 if mem_reserve is None else mem_reserve,
            nice=host_nice,
            ionice=host_ionice,
        )

    return cfg
