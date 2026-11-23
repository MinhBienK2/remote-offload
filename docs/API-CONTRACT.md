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
    shared.py              ← W3  (kho input dùng chung theo host: lock + marker)
    inputs.py              ← W3  (chuẩn bị input: path/git/http/job)
    toolchain.py           ← W3  (đảm bảo host có tool cần dùng)
    sshx.py                ← W3  (ControlMaster, fingerprint, cổng an toàn)
    perm.py                ← W3  (quyền file state: 0600 / thư mục 0700)
    doctor.py              ← W5  (`roff doctor` — chỉ báo, không sửa)
    infer.py               ← W6  (suy ra tool từ câu lệnh)
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
EXIT_CONFIG      = 3     # config sai / không đọc được / KHÔNG TỒN TẠI (B27)
EXIT_NO_HOST     = 10    # không còn host nào dùng được (hết slot / hết fleet)
EXIT_PULL_FAILED = 74    # job CHẠY XONG (exit 0) nhưng artifact CHƯA về local
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

# reason cho EXIT_PULL_FAILED
REASON_PULL_FAILED = "pull_failed"

# reason cho EXIT_PULL_FAILED do sync-back (B26) — cùng exit 74
REASON_SYNC_BACK_FAILED = "sync_back_failed"
REASON_SYNC_BACK_OUTSIDE_PROJECT = "sync_back_outside_project"   # exit 2

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

**Phân biệt 1 và 74 (B23) — quan trọng cho agent:**

| exit | Nghĩa | Agent nên làm gì |
|---|---|---|
| `1` | **job** chạy xong và **fail** (`exit` của job ≠ 0) | **đừng** chạy lại job; đọc log để sửa lệnh |
| `74` | job **chạy xong thành công** (`exit` = 0) nhưng **artifact chưa về** local (`pulled=false`, có `pull_error`) | **kéo lại** bằng `roff pull <job> --local <dir>` — **không** cần chạy lại job |

Trả về `74` mà báo `exit: 0` là cố ý: công việc trên máy remote **đã** xong,
chỉ là bước kéo về local hỏng (thường do reverse tunnel rớt). Job cũ (không có
khoá `pulled` trong ledger) thì **không** coi là hỏng ⇒ `roff wait` vẫn trả `0`
như trước.

**Exit 74 còn dùng cho `sync_back_failed` (B26).** Job đã chạy xong (`exit:0`),
artifact **đã** về (`pulled:true`), nhưng path `--sync-back` **chưa** về ⇒
`reason="sync_back_failed"` + `sync_back_error` khác rỗng ⇒ vẫn `74` ⇒ **kéo
lại**, không chạy lại job.

**Exit 3 cho `config_missing` (B27).** `ROFF_CONFIG` trỏ tới file **không tồn
tại** ⇒ `roff` **dừng** thay vì im lặng bỏ `include`/`exclude` của người dùng
⇒ fleet nới từ 4 lên 8 máy (gồm máy đã bị `exclude`). Không có `ROFF_CONFIG`
**và** không có config mặc định ⇒ **không** lỗi (fleet = mọi host, hành vi mặc
định).

## 3. `config.py`

```python
@dataclass
class HostConfig:
    name: str
    labels: list[str] = field(default_factory=list)
    weight: float = 1.0
    slots: int = 1
    mem_budget_mb: int = 0        # 0 = dùng mem_available thật
    nice: int | None = None       # ghi đè [remote] nice cho riêng host này (C9)
    ionice: str | None = None     # ghi đè [remote] ionice cho riêng host này (C9)

@dataclass
class Config:
    path: str | None
    fleet_include: list[str]      # glob, default ["*"]
    fleet_exclude: list[str]      # glob, default []
    probe_cache_s: int = 60
    max_parallel_transfers: int = 2
    nice: int = 19                # tiến trình LOCAL của roff (ssh/rsync/watcher)
    ionice: str = "idle"
    remote_nice: int = 19         # lệnh job chạy TRÊN máy remote (C9)
    remote_ionice: str = "idle"
    cache_max_mb: int = 5120
    cache_min_free_mb: int = 3072
    heavy_add: list[str]         # lệnh thêm vào danh sách nặng
    heavy_allow: list[str]       # lệnh miễn trừ
    hosts: dict[str, HostConfig]

    @staticmethod
    def load(path: str | None = None) -> "Config":
        """path=None → ~/.config/roff/config.ini nếu tồn tại, nếu không thì Config mặc định.
        File sai cú pháp → raise RoffError(EXIT_CONFIG).
        Section host.<alias>: labels (csv), weight (float), slots (int), mem_budget_mb (int),
          nice (int), ionice (str)."""

    def host(self, name: str) -> HostConfig:
        """Trả về HostConfig của alias, hoặc mặc định nếu không có trong config."""
```

**Chính sách `nice`/`ionice` — MỘT nguồn sự thật (B34, 23/11/2026).** Trước
B34 chính sách này bị **copy 5 lần** ở `probe`/`sshx`/`tunnel`/`job`/`transfer`
với **3 nguồn sự thật** (hằng / env / config) ⇒ `ROFF_NICE` không có tác dụng
với rsync/tar, còn `[local] nice` trong config chỉ có tác dụng với đường
truyền ⇒ **nút cấu hình chết**. Nay **mọi** module gọi chung:

```python
DEFAULT_NICE = 19
DEFAULT_IONICE = "idle"
IONICE_CLASSES = {"idle": "3", "best-effort": "2", "be": "2", "realtime": "1"}

@dataclass(frozen=True)
class NicePolicy:
    nice: int = DEFAULT_NICE       # 0 ⇒ KHÔNG bọc nice
    ionice: str | None = None      # None ⇒ KHÔNG bọc ionice
    def prefix(self) -> list[str]: ...   # ["nice","-n",N,"ionice","-c",C]

def local_nice_prefix() -> list[str]: ...        # tiến trình local
def remote_nice_policy(host=None, config=None) -> NicePolicy: ...  # lệnh job trên host
def remote_nice_args(host=None, config=None) -> tuple[str, str]: ... # ("19","3") | ("","")
```

Thứ tự ưu tiên: **biến môi trường > config > mặc định**.
* phía **local**: `ROFF_NICE`/`ROFF_IONICE` > `[local] nice`/`ionice` > mặc định;
* phía **remote** (C9): `ROFF_REMOTE_NICE`/`ROFF_REMOTE_IONICE` > `[host.<alias>]`
  > `[remote]` > mặc định.
`nice = 0` ⇒ **không** bọc `nice`; `ionice = none` (hoặc rỗng) ⇒ **không** bọc
`ionice`. Đọc config **hỏng** ⇒ **không** làm chết tiến trình (dùng mặc định —
nguyên tắc B27).

**Lệnh job trên host** (`job.py`, hằng `_RUN_SH`) được bọc:

```sh
if [ -n "__NICE__" ] && command -v nice >/dev/null 2>&1; then
  if [ -n "__IONICE__" ] && command -v ionice >/dev/null 2>&1; then
    nice -n __NICE__ ionice -c__IONICE__ -- sh -c "$(cat "$JOBDIR/cmd")"
  else
    nice -n __NICE__ -- sh -c "$(cat "$JOBDIR/cmd")"
  fi
elif ...
```

`nice`/`ionice` được kiểm tra **trên host** (`command -v`) ⇒ host không có thì
job **vẫn chạy**. **KHÔNG** dùng `exec`: `run.sh` phải còn sống để ghi
`$JOBDIR/exit` (`rc=$?` sau `exec` không bao giờ chạy ⇒ job không có exit code).
Lệnh của người dùng vẫn **nguyên văn** trong file `cmd`.

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

**Quy tắc**: mọi lệnh ssh/rsync chạy `nice -n 19 ionice -c3` — **lấy từ MỘT
chính sách chung** (xem §3: env > config > mặc định).
`--no-owner --no-group` **luôn** (R4).

## 10. `job.py`

```python
JOB_STATES = ("queued", "launched", "running", "exited", "lost", "cancelled", "unknown")
TERMINAL_STATES = ("exited", "lost", "cancelled", "unknown")

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
            "boot_id": str|None, "instance_token": str|None, "workdir_exists": bool}.

    **"lost" là nghi ngờ, chưa phải kết luận** (B42): verdict dựa trên
    ``kill -0 $PID`` trên host ⇒ đọc trúng lúc process vừa chết mà file
    ``exit`` chưa kịp ghi thì **sai** ⇒ người gọi phải **xác nhận lại**
    (xem §11: P1 hỏi lại host, P2 khoảng miễn sau launch)."""

def cancel(host: str, job: Job) -> dict:
    """Kill process group của job (pid đã ghi trong meta.json). Không pkill theo pattern."""

def heartbeat_path(job_id: str) -> str

def gc_jobs(state_dir: str | None = None,
            max_age_days: float = DEFAULT_GC_MAX_AGE_DAYS) -> dict:
    """Dọn ledger job (W5b — bug B13).

    Xoá: (1) file job **định dạng cũ** (không có khoá ``state``) — 951 file
    rác trên máy người dùng, mỗi file giữ 1 slot ⇒ ``roff wait`` treo tới
    hết timeout; (2) job **terminal** đã quá ``max_age_days`` (mặc định 30);
    (3) file phụ ``.heartbeat`` / ``.env`` của job đã bị xoá.

    **Không** xoá job đang chạy/xếp hàng, **không** xoá file JSON hỏng
    (chỉ đánh dấu ``skipped``). Trả ``{"removed": [...], "removed_count": int,
    "skipped": [...], "freed_bytes": int}``."""
```

**Job định dạng cũ** (không có khoá `state`) ⇒ `Job.from_dict` gán
`state = "unknown"`. `"unknown"` là **terminal**: không giữ slot, `roff wait`
trả về **ngay** (exit 1, `reason="unknown_state"`), `roff jobs` hiện nó là
`"unknown"`, và `roff gc` xoá file đó.

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
      2. nếu job **chưa** được launch (``deferred_start`` còn bật **và** chưa
         có attempt) ⇒ **đẩy input + launch** (B36/C10: ``roff do`` chỉ tạo
         job + xin slot, việc từ xa do watcher lo ⇒ lô ``--each`` gửi song
         song, các job chạy chồng nhau)
      3. remote_state()
      3. phân nhánh:
         - exited & exit==0 → pull artifact → state=exited, exit=0 → kết thúc
         - exited & exit!=0 → state=exited, exit=code → kết thúc (KHÔNG retry)
         - lost & reason=vanished & **trong khoảng miễn sau launch** → chờ,
           thử lại, KHÔNG failover  (B42/P2)
         - lost & reason=vanished & **ngoài** khoảng miễn → **hỏi lại host lần
           2** sau 4 s: lần 2 thấy exited → kết thúc bình thường (kéo artifact);
           lần 2 vẫn vanished → mất thật; lần 2 không với được host → chờ, thử
           lại  (B42/P1)
         - lost & reason mạnh (host_restarted/host_wiped/oom_killed)
           & needs_retry & attempts < retry → failover (**không** bị chậm thêm)
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
      giống cả hai nhưng không còn process → 'vanished'
      không kết nối được  → None  (chỉ mất tunnel → KHÔNG failover)"""
```

**`vanished` là verdict MẠNH nhưng KHÔNG phải bằng chứng tuyệt đối** (B42):
`remote_state()` đọc `kill -0 $PID` trên host ⇒ đọc trúng lúc process **vừa
chết** mà file `exit` **chưa kịp** ghi thì verdict **sai** ⇒ failover một job
**còn sống** (đo thật 24/11/2026 trên fleet Muse: job chạy thành công,
`exit=0`, nice 19 — nhưng poll báo `vanished`). ⇒ hành động theo `vanished`
phải qua **2 lớp bảo vệ**:

| Lớp | Hằng số | Việc |
|---|---|---|
| **P1** — xác nhận lần 2 | `VANISH_CONFIRM_DELAY_S = 4` | hỏi lại host một lần nữa; lần 2 thấy `exited` ⇒ kết thúc bình thường (**không** failover); lần 2 vẫn `vanished` ⇒ mất thật; lần 2 không với được ⇒ coi là **chưa chắc** ⇒ chờ thử lại |
| **P2** — khoảng miễn sau launch | `VANISH_GRACE_S = 30` | tính từ `attempt["started"]` của attempt **hiện tại** (⇒ mỗi lần failover lại có khoảng miễn mới); trong khoảng này thấy process không còn ⇒ **chưa chắc chắn** ⇒ ghi event `unverified` rồi chờ |

Verdict `host_restarted` / `host_wiped` / `oom_killed` là bằng chứng **mạnh
hơn** ⇒ failover **ngay**, **không** đi qua P1 ⇒ S20/S24 không bị chậm thêm.

> **Hệ quả (đánh đổi, nói rõ):** failover thật (verdict `vanished` đúng) chậm
> thêm ~4 s + 1 round-trip ssh; job vừa launch mà launch **thất bại** bị chờ
> tới hết khoảng miễn (30 s) rồi mới kết luận mất. Đổi lại: **không còn**
> chạy lại một job vẫn đang chạy.
>
> **Nguyên nhân gốc CHƯA khép** — đã loại trừ "khác PID namespace" (đo thật:
> `kill -0` **thành công** từ phiên ssh khác); chưa loại trừ được "file `exit`
> bị ghi trễ" hay "tiến trình bị giết giữa chừng" ⇒ bản sửa chặn **hậu quả**,
> chưa đóng nguyên nhân.

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

# --- W4: link phối hợp giữa 2 máy (xem §15m) --------------------------------
LINK_VIA_PORT_MIN = 18500
LINK_VIA_PORT_MAX = 18599

def tunnel_link(from_alias: str, to_alias: str, rport: int,
                via_lport: int | None = None,
                to_port: int | None = None) -> dict:
    """Mở link A ↔ B **qua máy local** (host↔host không nối thẳng trên fleet).

    A :rport ──(ssh -R rport:127.0.0.1:via)──▶ local :via
    local :via ──(ssh -L via:127.0.0.1:to_port)──▶ B :to_port

    Kiểm tra LISTEN ở **cả hai** đầu trước khi báo thành công; cổng trung
    gian được chọn bằng cách **bind thật** (SO_REUSEADDR rồi đóng)."""

def tunnel_stop_link(from_alias: str, rport: int) -> dict:
    """Dọn **cả hai** nhánh của link (huỷ `-R` trên A và `-L` qua B)."""

def link_list() -> list[dict]:
    """Các link đang ghi nhận (đọc state local, không gọi ssh)."""
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

**Ý nghĩa của `state` sau B36/C10 (23/11/2026):** `roff do` **không** đẩy
input và **không** launch — nó tạo job + xin slot, rồi **hoãn** việc đẩy input
+ launch cho **watcher** (mỗi job một watcher ⇒ lô `--each` chạy **song song**).
⇒ `state: "launched"` nghĩa là **"đã nhận slot, watcher đang khởi chạy"**,
**không** phải "tiến trình trên host đã chạy". Hệ quả đã nói rõ:
lỗi **đẩy input** (host chết lúc rsync, URL hỏng, file local biến mất) **lộ ra
ở `roff wait`**, không phải ở `roff do`; lỗi input **local** vẫn bị `roff do`
từ chối **ngay** (exit 2, trước cả khi probe fleet).

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
| `project_root` | thư mục làm việc lúc gọi `roff do` — gốc để hiểu `--sync-back` (B26) |
| `sync_back` | các path `--sync-back` người dùng đã yêu cầu (nguyên văn) |
| `sync_back_local` | **đích local** của từng path, **cùng thứ tự** với `sync_back_remote` — watcher kéo về đúng chỗ (B26) |
| `sync_back_remote` | đường dẫn **trên host** của từng path: `<workdir>/<rel>` (path tương đối trong project) hoặc path trong **kho input dùng chung** (path nằm trong một `--in`) |
| `sync_back_error` | mô tả lỗi khi kéo `--sync-back` hỏng; **xoá** khi kéo lại thành công (B26) |

Không có `input_specs` thì failover sang host mới sẽ chạy với `$ROFF_IN` trỏ
tới path không tồn tại ⇒ job chết ngay mà ledger vẫn báo "launch thành công".

Không có `sync_back_local` (job cũ) thì watcher **không đoán** ⇒ bỏ qua phần
sync-back; `roff pull` kéo các path vào thư mục `--local` như trước.

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

    **B22 — dọn lock mồ côi.** Thư mục ``<key>.lock`` quá hạn mà chủ đã chết
    (không có file ``owner``, hoặc chủ không còn sống) cũng bị xoá — nếu
    không, lock tích tụ mãi trên host. Dùng lại **đúng** ngưỡng của đường
    **đẩy** (``_lock_is_stale``: :data:`LOCK_STALE_S` / :data:`LOCK_HARD_STALE_S`)
    ⇒ không phát minh cơ chế khoá mới.

    Trả về (ngoài các khoá cũ): ``removed_locks`` — **key** của lock mồ côi đã
    dọn; ``skipped_locks`` — ``{"key", "reason"}`` cho lock **giữ lại**, reason:
    ``in_use`` (job đang dùng) | ``locked`` (còn chủ) | ``host_unreachable``
    (không biết ⇒ giữ) | ``remove_failed`` (xoá xong kiểm lại vẫn còn).

# sshx.py
def ssh_config_path() -> str: ...
def ssh_config_args() -> list[str]:
    """Tham số `-F <path>` cho ssh.

    **B25 — `ssh_config_path()` đã đổi ngữ nghĩa**: trước đây trả về đường dẫn
    **chỉ** khi có `$ROFF_SSH_CONFIG`; nay trả đường dẫn của file config mà roff
    dùng (mặc định `$HOME/.ssh/config`). Rỗng khi **không có file config nào**
    (khi đó ssh tự lo, như trước).

    Bắt buộc: `fleet.parse_ssh_config()` đọc cùng file đó — nếu ssh thật không
    đọc file đó thì alias trùng tên trỏ sang **máy khác**."""

# fleet.py — **nguồn sự thật duy nhất** cho file ssh-config (B25)
def ssh_config_file() -> str:
    """File ssh config roff dùng: `$ROFF_SSH_CONFIG` nếu có, else `$HOME/.ssh/config`.

    Luôn trả một **đường dẫn** (kể cả khi file không tồn tại) — fingerprint
    (B14) phải ổn định: đổi file ⇒ đổi fingerprint ⇒ đổi socket ControlMaster."""

def ssh_config_path() -> str:
    """Đường dẫn ssh-config phải truyền cho ssh bằng `-F` (rỗng nếu không có)."""

def ssh_config_args() -> list[str]:
    """`["-F", <path>]` — mọi nơi dựng argv ssh của roff đều phải đi qua đây."""

def env_fleet_aliases(cfg: Config) -> list[str]:
    """Alias của fleet theo `$ROFF_SSH_CONFIG` (seam cho test)."""

# fleet.py — luật mở rộng `Include` **phải khớp ssh thật** (B25)
#: `Include` **tương đối** resolve theo `$HOME/.ssh`, KHÔNG theo thư mục file
#: config (đo bằng `strace` trên OpenSSH 8.9p1: `Include inc.conf` với
#: `-F /tmp/x/config` ⇒ ssh mở `$HOME/.ssh/inc.conf`).
#: Áp cho **cả** `parse_ssh_config()` (danh sách alias) và `ssh_host_params()`
#: (tham số dùng để **fingerprint**) — lệch một bên ⇒ nối nhầm máy.

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


### 15m. API mới của wave W4/W5 — link phối hợp, `doctor`, B13/B14

#### 15m.1 `tunnel link` — nối 2 máy qua máy local (W4)

Host ↔ host **không** nối thẳng trên fleet này (bridge chết ở bước SSH
banner) ⇒ tiến trình ở máy A muốn nói chuyện với dịch vụ ở máy B thì phải
đi vòng qua máy local:

```
A :rport  ──(ssh -R rport:127.0.0.1:via)──▶  local :via
local :via ──(ssh -L via:127.0.0.1:to_port)──▶  B :to_port
```

Hai tiến trình ssh nền (`-f -N`), dùng **đúng** cơ chế ControlMaster sẵn có
(`ControlPath` riêng của roff, `ssh -O forward` / `ssh -O cancel`) — không
phát minh cơ chế ssh mới.

```bash
roff tunnel link --from <aliasA> --to <aliasB> --rport <port> \
                 [--via-lport <port>] [--to-port <port>]
```

JSON (thành công):

```json
{"ok": true, "exit_code": 0, "from": "muse-pmb333", "to": "muse-tampt",
 "rport": 19000, "to_port": 18899, "via_lport": 18500,
 "a_listening": true, "b_listening": true, "note": "a_listening=True; b_listening=True"}
```

* `via_lport` — cổng trung gian trên máy local; mặc định tự chọn trong dải
  **18500–18599** bằng cách **bind thật** (SO_REUSEADDR rồi đóng), không đoán
  bừa; `--via-lport` chỉ định tay ⇒ nếu cổng đang bị chiếm thì lỗi
  `local_port_busy`.
* `a_listening` / `b_listening` — đã kiểm tra LISTEN ở **cả hai** đầu chưa.
  `null` = **không kiểm tra được** (máy remote thiếu cả `ss`, `netstat` và
  `/proc/net/tcp`) — khác hẳn `false`.
* `note` — chuỗi tóm tắt; lỗi thì mang `reason` + `error` (tiếng Việt) và
  `detail` nguyên văn lỗi ssh (exit 255 khi host không với được).

`roff tunnel stop --from <aliasA> --rport <port>` dọn **cả hai** nhánh
(huỷ `-R` trên A, huỷ `-L` qua B) và đóng master nếu không còn forward nào
khác. `roff tunnel list` trả kèm mảng `links`.

Lỗi (xem bảng `reason` ở §17): `invalid_host`, `same_host`,
`remote_not_listening` (máy B không có gì LISTEN ⇒ **không** mở gì),
`local_port_busy`, `no_free_port`, `forward_failed` (đã mở forward nhưng máy
A không LISTEN ⇒ **dọn cả hai nhánh** rồi báo lỗi), `control_master_failed`,
`host_unreachable`.

An toàn: chỉ huỷ forward qua `ssh -O cancel` của **master của roff**
(`ControlPath` riêng) — **không** `kill`/`pkill` tiến trình nào; không huỷ
toàn bộ forward của master nếu máy đó còn tunnel khác của roff.

#### 15m.2 `doctor` — chẩn đoán, **chỉ báo** (W5a)

```bash
roff doctor [--probe]
```

```json
{"ok": false, "exit_code": 1, "problems": 2,
 "checks": [{"name": "config", "ok": true, "detail": "/home/x/.config/roff/config.ini"}, ...]}
```

* Mỗi check có **`name`**, **`ok`**, **`detail`**. `problems` = số check có
  `ok: false`; `ok` = (`problems == 0`); exit code = **1** khi có vấn đề, **0**
  khi sạch.
* Check lỗi **không** làm hỏng lệnh: exception bị bọc, check đó thành
  `ok: false` với `detail` kiểu `"lỗi khi kiểm tra: ..."`.
* Mặc định **không** probe mạng (đọc cache `probes.json` + kiểm tra local)
  ⇒ chạy < 1 s. `--probe` mới probe thật host.
* `doctor` **không ghi, không sửa, không đổi quyền** gì — kể cả bước chuẩn
  hoá quyền file state (bước đó bị bỏ qua khi lệnh là `doctor`).

Các check: `config`, `wrapper`, `hook`, `fleet`, `state_perms`,
`orphan_jobs`, `disk_local`.

#### 15m.3 B13 — job định dạng cũ (`unknown`) và `roff gc` (W5b)

* Job file **không có khoá `state`** ⇒ `state = "unknown"` (không phải mặc
  định `"queued"` ⇒ treo tới hết timeout).
* `"unknown"` là **terminal**: không giữ slot, `roff wait` trả về **ngay**
  (exit 1, `reason="unknown_state"`), `roff jobs` hiện `"unknown"`.
* `roff gc` xoá: file định dạng cũ, job terminal quá `--job-max-age-days`
  (mặc định **30** ngày), file phụ `.heartbeat`/`.env` mồ côi. **Không** xoá
  job đang chạy/xếp hàng, **không** xoá file JSON hỏng.
* JSON của `roff gc` có thêm `removed_jobs` (mảng id), `removed_jobs_count`,
  `skipped_jobs`, `freed_jobs_mb`; `freed_mb` = tổng (cache + job).

Đo thật trên máy người dùng (23/11/2026): ledger có **1009** file job, trong
đó **951** file không có khoá `state` ⇒ `roff gc` xoá đúng **951** file, còn
lại 58 job (`exited` 47, `queued` 9, `lost` 1, `launched` 1) được giữ nguyên.

#### 15m.4 B14 — fingerprint ssh trong `ControlPath` + cache probe (W5c)

* `fleet.ssh_host_params(alias)` / `fleet.ssh_fingerprint(alias)`: tham số
  ssh **đã resolve** của alias (`HostName`, `Port`, `User`, `IdentityFile`,
  `ProxyCommand`, `ProxyJump`) đọc từ `~/.ssh/config` (hoặc
  `$ROFF_SSH_CONFIG`) — **không** gọi ssh, không tốn round-trip.
* `sshx.control_path(alias)` = `<state>/cm/<alias>-<8 hex sha256>.sock` ⇒
  **đổi config ⇒ socket mới ⇒ master mới**; cùng alias + cùng config ⇒ cùng
  socket (tái dùng master vẫn chạy). Đường dẫn luôn ≤ 88 byte (giới hạn
  `sun_path` 108 byte + hậu tố ngẫu nhiên 17 byte của OpenSSH).
* `sshx.clean_stale_socket(alias)` xoá socket **cũ fingerprint** (và socket
  chết) — chỉ trong `<state>/cm/` của roff.
* Cache probe (`probes.json`) lưu kèm `fingerprint`; đổi config ⇒ mismatch ⇒
  entry đó **hết hạn** ⇒ probe lại (không xoá cả cache).

Đo thật (23/11/2026, fleet Muse, cùng alias `roff-b14-probe`):

| config | fingerprint | `control_path` | `roff probe` |
|---|---|---|---|
| `Port 2222` (cổng thật) | `3fb7ad22` | `.../cm/roff-b14-probe-3fb7ad22.sock` | `reachable: true`, exit 0 |
| `Port 9` (cổng chết) | `4fa7e32e` | `.../cm/roff-b14-probe-4fa7e32e.sock` | `reachable: false`, exit 255 (`Connection refused`) |

⇒ đổi `~/.ssh/config` **không** còn tái dùng master cũ (trước B14, lần
probe thứ hai vẫn trả về thông tin của máy cũ).


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

### 16.1 Cờ cảnh báo fleet (B27)

Khi fleet **không** bị giới hạn (config không có `[fleet] include`, hoặc
`include = *`), JSON của `roff hosts` và `roff do` mang **thêm** 2 khoá:

```json
{"ok": true, "exit_code": 0,
 "fleet_unrestricted": true,
 "fleet_warning": "config không giới hạn fleet ⇒ mọi host trong ~/.ssh/config đều được chọn, gồm cả host bạn không muốn dùng"}
```

Khi fleet **có** giới hạn ⇒ **không** có 2 khoá này (payload giữ nguyên như
trước). Agent **phải** đọc `fleet_unrestricted` trước khi gửi job: đó là tín
hiệu **máy đọc được** rằng job có thể chạy trên **bất kỳ** host nào trong
`~/.ssh/config`, kể cả host người dùng đã `exclude`.

> Kèm theo: `ROFF_CONFIG` trỏ tới file **không tồn tại** ⇒ `roff` **dừng**
> (exit 3, `config_missing`) thay vì im lặng nới fleet. Không có
> `ROFF_CONFIG` **và** không có config mặc định ⇒ vẫn chạy, fleet = mọi host
> (hành vi mặc định "chạy được ngay khi mới cài").

## 17. Bảng `reason` đầy đủ

Agent **phải** bóc theo `reason`, không theo `error` (tiếng Việt). Bảng này là
danh mục **đóng** — code mới phải được thêm vào đây cùng lúc với lệnh sinh ra nó.

| `reason` | exit | Nghĩa | Agent nên làm gì |
|---|---|---|---|
| `missing_cmd` | 2 | `--cmd` rỗng | hỏi lại người dùng cần chạy gì |
| `invalid_option` | 2 | option sai kiểu/giá trị | sửa lệnh, không phải chờ |
| `invalid_timeout` | 2 | `--timeout` ≤ 0 | sửa lệnh |
| `invalid_priority` | 2 | `--priority` không phải `high`/`normal` | sửa lệnh |
| `invalid_size` | 2 | `--mem`/`--disk`/`--cpus` sai định dạng | sửa lệnh |
| `invalid_env` | 2 | `--env` không phải `KEY=VALUE` | sửa lệnh |
| `invalid_input` | 2 | `--in` sai dạng (vd `job:` thiếu id) | sửa lệnh |
| `invalid_after` | 2 | `--after` sai dạng | sửa lệnh |
| `invalid_job` | 2 | job id không tồn tại trong ledger | kiểm tra `roff jobs` |
| `invalid_tunnel_action` | 2 | `tunnel` cần `start`/`stop`/`list` | sửa lệnh |
| `invalid_host` | 2 | `tunnel link`: `--from`/`--to` rỗng hoặc không phải alias | sửa lệnh |
| `same_host` | 2 | `tunnel link`: `--from` và `--to` là **cùng** một máy | sửa lệnh |
| `doctor_unavailable` | 2 | module `roffkit.doctor` không có trong bản cài đặt | cài lại skill |
| `config_missing` | 3 | `ROFF_CONFIG` trỏ tới file config **không tồn tại** ⇒ bỏ qua nó im lặng sẽ khiến fleet lấy **MỌI** host trong `~/.ssh/config` (kể cả host đã `exclude`) ⇒ **từ chối** (B27) | tạo file config, hoặc bỏ `ROFF_CONFIG` (không có config ⇒ fleet = mọi host, như thiết kế mặc định) |
| `no_items` | 2 | `--each` không khớp item nào | sửa glob |
| `input_not_found` | 2 **hoặc** 69 | path local của `--in` không tồn tại — **2** = bị chặn ngay trước khi chạy; **69** = file biến mất *giữa lúc chạy* ⇒ job `lost` | sửa đường dẫn; **không** gửi lại cùng lệnh (máy khác cũng không có file đó) |
| `missing_local` | 2 | `pull` thiếu `--local` | sửa lệnh |
| `missing_name` | 2 | `push`/`ensure` thiếu `--name` | sửa lệnh |
| `missing_setup` | 2 | `ensure` thiếu `--setup` | sửa lệnh |
| `setup_not_found` | 2 | script `--setup` không tồn tại | sửa đường dẫn |
| `tunnel_unavailable` | 2 | không dựng được tunnel (cổng bận, thiếu quyền) | thử cổng khác, báo lại |
| `empty_path` | 2 | đường dẫn remote rỗng | sửa tham số |
| `relative_remote_path` | 2 | đường dẫn remote phải **absolute** (không gửi `~` qua ssh) | sửa tham số |
| `local_dir_not_found` | 2 | thư mục local nguồn không tồn tại (`push`) | sửa đường dẫn |
| `download_failed` | 2 | tải `--in http(s)://…` thất bại (URL sai/hỏng) | sửa URL — **không** phải lỗi host |
| `invalid_port` | 2 | `--lport`/`--rport` không phải số, hoặc ngoài 1..65535 | sửa cổng |
| `job_not_found` | 2 | không có job id đó trong ledger | kiểm tra `roff jobs` |
| `job_unreadable` | 2 | file ledger của job hỏng (JSON không đọc được) | `roff gc` dọn job đó |
| `missing_out_remote` | 2 | ledger job chưa có `out_remote` | job chưa được launch lần nào |
| `bad_out_remote` | 2 | `out_remote` của job không kết thúc bằng `/out` | ledger hỏng ⇒ `roff gc` |
| `missing_project` | 2 | ledger job không có `project` ⇒ không xác định được thư mục làm việc | ledger hỏng ⇒ `roff gc` |
| `input_not_recreatable` | 2 | input kiểu `job:` **không** dựng lại được trên host mới (failover) | dữ liệu nằm trên host của job nguồn ⇒ chạy lại từ job nguồn, **không** failover tiếp |
| `empty_fleet` | 10 | **config ssh không có alias nào khớp fleet** — không phải "máy chết" | báo người dùng: cấu hình fleet sai, **không** phải fleet hỏng |
| `no_host` | 10 | fleet có nhưng **không còn host nào dùng được** (hết RAM, hết slot, chết) | xem `rejected` để biết vì sao |
| `no slot on <host>` | 10 | hết slot trên host đó (hàng đợi slot đầy) | đợi job khác xong, hoặc `--host` máy khác |
| `flock không khả dụng trên nền tảng này` | 10 | nền tảng không có `fcntl.flock` ⇒ không khoá được slot | lỗi môi trường, báo người dùng |
| `job_failed` | 1 | job chạy xong nhưng exit ≠ 0 | đọc `roff logs <job>` |
| `unknown_state` | 1 | job ở **định dạng cũ** (ledger không có khoá `state`) ⇒ không biết kết quả | `roff gc` để dọn; job này **không** phải job đang chạy |
| `setup_failed` | 1 | script setup hỏng trên host | xem `detail` |
| `launch_failed` | 1 | không launch được job (mất kết nối lúc launch) | `roff wait` job đó để biết kết cục |
| `remote_not_listening` | 1 | `tunnel link`: máy `--to` không có gì đang LISTEN ở `--to-port` | **không** mở link; kiểm tra dịch vụ trên máy đó |
| `local_port_busy` | 1 | `tunnel link`: cổng trung gian đang bị chiếm | thử cổng khác (`--via-lport`) |
| `no_free_port` | 1 | `tunnel link`: hết cổng trống trong dải 18500–18599 | đợi có chỗ, hoặc dùng `--via-lport` |
| `forward_failed` | 1 | `tunnel link`: đã mở forward nhưng máy `--from` không LISTEN | link **đã được dọn**; thử lại |
| `remote_listening` | 1 | máy đích **không** có gì đang LISTEN ở `rport` | bật dịch vụ ở máy đích rồi mở tunnel |
| `stale_forward` | 1 | không huỷ được forward cũ **của roff** trên cổng (`master pid` còn sống) | `roff tunnel stop` rồi thử lại |
| `internal_error` | 1 | lỗi nội bộ roff | báo lại, kèm `detail` |
| `cancel_failed` | 255 | không huỷ được job | kiểm tra host còn sống không |
| `host_unreachable` | 255 | ssh không với được host (bắt buộc có `detail`) | thử lại, hoặc đổi host |
| `unknown_host` | 255 | alias không có trong config | kiểm tra `~/.ssh/config` |
| `control_master_failed` | 255 | không dựng được ControlMaster của roff (socket không bind được) | xem `detail`; thường là thư mục state chưa tồn tại hoặc socket sót |
| `push_failed` | 255 | đẩy input lên host thất bại | xem `detail` |
| `shared_push_failed` | 255 | đẩy input vào **kho dùng chung** của host thất bại | xem `detail` |
| `shared_input_busy` | 255 | kho dùng chung của host đang bị khoá (tiến trình khác đang đẩy) | chờ rồi thử lại; nếu kéo dài ⇒ host đó kẹt |
| `shared_marker_failed` | 255 | không ghi được marker `.complete` trên host | xem `detail`; host có thể đang chết |
| `pull_failed` | 255 **hoặc** 74 | kéo artifact về local thất bại — **255** = `roff pull` không với được host (xem `detail`); **74** = job **đã chạy xong** (`exit`=0) nhưng artifact **chưa về** local (`pulled=false`, có `pull_error`) | 255: thử lại / đổi máy. 74: **kéo lại** bằng `roff pull <job> --local <dir>` — **đừng** chạy lại job |
| `sync_back_failed` | 74 | job **đã chạy xong** (`exit`=0) và artifact **đã** về (`pulled=true`), nhưng path `--sync-back` **chưa** về local (`sync_back_error` khác rỗng) — B26 | **kéo lại** bằng `roff pull <job> --local <dir>` (kéo cả path `--sync-back`); **đừng** chạy lại job |
| `sync_back_outside_project` | 2 | path `--sync-back` nằm ngoài project root **và** ngoài mọi `--in` ⇒ không có nơi nào trên host để tìm file đó — **từ chối lúc gửi job** (B26) | sửa đường dẫn (tương đối so với thư mục làm việc) hoặc thêm `--in` |
| `timeout` | 124 | hết thời gian chờ — **không phải lỗi** | xem `state` trong JSON rồi quyết định |
| `host_restarted` | 69 | host restart giữa lúc chạy | job đã được failover (trừ `--side-effects`) |
| `host_wiped` | 69 | host bị wipe (instance_token đổi) | failover sang host khác |
| `oom_killed` | 69 | host OOM giữa lúc chạy | failover; cân nhắc giảm `--mem` |
| `vanished` | 69 | host biến mất khỏi fleet (tunnel chết, alias bị gỡ) | failover sang host khác |
| `input_push_failed` | 69 | **không đẩy được input** lên host (kho input dùng chung bị kẹt, host flap) — lỗi **hạ tầng** | đã failover sang host khác (trừ job ghim `--host`); nếu hết lượt thì job `lost` với lý do này |
| `workdir_missing` | 69 | host không tạo được thư mục làm việc (disk/host flap) — lỗi **hạ tầng** | đã failover; nếu hết lượt thì job `lost` với lý do này |
| `cancelled` | 130 | job đã bị huỷ | không làm gì thêm |


---

## 18. B17 — kho input dùng chung không được **kẹt vĩnh viễn** (watcher W7)

> Sự cố đo thật trên fleet Muse 23/11/2026 (job `261123-141011-5884`): kho input
> dùng chung bị kẹt vì thư mục `<key>.lock` **mồ côi** còn lại mà `<key>.complete`
> **không có** ⇒ mọi lần đẩy sau đó đều fail `shared_input_busy` ⇒ watcher
> quay vòng **mãi** trên cùng host ⇒ job treo tới hết `timeout`
> (7200 + 600 = **2 giờ 10 phút**), không failover.

### 18.1 `shared.py` — lock không được bỏ lại, lock mồ côi phải dọn nhanh

```python
MARKER_WAIT_S    = 60.0    # chờ marker khi người khác đang đẩy
LOCK_STALE_S     = 30.0    # lock quá hạn ⇒ coi là chết ⇒ dọn rồi chiếm lại
LOCK_HARD_STALE_S= 900.0   # quá hạn cứng ⇒ dọn luôn (chủ lock bị kẹt)
LOCK_HEARTBEAT_S = 10.0    # nhịp "chạm" lock trong lúc đang đẩy
RELEASE_ATTEMPTS = 3       # số lần thử nhả lock
```

Bất biến bắt buộc:

* **`LOCK_STALE_S < MARKER_WAIT_S`** — nếu không, nhánh "lock chết" không bao
  giờ kịp chạy trong 1 lần chờ ⇒ phải quay vòng ~30 lần (≈ 30 phút) mới dọn
  được 1 lock mồ côi (đo thật: job kẹt **28,5 phút**).
* **Nhả lock dùng `rm -rf`**, không dùng `rmdir` — lock có file `owner` bên
  trong nên `rmdir` **luôn** fail và bỏ lại lock mồ côi. Lệnh nhả lock phải
  **kiểm tra lại** (`test ! -e`) và **thử lại** `RELEASE_ATTEMPTS` lần (host
  flap ⇒ lệnh ssh hỏng ⇒ lệnh xoá không hề chạy). Lock còn lại sau mọi lần
  thử ⇒ **ghi ra stderr** (không im lặng) và ghi vào lỗi đang bay
  (`lock_release_failed`).
* **Nhả lock phải chạy cả khi tiến trình sắp chết**: `try/finally` +
  `atexit` + (lần cuối) lệnh **tách tiến trình** trên host (`nohup`), có điều
  kiện "lock vẫn của mình" (token owner khớp) để không xoá nhầm.
* **Lock ghi chủ**: `{lock}/owner` = JSON `{id, pid, host, started}`. Lock của
  tiến trình **còn sống** không được xoá (trừ khi quá `LOCK_HARD_STALE_S`).
* **Nhịp chạm lock** (`LOCK_HEARTBEAT_S`): một lần đẩy có thể kéo dài **vài
  giờ** (băng thông local→host 0,5–1 MB/s) ⇒ không chạm lock thì mtime đứng
  yên ⇒ sau `LOCK_STALE_S` người khác tưởng lock mồ côi và xoá ⇒ **đẩy
  trùng**. Nhịp chạm (10 s) nhanh hơn ngưỡng stale (30 s) nên lock còn sống
  không bao giờ bị tưởng là mồ côi.

### 18.2 `watcher.py` — không được quay vòng vô hạn trên cùng host

```python
REASON_INPUT_PUSH_FAILED = "input_push_failed"   # LỖI HẠ TẦNG ⇒ được failover
MAX_START_FAILURES = 3                           # số lần hỏng liên tiếp trên 1 host
```

* `INFRASTRUCTURE_REASONS` **có thêm** `input_push_failed` ⇒ được failover/retry
  (tôn trọng `--side-effects` (`job.needs_retry`) và `--retry`).
* `_start()` thất bại ⇒ ghi **`start_error`** vào ledger (kèm event
  `start_failed`) ⇒ watcher biết đây là lỗi hạ tầng hay lỗi của job.
* Sau `MAX_START_FAILURES` lần hỏng **liên tiếp trên cùng host**:

  | lỗi | host | hành động |
  |---|---|---|
  | `input_push_failed` (hạ tầng) | không ghim | **failover** sang host khác (push lại input, launch lại) |
  | `input_push_failed` (hạ tầng) | **ghim** (`--host`) | `state=lost`, `reason=input_push_failed` |
  | lỗi của **job** (vd `input_not_found`) | bất kỳ | `state=lost`, `reason` đúng ngay lập tức (**không** failover) |

* Job **ghim host** (`--host <alias>` ⇒ `host_reasons` có
  `host bị ép qua --host <alias>`) **không được** failover — người dùng đã ép
  host ⇒ khi host đó hỏng thì job phải `lost` với reason rõ ràng.
* Bộ đếm lỗi nằm trong ledger (`start_failures`, `start_fail_host`) ⇒ watcher
  được gia hạn (spawn lại) vẫn nhớ ⇒ không quay vòng vô hạn qua nhiều đời
  watcher. Bộ đếm **tính theo host** ⇒ đổi host là về 0.

### 18.3 Reason mới — phân loại

| reason | loại | failover? | nguồn |
|---|---|---|---|
| `input_push_failed` | **hạ tầng** | có (trừ host ghim) | kho input kẹt / host flap / không với được host |
| `input_not_found` | **của job** | **không** | file input local không tồn tại (exit 2 khi bị chặn ngay, exit 69 khi mất giữa lúc chạy) |
| `workdir_missing` | của job/host | không | không tạo được thư mục làm việc |
| `launch_failed` | hỗn hợp | không | launch lỗi (không phải workdir/input) |

`input_not_found` **không** được đưa vào `INFRASTRUCTURE_REASONS`: file local
đã mất thì máy khác cũng không có ⇒ failover chỉ là đốt thêm thời gian rồi
cuối cùng cũng `lost` với lý do **sai**.
