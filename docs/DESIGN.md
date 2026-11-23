# remote-offload — thiết kế v2

> Trạng thái: **v2 đã viết xong** (wave W1–W3 đã giao, nghiệm thu trên fleet thật
> 23/11/2026). Xem `docs/REPORT.md` để biết cái gì đạt, cái gì hỏng thật và đã sửa.
> Người viết đặc tả và người nghiệm thu: Claude (Opus). Viết code: subagent.
> Ngày: 2026-11-23.

## 1. Vấn đề

Skill `remote-offload` v1 đã chạy được nhưng **không tổng quát**:

- Agent phải tự đi đủ 8 bước (probe → chọn host → ensure → push → run → wait → logs → pull).
  Mỗi agent làm mỗi kiểu, tốn token, dễ sai.
- Chọn host là việc của agent, đọc profile rồi đoán. Không có scheduler.
- Host chết/restart giữa chừng → dừng lại, chờ agent tự re-push và resubmit. Không failover.
- Skill chỉ kích hoạt khi agent "nhớ ra". Skill khác, dự án khác chạy `npm test` vẫn chạy ở local.
- Recipe gắn cứng vào từng loại task (ffmpeg / be-fe / e2e), không có mô hình chung.

## 2. Mục tiêu

1. **Tổng quát**: bất kỳ skill nào, dự án nào, task nặng nào → chuyển sang máy khác.
   Không phụ thuộc harness, không phụ thuộc loại task.
2. **Máy chính không giật**: mọi việc nặng (CPU, RAM, I/O, truyền dữ liệu) chạy ở máy khác.
   Máy local chỉ điều phối.
3. **Tận dụng cả fleet**: nhiều task đồng thời, nhiều máy chạy song song, tự chia tải.
4. **Sống sót khi máy remote lỗi**: restart, mất mạng, OOM, tunnel chết → tự xử lý.
5. **Tối ưu token**: context chính không bị nhiễu bởi log thô và vòng poll.

## 3. Những gì đo được trên môi trường thật

Các số dưới đây là **đo thật** trên fleet của người dùng (2026-11-22/23), không phải giả định.
Chúng là ràng buộc thiết kế.

### 3.1 Máy local

| Hạng mục | Giá trị | Hệ quả |
|---|---|---|
| Python | 3.10.12 | **không có `tomllib`** → config dùng INI (`configparser`) |
| CPU | 12 core | |
| RAM | 15,7 GB tổng, 7,4 GB đang dùng | |
| Disk | 159 GB, **chỉ còn 14 GB trống (91% đã dùng)** | cache phải có trần + LRU; `~/.claude` đang chiếm 22 GB |
| Uplink → Cloudflare | **8,5 MB/s** | |
| `rsync`, `ssh`, `flock`, `jq` | có sẵn | |
| `python3 -m tomllib` | **không có** | xác nhận INI |

### 3.2 Fleet Muse (4 alias, tất cả là reverse SSH tunnel về `127.0.0.1`)

Alias → port: `muse-pmb333`=2222, `muse-tampt`=2225, `muse-bienpm333`=2226, `muse-phamminhbien3333`=2227.

| Hạng mục | Giá trị |
|---|---|
| Cấu hình | 4 CPU, RAM tổng 8 GB |
| **RAM thực sự trống** | **~450–900 MB** (phần còn lại là postgres, telegram-bot, art-ex, cloudflared **của chính người dùng**) |
| cgroup | v2 nhưng **`memory.max` không đọc được** → phải dùng `MemAvailable` |
| Bền vững | `/home/hatch` là **mount thật** `/dev/mapper/rv`, 150 GB, mount riêng, **không bị xoá khi restart** |
| `$HOME` | `/root` — **trên overlay, bị xoá khi restart** |
| Quyền | chạy `root`; `chown` được trên `/home/hatch` |
| Tool có sẵn | node v24, ffmpeg, git, rsync, tar, gcc, make, apt, python3, curl, wget |
| **Tool thiếu** | **npm** (node có nhưng npm không), docker, nix, corepack |
| Internet | `muse-pmb333` có (github/npmjs = 200); `muse-tampt` **không** (timeout) |
| GPU | không có |
| `/tmp` | tmpfs 512 MB |

**Các máy Muse KHÔNG phải máy rảnh.** Chúng đang chạy nền tảng agent của người dùng:
`/home/hatch/artex` (2,4 GB, postgres + cloudflared), `/home/hatch/workspace` (857 MB, gồm
`film-studio`, `telegram-bot`), `/home/hatch/agents` (99 MB), `/home/hatch/hooks`.
Hệ quả: roff **không được** lấy hết RAM, và **không được** kill tiến trình không phải của nó.

### 3.3 Băng thông — nút thắt lớn nhất

| Đường | Tốc độ |
|---|---|
| local → Cloudflare | 8,5 MB/s |
| local → Muse (dữ liệu ngẫu nhiên, `-z`) | **0,5–1 MB/s** (có lúc 5,5 MB/s — **không ổn định**) |
| local → Muse (50 MB, lần lặp lại) | 41 s rồi 9 s — biến động 4× |
| Muse → Muse qua `cloudflared access ssh` | port mở nhưng **SSH banner timeout** → **không dùng được** |

**Hệ quả thiết kế then chốt:**

> Băng thông là nút thắt, không phải CPU. Đẩy 500 MB lên 4 máy = 2 GB truyền ≈ 35 phút,
> trong khi encode 720p chỉ mất vài phút.

⇒ Chiến lược "dùng hết fleet" phải đảo ngược: **dữ liệu phải ở sẵn trên máy, hoặc chỉ truyền một
lần, không phải N lần.** Mọi quyết định còn lại (chọn host, chia lô, truyền dữ liệu) đều phải
tối ưu theo "ít byte đi qua máy local", không phải "nhiều core nhất".

### 3.4 Hành vi quan sát được

- **Flap thật**: `muse-tampt` đổi `boot_id` (`b1a311d6…` → `92147370…`) trong ~30 phút.
  `muse-phamminhbien3333` chết giữa lúc đang kiểm tra.
- **Tunnel chết ≠ máy chết**: phải phân biệt 3 tình huống —
  (a) tunnel chết, máy vẫn sống, job vẫn chạy;
  (b) máy restart, job chết;
  (c) máy bị wipe, mất job dir.
- **Detached job sống sót** qua việc mất ssh: `setsid nohup` + `SID` riêng, đã kiểm chứng.
- **ControlMaster hoạt động**: lần đầu 4,9 s, lần sau 1,3 s; `ssh -O check` → `Master running (pid=…)`.
  ⇒ dùng để giảm 3,5 s/lệnh.
- **`sshd` trên Muse có thể chết hoàn toàn** sau restart, và phải được sửa tay (`sshd` user thiếu,
  binary bị xoá). Đây là việc của nền tảng Muse, không phải của roff — nhưng roff phải báo lỗi
  rõ ràng thay vì treo.
- `/home/hatch/.ssh/authorized_keys` **không được là symlink** (`/home/hatch` group-writable ⇒
  sshd `StrictModes` từ chối). Không copy key sang host mới.

## 4. Quyết định đã chốt

| # | Quyết định |
|---|---|
| Q1 | **Viết lại 100%** — code, SKILL.md, references, setups, docs |
| Q2 | Skill mô tả **+ hook `PreToolUse`** chặn lệnh nặng, gợi ý `roff`; có `ROFF_LOCAL=1` để bỏ qua |
| Q3 | **Scheduler trong CLI**: agent khai báo nhu cầu, CLI chấm điểm host và giải thích lý do |
| Q4 | Failover **tự động, mặc định `--retry 1`**, chỉ cho job chạy lại được; job có `--side-effects` thì không retry |
| Q5 | **Watcher tách khỏi agent** — tiến trình nền cục bộ, agent chỉ nhận kết quả cuối |
| Q6 | Lúc build dùng subagent theo model hợp phần. Lúc chạy, SKILL.md **chỉ gợi ý** dùng subagent, không gò bó |
| Q7 | **Lõi không phụ thuộc harness**; hook Claude Code nằm ở adapter riêng |
| Q8 | TDD: bug cũ → test hồi quy trước khi viết code |
| Q9 | `roff do` là lệnh chính; lệnh phụ (`status`/`logs`/`cancel`/`hosts`/`tunnel`/`pull`) cho can thiệp |
| Q10 | Danh sách lệnh nặng **mặc định có sẵn**, mở rộng/tắt qua config |
| Q11 | Config **INI** ở `~/.config/roff/config.ini` |
| Q12 | Input: thư mục local, git URL, http URL, output của job khác |
| Q13 | Ưu tiên chạy job ngay trên host đang giữ dữ liệu; không được thì **stream pipe qua local, không ghi đĩa** |
| Q14 | Checkpoint **tuỳ chọn** (`--checkpoint`) |
| Q15 | `--after`, `--in job:`, `--shards N` |
| Q16 | **Tự chạy setup script** khi thiếu tool; idempotent theo `boot_id` + `instance_token` |
| Q17 | Một chiều; `--sync-back <paths>` khi cần |
| Q18 | Mọi tiến trình local chạy `nice -n 19 ionice -c3` |
| Q19 | **Không giữ tương thích** v1. Xoá layout cũ trên host |
| Q20 | Bỏ lịch sử git; orphan branch mới |
| Q21 | Chia subagent: tôi viết test + nghiệm thu, subagent viết code, subagent Opus review độc lập |
| Q22 | Force-push GitHub **sau khi nghiệm thu đạt**, có hỏi lại lần cuối |
| Q23 | Danh sách lệnh nặng mặc định (media / build / test / container-ML) |
| Q24 | Công thức chấm điểm host: ấm + khoẻ + ổn định − đang bận, × weight |
| Q25 | `--retry 1` mặc định; **chỉ retry lỗi hạ tầng, không bao giờ retry job tự fail**; timeout 2 h |
| Q26 | Người dùng **restart thật** máy Muse giữa lúc test |
| Q27–Q28 | Dùng được mọi máy `muse-*`; ngưỡng tài nguyên local: CPU roff+ssh < 2% core, RAM < 100 MB |
| Q29–Q31 | Fleet = `muse-*` + `qwen1-direct`; 22 kịch bản nghiệm thu S1–S22 |
| Q32 | **Sổ chiếm chỗ chung ở local** (`flock`) cho mọi phiên/agent/harness |
| Q33 | **Xếp hàng FIFO** khi fleet bận, có `--priority`; không bao giờ chạy local thay thế |
| Q34 | `--cpus N` / `--mem`, export `$ROFF_CPUS`; mặc định job chiếm trọn host |
| Q35 | `--each` (work-stealing) + `--shards N`; recipe ffmpeg split→encode→concat |
| Q36 | **Bản sao chạy song song** cho item chậm ở cuối lô |
| Q37 | **Trần 2 luồng truyền đồng thời** toàn máy, chia sẻ qua sổ Q32 |
| Q38 | Fleet xác định bằng `[fleet] include`; cache probe 60 s |
| Q39–Q42 | Ngưỡng tăng tốc tách 2 phép đo: **tính toán ≥ 0,6×N**, **toàn cục ≥ 1,3× (N=2), ≥ 1,8× (N=4)** |
| Q43 | Host↔host **không dùng được** → stream pipe qua local, không ghi đĩa; `--prefer-same-host` mặc định bật |
| Q44 | Cache local có trần 5 GB + LRU; checkpoint dài hạn đẩy lên host có layout bền vững |
| Q45 | Fleet = `muse-*` + `qwen1-direct`; `comfy`/`colab-comfy` ngoài (bridge một phiên) |

## 5. Kiến trúc

```
skill/
  SKILL.md            ← agent đọc. Quyết định, workflow, hợp đồng exit code.
  roff.py             ← CLI. Stdlib-only. Mọi lệnh in 1 JSON + exit code.
  roff/               ← package nội bộ (import từ roff.py)
    config.py         ← parse INI, defaults, validate
    fleet.py          ← đọc ~/.ssh/config, liệt kê alias, dedupe theo host thật
    probe.py          ← dựng capability profile của 1 host
    policy.py         ← chấm điểm, lọc, chọn host (giải thích được)
    slots.py          ← sổ chiếm chỗ chung (flock) + hàng đợi FIFO
    transfer.py       ← push/pull: rsync, tar-over-ssh, resume, sha256 manifest
    job.py            ← tạo job, ledger, trạng thái, huỷ
    watcher.py        ← vòng lặp giám sát: chờ, phát hiện lỗi, failover, retry
    tunnel.py         ← cổng chuyển tiếp
    errors.py         ← hợp đồng exit code
    layout.py         ← layout root bền vững trên host, đường dẫn tuyệt đối
  references/         ← recipe theo loại task
  setups/             ← script cài toolchain (idempotent)
  hooks/
    claude-code/
      pretooluse.py   ← adapter hook Claude Code
      settings.json   ← mẫu merge vào ~/.claude/settings.json
docs/
  DESIGN.md           ← file này
  SPEC-CLI.md         ← hợp đồng CLI: từng lệnh, từng tham số, từng exit code
  SCENARIOS.md        ← 22 kịch bản nghiệm thu + 20 test hồi quy
  REPORT.md           ← kết quả nghiệm thu (viết sau khi chạy)
```

### 5.1 Mô hình tiến trình

```
agent (Claude Code / codex / ...)
  │  roff do --cmd '…' --in ./data --out ./out
  ▼
┌──────────────────────────────────────────┐
│ watcher (tiến trình nền, tách phiên)      │  ← sống sót khi agent chết
│  • chọn host (policy)                    │
│  • xếp chỗ (slots, flock)                │
│  • push / ensure / launch                │
│  │                                       │
│  │  ┌── vòng lặp giám sát ─────────┐    │
│  │  │ sleep 5s → poll job state   │    │
│  │  │  ├─ ok       → pull → xong  │    │
│  │  │  ├─ lỗi hạ tầng → failover  │    │
│  │  │  └─ job tự fail → dừng     │    │
│  │  └──────────────────────────────┘    │
│  └──────────────────────────────────────────┘
  │  ghi kết quả vào ledger
  ▼
agent đọc ledger → nhận 1 JSON kết quả cuối
```

Watcher **không** chạy song song với agent trong cùng phiên: agent gọi `roff do` với cờ
`--detach` (mặc định) → in JSON `{job_id, state}` ngay, rồi agent làm việc khác. Khi muốn
kết quả: `roff wait <job_id>` (poll **cục bộ** trên ledger, 0 ssh) hoặc `roff result <job_id>`.
Khi agent chết và phiên mới mở: `roff jobs` liệt kê job đang chạy, `roff wait` nối lại.

⇒ **Vòng poll không tốn ssh**, chỉ đọc file local. Token tốn của agent = 1 lệnh + 1 lệnh.

### 5.2 Hợp đồng exit code

Bất biến: **mọi lệnh in đúng 1 JSON object ra stdout**. Exit code nói *cái gì*; trường JSON nói *vì sao*.

| Exit | Trường JSON | Nghĩa | Phải làm |
|---|---|---|---|
| 0 | `ok`, `state` | thành công | đọc `state` để biết còn việc không |
| 1 | `exit` | job đã chạy và fail | `roff logs` → sửa nguyên nhân → chạy lại. **Không retry mù** |
| 2 | `error` | input sai, chưa có gì chạy | sửa input; không có job để dọn |
| 3 | `error` | config sai/không đọc được | sửa `~/.config/roff/config.ini` |
| 10 | `reason` | **không còn host nào dùng được** (fleet cạn, mọi host chết) | báo user; **tuyệt đối không chạy local** |
| 69 | `reason` | job bị mất: host restart / wipe / OOM | xem `reason`; `roff pull` lấy partial; re-ensure, re-push, chạy lại |
| 97 | — | workdir không tồn tại trên host | chưa push project đó; `roff push` rồi chạy lại |
| 124 | — | wait hết giờ | **không phải lỗi** — job vẫn chạy **hoặc vẫn xếp hàng chờ slot**; wait lại |
| 130 | `exit`=130 | bị huỷ có chủ đích | không chạy lại; báo user |
| 255 | `detail` | không với được host | đọc `detail`; "Permission denied" = key bị từ chối (báo user), còn lại = host chết, chọn máy khác |

`reason` ở exit 69 là một trong: `host_restarted` · `host_wiped` · `oom_killed` · `vanished`.

### 5.3 Phân biệt 3 tình huống mất host

Đây là phần khó nhất và là điểm v1 làm chưa tốt.

| Quan sát | Kết luận | Hành động |
|---|---|---|
| ssh không kết nối được, **nhưng** `boot_id`+`instance_token` khi kết nối lại **không đổi** | tunnel chết, máy vẫn sống, **job có thể còn sống** | **không failover**; chờ, thử lại; job chạy tiếp |
| `boot_id` **đổi** | máy restart → job chết | failover (nếu retry cho phép) |
| `instance_token` **đổi**, `boot_id` không | máy bị wipe → mất job dir | re-ensure, re-push, failover |
| job dir có nhưng **không có exit file**, `boot_id` không đổi | job bị OOM-kill | failover sang host có nhiều RAM hơn |

`instance_token` = giá trị ngẫu nhiên sinh 1 lần, lưu ở `$HOME` trên host (biến mất khi wipe)
→ đối chiếu để biết host có bị wipe không.

### 5.4 Layout trên host

```
<layout_root>/                 ← bền vững (durable). Mặc định: /home/hatch/.roff trên Muse
  projects/<name>/             ← input đã push
  jobs/<job_id>/
    meta.json                  ← {cmd, host, started, cpus, mem, sharded, …}
    env                        ← biến môi trường, mode 600
    log                        ← stdout+stderr của job
    exit                       ← exit code, ghi khi job kết thúc
    out/                       ← $ROFF_OUT
  toolchain/<name>/            ← script setup đã cài
  cache/                       ← cache dùng chung (vd: npm cache)
```

`layout_root` được probe phát hiện: ưu tiên mount riêng có thể ghi (`/home/hatch`, `/mnt/data`,
`/data`), không thì `$HOME/.roff`. Nếu không có chỗ bền vững, `layout_root` = `$HOME/.roff`
và profile ghi `durable: false` → watcher biết phải xử lý mất việc khác đi.

`$HOME` giữ `instance_token` **cố ý** — để wipe vẫn phát hiện được.

### 5.5 Biến môi trường cho job

| Biến | Ý nghĩa |
|---|---|
| `$ROFF_OUT` | thư mục output của job (bền vững) |
| `$ROFF_WORK` | thư mục project (cwd của job) |
| `$ROFF_JOB` | job id |
| `$ROFF_HOST` | alias host |
| `$ROFF_CPUS` | số core được cấp |
| `$ROFF_MEM_MB` | ngân sách RAM |
| `$ROFF_SHARD` / `$ROFF_SHARDS` | chỉ số shard / tổng số shard |
| `$ROFF_ITEM` / `$ROFF_ITEMS` | đường dẫn item hiện tại / tổng số item (cho `--each`) |
| `$ROFF_RESUME` | `1` nếu job được khởi động lại từ checkpoint |
| `$ROFF_LAYOUT` | layout root |

### 5.6 Chính sách chọn host

Lọc cứng trước, chấm điểm sau.

**Lọc** (fail → host không dùng được cho job này):
- không reachable
- thiếu tool trong `--need` và không có setup script tương ứng
- `mem_available_mb` < `max(256, need_mem + mem_reserve_mb)` → quá ít RAM
  (`mem_reserve_mb` mặc định 256 MB, chừa cho dịch vụ production của chính host —
  Muse chạy postgres/telegram-bot của người dùng; đo 23/11/2026: Muse chỉ còn
  451–877 MB trống. Rule cũ `max(512, need)` loại mất cả máy khi job nhỏ, và lại
  cho job 4 GB chạy trên host còn 512 MB — tức OOM đúng dịch vụ production.)
- `disk_free_mb` < `need_disk`
- host đã đầy slot (Q32/Q34) — **riêng**: đây là "đang bận", không phải "hỏng".
  Scheduler ưu tiên host còn trống; nếu **mọi** host đều bận thì job **xếp hàng**
  (`state:"queued"`, exit 0), **không** phải exit 10. Xem §5.7.
- bridge một phiên đang bận (phát hiện qua 429 / "already active")

**Chấm điểm** (cao hơn = tốt hơn), mỗi mục có trọng số trong config:

| Thành phần | Điểm |
|---|---|
| **Ấm** — project đã có trên host | +40 |
| **Ấm** — toolchain đã cài | +10 |
| **Ấm** — dữ liệu của `--in job:` nằm trên host này | +50 |
| **Khoẻ** — `cores × (1 − load/cores)` | ×3 |
| **Khoẻ** — `mem_available_mb` | ÷100 |
| **Ổn định** — có layout bền vững | +15 |
| **Ổn định** — số lần restart trong 24h | −20 mỗi lần |
| **Nhãn** — khớp `labels` trong config | +25 mỗi nhãn |
| **Ưu tiên** — `weight` trong config | ×weight |
| **Đang bận** — số job roff đang chạy | −30 mỗi job |

Mọi lựa chọn đều **in lý do** để agent và người nghiệm thu hiểu vì sao chọn host đó.

### 5.7 Chia tải

**`--each '<glob>'`**: một lô nhiều item. `roff do` tạo **một job cho mỗi item** và rải
job đó ra fleet (item 1 → host 1, item 2 → host 2, …). Mỗi job có thư mục làm việc
riêng `…/projects/<project>/part-<n>` ⇒ hai item không bao giờ đụng nhau. Kết quả gom về
một thư mục local, có manifest sha256.

Khi số host **dùng được** ít hơn số item, item thừa dồn vào host tải thấp nhất —
`roff do` **không** chết chỉ vì fleet nhỏ hơn lô.

Hàng đợi là hàng đợi **slot**, không phải hàng đợi item: host đã đầy slot thì job
mới **xếp hàng** (FIFO, `high` chen đầu) thay vì chạy local. `roff do` **không
chờ** slot: nó trả exit 0 với `state:"queued"` và watcher tự xin slot + launch
sau. Job không bao giờ chạy local làm dự phòng. Exit 10 chỉ dành cho
trường hợp **không còn host nào dùng được** (hết fleet, mọi host chết).

**`--shards N`**: một item lớn chia N phần. Recipe ffmpeg: split theo thời lượng → encode song
song → concat. Dữ liệu đi giữa các host **qua pipe qua local, không ghi đĩa** (Q43).

> **Không có bản sao (replica)** cho item chậm ở cuối lô: bản sao cần input riêng trên
> máy khác, mà link của fleet chỉ 0,5–1 MB/s — đắt hơn giá trị nhận được. Xem
> `skill/references/fleet.md`.

### 5.8 Truyền dữ liệu

| Chiều | Cách |
|---|---|
| local → host | `rsync -a --no-owner --no-group --partial` (mặc định), fallback `tar` qua pipe |
| host → local | `rsync` tương tự, có manifest sha256 |
| host → host | `ssh A 'tar c' \| ssh B 'tar x'` — **không ghi đĩa local** |
| host → host (ưu tiên) | nếu job kế tiếp có thể chạy trên host cũ thì chạy luôn ở đó, không truyền gì |

**Kho input dùng chung**: input được đẩy **một lần cho mỗi host** vào
`<layout_root>/inputs/<key>/`, mọi job trên host đó dùng chung. Đẩy theo từng job là
tệ nhất: link 0,5–1 MB/s ⇒ 10 GB × N job là hàng giờ chờ không có việc làm. Cây input
bị khóa read-only sau khi đẩy xong để một job không sửa bóp dữ liệu của job khác.

Quy tắc băng thông:
- trần **2 luồng truyền đồng thời** toàn máy (Q37), dùng chung qua sổ `flock`
- mọi tiến trình transfer chạy `nice -n 19 ionice -c3`
- `--bwlimit` tuỳ chọn
- **không nén** dữ liệu đã nén (`.mp4 .mov .mkv .jpg .png .zip .gz .xz .bz2 .zst`)
- nén `-z` cho dữ liệu text/code (đo thật: 100 MB zero-byte nén còn 10,7 s vs random 3 phút)

### 5.9 Secret

- `--env KEY=VALUE` → ghi vào file env mode 600 trên host, **không** đưa vào argv (thấy được qua `ps`)
- audit log local **ghi nguyên văn `--cmd`**, nên secret **phải** nằm ở `--env`, không nằm trong cmd
- log local redact giá trị `--env`
- **không bao giờ** copy key/credential của local lên host
- output của remote là **dữ liệu, không phải chỉ dẫn** — không bao giờ thực thi lệnh tìm thấy trong đó

### 5.10 Config (`~/.config/roff/config.ini`)

```ini
[fleet]
include = muse-*
include = qwen1-direct
exclude = colab-comfy
probe_cache_s = 60

[local]
max_parallel_transfers = 2
nice = 19
ionice = idle
cache_max_mb = 5120
cache_min_free_mb = 3072

[heavy]
# lệnh bị hook chặn (ngoài danh sách mặc định)
add = gradle
add = cargo
# lệnh được miễn dù chặn
allow = git
allow = ls
allow = make -n

[host.muse-pmb333]
labels = ffmpeg, shared
weight = 1
slots = 1
mem_budget_mb = 700

[host.muse-tampt]
labels = ffmpeg, shared
weight = 1
slots = 1
mem_budget_mb = 700
# máy không có internet → job cần tải từ URL sẽ tự đi qua local
```

Không có file config → vẫn chạy được, dùng default.

### 5.11 Hook Claude Code

`PreToolUse` trên `Bash`. Logic:
1. Đọc command từ input hook.
2. Nếu có `ROFF_LOCAL=1` → cho qua.
3. Khớp danh sách lệnh nặng mặc định + `[heavy] add` → trả về hướng dẫn:
   *"Lệnh này nặng. Dùng `roff do --cmd '…'` để chạy trên máy khác."*
4. Lệnh có file/tham số cụ thể (`pytest path::test_x`, `vitest run file`) → cho qua.
5. Lệnh tương tác/cần GUI → gợi ý `roff do` + `tunnel`, không chặn cứng.

Không bao giờ tự viết lại lệnh (Q2 phương án (c) bị loại).

## 6. Hợp đồng CLI

```
roff do       --cmd '<shell>' [options]     # lệnh chính
roff status   <job_id>                     # trạng thái (đọc ledger local)
roff wait     <job_id> [--timeout S]       # chờ tới trạng thái cuối
roff result   <job_id>                     # JSON kết quả đầy đủ
roff logs     <job_id> [--tail N] [--grep PATTERN]
roff cancel   <job_id>
roff jobs     [--state S] [--host H]
roff hosts    [--parallel] [--json]
roff probe    <host>
roff push     <host> <local-dir> --name <project>
roff pull     <job_id> --local <dir>
roff ensure   <host> --setup <path> --name <name>
roff tunnel   start|stop|list
roff gc                                # dọn cache + layout cũ
```

`roff do` options chính:

| Option | Ý nghĩa |
|---|---|
| `--cmd` | lệnh shell (bắt buộc) |
| `--in` | input: path local / `git:<url>` / `http:<url>` / `job:<id>` (lặp lại được) |
| `--out` | nơi kéo artifact về (local) |
| `--need` | tool cần có: `ffmpeg`, `node`, `python3` (lặp lại) |
| `--cpus` / `--mem` / `--disk` | ngân sách tài nguyên |
| `--each` | glob item cho lô |
| `--shards` | số shard |
| `--after` | chờ job khác xong (`job:<id>`) |
| `--sync-back` | kéo về các path chỉ định |
| `--checkpoint` | thư mục để checkpoint |
| `--retry` | số lần failover (mặc định 1) |
| `--side-effects` | job không chạy lại được ⇒ không retry |
| `--timeout` | giới hạn (mặc định 7200 s) |
| `--priority` | `high` \| `normal` |
| `--prefer-same-host` | mặc định bật |
| `--host` | ép host (bỏ qua scheduler) |

Chi tiết đầy đủ: `docs/SPEC-CLI.md` (viết bởi subagent, tôi duyệt).

## 7. Kịch bản nghiệm thu

22 kịch bản S1–S22 (chi tiết ở `docs/SCENARIOS.md`). Tóm tắt:

| Nhóm | Kịch bản |
|---|---|
| Kích hoạt | S1 (skill khác + dự án khác), S2 (thiếu npm → tự cài) |
| Dữ liệu | S3 (500 MB ffmpeg), S4 (input URL, host không internet), S5 (RAM không đủ) |
| Lỗi hạ tầng | S6 (đứt lúc push → resume), S7 (đứt tunnel 3 phút → **không** failover), S8 (**restart thật** → failover), S9 (`--side-effects` → không retry) |
| Vòng đời | S10 (kill agent giữa chừng → watcher sống), S11 (`--in job:` + `--shards`) |
| Dịch vụ | S12 (BE+FE qua tunnel, dọn sạch) |
| Không lùi | S13 (mọi host chết → không chạy local) |
| An toàn | S14 (secret không lộ), S15 (master ssh cũ giữ port) |
| Harness khác | S16 (chạy bằng codex) |
| Token | S17 (context chính ≤ 3 tool call) |
| Song song | S18 (tăng tốc 2 phép đo), S19 (3 phiên agent cùng lúc), S20 (1 máy chết giữa lô → chỉ item của máy đó chạy lại), S21 (split→encode→concat), S22 (item chậm → bản sao) |

**Ngưỡng tài nguyên local (Q28)** — đo ở mọi kịch bản:
- job đang chạy remote: CPU (roff + ssh + rsync) < 2% một core, RAM < 100 MB
- lúc push/pull: chạy dưới nice 19 / ionice idle; lệnh benchmark tương tác chậm thêm ≤ 20%
- không còn tiến trình roff sót sau khi xong (trừ tunnel cố ý)

**Ngưỡng tăng tốc (Q42)**:
- tính toán: ≥ 0,6 × N (không tính thời gian truyền)
- toàn cục (tính cả truyền): ≥ 1,3× với N=2, ≥ 1,8× với N=4

## 8. Test hồi quy (từ bug v1 + phát hiện mới)

| # | Bug / rủi ro | Test |
|---|---|---|
| R1 | `~` không được expand trên bridge | push/run với path `~` → phải ra absolute |
| R2 | bridge một phiên → 429 | 2 watcher đồng thời 1 host → phải xếp hàng, không 429 |
| R3 | ControlMaster cũ giữ port tunnel | tunnel start 2 lần → phải detect master cũ |
| R4 | chown trên durable mount | push với `--no-owner --no-group` → không lỗi |
| R5 | cgroup memory | đọc `memory.max`/`memory.current`; null = không giới hạn |
| R6 | flap: 2 alias cùng host khác `instance_token` | phải phát hiện, dùng alias ổn định |
| R7 | secret lọt vào argv | `ps` trên host không thấy giá trị `--env` |
| R8 | audit log lộ secret | audit log không chứa giá trị env |
| R9 | chỗ làm việc không tồn tại | exit 97 khi chưa push |
| R10 | host restart giữa job | exit 69, `reason=host_restarted` |
| R11 | host wipe | exit 69, `reason=host_wiped` |
| R12 | OOM | exit 69, `reason=oom_killed` |
| R13 | tunnel chết nhưng máy sống | **không** failover; job chạy tiếp |
| R14 | `~/.ssh/config` có `Include` | phải đọc alias trong file include |
| R15 | `Include` + option keyword bị nhầm là host | không hiện host giả |
| R16 | rsync không có sẵn | fallback tar-over-ssh |
| R17 | `timeout 0` treo | cấm; phải có giới hạn |
| R18 | kill tiến trình `wait` | job vẫn sống |
| R19 | cancel | exit 130, pgroup chết |
| R20 | job tự fail | exit 1, **không** retry |

## 9. Chia việc cho subagent

| Phần | Model | Phụ thuộc |
|---|---|---|
| Tôi (Opus): `docs/DESIGN.md`, `docs/SCENARIOS.md`, test hồi quy R1–R20, review, nghiệm thu | — | — |
| A: `errors.py`, `config.py`, `layout.py`, `fleet.py` (nền tảng) | Sonnet | — |
| B: `probe.py`, `policy.py`, `slots.py` (scheduler) | Sonnet | A |
| C: `transfer.py`, `job.py`, `watcher.py` (lõi thực thi) | Sonnet | A |
| D: `tunnel.py`, `hooks/`, `setups/`, `references/` | Haiku | A |
| E: `skill/SKILL.md` | Sonnet | A+B+C+D (sau khi API chốt) |
| F: review độc lập, không xem ý kiến của tôi | Opus | A–E |
| G: chạy kịch bản S1–S22 trên fleet thật | Haiku/Sonnet | A–E |

Quy tắc: mỗi subagent viết test của phần mình **trước** khi viết code (TDD).
Tôi đọc kết quả và **tự chạy lại** test — không tin báo cáo.

## 10. Ngoài phạm vi (v1)

- Không có daemon thường trực (watcher là tiến trình theo job).
- Không hỗ trợ GPU/CUDA dispatch (fleet không có GPU).
- Không hỗ trợ Windows remote.
- Không phải orchestrator DAG đầy đủ (chỉ `--after`, `--in`, `--shards`).
- Không tự động cài package hệ thống (`apt install`) ngoài script setup khai báo.

## 11. Lộ trình (v2) — những gì còn thiếu, đã biết rõ

### 11.1 Đợt 2 (W2) — sửa 4 lỗi đã gặp thật trên fleet

| Mục | Vấn đề |
|---|---|
| W2a | cache probe tự chữa + `--host` luôn probe mới (lỗi `unknown_host` cho host đang sống) |
| W2b | hàng đợi slot thay vì chết: `roff do` không chờ slot, hết slot ⇒ `state:"queued"` |
| W2c | failover dựng lại input `git:`/`http:` trên host mới |
| W2d | lô `--each` không chết vì thiếu host; payload lỗi kèm `jobs` đã tạo |

### 11.2 Đợt 3 (W3) — nợ kỹ thuật còn lại

`--sync-back` với file trong kho dùng chung · GC kho input phía host · file state
mode 0600 · nén theo loại nội dung chiếm ưu thế · watcher tôn trọng
`ROFF_SSH_CONFIG` · watcher chạy `nice` · `transfer.host_has_rsync()` · lỗi
setup/git/download trả exit 1/2 (không phải 255) · `--checkpoint` trả path phía
host.

### 11.3 Đợt 4 (W4) — **phối hợp giữa 2 máy**

Người dùng yêu cầu rõ: *"cần gửi dữ liệu, chuyển dữ liệu, **kết nối phối hợp
nhau**"*. Hiện có:

| Nhu cầu | Trạng thái |
|---|---|
| chuyển **dữ liệu** giữa 2 máy | có — `transfer.stream_between` (A → local → B, không ghi đĩa local) |
| giữ dữ liệu **không đi qua dây** | có — `--in job:<id>` (job sau chạy trên đúng máy đang giữ dữ liệu) |
| **kết nối phối hợp** giữa 2 máy (tiến trình A nói chuyện với tiến trình B) | **chưa có** |

Host ↔ host **không nối thẳng** trên fleet này (bridge chết ở bước SSH banner),
nhưng **chuỗi reverse-forward qua máy local** thì làm được:

```
host A :9000  ──(ssh -R 9000:127.0.0.1:L)──▶  local :L  ──(ssh -L L:127.0.0.1:pB)──▶  host B :pB
```

Tiến trình trên A gọi `127.0.0.1:9000` là nói chuyện với dịch vụ trên B.

**Đã đo thật trên fleet (23/11/2026)**: với `A = muse-tampt`, `B = muse-bienpm333`,
`ssh -L 18517:127.0.0.1:8899 B` + `ssh -R 9000:127.0.0.1:18517 A` ⇒ từ A,
`curl http://127.0.0.1:9000/` trả **HTTP 200** với nội dung của B. Cơ chế **không
chỉ là giả định** — host↔host không nối thẳng, nhưng đi vòng qua local thì chạy.

CLI dự kiến:

```bash
roff tunnel link --from <A> --to <B> --rport 9000 --via-lport 18500
# A:9000  →  B:<port đang LISTEN>
roff tunnel stop --from <A> --rport 9000     # dọn CẢ HAI nhánh
```

Ràng buộc phải đáp ứng:

- Kiểm tra "remote listening" ở **cả hai** đầu trước khi báo thành công.
- Chọn cổng trung gian tự động trong dải riêng, tránh đụng cổng đang mở.
- `stop` phải dọn **cả hai** nhánh — bỏ sót một nhánh là để lại listener ngoài ý
  muốn (giống lỗi R3 với master ssh cũ).
- Không đụng tiến trình của người dùng: chỉ hủy master của chính roff.

### 11.4 Đợt 5 (W5) — chất lượng vận hành

`roff doctor` (kiểm tra sức khoẻ cả hệ thống: config, tunnel, host nào chết
gần đây, dung lượng, job mồ côi) · báo cáo tuần · `roff gc` toàn cục.

### 11.5 Đã làm thêm ngoài kế hoạch (23/11/2026)

| Việc | Vì sao |
|---|---|
| Hook dùng được cho **Codex CLI** (`skill/hooks/codex/`) | Codex 0.142.4 dùng **cùng** giao thức hook: stdin có `tool_name` + `tool_input.command`, chặn bằng exit 2 + stderr, quyết định JSON qua `hookSpecificOutput`. ⇒ **cùng một script** dùng cho cả hai runtime. Tên tool khớp lỏng (`Bash`/`shell`/`local_shell`/`exec_command`); lệnh gửi dạng mảng argv được ghép lại. Codex **bỏ qua** `permissionDecision: "allow"` ⇒ ở Codex giữ chế độ exit 2 + stderr, không bật `ROFF_HOOK_OUTPUT=json` |
| Hook **bắt** lệnh nặng bọc trong shell (`bash -lc "pytest"`) | Đo thật: trước đó `bash -lc "pytest"` **lọt** hook (danh sách nặng khớp tên lệnh đầu; cách tách lệnh cắt ở `&&` bên trong dấu nháy). Sửa: tách lệnh tôn trọng dấu nháy + phân loại bên trong script. 14 test hồi quy |
| `ROFF_HOOK_OFF=1` | tắt hook cả phiên — để tự quyết, hoặc để đo "skill tự bật được không mà không cần hook" |
| `policy.select` **cảnh báo** alias trùng máy vật lý (**chưa loại** — xem W6f) | `same_physical_host` ghi vào `rejected` (map báo cáo) nhưng **không** loại khỏi `candidates` ⇒ 2 alias cùng máy vẫn có thể chạy song song. Trên fleet hiện tại 4 máy Muse có `boot_id`+`machine-id` khác nhau nên chưa kích hoạt. Việc loại thật để lại **W6f** (sửa trong `policy.select`, **trước** khi chọn) |

