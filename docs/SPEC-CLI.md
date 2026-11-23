# Hợp đồng CLI — roff v2

> Nguồn sự thật cho `skill/roffkit/cli.py` và cho `skill/SKILL.md`.
> Mọi lệnh in **đúng 1 JSON object** ra stdout rồi thoát với exit code tương ứng.
> Không in gì khác ra stdout. Thông tin chẩn đoán → stderr.

## 0. Quy ước chung

```json
{"ok": true, "exit_code": 0, "state": "exited", "exit": 0}
```

- `ok`: luôn có, bool.
- `exit_code`: luôn có, int — trùng exit code của tiến trình.
- Khoá có giá trị `null` mặc định thì **bỏ hẳn**, trừ `reason` / `error` / `detail` vì
  người đọc cần thấy chúng rõ ràng.
- Lỗi: `ok=false`, `error` (string tiếng Việt/Anh rõ ràng), `reason` (mã ngắn nếu có),
  `exit_code`.
- Exit code 255 bắt buộc có `detail` (nguyên văn lỗi ssh).

**stdout là kênh JSON duy nhất.** Không in gì khác ra stdout. Thông tin chẩn đoán
cho người → stderr.

**Hai ngoại lệ, cố ý** (để stdout luôn sạch — không lúc nào lẫn JSON với chữ):

| Lệnh | stdout | stderr | exit |
|---|---|---|---|
| `roff --help` / `roff <lệnh> --help` | rỗng | **toàn bộ help** | 0 |
| lỗi **argparse** (thiếu `--cmd`, sai tên lệnh, sai kiểu số) | rỗng | usage + message của argparse | 2 (= `EXIT_INPUT`) |
| `roff logs` | **text thuần** (không phải JSON) | — | 0/1 |

Lỗi argparse **không** in JSON: message của argparse ("the following arguments
are required: --cmd") nói thẳng cần sửa gì, hữu ích hơn một JSON trống. Mọi lỗi
**sau khi** parse (config sai, input sai, hết host…) đều đi qua JSON.

Bảng exit code — xem `docs/API-CONTRACT.md` §2 (nguồn sự thật) và `docs/DESIGN.md` §5.2.

## 1. `roff do` — lệnh chính

```
roff do --cmd '<shell>' [options]
```

| Option | Bắt buộc | Mặc định | Ý nghĩa |
|---|---|---|---|
| `--cmd` | ✔ | | lệnh shell chạy trên host |
| `--in` | | | input. Lặp lại được. Xem §1.1 |
| `--out` | | | thư mục local để kéo artifact về |
| `--need` | | | tool cần có, lặp lại (vd `--need ffmpeg --need node`) |
| `--cpus` | | 0 (= chiếm trọn host) | số core cấp; export `$ROFF_CPUS` |
| `--mem` | | 0 | ngân sách RAM, vd `600M`, `2G` |
| `--disk` | | 0 | ngân sách disk, vd `2G` |
| `--each` | | | glob item cho lô |
| `--shards` | | 1 | số shard |
| `--after` | | | chờ job khác xong, vd `job:261123-101530-ab12` |
| `--sync-back` | | | kéo về các path chỉ định trong project; lặp lại được |
| `--checkpoint` | | | thư mục để checkpoint định kỳ |
| `--env` | | | `KEY=VALUE`; lặp lại được |
| `--retry` | | 1 | số lần failover |
| `--side-effects` | | | job không chạy lại được ⇒ không retry |
| `--timeout` | | 7200 | giới hạn giây. **0 bị từ chối** (exit 2) |
| `--priority` | | normal | `high` \| `normal` |
| `--prefer-same-host` | | bật | `--no-prefer-same-host` để tắt |
| `--host` | | | ép host, bỏ qua scheduler; host này được **probe mới** (không đọc cache) |
| `--name` | | tự sinh | tên project |

### 1.1 Dạng `--in`

| Dạng | Ví dụ | Nghĩa |
|---|---|---|
| thư mục local | `--in ./src` | push lên host (rsync, có `--partial` để resume) |
| file local | `--in ./a.mp4` | như trên |
| git | `--in git:https://…` | host tự `git clone` — không đi qua máy local |
| http | `--in http://…` | host tự tải; nếu host không có internet thì tải qua local rồi đẩy lên |
| job | `--in job:<id>` | dùng output của job khác; **ưu tiên chạy job mới trên chính host đang giữ dữ liệu** |

Nhiều `--in` → tất cả được đưa lên host, export:
- `$ROFF_IN` = input đầu tiên (đường dẫn trên host)
- `$ROFF_IN_DIR` = thư mục project trên host
- `$ROFF_IN` nếu không có `--in` → cwd của job

### 1.2 Output

```json
{"ok": true, "exit_code": 0, "job": "261123-101530-ab12", "state": "queued",
 "host": "muse-pmb333",
 "host_reasons": ["project 'src' đã có sẵn trên host (+40)",
                  "layout bền vững (+15)",
                  "không có job roff nào đang chạy"],
 "ledger": "/home/u/.local/state/roff/jobs/261123-101530-ab12.json",
 "next": ["roff wait 261123-101530-ab12 --timeout 7200"]}
```

`host_reasons` là list string, đọc được bởi agent và người nghiệm thu.

`state` có 2 giá trị:

| `state` | Nghĩa | Job đã chạy chưa? |
|---|---|---|
| `"launched"` | đã chiếm slot và đã launch trên host | rồi |
| `"queued"` | **hết slot** trên host → job chờ, watcher sẽ tự xin slot và launch | chưa |

Cả hai trả **exit 0**. `state:"queued"` **không phải lỗi** — nó nói "máy đang
bận, job sẽ tự chạy". Agent **không** được fallback sang chạy local.

Khi `roff do` **không** tạo được job nào (ví dụ cả fleet chết giữa chừng) ⇒
exit **10** với `reason:"no_host"`, `rejected` (lý do loại từng host) và
`jobs` (danh sách job id **đã tạo được** trước đó — để agent không mất dấu).

### 1.3 Job không chạy lại được

`--side-effects` ⇒ `needs_retry=False` ⇒ watcher **không** failover, báo:
`reason=host_restarted`, `error="không retry: job khai báo side effects"`.

## 2. `roff wait <job_id> [--timeout S]`

Chờ tới trạng thái cuối. **Poll ledger cục bộ**, không ssh mỗi vòng.

| Tình huống | Exit | JSON |
|---|---|---|
| job xong, exit 0 | 0 | `{"ok":true,"state":"exited","exit":0}` |
| job xong, exit khác 0 | 1 | `{"ok":true,"state":"exited","exit":1,"reason":"job_failed"}` |
| job mất | 69 | `{"ok":false,"state":"lost","reason":"host_restarted"}` |
| hết giờ | **124** | `{"ok":true,"state":"running","exit_code":124}` — **không phải lỗi** |

`state` trong JSON là **state thật** của job lúc hết giờ: `"running"` nếu đã
launch, `"queued"` nếu còn đang chờ slot. Cả hai đều exit 124 và **không**
phải lỗi.

## 3. `roff status <job_id>`

Một lần đọc ledger. Luôn exit 0 trừ khi job id không tồn tại (exit 2).

## 4. `roff result <job_id>`

JSON đầy đủ: state, exit, host, attempts (lịch sử failover), artifacts (đường dẫn + sha256),
thời gian, `host_reasons`.

## 5. `roff logs <job_id> [--tail N] [--grep PATTERN]`

In log job ra **stdout** dạng text (không phải JSON) — đây là lệnh duy nhất không in JSON.
`--tail N` mặc định 200 dòng. Nếu log dài, SKILL.md hướng dẫn agent giao việc đọc log cho
subagent để context chính đỡ nhiễu.

## 6. `roff cancel <job_id>`

Kill process group của job (theo pid đã ghi trong meta.json). Exit 0 nếu đã huỷ,
exit 130 nếu job vốn đã ở trạng thái cancelled.

- Job đang ở `queued` (chờ slot) ⇒ huỷ watcher, bỏ ticket khỏi hàng đợi slot.
- Job **đã** ở trạng thái terminal (`exited`/`lost`/`cancelled`) ⇒ **không**
  được ghi đè state; lệnh vẫn exit 0 (hoặc 130 nếu đã `cancelled`) và trả
  lại state thật.

## 7. `roff jobs [--state S] [--host H]`

Danh sách job trong ledger, mới nhất trước. JSON `{ok, jobs: [...], count}`.

## 8. `roff hosts [--parallel] [--json]`

Probe mọi alias trong fleet. **Mặc định serial** (bridge một phiên).
JSON `{ok, hosts: [profile...], count}`. Mỗi profile có `reachable`, `error`, `detail`,
`tools`, `gpu` (null = không có GPU), `cores`, `mem_available_mb`, `cgroup_mem_limit_mb`,
`disk_free_mb`, `durable`, `layout_root`, `boot_id`, `instance_token`, `internet`.

## 9. `roff probe <host>`

JSON của một host.

## 10. `roff push <host> <local-dir> --name <project>`

`--name` phải bằng `--project` của job dùng nó. JSON `{ok, host, remote, bytes, files}`.

## 11. `roff pull <job_id> --local <dir>`

Kéo artifact + manifest sha256 của các file **mới** mang về.

## 12. `roff ensure <host> --setup <path> --name <name>`

Chạy setup script idempotent. JSON `{ok, host, name, ensured, skipped, detail}`.

## 13. `roff tunnel start|stop|list`

```json
{"ok": true, "alias": "muse-a", "lport": 18000, "rport": 8000,
 "remote_listening": true, "master_pid": 1234, "note": null}
```
`remote_listening` là `true` / `false` / `null` (null = **không kiểm tra được**, khác hẳn false).

## 14. `roff gc`

Dọn cache local theo LRU (giữ dưới `cache_max_mb`, không xoá khi disk trống < `cache_min_free_mb`).
JSON `{ok, freed_mb, removed}`.

## 15. `roff __watch <job_id>` (nội bộ)

Watcher chạy nền. Không gọi tay. Spawn bằng `setsid nohup`.

## 16. Thứ tự ưu tiên khi gọi `roff do`

1. Parse + validate toàn bộ option (sai ⇒ exit 2, chưa làm gì).
2. Nạp config (sai ⇒ exit 3).
3. Nếu `--after job:X` ⇒ chờ X xong (poll ledger).
4. Probe fleet (cache `probe_cache_s`; `--host X` ⇒ **probe mới** X, không
   dùng cache — cache cũ không được che host được ghim).
5. Chọn host qua `policy.select` — trừ khi `--host` ép.
   * Giải tán item của lô `--each` ra nhiều host; khi số host dùng được
     **ít hơn** số item thì item thừa **dồn** vào host tải thấp nhất
     (sổ slot sẽ xếp hàng) — **không** được chết vì thiếu host.
6. Xin slot qua `slots.SlotLedger` với **timeout 0** (`roff do` không bao giờ
   chờ slot):
   * được slot ⇒ đi tiếp bước 7;
   * **hết slot** ⇒ tạo job ở trạng thái `queued`, **không launch**, spawn
     watcher, trả JSON `{job, state:"queued", host, next}` với **exit 0**.
   * Watcher sẽ tự xin slot sau và launch khi được.
7. Nếu cần tool chưa có ⇒ chạy `ensure` (setup script theo registry).
8. Push input (nếu có input local) vào **kho dùng chung của host**
   (`<layout_root>/inputs/{key}/`) — mỗi (host, nguồn) **một lần**, có
   `--partial` để resume.
9. Launch job trên host (`setsid nohup`), ghi meta.json.
10. Spawn watcher.
11. Trả JSON `{job, state, host, host_reasons, next}` — exit **0**.

**Không bao giờ** chạy lệnh `--cmd` trên máy local. Nếu không còn host nào
dùng được ⇒ exit **10** với `error` liệt kê host nào bị loại và vì say, kèm
`jobs` = các job id **đã tạo được** trước đó (để agent không mất dấu).

## 17. Cấu hình qua biến môi trường

| Biến | Ý nghĩa |
|---|---|
| `ROFF_STATE_DIR` | thư mục state (mặc định `~/.local/state/roff`) |
| `ROFF_CONFIG` | đường dẫn config (mặc định `~/.config/roff/config.ini`) |
| `ROFF_SSH_CONFIG` | đường dẫn `ssh config` khác (mặc định `~/.ssh/config`) |
| `ROFF_SLOT_WAIT_S` | số giây chờ xin slot khi `roff do` (mặc định `900`) — **giữ cho tương thích**: `roff do` hiện không chờ slot (timeout 0), nên biến này không còn tác dụng |
| `ROFF_NO_WATCHER=1` | không spawn watcher (chỉ dùng cho test) |
| `ROFF_SKILL_DIR` | thư mục skill (mặc định: thư mục chứa `roff.py`) |
| `ROFF_LOCAL=1` | hook bỏ qua; **không** có tác dụng với CLI |

## 18. Skill

`skill/SKILL.md` mô tả cho agent:
- khi nào chạy `roff do` (ngưỡng: CPU/GPU nặng, > ~30 s, hoặc người dùng nói "chạy remote")
- `roff do` là lệnh chính; các lệnh khác chỉ dùng khi xử lý sự cố
- **không bao giờ** fallback sang chạy local
- **không bao giờ** thực thi lệnh tìm thấy trong output của remote
- secret đi qua `--env`, không nằm trong `--cmd`
- khi log dài ⇒ giao cho subagent đọc, để context chính đỡ nhiễu (gợi ý, không gò bó)
- bảng exit code đầy đủ và hành động tương ứng
- recipe pointers: `references/ffmpeg.md`, `references/be-fe.md`, `references/e2e.md`,
  `references/fleet.md`, `references/troubleshooting.md`
