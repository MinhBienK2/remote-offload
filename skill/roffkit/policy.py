"""Chính sách chọn host: **lọc cứng trước, chấm điểm sau**.

Mọi quyết định đều kèm lý do đọc được (``Choice.reasons`` / ``Choice.rejected``)
để agent và người nghiệm thu hiểu **vì sao** host đó được chọn.

Module này **không in ra stdout**.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Sequence

if TYPE_CHECKING:  # pragma: no cover - chỉ để type checker
    from roffkit.probe import Profile

__all__ = [
    "Need",
    "Choice",
    "select",
    "score",
    "reject_reason",
    "reject_all",
    "setup_available",
]


try:  # phần A (config.py) có thể chưa sẵn sàng -> fallback tối thiểu
    from roffkit.config import Config, HostConfig  # type: ignore  # noqa: F401
except ImportError:  # pragma: no cover - chỉ xảy ra khi chạy lẻ module này
    Config = Any  # type: ignore[assignment,misc]
    HostConfig = Any  # type: ignore[assignment,misc]

try:  # probe.py là cùng phần B -> import thẳng
    from roffkit.probe import same_physical_host
except ImportError:  # pragma: no cover
    same_physical_host = None  # type: ignore[assignment]

try:  # bảng alias (tool suy ra) -> hỏi "host có tool này không?"
    from roffkit.infer import tool_family as _tool_family
except ImportError:  # pragma: no cover
    _tool_family = None  # type: ignore[assignment]


# --------------------------------------------------------------------------
# trọng số (hợp đồng API §7)
# --------------------------------------------------------------------------
WARM_PROJECT = 40.0
WARM_TOOLCHAIN = 10.0
WARM_DATA = 50.0
HEALTH_CORES = 3.0
HEALTH_MEM_DIV = 100.0
STABLE_DURABLE = 15.0
STABLE_RESTART = -20.0
LABEL_MATCH = 25.0
BUSY_JOB = -30.0

#: Sàn RAM: host bị loại nếu
#: ``mem_available_mb < max(MIN_MEM_MB, need.mem_mb + mem_reserve_mb)``.
#: Xem regression R21 — đừng đổi lại thành ``max(512, need.mem_mb)``.
MIN_MEM_MB = 256
"""Sàn tuyệt đối: host đang thrash thì không dispatch job nào lên.

Không phải "mọi job cần ≥ 256 MB" — mà là "host phải còn ≥ 256 MB trống",
kể cả khi job chỉ cần vài MB.
"""

RESERVE_MEM_MB = 256
"""RAM chừa mặc định cho dịch vụ production của chính host (Muse chạy
postgres, telegram-bot, art-ex — dịch vụ của chính người dùng)."""


@dataclass
class Need:
    """Nhu cầu của 1 job (nguồn cho cả lọc lẫn chấm điểm)."""

    tools: list[str] = field(default_factory=list)
    mem_mb: int = 0
    disk_mb: int = 0
    cpus: int = 0
    internet: bool = False
    # --- mở rộng ngoài hợp đồng (báo cáo sẽ nêu) ---
    project: str = ""            # tên project cần chạy (điểm "ấm" +40)
    labels: list[str] = field(default_factory=list)  # nhãn muốn khớp (+25/nhãn)


@dataclass
class Choice:
    """Kết quả chọn host."""

    host: str
    score: float
    reasons: list[str]
    rejected: dict[str, str]


# --------------------------------------------------------------------------
# trợ giúp
# --------------------------------------------------------------------------
class _DefaultHostCfg:
    """HostConfig tối thiểu khi config không có entry cho alias."""

    name = ""
    labels: list[str] = []
    weight = 1.0
    slots = 1
    mem_budget_mb = 0
    mem_reserve_mb = 256


_DEFAULT_HOST = _DefaultHostCfg()


def _host_cfg(cfg: Any, alias: str) -> Any:
    """Lấy HostConfig của alias, an toàn khi config thiếu/hỏng."""
    if cfg is None:
        return _DEFAULT_HOST
    getter = getattr(cfg, "host", None)
    if getter is None:
        return _DEFAULT_HOST
    try:
        host = getter(alias)
    except Exception:
        return _DEFAULT_HOST
    return host if host is not None else _DEFAULT_HOST


def _setup_dir() -> str:
    """Thư mục script setup của skill (``skill/setups``)."""
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(here), "setups")


def setup_available(tool: str) -> bool:
    """Có script setup cho tool này không?

    Chưa có registry chính thức; hiện tại tra ``skill/setups/<tool>.sh``
    (và các biến thể). Chưa có script ⇒ coi như **thiếu tool** và loại host.
    """
    if not tool:
        return False
    base = _setup_dir()
    candidates = (
        os.path.join(base, f"{tool}.sh"),
        os.path.join(base, f"{tool}.bash"),
        os.path.join(base, tool, "setup.sh"),
        os.path.join(base, tool, "install.sh"),
    )
    return any(os.path.isfile(path) for path in candidates)


def _missing_tools(profile: Any, need: Need) -> list[str]:
    """Tool trong ``need`` mà host không có (bỏ qua tool có script setup)."""
    have = getattr(profile, "tools", None) or {}
    missing: list[str] = []
    for tool in (need.tools or []):
        if tool in have:
            continue
        if setup_available(tool):
            continue
        missing.append(tool)
    return missing


def _setuptable_tools(profile: Any, need: Need) -> list[str]:
    """Tool thiếu nhưng **có** script setup (sẽ chạy ensure sau)."""
    have = getattr(profile, "tools", None) or {}
    out: list[str] = []
    for tool in (need.tools or []):
        if tool not in have and setup_available(tool):
            out.append(tool)
    return out


def _detail_snippet(text: str | None, limit: int = 160) -> str:
    """Rút gọn text lỗi cho lý do (Profile.detail vẫn giữ nguyên văn)."""
    if not text:
        return ""
    first = text.strip().splitlines()[0] if text.strip() else ""
    if len(first) > limit:
        return first[: limit - 3] + "..."
    return first


# --------------------------------------------------------------------------
# lọc cứng
# --------------------------------------------------------------------------
def reject_reason(
    profile: "Profile",
    need: Need,
    cfg: "Config",
    busy: int = 0,
    ignore_slots: bool = False,
    ignore_busy: bool = False,
) -> str | None:
    """Trả về lý do host bị loại, hoặc ``None`` nếu host dùng được.

    Đây là phần "lọc cứng" của ``select()`` — tách riêng để in ra lý do cho
    người đọc và để test được.

    Args:
        profile: profile của host.
        need: nhu cầu của job.
        cfg: config.
        busy: số job roff đang **giữ slot** trên host này (dùng cho luật
            "đã đầy slot").
        ignore_slots: ``True`` ⇒ host **đã đầy slot** vẫn là ứng viên hợp lệ
            (đầy slot = "đang bận" ⇒ xếp hàng, KHÔNG phải "không dùng được").
        ignore_busy: ``True`` ⇒ host **bridge đang bận** vẫn là ứng viên hợp lệ
            (bridge bận = "đang bận" ⇒ xếp hàng, KHÔNG phải "không dùng được").
    """
    alias = getattr(profile, "alias", "?")

    if not getattr(profile, "reachable", False):
        err = getattr(profile, "error", None) or "lỗi ssh"
        detail = _detail_snippet(getattr(profile, "detail", None))
        if err == "permission_denied":
            return (
                f"{alias}: không với được host — key bị từ chối "
                f"(Permission denied), KHÁC host chết ({detail})"
            )
        return f"{alias}: không với được host ({err}) {detail}".rstrip()

    if getattr(profile, "bridge_busy", False) and not ignore_busy:
        return (
            f"{alias}: bridge một phiên đang bận (429 / already active) — "
            "host này chỉ nhận 1 phiên tại 1 thời điểm"
        )

    missing = _missing_tools(profile, need)
    if missing:
        return (
            f"{alias}: thiếu tool {', '.join(missing)} "
            "(chưa có script setup nào tương ứng)"
        )

    mem_avail = int(getattr(profile, "mem_available_mb", 0) or 0)
    reserve = int(
        getattr(_host_cfg(cfg, alias), "mem_reserve_mb", RESERVE_MEM_MB) or 0
    )
    # CỘNG DỒN, không phải max(): job cần 4 GB thì host phải còn 4 GB + reserve,
    # còn job 20 MB vẫn chạy được trên host còn 300 MB trống.
    mem_need = max(MIN_MEM_MB, int(need.mem_mb or 0) + reserve)
    if mem_avail < mem_need:
        return (
            f"{alias}: RAM khả dụng {mem_avail} MB < yêu cầu {mem_need} MB "
            f"(job {int(need.mem_mb or 0)} MB + chừa {reserve} MB cho dịch vụ "
            "của host, sàn "
            f"{MIN_MEM_MB} MB)"
        )

    disk_free = int(getattr(profile, "disk_free_mb", 0) or 0)
    disk_need = int(need.disk_mb or 0)
    if disk_free < disk_need:
        return f"{alias}: disk trống {disk_free} MB < yêu cầu {disk_need} MB"

    if need.internet and not getattr(profile, "internet", False):
        return f"{alias}: host không có internet (job cần tải từ URL)"

    slots = int(getattr(_host_cfg(cfg, alias), "slots", 1) or 1)
    used = int(busy or 0)
    if not ignore_slots and used >= slots:
        return f"{alias}: đã đầy slot ({used}/{slots} job roff đang chạy)"

    return None


# --------------------------------------------------------------------------
# chấm điểm
# --------------------------------------------------------------------------
def score(profile: "Profile", need: Need, cfg: "Config", busy: int = 0) -> tuple[float, list[str]]:
    """Chấm điểm 1 host theo công thức của hợp đồng.

    Công thức (hợp đồng §7):

    * ấm: project đã có ``+40`` | toolchain đã cài ``+10`` | giữ dữ liệu ``+50``
    * khoẻ: ``cores * (1 - load1/cores) * 3`` và ``mem_available_mb / 100``
    * ổn định: layout bền vững ``+15``; mỗi lần restart trong 24h ``-20``
    * nhãn: ``+25`` mỗi nhãn khớp
    * ưu tiên: ``* cfg.host(alias).weight``
    * đang bận: ``-30`` mỗi job roff đang chạy

    Trả về ``(điểm, lý do)`` — lý do là list[str] đọc được cho người.
    """
    alias = getattr(profile, "alias", "?")
    reasons: list[str] = []
    total = 0.0

    # --- ấm: project đã có trên host ---
    projects = list(getattr(profile, "projects", None) or [])
    project = getattr(need, "project", "") or ""
    if project and project in projects:
        total += WARM_PROJECT
        reasons.append(f"project '{project}' đã có sẵn trên host (+{WARM_PROJECT:.0f})")

    # --- ấm: toolchain đã cài ---
    toolchains = list(getattr(profile, "toolchains", None) or [])
    if toolchains:
        total += WARM_TOOLCHAIN
        reasons.append(
            f"toolchain đã cài sẵn ({', '.join(toolchains)}) (+{WARM_TOOLCHAIN:.0f})"
        )

    # --- khoẻ: core rảnh ---
    cores = int(getattr(profile, "cores", 0) or 0)
    if cores > 0:
        load1 = float(getattr(profile, "load1", 0.0) or 0.0)
        free = cores * (1.0 - min(1.0, load1 / cores))
        free = max(0.0, free)
        total += free * HEALTH_CORES
        reasons.append(
            f"còn {free:.1f}/{cores} core rảnh (×{HEALTH_CORES:.0f} → +{free * HEALTH_CORES:.1f})"
        )

    # --- khoẻ: RAM khả dụng ---
    mem_avail = int(getattr(profile, "mem_available_mb", 0) or 0)
    total += mem_avail / HEALTH_MEM_DIV
    reasons.append(f"RAM khả dụng {mem_avail} MB (+{mem_avail / HEALTH_MEM_DIV:.1f})")

    # --- ổn định ---
    if getattr(profile, "durable", False):
        total += STABLE_DURABLE
        reasons.append(
            f"layout bền vững ({getattr(profile, 'layout_root', None) or '?'}) "
            f"(+{STABLE_DURABLE:.0f})"
        )
    restarts = int(getattr(profile, "restarts_24h", 0) or 0)
    if restarts:
        total += STABLE_RESTART * restarts
        reasons.append(
            f"máy restart {restarts} lần trong 24h ({STABLE_RESTART * restarts:.0f})"
        )

    # --- nhãn ---
    host_cfg = _host_cfg(cfg, alias)
    host_labels = list(getattr(host_cfg, "labels", None) or [])
    matched = [label for label in (getattr(need, "labels", None) or []) if label in host_labels]
    if matched:
        total += LABEL_MATCH * len(matched)
        reasons.append(
            f"khớp nhãn {', '.join(matched)} (+{LABEL_MATCH * len(matched):.0f})"
        )

    # --- ưu tiên: nhân trọng số ---
    weight = float(getattr(host_cfg, "weight", 1.0) or 1.0)
    if weight != 1.0:
        reasons.append(f"trọng số host {weight} (×{weight})")
    total *= weight

    # --- đang bận ---
    used = int(busy or 0)
    if used:
        total += BUSY_JOB * used
        reasons.append(f"đang chạy {used} job roff ({BUSY_JOB * used:.0f})")

    return round(total, 3), reasons


# --------------------------------------------------------------------------
# select
# --------------------------------------------------------------------------
def _normalize_needs(inferred_needs: Sequence[str] | None) -> list[str]:
    """Chuẩn hoá danh sách tool suy ra (bỏ rỗng, bỏ trùng, giữ thứ tự)."""
    out: list[str] = []
    for raw in inferred_needs or []:
        tool = str(raw or "").strip()
        if not tool or tool in out:
            continue
        out.append(tool)
    return out


def _tool_names(tool: str) -> tuple[str, ...]:
    """Mọi tên khác nhau của 1 tool (chuẩn + alias) — vd ``maven``/``mvn``."""
    if _tool_family is not None:
        try:
            return _tool_family(tool)
        except Exception:  # pragma: no cover - bảng alias hỏng thì bỏ qua
            pass
    return (tool,)


def _host_has_tool(have: Any, tool: str) -> bool:
    """Host có tool này không (theo ``Profile.tools`` — **không** gọi ssh)."""
    for name in _tool_names(tool):
        try:
            if name in have:
                return True
        except TypeError:  # pragma: no cover - profile.tools lạ kiểu
            return False
    return False


def _need_tier(profile: Any, wants: list[str]) -> tuple[int, list[str], list[str]]:
    """Bậc ưu tiên của host theo tool suy ra: ``(bậc, tool khớp, tool thiếu)``.

    * ``2`` — có **đủ** tool suy ra;
    * ``1`` — có **một phần**;
    * ``0`` — không có tool nào (hoặc không có tool suy ra nào).

    Tool suy ra là tiêu chí **mềm**: thiếu tool ⇒ host **vẫn là ứng viên hợp
    lệ**, chỉ bị đẩy xuống cuối (không loại).
    """
    if not wants:
        return 0, [], []
    have = getattr(profile, "tools", None) or {}
    matched = [tool for tool in wants if _host_has_tool(have, tool)]
    missing = [tool for tool in wants if tool not in matched]
    if not missing:
        return 2, matched, missing
    if matched:
        return 1, matched, missing
    return 0, matched, missing


def _same_physical(a: Any, b: Any) -> bool:
    """Hai profile có phải cùng **một máy vật lý** không?

    ``same_physical_host`` trả ``None``/``False`` (hoặc ném lỗi) ⇒ coi là
    **khác máy**: thiếu dữ liệu (``boot_id``/``instance_token``) không phải
    bằng chứng ⇒ không được loại nhầm.
    """
    if same_physical_host is None:
        return False
    try:
        return bool(same_physical_host(a, b))
    except Exception:  # pragma: no cover - hàm thay thế hỏng
        return False


def _group_physical_host(profiles: list[Any]) -> list[list[Any]]:
    """Chia alias thành nhóm theo **máy vật lý** (``same_physical_host``).

    Thiếu dữ liệu (``boot_id``/``instance_token``) ⇒ alias đó đứng một mình
    (không đoán bừa ⇒ không loại nhầm).
    """
    groups: list[list[Any]] = []
    for profile in profiles:
        for group in groups:
            if _same_physical(group[0], profile):
                group.append(profile)
                break
        else:
            groups.append([profile])
    return groups


def _pick_keeper(
    group: list[Any],
    prefer_index: dict[str, int],
    base_points: dict[str, float],
) -> Any:
    """Alias giữ lại của 1 nhóm: **prefer** > **điểm** > thứ tự danh sách.

    * alias người dùng prefer (``--in job:``) được giữ trước — không được loại
      trắng alias user chỉ định;
    * cùng cấp thì giữ alias **điểm cao hơn** (RAM/load/khoẻ…);
    * vẫn bằng thì giữ alias **đầu tiên** (thứ tự vào ổn định).
    """
    best = None
    best_key: tuple | None = None
    for profile in group:
        alias = getattr(profile, "alias", "?")
        rank = prefer_index.get(alias)
        key = (
            0 if rank is not None else 1,
            rank if rank is not None else 0,
            -float(base_points.get(alias, 0.0)),
        )
        if best_key is None or key < best_key:
            best, best_key = profile, key
    return best


def _dedupe_physical_host(
    profiles: list[Any],
    prefer: list[str] | None = None,
    base_points: dict[str, float] | None = None,
) -> tuple[list[Any], dict[str, str]]:
    """Loại alias trùng máy vật lý — mỗi nhóm chỉ **giữ 1 alias**.

    Trả ``(candidates còn lại, {alias: lý do loại})``. Luôn giữ lại **ít nhất
    1 alias** mỗi nhóm ⇒ không mất host khỏi fleet.

    Alias **giữ lại** (keeper) của mỗi nhóm, theo thứ tự ưu tiên:

    1. alias mà người dùng **prefer** (``--in job:``) — không được loại trắng
       alias user chỉ định;
    2. alias **điểm cao hơn** (RAM/load/khoẻ…);
    3. thứ tự trong danh sách (thứ tự vào ổn định).
    """
    if same_physical_host is None or len(profiles) < 2:
        return list(profiles), {}

    groups = _group_physical_host(profiles)
    prefer_index = {alias: i for i, alias in enumerate(prefer or [])}
    points_by_alias = dict(base_points or {})

    kept: list[Any] = []
    rejected: dict[str, str] = {}
    for group in groups:
        keeper = _pick_keeper(group, prefer_index, points_by_alias)
        kept.append(keeper)
        keeper_alias = getattr(keeper, "alias", "?")
        for other in group:
            if other is keeper:
                continue
            alias = getattr(other, "alias", "?")
            rejected[alias] = (
                f"{alias}: cùng máy vật lý với {keeper_alias} "
                "(cùng boot_id + instance_token) — chỉ dùng 1 alias cho 1 máy"
            )
    return kept, rejected


def reject_all(
    profiles: list["Profile"],
    need: Need,
    cfg: "Config",
    busy: dict[str, int] | None = None,
    ignore_slots: bool = False,
    ignore_busy: bool = False,
    running: dict[str, int] | None = None,
) -> dict[str, str]:
    """Lý do bị loại của **mọi** host (kể cả host đang dùng được → không có).

    ``select()`` trả ``None`` khi không host nào dùng được, lúc đó lý do loại
    bị mất. Hàm này giữ lại lý do để CLI in ra (S5: "no host with ≥ 6144 MB
    available; best is 842 MB on muse-tampt").
    """
    busy_map = busy or {}
    run_map = running if running is not None else busy_map
    out: dict[str, str] = {}
    for profile in profiles or []:
        alias = getattr(profile, "alias", "?")
        reason = reject_reason(
            profile,
            need,
            cfg,
            int(run_map.get(alias, 0) or 0),
            ignore_slots,
            ignore_busy,
        )
        if reason is not None:
            out[alias] = reason
    return out


def select(
    profiles: list["Profile"],
    need: Need,
    cfg: "Config",
    busy: dict[str, int] | None = None,
    prefer_hosts: list[str] | None = None,
    ignore_slots: bool = False,
    ignore_busy: bool = False,
    running: dict[str, int] | None = None,
    inferred_needs: Sequence[str] | None = None,
) -> Choice | None:
    """Lọc cứng rồi chấm điểm; trả về host tốt nhất.

    ``prefer_hosts`` = danh sách alias đang giữ dữ liệu (``--in job:``).
    Alias **đầu tiên có mặt** trong danh sách được ``+50`` (ưu tiên mạnh).
    Trả ``None`` nếu không host nào dùng được.

    Thứ tự 3 bước:

    1. **Lọc cứng** (:func:`reject_reason`) — host không với được / thiếu tool
       ghi rõ / thiếu RAM / hết slot ⇒ loại, lý do lưu vào ``rejected``.
    2. **Loại alias trùng máy vật lý** (W6f) — 1 máy vật lý chỉ dùng qua
       **1 alias** (nếu không 2 job có thể chạy song song trên cùng 1 máy).
       Việc này chạy **trước** khi chọn winner ⇒ alias trùng **không** còn là
       ứng viên. Alias giữ lại của mỗi nhóm: alias user **prefer** trước,
       rồi alias **điểm cao hơn**, rồi thứ tự danh sách. Thiếu
       ``boot_id``/``instance_token`` ⇒ coi là khác máy (không loại nhầm);
       luôn giữ lại **ít nhất 1 alias** mỗi nhóm.
    3. **Chấm điểm** (:func:`score`) — trong cùng nhóm ưu tiên, điểm cao hơn
       thắng (điểm cũ của hợp đồng §7, không đổi).

    Args:
        busy: số job **đang bận** trên từng host (đang chạy + đang xếp hàng) —
            dùng cho điểm "đang bận" (−30 mỗi job).
        ignore_slots: ``True`` ⇒ host đã đầy slot **vẫn** là ứng viên hợp lệ
            (đầy slot là "đang bận" ⇒ job sẽ xếp hàng, không phải lỗi).
            Điểm "đang bận" (−30 mỗi job) vẫn áp dụng nên host rảnh được
            ưu tiên hơn.
        ignore_busy: ``True`` ⇒ host **bridge đang bận** vẫn là ứng viên hợp lệ
            (bridge bận là "đang bận" ⇒ job sẽ xếp hàng, không phải lỗi).
        running: số job **đang giữ slot** trên từng host (dùng cho luật "đã
            đầy slot"). Mặc định lấy từ ``busy``.
        inferred_needs: tool **suy ra** từ câu lệnh (xem
            :func:`roffkit.infer.infer_tools`) — tiêu chí **mềm**: host có
            tool đó trong ``Profile.tools`` được **ưu tiên** (đứng trước
            trong thứ tự ưu tiên), host không có **vẫn là ứng viên hợp lệ**,
            chỉ bị đẩy xuống cuối — **không** bị loại. Tool ghi rõ bằng
            ``--need`` (``need.tools``) vẫn là tiêu chí **cứng** như trước.
            Rỗng (mặc định) ⇒ hành vi **không đổi**.

            Lý do về tool suy ra chỉ được kèm vào ``reasons`` khi **có**
            host nào **thực sự** có tool đó (W8b): nếu không host nào có thì
            dòng "không có tool suy ra từ lệnh: …" chỉ là **nhiễu** (lặp ở
            mọi host, không giúp ích ai) ⇒ **không** kèm. Hành vi **chọn
            host** không đổi.
    """
    busy_map = busy or {}
    run_map = running if running is not None else busy_map
    prefer = list(prefer_hosts or [])
    wants = _normalize_needs(inferred_needs)

    rejected: dict[str, str] = {}
    candidates: list[Any] = []
    for profile in profiles or []:
        alias = getattr(profile, "alias", "?")
        used = int(run_map.get(alias, 0) or 0)
        reason = reject_reason(
            profile, need, cfg, used, ignore_slots, ignore_busy
        )
        if reason is not None:
            rejected[alias] = reason
            continue
        candidates.append(profile)

    if not candidates:
        return None

    # --- điểm gốc (chưa cộng +50 của prefer) — dùng để chọn keeper ------
    base_points: dict[str, float] = {}
    base_reasons: dict[str, list[str]] = {}
    for profile in candidates:
        alias = getattr(profile, "alias", "?")
        used = int(busy_map.get(alias, 0) or 0)
        points, reasons = score(profile, need, cfg, used)
        base_points[alias] = points
        base_reasons[alias] = reasons

    # --- W6f: 1 máy vật lý = 1 alias (loại TRƯỚC khi chọn winner) ---------
    candidates, duplicates = _dedupe_physical_host(
        candidates, prefer, base_points
    )
    for alias, reason in duplicates.items():
        rejected[alias] = (
            f"{rejected[alias]}; {reason}" if alias in rejected else reason
        )
    if not candidates:
        return None

    available = {getattr(p, "alias", "?") for p in candidates}
    preferred = next((alias for alias in prefer if alias in available), None)

    # --- W8b: bậc ưu tiên theo tool suy ra — tính TRƯỚC cho MỌI ứng viên ---
    # Trước đây tier lẫn điểm tính trong **cùng 1 vòng** ⇒ không biết được
    # "có host nào thực sự có tool suy ra không" mà không làm 2 vòng.
    tiers: dict[str, int] = {}
    matched_by_alias: dict[str, list[str]] = {}
    missing_by_alias: dict[str, list[str]] = {}
    for profile in candidates:
        alias = getattr(profile, "alias", "?")
        tier, matched, missing = _need_tier(profile, wants)
        tiers[alias] = tier
        matched_by_alias[alias] = matched
        missing_by_alias[alias] = missing

    # Lý do về tool suy ra chỉ **có ích** khi có **ít nhất một** ứng viên có
    # tier > 0 (tức có host nào **thực sự** có tool đó). Nếu không host nào
    # có ⇒ **không** kèm lý do đó: dòng "không có tool suy ra từ lệnh: …"
    # lặp ở **mọi** host mà không giúp ích ai. Hành vi **chọn host** không đổi.
    tool_reason_useful = any(tier > 0 for tier in tiers.values())

    best_profile = None
    best_score = float("-inf")
    best_tier = -1
    best_reasons: list[str] = []
    for profile in candidates:
        alias = getattr(profile, "alias", "?")
        points = float(base_points.get(alias, 0.0))
        reasons = list(base_reasons.get(alias, []))
        if preferred is not None and alias == preferred:
            points += WARM_DATA
            reasons = list(reasons) + [
                f"đang giữ dữ liệu của --in job: ({preferred}) (+{WARM_DATA:.0f})"
            ]
        setuptable = _setuptable_tools(profile, need)
        if setuptable:
            reasons = list(reasons) + [
                f"cần chạy setup cho: {', '.join(setuptable)}"
            ]
        if getattr(profile, "bridge_busy", False) and ignore_busy:
            reasons = list(reasons) + [
                "bridge đang bận (1 phiên/host) → job sẽ xếp hàng"
            ]
        # --- W6b: ưu tiên host CÓ tool suy ra từ lệnh (tiêu chí mềm) ------
        tier = int(tiers.get(alias, 0))
        if wants and tool_reason_useful:
            matched = matched_by_alias.get(alias, [])
            missing = missing_by_alias.get(alias, [])
            # Báo **mọi** tool suy ra (có ⇒ "có …", thiếu ⇒ "không có …") —
            # câu chữ giữ nguyên như trước.
            if matched:
                reasons = list(reasons) + [
                    f"có tool suy ra từ lệnh: {', '.join(matched)} (ưu tiên)"
                ]
            if missing:
                reasons = list(reasons) + [
                    "không có tool suy ra từ lệnh: "
                    f"{', '.join(missing)} (vẫn dùng được — ưu tiên là mềm)"
                ]
        if tier > best_tier or (tier == best_tier and points > best_score):
            best_profile, best_score, best_tier, best_reasons = (
                profile,
                points,
                tier,
                reasons,
            )

    if best_profile is None:  # pragma: no cover - không xảy ra nếu candidates rỗng
        return None

    winner_alias = getattr(best_profile, "alias", "?")
    return Choice(
        host=winner_alias,
        score=round(best_score, 3),
        reasons=best_reasons,
        rejected=rejected,
    )
