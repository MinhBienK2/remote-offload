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

    [heavy]
    add = gradle
    allow = git

    [host.muse-pmb333]
    labels = ffmpeg, shared
    weight = 1
    slots = 1
    mem_budget_mb = 700

Quy tắc:
  * không có file → :meth:`Config.load` trả config mặc định, ``path=None``;
  * file sai cú pháp INI (hoặc giá trị sai kiểu) → ``RoffError(EXIT_CONFIG)``;
  * khoá lặp lại trong cùng section (``include = ...`` nhiều dòng) được **cộng
    dồn** thành danh sách; giá trị cũng chấp nhận dạng CSV và dạng nhiều dòng;
  * **B27**: người dùng **chỉ định** một file config (qua ``ROFF_CONFIG``) mà
    file đó **không tồn tại** ⇒ ``strict=True`` ném ``RoffError(EXIT_CONFIG,
    "config_missing")``. Bỏ qua im lặng sẽ **nới rộng fleet** thành MỌI host
    trong ``~/.ssh/config`` (và làm mất luôn ``exclude`` của người dùng).
"""

from __future__ import annotations

import configparser
import os
import re
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
        cfg.hosts[alias] = HostConfig(
            name=alias,
            labels=labels,
            weight=1.0 if weight is None else weight,
            slots=1 if slots is None else slots,
            mem_budget_mb=0 if mem_budget is None else mem_budget,
            mem_reserve_mb=256 if mem_reserve is None else mem_reserve,
        )

    return cfg
