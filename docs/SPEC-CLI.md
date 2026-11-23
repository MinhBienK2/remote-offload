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
| `--sync-back` | | | kéo path chỉ định về **project**; lặp lại được. Path **tương đối** ⇒ tính theo **thư mục làm việc lúc gọi `roff do`**; path **tuyệt đối** ⇒ giữ nguyên. Path nằm ngoài project **và** ngoài mọi `--in` ⇒ **từ chối lúc gửi job** (exit 2, `sync_back_outside_project`). Kéo hỏng ⇒ exit 74 `sync_back_failed` |
| `--checkpoint` | | | thư mục **trên host** để **job tự** lưu checkpoint (`$ROFF_CHECKPOINT`). `roff` **không** tự chụp snapshot — nó chỉ tạo thư mục và export biến |
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

**`inferred_needs`** — tool **suy ra** từ câu lệnh (vd `ffmpeg -i in.mp4 out.mp4`
⇒ `["ffmpeg"]`, `npm test` ⇒ `["node"]`), tên đã chuẩn hoá (`npm` ⇒ `node`).
Đây là tiêu chí **MỀM**: host thiếu tool suy ra **vẫn được chọn**, chỉ bị
đẩy xuống thấp hơn; `--need` (viết rõ) mới là tiêu chí **cứng** ⇒ thiếu tool
thì host bị loại. `inferred_needs` là `[]` khi câu lệnh không đủ dấu hiệu.

`state` có 2 giá trị:

| `state` | Nghĩa | Job đã chạy chưa? |
|---|---|---|
| `"launched"` | đã **chiếm slot**; watcher đang đẩy input + launch trên host | đang khởi chạy |
| `"queued"` | **hết slot** trên host → job chờ, watcher sẽ tự xin slot và launch | chưa |

Cả hai trả **exit 0**. `state:"queued"` **không phải lỗi** — nó nói "máy đang
bận, job sẽ tự chạy". Agent **không** được fallback sang chạy local.

**Mốc "input được đẩy lúc nào" (B36/C10).** `roff do` **không** đẩy input và
**không** launch: nó chỉ tạo job + xin slot, rồi **hoãn** việc đẩy input +
launch cho **watcher** (mỗi job 1 watcher). Nhờ vậy một lô `--each` được gửi
**song song** — trước đây mỗi item tốn ~21–26 s tuần tự nên các job của một
lô **không bao giờ chạy chồng nhau** (đo thật 23/11/2026:
`docs/evidence/final-20261123/s18-submit-latency.txt`).

Hệ quả (đánh đổi, nói rõ):

* `state:"launched"` ⇒ job **đã nhận slot**, watcher **đang** khởi chạy nó
  (đẩy input rồi launch) — process trên host có thể chưa chạy lúc `do` trả
  JSON (vài giây).
* Lỗi **đẩy input** (host chết lúc rsync/clone, URL hỏng, file local biến
  mất) **không** lộ ra ở `roff do` nữa mà lộ ra ở **watcher** ⇒ `roff do` trả
  exit 0, còn `roff wait` mới báo lỗi (reason rõ ràng trong ledger). Việc
  **failover** sang host khác vẫn còn nguyên (watcher tự failover).
* Lỗi **sai cấu hình** vẫn lộ ra ở `roff do` ngay: input local không tồn
  tại (exit 2), option sai (exit 2), cả fleet chết (exit 10).

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
| job xong, exit 0 nhưng **artifact chưa về** | **74** | `{"ok":false,"state":"exited","exit":0,"reason":"pull_failed","pulled":false,"pull_error":"…"}` |
| job xong, exit 0, artifact **đã** về nhưng path `--sync-back` **chưa** về | **74** | `{"ok":false,"state":"exited","exit":0,"reason":"sync_back_failed","pulled":true,"sync_back_error":"…"}` — `pulled` vẫn `true` vì artifact **chính** đã về |
| job mất | 69 | `{"ok":false,"state":"lost","reason":"host_restarted"}` |
| hết giờ | **124** | `{"ok":true,"state":"running","exit_code":124}` — **không phải lỗi** |

`state` trong JSON là **state thật** của job lúc hết giờ: `"running"` nếu đã
launch, `"queued"` nếu còn đang chờ slot. Cả hai đều exit 124 và **không**
phải lỗi.

**Exit 74 — phải phân biệt với exit 1 (B23).** `1` = *job* fail ⇒ **đừng**
chạy lại job. `74` = job **đã chạy xong thành công** (`exit` trong JSON vẫn là
`0`) nhưng bước **kéo artifact về local** hỏng (thường do reverse tunnel rớt)
⇒ **kéo lại** bằng `roff pull <job> --local <dir>`, **không** cần chạy lại job.

`pulled` trong JSON là trạng thái thật của lần kéo: `true` = artifact **đã** nằm
trong `--out`; `false` + `pull_error` khác rỗng = **chưa** về.

**Exit 74 với `reason=sync_back_failed` (B26).** Job **đã** chạy xong (`exit:0`),
artifact **đã** về (`pulled:true`), nhưng path `--sync-back` **không** về local
(`sync_back_error` khác rỗng) ⇒ vẫn exit **74** ⇒ **kéo lại** bằng
`roff pull <job> --local <dir>` (lần này kéo cả các path `--sync-back` về đúng
đích của chúng), **không** chạy lại job. `roff pull` thành công ⇒ xoá
`sync_back_error` ⇒ `roff wait` trở lại exit 0.

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

### 13.1 `roff tunnel link --from A --to B --rport P [--via-lport V] [--to-port Q]`

Nối phối hợp giữa **2 máy** qua máy local (host↔host không nối thẳng trên
fleet này): tiến trình ở máy A gọi `127.0.0.1:P` là nói chuyện với dịch vụ
đang LISTEN ở máy B.

```json
{"ok": true, "exit_code": 0, "from": "muse-pmb333", "to": "muse-tampt",
 "rport": 19000, "to_port": 18899, "via_lport": 18500,
 "a_listening": true, "b_listening": true, "note": "a_listening=True; b_listening=True"}
```

* `via_lport` — cổng trung gian trên máy local; mặc định tự chọn trong dải
  **18500–18599** (chọn bằng cách bind thật). `--to-port` mặc định = `--rport`.
* `a_listening` / `b_listening` — đã kiểm tra LISTEN ở cả hai đầu chưa; `null`
  = **không kiểm tra được** (khác hẳn `false`).
* Lỗi ⇒ `ok:false` + `reason` (xem `docs/API-CONTRACT.md` §17) + `error`
  tiếng Việt; host không với được ⇒ exit 255 kèm `detail` nguyên văn lỗi ssh.
  Nếu máy `--to` không có gì đang LISTEN ⇒ lỗi `remote_not_listening` và
  **không** mở forward nào.

### 13.2 `roff tunnel stop --from A --rport P`

Dọn **cả hai** nhánh của link (`-R` trên A và `-L` qua B). JSON
`{ok, from, rport, to_port, via_lport, stopped, remote_stopped, local_stopped,
master_stopped, detail}`. `roff tunnel list` liệt kê link đang mở trong mảng
`links`.

## 14. `roff gc`

Dọn cache local theo LRU (giữ dưới `cache_max_mb`, không xoá khi disk trống < `cache_min_free_mb`).
JSON `{ok, freed_mb, removed}`.

| Cờ | Mặc định | Nghĩa |
|---|---|---|
| `--max-age-days N` | `7` | xoá bản copy trong **kho input dùng chung** của host nếu cũ hơn N ngày |
| `--job-max-age-days N` | `30` | xoá job terminal trong ledger nếu cũ hơn N ngày |

> **Cả hai cờ nhận giá trị `0`** ⇒ "xoá mọi thứ đủ điều kiện, kể cả thứ vừa
> mới tạo" (dùng để dọn khi kiểm chứng). Giá trị **âm** ⇒ exit **2** với
> `reason=invalid_option` (như `--timeout`/`--cpus`). Trước B32, `0` bị nuốt
> (0 là *falsy*) ⇒ `--max-age-days 0` thành 7 ngày ⇒ kho input **phình vô hạn**.

Ngoài ra `roff gc` **dọn ledger job**: xoá job **định dạng cũ** (file không
có khoá `state` ⇒ rác, giữ slot ⇒ `roff wait` treo), job terminal quá
`--job-max-age-days` (mặc định **30**), và file phụ `.heartbeat`/`.env` mồ côi.
Job đang chạy/xếp hàng **không** bị xoá. JSON có thêm `removed_jobs`,
`removed_jobs_count`, `skipped_jobs`, `freed_jobs_mb` (`freed_mb` = tổng).

`gc` cũng dọn **lock mồ côi** trên host (B22): thư mục `<key>.lock` quá hạn mà
chủ đã chết ⇒ xoá. Lock **còn chủ** (tiến trình đang giữ) hoặc thuộc job đang
chạy ⇒ **giữ**. JSON có thêm `removed_locks` (dạng `"<host>:<key>"`) và
`removed_locks_count`; từng host trong `hosts` có `removed_locks` +
`skipped_locks` (`{"key", "reason"}`, reason: `in_use` / `locked` /
`host_unreachable` / `remove_failed`).

## 14.1 `roff doctor [--probe]`

Chẩn đoán sức khoẻ hệ thống — **chỉ báo, không sửa gì** (không ghi file, không
đổi quyền, không kill tiến trình).

```json
{"ok": false, "exit_code": 1, "problems": 2,
 "checks": [{"name": "config", "ok": true, "detail": "..."}]}
```

* `problems` = số check `ok:false`; `ok` = (`problems == 0`); exit **1** khi có
  vấn đề, **0** khi sạch.
* Mặc định **không** probe mạng (đọc cache) ⇒ chạy < 1 s; `--probe` mới probe
  thật host.
* Check lỗi không làm hỏng lệnh (check đó thành `ok:false` với `detail`).
* Tên check: `config`, `wrapper`, `hook`, `fleet`, `state_perms`,
  `orphan_jobs`, `disk_local`.

## 15. `roff __watch <job_id>` (nội bộ)

Watcher chạy nền. Không gọi tay. Spawn bằng `setsid nohup`.

## 16. Thứ tự ưu tiên khi gọi `roff do`

1. Parse + validate toàn bộ option (sai ⇒ exit 2, chưa làm gì).
2. Nạp config (sai ⇒ exit 3).
3. Nếu `--after job:X` ⇒ chờ X xong (poll ledger).
4. Probe fleet (cache `probe_cache_s`; `--host X` ⇒ **probe mới** X, không
   dùng cache — cache cũ không được che host được ghim). Probe **1 lần cho
   cả lô** — không probe lại cho từng item.
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
8. Ghi ledger: kế hoạch input (`input_specs`, `input_sources`, `item_local`)
   + cờ `deferred_start` ⇒ **watcher** sẽ tự làm bước 9–10.
9. **Watcher** (nền, 1 tiến trình/job): push input (nếu có input local) vào
   **kho dùng chung của host** (`<layout_root>/inputs/{key}/`) — mỗi (host,
   nguồn) **một lần**, có `--partial` để resume — rồi launch job trên host
   (`setsid nohup`), ghi meta.json.
10. Watcher theo dõi job (heartbeat, failover, pull artifact).
11. Trả JSON `{job, state, host, host_reasons, next}` — exit **0**.

**Vì sao bước 9–10 nằm ở watcher (B36/C10).** Trước đây `do` tự push input +
launch ⇒ mỗi item của lô `--each` tốn ~7 round-trip ssh (~21–26 s đo thật)
**tuần tự** ⇒ các job của lô **không bao giờ chạy chồng nhau** ⇒ fleet không
tính toán song song. Hoãn cho watcher ⇒ mỗi watcher tự đẩy + launch **song
song** ⇒ lô chạy chồng nhau, và `do` trả JSON **ngay**.

Ngoại lệ: nếu **không có watcher nào** sẽ khởi chạy job (chế độ
`ROFF_NO_WATCHER=1` — chỉ dùng cho test) thì `do` tự làm bước 9 **ngay** (để
job không bị bỏ rơi) và báo lỗi ngay như trước.

**Không bao giờ** chạy lệnh `--cmd` trên máy local. Nếu không còn host nào
dùng được ⇒ exit **10** với `error` liệt kê host nào bị loại và vì say, kèm
`jobs` = các job id **đã tạo được** trước đó (để agent không mất dấu).

## 17. Cấu hình qua biến môi trường

| Biến | Ý nghĩa |
|---|---|
| `ROFF_STATE_DIR` | thư mục state (mặc định `~/.local/state/roff`) |
| `ROFF_CONFIG` | đường dẫn config (mặc định `~/.config/roff/config.ini`) |
| `ROFF_SSH_CONFIG` | đường dẫn `ssh config` khác (mặc định `~/.ssh/config`) — mọi lệnh ssh sẽ dùng `-F` file này |
| `ROFF_CACHE_DIR` | thư mục cache local mà `roff gc` dọn (mặc định `<state>/cache`) |
| `ROFF_NICE` | nice level cho **mọi** tiến trình local của roff — ssh/rsync/tar/probe/tunnel (mặc định `19`) — **thắng** `[local] nice` trong config |
| `ROFF_IONICE` | ionice class cho **mọi** tiến trình local của roff (mặc định `idle`) — **thắng** `[local] ionice` trong config |
| `ROFF_REMOTE_NICE` | nice level cho **lệnh job chạy trên máy remote** (mặc định `19`) — **thắng** `[remote] nice` và `[host.<alias>] nice` |
| `ROFF_REMOTE_IONICE` | ionice class cho **lệnh job chạy trên máy remote** (mặc định `idle`) — **thắng** `[remote] ionice` và `[host.<alias>] ionice` |
| `ROFF_SLOT_WAIT_S` | số giây chờ xin slot khi `roff do` (mặc định `900`) — **giữ cho tương thích**: `roff do` hiện không chờ slot (timeout 0), nên biến này không còn tác dụng |
| `ROFF_PULL_RETRIES` | số lần **thử lại** kéo artifact khi pull hỏng (mặc định `3`) — reverse tunnel hay flap nên thử lại trước khi báo `pull_failed` (exit 74) |
| `ROFF_PULL_RETRY_DELAY_S` | giãn cách giữa 2 lần thử kéo artifact, giây (mặc định `5`) |
| `ROFF_NO_WATCHER=1` | không spawn watcher (chỉ dùng cho test) — khi đó `do` tự đẩy input + launch (đường dẫn nội tuyến, tuần tự) |
| `ROFF_LOCAL=1` | hook bỏ qua; **không** có tác dụng với CLI |

> Thư mục skill **không** cấu hình được: nó suy ra từ vị trí `roff.py` (`__file__`),
> nên cài skill ở đâu thì chạy đúng bản đó.

Biến của **hook** (không phải của CLI) — xem `skill/hooks/claude-code/README.md`:
`ROFF_LOCAL=1`, `ROFF_HOOK_OFF=1`, `ROFF_HOOK_OUTPUT=json`, `ROFF_HOOK_TOOLS=name1,name2`.

### 17.1 Chính sách `nice`/`ionice`

**Một** nơi quyết định (`roffkit.config`) — mọi module (`probe`, `sshx`,
`tunnel`, `job`, `transfer`) dùng chung ⇒ cùng env + cùng config ⇒ **cùng**
một kết quả.

Thứ tự ưu tiên: **biến môi trường > config > mặc định**.

| Nơi | Dùng cho | Mặc định | Biến môi trường |
|---|---|---|---|
| `[local] nice` / `[local] ionice` | tiến trình **local** của roff (ssh/rsync/tar/probe/tunnel) | `19` / `idle` | `ROFF_NICE` / `ROFF_IONICE` |
| `[remote] nice` / `[remote] ionice` | **lệnh của người dùng chạy trên máy remote** | `19` / `idle` | `ROFF_REMOTE_NICE` / `ROFF_REMOTE_IONICE` |
| `[host.<alias>] nice` / `ionice` | ghi đè `[remote]` cho **riêng host** đó | (không ghi đè) | (như trên) |

Giá trị `ionice` nhận: `idle` (→ `ionice -c3`), `best-effort` (→ `-c2`),
`realtime` (→ `-c1`), hoặc số `0`–`3`.

Quy tắc biên:

* `nice = 0` ⇒ **không** bọc `nice` (người dùng muốn job ưu tiên cao);
* `nice` âm ⇒ coi như `0` (nâng ưu tiên cần quyền đặc biệt — roff không tự
  làm việc đó); `nice > 19` ⇒ coi như `19`;
* `ionice` rỗng hoặc `none` ⇒ **không** bọc `ionice`;
* giá trị rác trong biến môi trường ⇒ **bỏ qua** (đọc tiếp config);
* config **hỏng / không đọc được** ⇒ dùng **mặc định**, **không** làm chết
  tiến trình (cùng nguyên tắc `strict=False` của B27);
* `ionice = realtime` (`-c1`) **cần quyền** (`CAP_SYS_NICE` / root) — đo thật
  24/11/2026 trên máy chạy unit test: không có quyền thì `ionice` fail với
  *"Operation not permitted"* ⇒ **job không chạy**. Với `idle` (mặc định) thì
  mọi user đều được; trên fleet Muse (đăng nhập **root**) `realtime` dùng
  được, nhưng nếu host đăng nhập bằng user thường thì **đừng** đặt
  `realtime`.

Trên máy remote, `nice`/`ionice` được kiểm tra **trên host** (`command -v`)
trước khi bọc ⇒ host không có thì job chạy bình thường.

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
