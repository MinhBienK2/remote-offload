"""Cổng chuyển tiếp SSH cho roff v2 — hợp đồng `API-CONTRACT.md` §12.

Ba quy tắc không được vi phạm:

1. **Không bao giờ kill pid của người khác.** Master ssh cũ đang giữ cổng local
   thì huỷ **forward** bằng ``ssh -O cancel`` (chỉ master của chính roff).
   Tuyệt đối không ``kill``/``pkill`` (bug R3 đã xảy ra thật: master cũ giữ port,
   ``curl`` bị reset).
2. **Phân biệt "không có gì nghe" với "không kiểm tra được".** Remote không
   LISTEN ở rport → ``remote_listening=False`` (lỗi, exit 1). Remote không có
   ``ss``/``netstat`` và không đọc được ``/proc/net/tcp`` →
   ``remote_listening=None`` (không biết — vẫn mở tunnel nhưng phải ghi rõ).
3. **ControlMaster của roff.** Socket riêng theo alias
   (``<state>/cm/<alias>.sock``), ``ControlPersist=300`` — đo thật: lần đầu
   4,9 s, lần sau 1,3 s, tức tiết kiệm ~3,5 s cho mỗi lệnh ssh.

Module này **không in gì ra stdout** (chỉ ``roffkit.cli`` mới in JSON).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

try:  # pragma: no cover - hai cách import đều phải chạy được
    from roffkit.errors import EXIT_INPUT, EXIT_JOB_FAILED, EXIT_UNREACHABLE, RoffError
except ImportError:  # pragma: no cover
    from .errors import EXIT_INPUT, EXIT_JOB_FAILED, EXIT_UNREACHABLE, RoffError

try:  # pragma: no cover
    from roffkit import sshx
except ImportError:  # pragma: no cover
    from . import sshx

try:  # pragma: no cover
    from roffkit.sshx import ssh_config_args
except ImportError:  # pragma: no cover
    from .sshx import ssh_config_args

log = logging.getLogger("roff.tunnel")

CONTROL_PERSIST = 300
"""Giây giữ ControlMaster sau khi lệnh cuối cùng kết thúc."""

CONNECT_TIMEOUT = 15
DEFAULT_TIMEOUT = 30.0
REMOTE_CHECK_MARKER = "__ROFF_SRC__"
"""Marker để biết remote dùng công cụ nào để kiểm tra LISTEN."""

__all__ = [
    "Tunnel",
    "tunnel_start",
    "tunnel_stop",
    "tunnel_list",
    "state_dir",
    "cm_dir",
    "control_path",
]


# --------------------------------------------------------------------------------------
# Kiểu dữ liệu công khai
# --------------------------------------------------------------------------------------
@dataclass
class Tunnel:
    """Một forward local→remote đang sống."""

    alias: str
    lport: int
    rport: int
    master_pid: int | None
    note: str | None
    remote_listening: bool | None = None
    """Remote có gì đang LISTEN ở ``rport`` không.

    ``True`` = có, ``False`` = không (lỗi), ``None`` = **không kiểm tra được**
    (remote thiếu ``ss``/``netstat`` và không đọc được ``/proc/net/tcp``).
    Ba trạng thái này khác nhau — xem ``docs/SPEC-CLI.md`` §13.
    """


# --------------------------------------------------------------------------------------
# Đường dẫn state
# --------------------------------------------------------------------------------------
def state_dir() -> Path:
    """Thư mục state của roff (``$ROFF_STATE_DIR`` → ``~/.local/state/roff``)."""
    env = os.environ.get("ROFF_STATE_DIR")
    return Path(env) if env else Path.home() / ".local" / "state" / "roff"


def cm_dir() -> Path:
    """Thư mục chứa socket ControlMaster của roff."""
    return state_dir() / "cm"


def control_path(alias: str) -> str:
    """Socket ControlMaster của một alias.

    **Dùng chung với :mod:`roffkit.sshx`** — tunnel và lệnh ssh thường phải
    dùng cùng một master, nếu hai module tự tính đường dẫn khác nhau thì
    ``ssh -O cancel`` không trúng master đang giữ cổng. Hàm của ``sshx`` còn
    lo việc giới hạn độ dài đường dẫn Unix socket (108 byte).
    """
    return str(sshx.control_path(alias))


def _state_file() -> Path:
    return state_dir() / "tunnels.json"


def _load_state() -> list[dict]:
    """Đọc danh sách tunnel đã ghi nhận (file hỏng → coi như rỗng)."""
    path = _state_file()
    try:
        data = json.loads(path.read_text() or "{}")
    except (OSError, ValueError):
        return []
    if isinstance(data, dict):
        data = data.get("tunnels") or []
    if not isinstance(data, list):
        return []
    return [rec for rec in data if isinstance(rec, dict)]


def _save_state(records: list[dict]) -> None:
    path = _state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"tunnels": records}, ensure_ascii=False, indent=2))
    os.replace(tmp, path)


# --------------------------------------------------------------------------------------
# Chạy lệnh local
# --------------------------------------------------------------------------------------
def _prefix() -> list[str]:
    """Tiền tố nice/ionice — tiến trình local của roff phải nhẹ (Q18/Q28)."""
    nice = os.environ.get("ROFF_NICE", "19")
    ionice = os.environ.get("ROFF_IONICE", "idle")
    prefix: list[str] = []
    if ionice and shutil.which("ionice"):
        prefix += ["ionice", "-c", ionice]
    if nice and shutil.which("nice"):
        prefix += ["nice", "-n", nice]
    return prefix


def _run(argv: list[str], timeout: float = DEFAULT_TIMEOUT) -> tuple[int, str, str]:
    """Chạy một lệnh local, trả ``(rc, stdout, stderr)``.

    Seam duy nhất để test — không bao giờ raise vì lệnh fail.
    """
    cmd = _prefix() + list(argv)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        return 127, "", str(exc)
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout sau {timeout}s"
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _ssh_opts(alias: str) -> list[str]:
    """Option ssh chung: ControlMaster của roff + không bao giờ hỏi mật khẩu.

    Có ``-F <$ROFF_SSH_CONFIG>`` nếu được set — tunnel phải nối **đúng** máy
    mà ``fleet`` đã liệt kê (xem :func:`roffkit.sshx.ssh_config_args`).
    """
    return [
        "ssh",
        *ssh_config_args(),
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={CONNECT_TIMEOUT}",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
        "-o", "ControlMaster=auto",
        "-o", f"ControlPath={control_path(alias)}",
        "-o", f"ControlPersist={CONTROL_PERSIST}",
        "-o", "LogLevel=ERROR",
    ]


def _ssh_run(alias: str, remote_cmd: str, timeout: float = DEFAULT_TIMEOUT) -> tuple[int, str, str]:
    """Chạy một script trên host (một round-trip)."""
    return _run(_ssh_opts(alias) + [alias, remote_cmd], timeout=timeout)


def _ssh_mux(alias: str, args: list[str], timeout: float = DEFAULT_TIMEOUT) -> tuple[int, str, str]:
    """Lệnh ssh dạng mux (``-O check/cancel/forward/exit``) hoặc mở master."""
    return _run(_ssh_opts(alias) + list(args) + [alias], timeout=timeout)


# --------------------------------------------------------------------------------------
# Kiểm tra remote có LISTEN ở rport không
# --------------------------------------------------------------------------------------
_REMOTE_CHECK = """
if command -v ss >/dev/null 2>&1; then
  _roff_out="$(ss -ltn 2>/dev/null)" && { echo '__ROFF_SRC__=ss'; echo "$_roff_out"; exit 0; }
fi
if command -v netstat >/dev/null 2>&1; then
  _roff_out="$(netstat -ltn 2>/dev/null)" && { echo '__ROFF_SRC__=netstat'; echo "$_roff_out"; exit 0; }
fi
if [ -r /proc/net/tcp ]; then
  echo '__ROFF_SRC__=proc'
  cat /proc/net/tcp 2>/dev/null
  cat /proc/net/tcp6 2>/dev/null || true
  exit 0
fi
echo '__ROFF_SRC__=none'
"""


def _port_of(field: str) -> int | None:
    """``127.0.0.1:8000`` → ``8000``; ``*`` → ``None``."""
    if ":" not in field:
        return None
    tail = field.rsplit(":", 1)[1]
    return int(tail) if tail.isdigit() else None


def _text_listening(lines: list[str], rport: int) -> bool:
    """Dò output kiểu ``ss -ltn`` / ``netstat -ltn``."""
    for line in lines:
        if "LISTEN" not in line.upper():
            continue
        for field in line.split():
            if _port_of(field) == rport:
                return True
    return False


def _proc_listening(lines: list[str], rport: int) -> bool:
    """Dò ``/proc/net/tcp`` — cổng ở hex, state LISTEN = ``0A``."""
    for line in lines:
        parts = line.split()
        if len(parts) < 4 or not parts[0].rstrip(":").isdigit():
            continue
        local = parts[1]
        state = parts[3]
        if ":" not in local or state.upper() != "0A":
            continue
        try:
            port = int(local.rsplit(":", 1)[1], 16)
        except ValueError:
            continue
        if port == rport:
            return True
    return False


def _parse_listen(text: str, rport: int) -> bool | None:
    """Phân tích output kiểm tra LISTEN.

    Trả ``True`` (có nghe), ``False`` (không có gì nghe) hoặc ``None``
    (**không kiểm tra được** — máy remote thiếu mọi công cụ).
    """
    src: str | None = None
    body: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith(REMOTE_CHECK_MARKER + "="):
            src = line.split("=", 1)[1].strip()
            continue
        body.append(line)
    if src == "none":
        return None
    if src == "proc":
        return _proc_listening(body, rport)
    if src in ("ss", "netstat"):
        return _text_listening(body, rport)
    # Không có marker (script cũ / output lạ): chỉ dám kết luận khi thấy dòng
    # LISTEN — không thấy thì coi như "không kiểm tra được".
    if any("LISTEN" in line.upper() for line in body):
        return _text_listening(body, rport)
    return None


def _remote_listening(alias: str, rport: int) -> bool | None:
    """Hỏi host: có đang LISTEN ở ``rport`` không?

    ``False`` = không có gì nghe. ``None`` = **không kiểm tra được**.
    """
    rc, out, err = _ssh_run(alias, _REMOTE_CHECK, timeout=DEFAULT_TIMEOUT)
    if rc != 0:
        raise RoffError(
            EXIT_UNREACHABLE,
            "host_unreachable",
            detail=(err or out).strip() or f"rc={rc}",
            alias=alias,
        )
    return _parse_listen(out, rport)


# --------------------------------------------------------------------------------------
# Cổng local: ai đang giữ?
# --------------------------------------------------------------------------------------
def _parse_ss_owner(text: str, lport: int) -> tuple[bool, int | None]:
    """``ss -ltnp`` → ``(cổng có bị chiếm, pid sở hữu nếu biết)``."""
    for line in text.splitlines():
        if "LISTEN" not in line.upper():
            continue
        if lport not in [_port_of(f) for f in line.split()]:
            continue
        match = re.search(r"pid=(\d+)", line)
        return True, int(match.group(1)) if match else None
    return False, None


def _local_port_state(lport: int) -> tuple[bool, int | None]:
    """Kiểm tra cổng local có bị chiếm không (``ss -ltnp``).

    Không có ``ss`` → coi như không chiếm (không kiểm tra được thì không đoán).
    """
    rc, out, err = _run(["ss", "-ltnp"], timeout=10)
    if rc != 0:
        return False, None
    return _parse_ss_owner(out, lport)


def _pid_cmdline(pid: int) -> str:
    """Cmdline của một pid (rỗng nếu không đọc được)."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            return handle.read().decode("utf-8", "replace").replace("\0", " ")
    except OSError:
        return ""


def _is_own_master(pid: int, alias: str) -> bool:
    """Pid này có phải master ssh **của roff** không?

    Chỉ trả ``True`` khi có bằng chứng: cmdline chứa ControlPath của roff, hoặc
    pid là master đã ghi nhận trong state của roff. Mọi trường hợp khác coi như
    **tiến trình của người khác** → không đụng vào.
    """
    cmdline = _pid_cmdline(pid)
    path = control_path(alias)
    if path and path in cmdline:
        return True
    # Thư mục chứa socket của chính roff (có thể là đường dẫn rút gọn khi
    # state dir quá sâu — xem sshx.control_path).
    for base in {str(cm_dir()), str(Path(path).parent) if path else ""}:
        if base and base in cmdline:
            return True
    for rec in _load_state():
        if rec.get("alias") == alias and rec.get("master_pid") == pid:
            return True
    return False


def _has_other_tunnels(alias: str, exclude_lport: int) -> bool:
    return any(
        rec.get("alias") == alias and int(rec.get("lport", -1)) != exclude_lport
        for rec in _load_state()
    )


def _cancel_forward(alias: str, lport: int, rport: int) -> bool:
    """Huỷ forward của master roff trên cổng ``lport``.

    Chỉ đụng vào master của roff (ControlPath riêng). Không bao giờ kill pid.
    """
    spec = f"127.0.0.1:{lport}:127.0.0.1:{rport}"
    rc, _out, _err = _ssh_mux(alias, ["-O", "cancel", "-L", spec])
    if rc == 0:
        return True
    # OpenSSH không huỷ được từng forward → huỷ toàn bộ forward của master này,
    # nhưng chỉ khi không còn tunnel nào khác của cùng host.
    if not _has_other_tunnels(alias, lport):
        rc, _out, _err = _ssh_mux(alias, ["-O", "cancel"])
        if rc == 0:
            return True
    return False


def _wait_port_free(lport: int, timeout: float = 5.0) -> bool:
    """Chờ cổng local được nhả (sau khi huỷ forward cũ)."""
    deadline = time.time() + timeout
    while True:
        occupied, _pid = _local_port_state(lport)
        if not occupied:
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.2)


def _parse_master_pid(text: str) -> int | None:
    """``Master running (pid=1234)`` → ``1234``."""
    match = re.search(r"Master running \(pid=(\d+)\)", text or "")
    return int(match.group(1)) if match else None


def _ensure_master(alias: str) -> int | None:
    """Đảm bảo có ControlMaster của roff cho alias; trả pid của master."""
    rc, out, _err = _ssh_mux(alias, ["-O", "check"])
    pid = _parse_master_pid(out)
    if rc == 0 and pid:
        return pid
    # Chưa có master → mở một master nền (ControlPersist=300 giữ kết nối).
    rc, out, err = _ssh_mux(alias, ["-f", "-N"], timeout=90)
    if rc != 0:
        raise RoffError(
            EXIT_UNREACHABLE,
            "control_master_failed",
            detail=(err or out).strip() or f"rc={rc}",
            alias=alias,
        )
    rc, out, _err = _ssh_mux(alias, ["-O", "check"])
    return _parse_master_pid(out)


def _add_forward(alias: str, lport: int, rport: int) -> None:
    """Mở forward ``127.0.0.1:lport`` → ``127.0.0.1:rport`` trên remote."""
    spec = f"127.0.0.1:{lport}:127.0.0.1:{rport}"
    rc, _out, _err = _ssh_mux(alias, ["-O", "forward", "-L", spec])
    if rc == 0:
        return
    # OpenSSH cũ không có lệnh mux `forward` → mở client riêng, master giữ forward.
    rc, out, err = _ssh_mux(alias, ["-f", "-N", "-L", spec], timeout=60)
    if rc != 0:
        raise RoffError(
            EXIT_JOB_FAILED,
            "forward_failed",
            detail=(err or out).strip() or f"rc={rc}",
            alias=alias,
            lport=lport,
            rport=rport,
        )


# --------------------------------------------------------------------------------------
# API công khai
# --------------------------------------------------------------------------------------
def _as_port(value, name: str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise RoffError(
            EXIT_INPUT, "invalid_port", error=f"{name} không phải số: {value!r}"
        ) from None
    if not 1 <= port <= 65535:
        raise RoffError(
            EXIT_INPUT,
            "invalid_port",
            error=f"{name} ngoài khoảng 1..65535: {port}",
        )
    return port


def tunnel_start(host: str, lport: int, rport: int) -> Tunnel:
    """Mở tunnel ``127.0.0.1:lport`` (local) → ``host:rport`` (remote).

    Ném :class:`RoffError` khi:

    * remote **không** có gì nghe ở ``rport`` → ``reason="remote_listening"``,
      ``remote_listening=False``;
    * cổng local bị **tiến trình khác** giữ → ``reason="local_port_busy"``
      (không bao giờ kill tiến trình đó);
    * host không với được → exit 255 kèm ``detail``.
    """
    alias = (host or "").strip()
    if not alias:
        raise RoffError(EXIT_INPUT, "invalid_host", error="host rỗng")
    lport = _as_port(lport, "lport")
    rport = _as_port(rport, "rport")

    # 1. Remote có đang LISTEN ở rport không? (False = lỗi; None = không biết)
    listening = _remote_listening(alias, rport)
    if listening is False:
        raise RoffError(
            EXIT_JOB_FAILED,
            "remote_listening",
            remote_listening=False,
            error=f"{alias}:{rport} không có gì đang LISTEN",
            alias=alias,
            rport=rport,
        )

    # 2. Cổng local có bị chiếm không? Master cũ CỦA ROFF → huỷ forward (R3).
    occupied, pid = _local_port_state(lport)
    if occupied:
        if pid is not None and _is_own_master(pid, alias):
            if not _cancel_forward(alias, lport, rport):
                raise RoffError(
                    EXIT_JOB_FAILED,
                    "stale_forward",
                    error=(
                        f"không huỷ được forward cũ của roff trên cổng {lport} "
                        f"(master pid={pid})"
                    ),
                    alias=alias,
                    lport=lport,
                )
            if not _wait_port_free(lport):
                raise RoffError(
                    EXIT_JOB_FAILED,
                    "local_port_busy",
                    error=(
                        f"cổng local {lport} vẫn bị chiếm sau khi huỷ forward cũ"
                    ),
                    alias=alias,
                    lport=lport,
                )
        else:
            raise RoffError(
                EXIT_JOB_FAILED,
                "local_port_busy",
                local_port_busy=True,
                owner_pid=pid,
                error=(
                    f"cổng local {lport} đang bị giữ bởi pid {pid} "
                    "— không phải master của roff, roff KHÔNG kill tiến trình khác"
                ),
                alias=alias,
                lport=lport,
            )

    # 3. ControlMaster của roff (socket riêng, ControlPersist=300).
    master_pid = _ensure_master(alias)

    # 4. Mở forward.
    _add_forward(alias, lport, rport)

    # 5. Ghi lại state để `tunnel stop`/`tunnel list` biết.
    records = [
        rec
        for rec in _load_state()
        if not (rec.get("alias") == alias and int(rec.get("lport", -1)) == lport)
    ]
    note = (
        f"remote_listening={'True' if listening else 'unknown'}"
        f"; master={'reused' if master_pid else 'unknown'}"
    )
    records.append(
        {
            "alias": alias,
            "lport": lport,
            "rport": rport,
            "master_pid": master_pid,
            "control_path": control_path(alias),
            "created_at": time.time(),
            "note": note,
            "remote_listening": listening,
        }
    )
    _save_state(records)
    log.debug("tunnel mở: %s:%s -> %s:%s (master=%s)", alias, lport, alias, rport, master_pid)
    return Tunnel(alias, lport, rport, master_pid, note, remote_listening=listening)


def tunnel_stop(host: str, lport: int) -> dict:
    """Huỷ tunnel của ``host`` trên cổng local ``lport``.

    Chỉ huỷ forward qua ControlMaster của roff — không kill tiến trình nào.
    """
    alias = (host or "").strip()
    lport = _as_port(lport, "lport")
    records = _load_state()
    record = None
    for rec in records:
        if rec.get("alias") == alias and int(rec.get("lport", -1)) == lport:
            record = rec
            break
    if record is None:
        return {
            "ok": True,
            "alias": alias,
            "lport": lport,
            "stopped": False,
            "detail": "không có tunnel nào được ghi nhận",
        }

    rport = int(record.get("rport", 0))
    stopped = _cancel_forward(alias, lport, rport)
    master_stopped = False
    if not _has_other_tunnels(alias, lport):
        rc, _out, _err = _ssh_mux(alias, ["-O", "exit"])
        master_stopped = rc == 0
        try:
            Path(control_path(alias)).unlink()
        except OSError:
            pass
    _save_state(
        [
            rec
            for rec in records
            if not (rec.get("alias") == alias and int(rec.get("lport", -1)) == lport)
        ]
    )
    return {
        "ok": True,
        "alias": alias,
        "lport": lport,
        "rport": rport,
        "stopped": stopped,
        "master_stopped": master_stopped,
        "detail": "forward đã huỷ" if stopped else "không huỷ được forward",
    }


def tunnel_list() -> list[Tunnel]:
    """Liệt kê các tunnel roff đang quản lý (đọc state local, không gọi ssh)."""
    tunnels: list[Tunnel] = []
    for rec in _load_state():
        try:
            lport = int(rec.get("lport"))
            rport = int(rec.get("rport"))
        except (TypeError, ValueError):
            continue
        pid = rec.get("master_pid")
        tunnels.append(
            Tunnel(
                alias=str(rec.get("alias", "")),
                lport=lport,
                rport=rport,
                master_pid=int(pid) if pid else None,
                note=rec.get("note"),
            )
        )
    return tunnels
