# Hợp đồng API nội bộ — roff v2

> **Đây là nguồn sự thật duy nhất cho mọi subagent.** Không tự ý đổi chữ ký ở đây.
> Mọi module dùng đúng những gì tài liệu này ghi. Nếu cần đổi: sửa file này **trước**,
> rồi mới sửa code, và ghi rõ trong báo cáo.

## 0. Bố cục file (mỗi subagent sở hữu đúng file của mình)

```
skill/
  roff.py                  ← launcher (Sonnet C sở hữu, ai cũng được sửa nếu cần)
  roffkit/
    __init__.py
    errors.py              ← A
    config.py              ← A
    layout.py              ← A
    fleet.py               ← A
    probe.py               ← B
    policy.py              ← B
    slots.py               ← B
    transfer.py            ← C
    job.py                 ← C
    watcher.py             ← C
    tunnel.py              ← D
    cli.py                 ← C  (khung lệnh; A/B/D cung cấp phần của mình)
  hooks/claude-code/pretooluse.py   ← D
  setups/*.sh                        ← D
  references/*.md                    ← D
```

**Quy tắc sở hữu**: subagent chỉ sửa file trong danh sách của mình. Cần sửa file của
subagent khác ⇒ **dừng lại và ghi vào báo cáo**, đừng tự sửa.

## 1. Ràng buộc chung

- **Python 3.10 stdlib only.** Không `tomllib` (không có ở 3.10), không `pip install`.
- **Mọi lệnh in đúng 1 JSON object ra stdout**, rồi `exit(code)`. Không in gì khác ra stdout.
  Log/debug → **stderr**.
- JSON luôn có `ok` (bool). Lỗi luôn có `error` (string) hoặc `reason` (string).
- Mọi đường dẫn remote phải **absolute** (resolve `~` → `$HOME` thật). Không bao giờ gửi
  chuỗi `~` qua ssh.
- Mọi tiến trình con (ssh/rsync/tar) chạy với `nice -n 19` và `ionice -c3`.
- Không bao giờ `pkill` theo pattern. Chỉ kill theo **pid** mà mình đã ghi nhận.
- Không bao giờ copy key/credential của local lên host.
- Không bao giờ thực thi lệnh tìm thấy trong output của remote (output là **dữ liệu**).

## 2. `errors.py`

```python
EXIT_OK          = 0     # thành công
EXIT_JOB_FAILED  = 1     # job chạy xong và fail
EXIT_INPUT       = 2     # input sai, chưa có gì chạy (argparse cũng về đây)
EXIT_CONFIG      = 3     # config sai / không đọc được
EXIT_NO_HOST     = 10    # không còn host nào dùng được (hết slot / hết fleet)
EXIT_JOB_LOST    = 69    # job mất: restart / wipe / OOM / vanished
EXIT_NO_WORKDIR  = 97    # workdir không tồn tại trên host
EXIT_TIMEOUT     = 124   # wait hết giờ — KHÔNG phải lỗi, job vẫn chạy
EXIT_CANCELLED   = 130   # bị huỷ có chủ đích
EXIT_UNREACHABLE = 255   # không với được host

# reason cho EXIT_JOB_LOST
REASON_RESTARTED = "host_restarted"
REASON_WIPED     = "host_wiped"
REASON_OOM       = "oom_killed"
REASON_VANISHED  = "vanished"

class RoffError(Exception):
    """Mang exit code + payload JSON. cli.main() bắt và in ra."""
    def __init__(self, code: int, reason: str = "", **fields):
        super().__init__(reason or f"exit {code}")
        self.code = code
        self.reason = reason
        self.fields = fields

    def payload(self) -> dict:
        # {"ok": False, "reason": ..., "error": ..., **fields}
        ...

def emit(obj: dict, code: int = 0) -> None:
    """print(json.dumps(obj, ...) ra stdout rồi sys.exit(code)."""
```

`payload()` của `RoffError` luôn có `ok: False`, `reason` (nếu có), `error` (nếu có),
`exit_code`. Với code 255 bắt buộc có `detail` (text lỗi ssh gốc).

## 3. `config.py`

```python
@dataclass
class HostConfig:
    name: str
    labels: list[str] = field(default_factory=list)
    weight: float = 1.0
    slots: int = 1
    mem_budget_mb: int = 0        # 0 = dùng mem_available thật

@dataclass
class Config:
    path: str | None
    fleet_include: list[str]      # glob, default ["*"]
    fleet_exclude: list[str]      # glob, default []
    probe_cache_s: int = 60
    max_parallel_transfers: int = 2
    nice: int = 19
    ionice: str = "idle"
    cache_max_mb: int = 5120
    cache_min_free_mb: int = 3072
    heavy_add: list[str]         # lệnh thêm vào danh sách nặng
    heavy_allow: list[str]       # lệnh miễn trừ
    hosts: dict[str, HostConfig]

    @staticmethod
    def load(path: str | None = None) -> "Config":
        """path=None → ~/.config/roff/config.ini nếu tồn tại, nếu không thì Config mặc định.
        File sai cú pháp → raise RoffError(EXIT_CONFIG).
        Section host.<alias>: labels (csv), weight (float), slots (int), mem_budget_mb (int)."""

    def host(self, name: str) -> HostConfig:
        """Trả về HostConfig của alias, hoặc mặc định nếu không có trong config."""
```

**Mặc định khi không có file config**: `fleet_include=["*"]`, `fleet_exclude=[]`,
`heavy_add=[]`, `heavy_allow=[]`, `hosts={}`.

## 4. `layout.py`

```python
DURABLE_CANDIDATES = ("/home/hatch", "/mnt/data", "/data", "/workspace", "/scratch")

@dataclass
class Layout:
    root: str                    # absolute, ví dụ /home/hatch/.roff
    durable: bool                # True nếu nằm trên mount riêng
    home: str                    # absolute $HOME của host (nơi đặt instance_token)

def layout_for(host: str, home: str, probe_result: dict | None = None) -> Layout:
    """Chọn root: durable candidate đầu tiên tồn tại & ghi được & khác filesystem với home
    → <candidate>/.roff ; ngược lại <home>/.roff (durable=False)."""

def instance_token(host: str, home: str) -> str:
    """Đọc token từ <home>/.roff-instance-token; nếu chưa có thì tạo (mktemp-style,
    32 hex) và ghi mode 600. Trả về token."""

def remote_path(profile: dict, path: str) -> str:
    """Giải '~' và '~/x' thành <profile['home']>/x. Đường dẫn khác giữ nguyên,
    bảo đảm absolute."""

def remote_mkdirs(host: str, path: str) -> None:
    """mkdir -p trên host qua ssh."""
```

**Bất biến**: `instance_token` nằm ở `$HOME` **cố ý** — `$HOME` bị xoá khi host wipe,
nên token đổi ⇒ phát hiện wipe. Token **không** nằm ở layout root bền vững.

## 5. `fleet.py`

```python
@dataclass
class SshHost:
    alias: str
    hostname: str | None
    port: int | None
    user: str | None
    proxy_command: str | None

def parse_ssh_config(path: str | None = None) -> list[SshHost]:
    """Đọc ~/.ssh/config, theo Include (dùng glob, đệ quy, chống lặp).
    Bỏ qua các từ khoá option (HostName, Port, User, ProxyCommand, IdentityFile…).
    Host block 'Host a b' sinh 2 alias."""

def fleet_aliases(cfg: Config) -> list[str]:
    """Lọc alias theo fleet_include/fleet_exclude (fnmatch, hỗ trợ 'muse-*')."""
```

**Bẫy đã biết (R14, R15)**: phải đọc `Include`; phải không nhầm từ khoá option là tên host.

## 6. `probe.py`

```python
@dataclass
class Profile:
    alias: str
    reachable: bool = False
    error: str | None = None        # khi không reachable
    detail: str | None = None       # text lỗi ssh gốc
    hostname: str | None = None
    boot_id: str | None = None
    instance_token: str | None = None
    home: str | None = None
    layout_root: str | None = None
    durable: bool = False
    tools: dict[str, str] = field(default_factory=dict)   # {"ffmpeg": "/usr/bin/ffmpeg"}
    gpu: dict | None = None          # None = KHÔNG có GPU. Không bao giờ suy đoán
    cores: int = 0
    mem_total_mb: int = 0
    mem_available_mb: int = 0
    cgroup_mem_limit_mb: int | None = None    # None = không đọc được
    cgroup_mem_used_mb: int | None = None
    load1: float = 0.0
    disk_free_mb: int = 0
    internet: bool = False
    probed_at: float = 0.0

    def to_dict(self) -> dict
    @staticmethod
    def from_dict(d: dict) -> "Profile"

def probe(alias: str, timeout: int = 30) -> Profile:
    """Một lần ssh, gom TẤT CẢ vào 1 script (một round-trip):
    hostname, boot_id, home, nproc, meminfo, cgroup, loadavg, df, PATH tools, internet.
    Nếu ssh lỗi → Profile(reachable=False, error=..., detail=<text lỗi gốc>)."""

def probe_many(aliases: list[str], timeout: int = 30, parallel: bool = False) -> list[Profile]:
    """Mặc định SERIAL (bridge một phiên). parallel=True chỉ khi caller biết chắc."""

def cached_profiles(aliases: list[str], cfg: Config) -> list[Profile]:
    """Cache ở ~/.local/state/roff/probes.json, TTL = cfg.probe_cache_s."""
```

**Cách đo internet**: `curl -sS -o /dev/null -w '%{http_code}' -m 8 https://github.com`.
Bị timeout ⇒ `internet=False`. **Không** coi `429`/`already active` là "không có internet" —
đó là dấu hiệu bridge một phiên, phải ghi riêng.

## 7. `policy.py`

```python
@dataclass
class Need:
    tools: list[str] = field(default_factory=list)
    mem_mb: int = 0
    disk_mb: int = 0
    cpus: int = 0
    internet: bool = False

@dataclass
class Choice:
    host: str
    score: float
    reasons: list[str]                       # lý do được chọn (chữ cho người đọc)
    rejected: dict[str, str]                 # alias -> lý do bị loại

def select(profiles: list[Profile], need: Need, cfg: Config,
           busy: dict[str, int] | None = None,
           prefer_hosts: list[str] | None = None) -> Choice | None:
    """Lọc cứng rồi chấm điểm. Trả None nếu không host nào dùng được.
    `prefer_hosts` = host đang giữ dữ liệu (--in job:) → cộng điểm mạnh."""

def score(profile: Profile, need: Need, cfg: Config, busy: int) -> tuple[float, list[str]]:
    """Công thức (tham chiếu, được phép điều chỉnh có lý do và phải ghi lại):
      ấm  : project đã có +40 | toolchain đã cài +10 | giữ dữ liệu +50
      khoẻ: cores * (1 - load1/cores) * 3 ; mem_available_mb / 100
      ổn  : durable +15 ; restart_24h * -20
      nhãn: +25 mỗi label khớp
      ưu tiên: * cfg.host(alias).weight
      bận : -30 mỗi job roff đang chạy
    """
```

**Sàn RAM**: host bị loại nếu `mem_available_mb < max(256, need.mem_mb + reserve)`,
trong đó `reserve = cfg.host(alias).mem_reserve_mb` (mặc định 256). Cộng dồn, không
phải `max()` — job nhỏ vẫn chạy được trên host còn 300 MB trống; job 4 GB thì host
phải còn 4 GB + 256 MB.
**Sàn disk**: loại nếu `disk_free_mb < need.disk_mb`.

## 8. `slots.py`

Sổ chiếm chỗ **dùng chung cho mọi phiên, agent, harness** — trên local, dùng `flock`.

```python
@dataclass
class Lease:
    host: str
    ticket: str            # id để tham chiếu khi release
    acquired_at: float

class SlotLedger:
    def __init__(self, state_dir: str | None = None):
        """state_dir mặc định ~/.local/state/roff"""

    def acquire(self, host: str, slots: int, timeout: float,
                priority: str = "normal") -> Lease:
        """Chờ tới khi host còn slot trống. FIFO theo thời điểm xin; 'high' chen đầu.
        Hết timeout → raise RoffError(EXIT_NO_HOST, 'no slot on <host>').
        Phải dùng fcntl.flock để tránh race."""

    def release(self, lease: Lease) -> None

    def counts(self) -> dict[str, int]        # host -> số slot đang dùng
    def queue_length(self) -> int
    def position(self, ticket: str) -> int   # vị trí trong hàng (1-based)
```

Cấu trúc file trạng thái: `~/.local/state/roff/slots.json`, kèm `slots.lock`.

## 9. `transfer.py`

```python
@dataclass
class TransferResult:
    ok: bool
    bytes_transferred: int
    files: list[dict]        # [{"path":..., "sha256":..., "bytes":...}]
    resumed: bool
    error: str | None

DEFAULT_EXCLUDES = (".git", "node_modules", ".venv", "venv", "__pycache__",
                    ".next", "dist", "build", ".cache")

INCOMPRESSIBLE_SUFFIXES = (".mp4",".mov",".mkv",".avi",".webm",".jpg",".jpeg",
                           ".png",".gif",".zip",".gz",".xz",".bz2",".zst",".7z",".pdf")

def push(host: str, local_dir: str, remote_dir: str,
         excludes: list[str] | None = None, timeout: int = 3600) -> TransferResult:
    """rsync -a --no-owner --no-group --partial nếu host có rsync,
    ngược lại tar-over-ssh. --partial để resume (R: đứt giữa push).
    Không nén nếu là file đã nén."""

def pull(host: str, remote_dir: str, local_dir: str, timeout: int = 3600) -> TransferResult:
    """Kèm manifest sha256 của các file MỚI mang về."""

def pull_path(host: str, remote_path: str, local_path: str) -> TransferResult:
    """Kéo một file/dir bất kỳ từ host."""

def stream_between(src_host: str, src_path: str,
                    dst_host: str, dst_path: str, timeout: int = 3600) -> TransferResult:
    """ssh A 'tar c' | ssh B 'tar x' — KHÔNG ghi đĩa local (Q43)."""

def transfer_slot(timeout: float) -> ContextManager:
    """Giới hạn max_parallel_transfers luồng đồng thời toàn máy (Q37),
    dùng chung qua SlotLedger (state_dir riêng 'transfers')."""
```

**Quy tắc**: mọi lệnh ssh/rsync chạy `nice -n 19 ionice -c3` (lấy từ config).
`--no-owner --no-group` **luôn** (R4).

## 10. `job.py`

```python
JOB_STATES = ("queued", "launched", "running", "exited", "lost", "cancelled")

@dataclass
class Job:
    id: str                       # "YYMMDD-HHMMSS-xxxx"
    cmd: str
    host: str
    project: str
    created_at: float
    state: str = "queued"
    exit_code: int | None = None
    reason: str | None = None     # lý do nếu state == "lost"
    attempts: list[dict] = field(default_factory=list)   # lịch sử failover
    cpus: int = 0
    mem_mb: int = 0
    out_remote: str | None = None
    out_local: str | None = None
    shard: int | None = None
    shards: int | None = None
    item: str | None = None
    needs_retry: bool = True      # False khi --side-effects
    heartbeat: float = 0.0

    def to_dict(self) / from_dict(d) -> Job

def new_job_id() -> str
def ledger_dir() -> str                     # ~/.local/state/roff/jobs
def save_job(job: Job) -> None
def load_job(job_id: str) -> Job            # raise RoffError(EXIT_INPUT) nếu không có
def list_jobs(state: str | None = None) -> list[Job]

def launch(host: str, job: Job, env: dict[str,str] | None = None,
           cpus: int = 0, mem_mb: int = 0) -> None:
    """Trên host: setsid nohup <cmd> > <jobdir>/log 2>&1; ghi <jobdir>/env (mode 600),
    <jobdir>/meta.json. Job sống sót khi ssh rớt."""

def remote_state(host: str, job: Job, timeout: int = 30) -> dict:
    """Trả {"state": "running"|"exited"|"lost"|..., "exit": int|None,
            "boot_id": str|None, "instance_token": str|None, "workdir_exists": bool}."""

def cancel(host: str, job: Job) -> dict:
    """Kill process group của job (pid đã ghi trong meta.json). Không pkill theo pattern."""

def heartbeat_path(job_id: str) -> str
```

**Quy tắc nhận diện mất job** (dùng trong `remote_state`):
- `exit` file tồn tại → `exited`, đọc exit code
- `exit` không có, `boot_id` **khác** lúc launch → `lost`, reason `host_restarted`
- `exit` không có, `instance_token` **khác** → `lost`, reason `host_wiped`
- `exit` không có, `boot_id`+token **giống**, không còn process → `lost`, reason `vanished`
- process biến mất giữa chừng khi RAM thấp → reason `oom_killed`

## 11. `watcher.py`

```python
def watch(job_id: str) -> None:
    """Vòng lặp giám sát. Đây là tiến trình NỀN, tách phiên.
    Mỗi vòng:
      1. đọc job từ ledger
      2. remote_state()
      3. phân nhánh:
         - exited & exit==0 → pull artifact → state=exited, exit=0 → kết thúc
         - exited & exit!=0 → state=exited, exit=code → kết thúc (KHÔNG retry)
         - lost & needs_retry & attempts < retry → failover
         - lost & không retry → state=lost → kết thúc
         - host unreachable nhưng chưa xác minh restart → chờ, thử lại, KHÔNG failover
         - còn chạy → sleep 5s, ghi heartbeat
    Failover: chọn host mới bằng policy.select (loại host cũ),
      push lại input (nếu cần), launch lại, ghi vào job.attempts."""

def spawn_watcher(job_id: str) -> None:
    """setsid nohup python3 <skill>/roff.py __watch <job_id> </dev/null >/dev/null 2>&1 &"""

def wait_for(job_id: str, timeout: float = 7200) -> Job:
    """Chờ tới trạng thái cuối. Poll LEDGER CỤC BỘ (không ssh)."""

def failover_decision(profile_before: dict, profile_now: dict) -> str | None:
    """Trả reason nếu chắc chắn mất job, None nếu chưa chắc (chỉ mất tunnel).
    Quy tắc:
      boot_id khác        → 'host_restarted'
      instance_token khác → 'host_wiped'
      giống cả hai        → None  (chỉ mất tunnel → KHÔNG failover)"""
```

**Quy tắc vàng**: mất kết nối **không** đồng nghĩa job chết. Chỉ failover khi chắc chắn.

## 12. `tunnel.py`

```python
@dataclass
class Tunnel:
    alias: str
    lport: int
    rport: int
    master_pid: int | None
    note: str | None

def tunnel_start(host: str, lport: int, rport: int) -> Tunnel:
    """Kiểm tra remote có đang LISTEN ở rport không (ss/netstat/proc/net/tcp).
    Không listen → raise RoffError(EXIT_JOB_FAILED, 'remote_listening', remote_listening=False).
    Phát hiện master ssh cũ đang giữ lport (ss -ltnp) → huỷ forward bằng
    'ssh -O cancel' CHỈ của chính nó, KHÔNG kill pid không liên quan (R3)."""

def tunnel_stop(host: str, lport: int) -> dict
def tunnel_list() -> list[Tunnel]
```

## 13. `cli.py` — khung lệnh

```python
def main(argv: list[str] | None = None) -> int:
    """argparse; lỗi argparse → stderr + exit 2 (KHÔNG in JSON).
    Bắt RoffError → in JSON + exit(code)."""
```

Danh sách lệnh: `do`, `status`, `wait`, `result`, `logs`, `cancel`, `jobs`, `hosts`,
`probe`, `push`, `pull`, `ensure`, `tunnel`, `gc`, và `__watch` (nội bộ).

**`roff do`** in JSON tối thiểu:
```json
{"ok": true, "job": "<id>", "state": "queued", "host": "<alias>",
 "host_reasons": ["..."], "ledger": "<path>", "next": ["roff wait <id>"]}
```

**`roff wait`** khi hết giờ ⇒ exit **124**, JSON `{"ok": true, "state": "running", ...}`.

## 14. `skill/roff.py` — launcher

```python
#!/usr/bin/env python3
"""Launcher: đặt thư mục này vào sys.path rồi gọi roffkit.cli.main()."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from roffkit.cli import main
if __name__ == "__main__":
    sys.exit(main())
```

## 15. Test

- Mọi module viết test riêng trong `tests/unit/test_<module>.py`.
- Test hồi quy R1–R20 do **Claude** viết, ở `tests/regression/`.
- Dùng `pytest`. Không network thật ở `tests/unit` (fake ssh qua `PATH`).
- Marker `acceptance` cho test cần fleet thật.

## 15b. Bổ sung chốt sau vòng review phần A

Các quyết định dưới đây **bổ sung** cho hợp đồng, không thay đổi phần nào đã có.

### 15b.1 Runner shell — chữ ký chuẩn

```python
# skill/roffkit/sshx.py  (Claude sở hữu, phần C dùng)
def run_shell(host: str, script: str, timeout: float = 60.0) -> tuple[int, str, str]:
    """(rc, stdout, stderr). Mọi lệnh ssh đi qua đây:
    - BatchMode=yes, ConnectTimeout, ServerAliveInterval=30/CountMax=5
    - ControlMaster của roff (socket ~/.local/state/roff/cm/<alias>.sock)
    - nice -n 19 ionice -c3
    - KHÔNG bao giờ truyền secret trong argv"""
```

`layout.ShellRunner` (phần A) dùng protocol `(host, script) -> (rc, stdout, stderr)`.
`run_shell` **tương thích** với protocol đó (timeout là tham số keyword có default).
⇒ Phần C gọi `layout.instance_token(host, home, run=run_shell)` và
`layout.remote_mkdirs(host, path, run=run_shell)`.

### 15b.2 `emit()` không tự chèn `ok`

`emit(obj, code)` **dump đúng** object được truyền vào. Trách nhiệm có `ok` thuộc về
`cli.main()`. Sửa §16: "JSON do cli in **luôn** có `ok`".

### 15b.3 `SshHost.hostname` có thể là `None`

Khi block `Host` không khai báo `HostName`, `hostname` là `None`. Khi cần kết nối,
dùng **`host.hostname or host.alias`** — đúng như OpenSSH (alias là tên host mặc định).

### 15b.4 `max_parallel_transfers` nằm ở `[local]`

`[local]` là nguồn chính; `[fleet]` vẫn được chấp nhận làm fallback để tương thích.
Sửa `DESIGN.md` §5.10 cho khớp.

### 15b.5 Dedupe máy vật lý + phát hiện flap — thuộc `probe.py`

```python
def physical_host_key(profile: Profile) -> tuple:
    """Khoá nhận diện máy VẬT LÝ: (hostname, boot_id, instance_token)."""

def detect_flapping(profiles: list[Profile]) -> list[dict]:
    """Nhóm profile theo physical_host_key nhưng khác instance_token
    ⇒ alias đó flap (trỏ cùng boot_id nhưng khác container).
    Trả list dict: {"boot_id":..., "aliases": [...], "tokens": {...}}."""

def same_physical_host(a: Profile, b: Profile) -> bool:
    """Cùng boot_id + cùng instance_token + cùng hostname."""
```

`policy.select()` **không được** chọn hai alias thuộc cùng một máy vật lý trong cùng
một lần gọi (nếu chọn host A thì alias khác của A bị loại với lý do "cùng máy vật lý").

### 15b.6 `watcher` — hai hàm quyết định, phải có tên đúng

```python
def failover_decision(profile_before: dict, profile_now: dict) -> str | None:
    """'host_restarted' | 'host_wiped' | None (chưa chắc → KHÔNG failover)."""

def should_retry(kind: str, attempts: int, max_attempts: int, needs_retry: bool) -> bool:
    """kind='job_failed' → LUÔN False (bug R20: job tự fail không được retry).
    kind='host_restarted'|'host_wiped'|'oom_killed'|'vanished' →
        True khi needs_retry and attempts < max_attempts, ngược lại False."""
```

### 15b.7 `transfer` — hai hàm phụ

```python
def host_has_rsync(host: str) -> bool: ...
```
Dùng bởi `push()` để quyết định rsync hay fallback tar.

### 15c. `shared.py` — kho input dùng chung theo host

Băng thông local→host đo được chỉ **0,5–1 MB/s**. Đẩy cùng một dataset cho
từng job là đẩy N lần ⇒ giờ. Vì vậy input được đẩy **một lần cho mỗi host**,
rồi các job trên host đó dùng chung.

```python
def shared_key(local_path: str) -> str:
    """key = sha256(abspath(nguồn))[:16] — cùng nguồn trên cùng host chỉ 1 bản copy."""

def shared_root(layout_root: str) -> str: ...        # <layout_root>/inputs
def shared_dir(layout_root: str, local_path: str) -> str: ...    # <root>/{key}
def shared_marker(layout_root: str, local_path: str) -> str: ... # <root>/{key}.complete
def shared_remote_path(layout_root: str, key: str, rel: str) -> str: ...
def shared_rel(local_path: str) -> str: ...          # "" nếu nguồn là thư mục

def ensure_shared_input(host, local_path, layout_root, timeout=...) -> str:
    """Bảo đảm có 1 bản copy của nguồn local trên host; trả đường dẫn remote."""
```

Layout trên host:

```
<layout_root>/inputs/{key}/           bản copy dùng chung của 1 nguồn local
<layout_root>/inputs/{key}.complete   marker JSON (nằm CẠNH, không bị chmod)
<layout_root>/inputs/{key}.lock/      mutex tạm (mkdir), xoá khi xong
```

Quy tắc:

- Nguồn là **thư mục** ⇒ `{key}/` chứa *nội dung* thư mục đó (cùng ngữ nghĩa với
  `transfer.push`). Nguồn là **file** ⇒ `{key}/` chứa đúng file đó.
- Marker chứa `{source, kind, size, mtime, basename}`. Nếu `size`/`mtime` của nguồn
  local **không đổi** ⇒ dùng lại, **không truyền lại**. Nếu đã đổi ⇒ dọn bản cũ rồi
  đẩy lại.
- Cây input được `chmod -R a-w` sau khi đẩy xong: job sửa input của job khác sẽ
  **fail loudly** thay vì hỏng dữ liệu âm thầm. Muốn sửa thì copy vào `$ROFF_WORK`.
- Mutex bằng `mkdir` (atomic). Lock quá 30 phút ⇒ coi là stale, chiếm lại.
- `options["input_remote"]` = đường dẫn remote của bản copy; `options["input_dir"]`
  = đường dẫn **local** của nguồn (watcher dùng để đẩy lại khi failover).

### 15d. `toolchain.py` — ensure toolchain dùng chung

```python
def ensure_tools(host, profile, tools) -> list[dict]: ...
```

Tách khỏi `cli.py` vì `cli.py` import `watcher.py` (một chiều): watcher cần ensure
tools trên **host mới** khi failover mà không được tạo import vòng.

### 15e. `probe.py` — cache phải tự chữa

Cache probe (`<state>/probes.json`) lưu `{"ts":…, "profile": {…}}`. Khi đọc, phải
**kiểm tra `profile["alias"] == alias` đang yêu cầu**; nếu lệch (cache cũ, file bị
sửa tay, schema đổi) ⇒ coi như cache hỏng và **probe lại**. Một entry hỏng không
được làm biến mất host khỏi fleet (lỗi đã gặp thật: `roff do --host muse-pmb333`
báo `unknown_host` trong khi host đó sống và có trong config).

Ngoài ra, `--host <alias>` phải **probe lại chính alias đó** (bỏ qua cache) để
ghim host không bao giờ bị cache cũ giấu; lỗi phải là `host_unreachable` kèm
nguyên văn lỗi ssh, không phải `unknown_host`.

### 15f. `slots.py` — hàng đợi bền vững (P2)

`roff do` **không được chờ slot** (timeout 0). Hết slot là "máy đang bận", không
phải lỗi. Vì vậy cần tách "đăng ký vào hàng" khỏi "chiếm slot":

```python
class SlotLedger:
    def request_slot(self, host: str, slots: int,
                     priority: str = "normal") -> str:
        """Đăng ký vào hàng đợi của host, **vĩnh viễn**, trả ticket.
        Không chờ, không chiếm slot. 'high' chen đầu, còn lại FIFO."""

    def try_acquire(self, ticket: str) -> Lease | None:
        """Thử chiếm slot nếu ticket đang **đầu hàng** của host đó và host còn
        capacity. Không được ⇒ None (ticket vẫn nằm trong hàng, giữ nguyên thứ tự).
        Được ⇒ ticket rời hàng, trả Lease."""

    def cancel_slot(self, ticket: str) -> None:
        """Bỏ ticket khỏi hàng đợi (job bị huỷ / hết hạn)."""
```

`acquire()` (API cũ) được **viết lại trên nền** `request_slot` + `try_acquire`
để không có hai nguồn sự thật; hành vi cũ giữ nguyên (hết timeout ⇒
`RoffError(EXIT_NO_HOST, 'no slot on <host>')`).

Quy tắc FIFO: **chỉ ticket đầu hàng của host** mới được cấp slot — nếu không,
một job đang poll sẽ chen lên trước job đã chờ lâu hơn.

### 15g. Ledger options — `input_specs` và `slot_host`

`job.set_job_options()` phải giữ được:

| Khoá | Ý nghĩa |
|---|---|
| `slot_host` | host mà job sẽ xin slot (watcher dùng khi failover cần xin lại) |
| `slot_ticket` | ticket hiện đang giữ (bằng rỗng nếu chưa có) |
| `input_specs` | `[{kind: "path"\|"git"\|"http"\|"job", value: "<url hoặc path hoặc job id>"}, …]` — **mọi** input, để watcher dựng lại được sau failover |

Không có `input_specs` thì failover sang host mới sẽ chạy với `$ROFF_IN` trỏ
tới path không tồn tại ⇒ job chết ngay mà ledger vẫn báo "launch thành công".

### 15h. `cli.py` — lỗi `no_host` phải kèm `jobs`

Khi `roff do` không còn host dùng được **sau khi đã tạo được vài job** (lô
`--each` bị host chết giữa chừng), payload lỗi phải kèm khoá `jobs` =
`[{job, host}, …]` của các job **đã tạo**. Không có khoá này, agent không biết
job id nào đang chạy ⇒ job mồ côi.

### 15i. `slots.py` — ai giữ slot, và thu hồi slot mồ côi

`request_slot` ghi ticket **vĩnh viễn** và `try_acquire` chỉ cấp slot cho **đầu
hàng** ⇒ một ticket mồ côi ở đầu hàng kẹt **vĩnh viễn** host đó. Vì vậy mọi
ticket/lease phải biết **ai là chủ**:

```python
class SlotLedger:
    def request_slot(self, host: str, slots: int,
                     priority: str = "normal", owner: str = "") -> str:
        """owner = id của job sở hữu ticket (rỗng nếu chưa biết)."""

    def try_acquire(self, ticket: str, owner: str = "",
                    holder_pid: int = 0, holder_started: float = 0.0) -> Lease | None:
        """Chấp nhận owner/holder; thiếu thì **kế thừa** từ entry trong hàng."""

    def set_holder(self, ticket: str, pid: int, started: float | None = None) -> bool:
        """Ghi (lại) chủ đang giữ slot — watcher gọi khi nó nắm quyền."""

    def reclaim_orphans(self, alive_owners=None, max_age_s=..., 
                        exclude_owners=None, keep_leases=None) -> list[dict]:
        """Thu hồi ticket/lease mồ côi. reason ∈ holder_dead|owner_dead|expired."""


def reconcile_slots(state_dir=None, max_age_s=..., exclude_owners=None,
                    mark_lost=True) -> dict:
    """Dọn sổ slot theo **ledger job local** làm nguồn sự thật.

    * lease mà tiến trình giữ (watcher) đã chết ⇒ thu hồi **và** đánh dấu job
      chủ là `lost` với reason `watcher_died` (exit 69) — roff đã mất khả năng
      theo dõi job đó nên giữ slot là vô nghĩa;
    * lease của job **có** watcher còn sống ⇒ **không** đụng tới;
    * ticket quá hạn (mặc định 24 h) ⇒ thu hồi.

    ``state_dir`` phải chỉ **cùng** thư mục mà `SlotLedger` dùng — ledger job
    cũng được đọc từ đó (`list_jobs(state_dir=...)`). Đọc ledger ở thư mục khác
    là bug: job thật của người dùng sẽ bị tính là "owner còn sống" (giữ slot
    mồ côi) còn job của thư mục này thì bị coi là đã chết (dọn oan).

    Khi **đọc ledger lỗi** ⇒ danh sách owner sống phải là *"không biết"*
    (`None`), **không** phải *"không ai sống"* (`set()` rỗng) — nếu là `set()`
    rỗng thì **mọi** ticket đang xếp hàng bị dọn và job xếp hàng chết âm thầm.

    Idempotent. Trả `{"removed": [...], "lost_jobs": [...]}`."""
```

Hai trợ giúp để kiểm tra tiến trình còn sống (pid có thể bị tái sử dụng ⇒ so cả
thời điểm khởi tạo):

```python
def proc_start_time(pid: int) -> float: ...
def pid_alive(pid: int, started: float | None = None) -> bool: ...
```

Ledger options mới: `watcher_pid`, `watcher_started` (để reconcile biết chủ
ticket còn sống hay không), cùng `slot_host`, `slot_ticket` (§15g).

### 15j. `perm.py` — quyền file state

Mọi file state chứa dữ liệu người dùng phải **0600**, mọi thư mục state **0700**
— kể cả file **đã tồn tại** từ trước (đo thật: `audit.jsonl` từng là 0664).

```python
FILE_MODE = 0o600
DIR_MODE = 0o700

def secure_file(path: str) -> None: ...
def secure_dir(path: str) -> None: ...
def normalize_tree(root: str) -> None: ...
```

Mọi hàm **không** ném lỗi (lỗi quyền không được làm hỏng lệnh).

### 15k. `policy.py` — "đang bận" khác "hỏng"

`reject_reason()` / `select()` / `reject_all()` nhận thêm `ignore_busy`:

| Cờ | Ý nghĩa |
|---|---|
| `ignore_slots=True` | host **đã đầy slot** vẫn là ứng viên hợp lệ (đầy slot = "đang bận" ⇒ job xếp hàng) |
| `ignore_busy=True` | host **bridge đang bận** vẫn là ứng viên hợp lệ (bridge một phiên ⇒ tạm thời) |

Không cờ nào bật thì lý do loại vẫn hiện trong `rejected`.

### 15l. API mới của wave W3

```python
# transfer.py
def host_has_rsync(host: str, timeout: int = RSYNC_PROBE_TIMEOUT) -> bool:
    """Host có `rsync` không? Dò 1 lần rồi cache theo alias (trong bộ nhớ).

    Probe lỗi ⇒ trả ``True`` (không kết luận được) ⇒ rsync vẫn được thử,
    fallback tar giữ nguyên."""

def should_compress_batch(root: str) -> tuple[bool, dict]:
    """Có nén lô này không — quyết định theo **loại nội dung chiếm ưu thế**.

    ≥80 % byte là text ⇒ nén; ≥80 % là media ⇒ không nén; phải tiết kiệm ≥10 %
    mới nén. Đo trên **mẫu** (≤4 MB, ≤16 file) — nén cả cây local là quá tốn
    CPU trên máy người dùng."""

# shared.py
def gc_shared_store(host: str, layout_root: str, keep_keys=(),
                    max_age_s: float = DEFAULT_STORE_MAX_AGE_S,
                    now: float | None = None) -> dict:
    """Dọn kho input dùng chung phía host.

    Dùng lại đúng cơ chế khoá sẵn có (``.complete`` + ``.lock``) — **không**
    thêm cơ chế khoá mới. Không xoá entry đang được job chạy dùng
    (``keep_keys``) và không xoá entry còn mới."""

# sshx.py
def ssh_config_path() -> str: ...
def ssh_config_args() -> list[str]:
    """Tham số `-F <path>` cho ssh (rỗng nếu không có config thay thế).

    Bắt buộc: `fleet.parse_ssh_config()` đọc `$ROFF_SSH_CONFIG` — nếu ssh thật
    không đọc file đó thì alias trùng tên trỏ sang **máy khác**."""

# fleet.py
def env_fleet_aliases(cfg: Config) -> list[str]:
    """Alias của fleet theo `$ROFF_SSH_CONFIG` (seam cho test)."""

# job.py — mọi hàm ledger nhận `state_dir` tuỳ chọn
def ledger_dir(state_dir: str | None = None) -> str: ...
def list_jobs(state: str | None = None, state_dir: str | None = None) -> list[Job]: ...
def load_job(job_id: str, state_dir: str | None = None) -> Job: ...
def job_options(job_id: str, state_dir: str | None = None) -> dict: ...
def set_job_options(job_id: str, state_dir: str | None = None, **options) -> dict: ...
def save_job(job: Job, state_dir: str | None = None) -> None: ...
```

`state_dir` mặc định là `$ROFF_STATE_DIR` (hoặc `~/.local/state/roff`).
**Nguyên tắc**: nếu một lớp nhận `state_dir` thì mọi lớp dưới nó phải dùng
**đúng** thư mục đó — đọc ledger ở thư mục khác là bug (xem §15i).


## 16. Định dạng JSON (chuẩn hoá cho mọi lệnh)

```json
{
  "ok": true,
  "exit_code": 0,
  "state": "exited",
  "exit": 0,
  "reason": null,
  "error": null,
  "detail": null,
  "host": "muse-pmb333",
  "job": "261123-101530-ab12"
}
```

Khoá không có giá trị thì **bỏ hẳn** khỏi JSON (không trả `null` thừa), trừ `reason`/`error`/
`detail` ở trên.
