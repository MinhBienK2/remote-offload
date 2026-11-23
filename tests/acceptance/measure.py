"""Bộ đo tài nguyên máy local — ngưỡng nghiệm thu Q28.

Mục tiêu của skill là **không làm nặng máy chính của người dùng**. Bộ đo này
lấy mẫu mọi tiến trình do roff sinh ra (roff, ssh, rsync, tar) và báo:

  * CPU% (theo %user+%system của /proc/<pid>/stat, chuẩn hoá theo số core)
  * RSS (MB)
  * %iowait và %idle của hệ thống

Ngưỡng đạt (docs/DESIGN.md §7):
  * CPU tổng của (roff + ssh + rsync) < 2% MỘT core
  * RAM tổng < 100 MB
  * lệnh benchmark tương tác chậm thêm ≤ 20%
  * không còn tiến trình roff sót lại sau khi kết thúc

**Phân định tiến trình**: máy local có thể đang chạy ssh của riêng người dùng
(reverse tunnel của họ chẳng hạn). Chỉ tính ssh là của roff khi nó dùng đúng
socket ControlPath của roff — nếu đếm mọi ssh thì số đo nhiễu bởi việc của
người dùng, và ngưỡng "không làm nặng máy chính" trở nên vô nghĩa.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

#: Thư mục chứa skill (để import roffkit lấy đúng đường dẫn ControlPath).
SKILL_DIR = Path(__file__).resolve().parents[2] / "skill"

#: Marker argv chắc chắn là của roff.
ROFF_ARGV_MARKERS = (
    "roff.py",
    "roffkit",
)

#: Chữ ký rsync của roff (xem roffkit.transfer._rsync_args).
RSYNC_ROFF_SIGNATURE = ("--no-owner", "--no-group", "--partial")

CLK_TCK = os.sysconf("SC_CLK_TCK")
PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")


@dataclass
class ProcSample:
    pid: int
    name: str
    cmd: str
    cpu_pct: float          # % của MỘT core (tích luỹ từ đầu)
    rss_mb: float


@dataclass
class Measurement:
    """Kết quả đo trong một khoảng thời gian."""
    samples: list[ProcSample] = field(default_factory=list)
    max_cpu_pct: float = 0.0
    max_rss_mb: float = 0.0
    mean_cpu_pct: float = 0.0
    iowait_pct: float = 0.0
    idle_pct: float = 100.0
    duration_s: float = 0.0
    leaked: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "max_cpu_pct_one_core": round(self.max_cpu_pct, 2),
            "mean_cpu_pct_one_core": round(self.mean_cpu_pct, 2),
            "max_rss_mb": round(self.max_rss_mb, 1),
            "iowait_pct": round(self.iowait_pct, 2),
            "idle_pct": round(self.idle_pct, 2),
            "duration_s": round(self.duration_s, 2),
            "leaked_processes": self.leaked,
        }

    def meets_budget(self, cpu_limit=2.0, rss_limit=100.0) -> tuple[bool, list[str]]:
        """Đạt hay chưa, kèm lý do vi phạm."""
        problems = []
        if self.max_cpu_pct > cpu_limit:
            problems.append(
                f"CPU {self.max_cpu_pct:.2f}% > {cpu_limit}% một core")
        if self.max_rss_mb > rss_limit:
            problems.append(f"RAM {self.max_rss_mb:.1f} MB > {rss_limit} MB")
        if self.leaked:
            problems.append(f"còn {len(self.leaked)} tiến trình roff sót: "
                            f"{self.leaked[:3]}")
        return (not problems), problems


def _cpu_ticks() -> tuple[int, int, int]:
    """(tổng tick, tick idle, tick iowait) từ /proc/stat."""
    try:
        with open("/proc/stat") as fh:
            line = fh.readline().split()
    except OSError:
        return 0, 0, 0
    values = [int(v) for v in line[1:]]
    total = sum(values)
    idle = values[3] if len(values) > 3 else 0
    iowait = values[4] if len(values) > 4 else 0
    return total, idle, iowait


# --------------------------------------------------------------------------
# phân định tiến trình của roff
# --------------------------------------------------------------------------
def _ssh_config_aliases() -> list[str]:
    """Alias trong ~/.ssh/config (mọi host roff có thể nói chuyện tới)."""
    path = Path(os.environ.get("ROFF_SSH_CONFIG") or (Path.home() / ".ssh" / "config"))
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    aliases: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.lower().startswith("host "):
            continue
        for name in stripped[5:].split():
            if name != "*" and not any(c in name for c in "*?!"):
                aliases.append(name)
    return aliases


def _roff_control_paths() -> set[str]:
    """Các đường dẫn socket ControlMaster mà roff có thể dùng."""
    paths: set[str] = set()
    try:
        if str(SKILL_DIR) not in sys.path:
            sys.path.insert(0, str(SKILL_DIR))
        from roffkit import sshx  # noqa: PLC0415 - import lười để bộ đo chạy độc lập
    except Exception:
        return paths
    for alias in _ssh_config_aliases():
        try:
            paths.add(str(sshx.control_path(alias)))
        except Exception:
            continue
    return paths


_CONTROL_PATH_RE = re.compile(r"ControlPath=([^\s]+)")


def _control_paths_in(cmd: str) -> list[str]:
    return _CONTROL_PATH_RE.findall(cmd)


def _is_roff_process(cmd: str) -> bool:
    """Tiến trình này có phải do roff tạo ra không?"""
    if not cmd:
        return False
    if any(marker in cmd for marker in ROFF_ARGV_MARKERS):
        return True
    if "rsync" in cmd and all(flag in cmd for flag in RSYNC_ROFF_SIGNATURE):
        return True
    # ssh của roff: dùng đúng socket ControlPath của roff.
    paths = _control_paths_in(cmd)
    if not paths:
        return False
    try:
        mine = _roff_control_paths()
    except Exception:
        mine = set()
    return any(p in mine for p in paths)


def _proc_sample(pid: int) -> ProcSample | None:
    try:
        with open(f"/proc/{pid}/stat") as fh:
            data = fh.read()
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            cmd = fh.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
    except OSError:
        return None
    # phần sau ')' chứa state và các field hệ thống
    tail = data[data.rfind(")") + 2:].split()
    if len(tail) < 13:
        return None
    utime, stime = int(tail[11]), int(tail[12])
    rss_pages = int(tail[21]) if len(tail) > 21 else 0
    name = data[data.find("(") + 1:data.rfind(")")]
    total_ticks = utime + stime
    return ProcSample(
        pid=pid,
        name=name,
        cmd=cmd[:160],
        cpu_pct=total_ticks / CLK_TCK * 100.0,
        rss_mb=rss_pages * PAGE_SIZE / (1024 * 1024),
    )


def _roff_pids() -> list[int]:
    out = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        sample = _proc_sample(int(entry.name))
        if sample and _is_roff_process(sample.cmd):
            out.append(sample.pid)
    return out


def _bench_loop(rounds: int = 20) -> float:
    """Benchmark tương tác nhẹ: trả về thời gian (giây)."""
    start = time.perf_counter()
    for _ in range(rounds):
        subprocess_run_burn()
    return time.perf_counter() - start


def subprocess_run_burn() -> None:
    """Một vòng tính nhẹ, tương tác với máy (đo độ trễ cảm nhận)."""
    total = 0
    for i in range(20000):
        total += i * i
    _ = total


def leaked_processes() -> list[str]:
    """Danh sách tiến trình của roff còn sống (dùng để kiểm tra rò sau khi job xong)."""
    return [f"{p.pid} {p.cmd[:80]}" for p in _roff_pids()]


def measure(duration_s: float = 20.0, interval_s: float = 0.5) -> Measurement:
    """Lấy mẫu trong `duration_s`, trả về kết quả tổng hợp."""
    result = Measurement()
    start = time.monotonic()
    prev_total, prev_idle, prev_iowait = _cpu_ticks()
    cpu_series: list[float] = []
    prev_proc: dict[int, ProcSample] = {}
    while time.monotonic() - start < duration_s:
        cur_total, cur_idle, cur_iowait = _cpu_ticks()
        delta_total = max(1, cur_total - prev_total)
        delta_idle = cur_idle - prev_idle
        delta_iowait = cur_iowait - prev_iowait
        idle_pct = 100.0 * delta_idle / delta_total
        iowait_pct = 100.0 * delta_iowait / delta_total
        result.idle_pct = min(result.idle_pct, idle_pct)
        result.iowait_pct = max(result.iowait_pct, iowait_pct)
        prev_total, prev_idle, prev_iowait = cur_total, cur_idle, cur_iowait

        cur_proc: dict[int, ProcSample] = {}
        total_cpu = 0.0
        total_rss = 0.0
        for pid in _roff_pids():
            sample = _proc_sample(pid)
            if not sample:
                continue
            cur_proc[pid] = sample
            total_rss += sample.rss_mb
            old = prev_proc.get(pid)
            if old is not None:
                delta = sample.cpu_pct - old.cpu_pct
                if delta >= 0:
                    total_cpu += delta
        result.max_cpu_pct = max(result.max_cpu_pct, total_cpu)
        result.max_rss_mb = max(result.max_rss_mb, total_rss)
        cpu_series.append(total_cpu)
        result.samples = list(cur_proc.values())
        prev_proc = cur_proc
        time.sleep(interval_s)

    result.duration_s = time.monotonic() - start
    if cpu_series:
        result.mean_cpu_pct = sum(cpu_series) / len(cpu_series)
    result.leaked = [f"{p.pid} {p.cmd[:80]}" for p in _roff_pids()]
    return result


if __name__ == "__main__":
    import json

    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 20.0
    m = measure(secs)
    print(json.dumps(m.as_dict(), ensure_ascii=False, indent=2))
