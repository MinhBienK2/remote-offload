# Báo cáo nghiệm thu — `remote-offload` / `roff`

Ngày đo: 23/11/2026 · Fleet: 4 máy `muse-*` (4 CPU, `/home/hatch` bền vững 150 GB)

Báo cáo này là **hợp đồng nghiệm thu**: mỗi kịch bản S1–S24 có trạng thái
(đạt / chưa đạt / không làm), bằng chứng, và — quan trọng nhất — **những gì
đã hỏng thật trên fleet thật** và cách đã sửa.

> Vai trò của người viết báo cáo này: **nghiệm thu + thiết kế kịch bản**, không
> viết code. Phần code do subagent triển khai, người nghiệm thu đặt hợp đồng và
> duyệt lại.

---

## 1. Môi trường đo

| | Máy local | Máy Muse (×4) |
|---|---|---|
| CPU | 12 core | 4 core |
| RAM | 15,7 GB tổng / **6,6 GB khả dụng** | 15,7 GB tổng / **419–849 MB trống** (đo lúc 13:39) · 622–686 MB (đo trực tiếp lúc 13:52) — RAM dao động mạnh vì máy đang chạy postgres/telegram-bot/cloudflared của chính người dùng |
| Disk | `/` 159 GB, **đã dùng 92 %** (13 GB trống) | `/home/hatch` 150 GB bền vững, dùng 3–8 % |
| OS | Linux 6.8, Python 3.10.12 | container cùng runtime (`hostname=htch-runtime`) |
| Đường lên | — | **0,5–1 MB/s** (đo thật, xem §3) |

Hai điều kiện này **lật ngược** chiến lược:

1. **Băng thông, không phải CPU, là nút thắt.** CPU của cả 4 máy Muse đều dư;
   đường truyền local → host thì yếu. ⇒ thiết kế bám băng thông: **input đẩy
   một lần cho mỗi host**, dùng chung giữa các job trên host đó.
2. **Host không rảnh.** 593–1103 MB trống, và các máy đó đang chạy
   postgres/telegram-bot/cloudflared của chính người dùng. ⇒ `mem_reserve_mb`
   chừa chỗ cho dịch vụ production; `slots = 1` mặc định.

Host ↔ host **không nối được** (bridge chết ở bước SSH banner) ⇒ không có
tính năng "gửi dữ liệu giữa 2 máy qua đường trực tiếp"; mọi chuyển dữ liệu đi
qua máy local.

---

## 2. Nguyên tắc nghiệm thu

- **Không bao giờ** chấp nhận "chạy local" là kết quả. Mọi kịch bản nặng phải
  chứng minh lệnh **không** chạy trên máy người dùng.
- Fleet **flap** liên tục (tunnel rớt giữa chừng). Kịch bản nào đụng mạng thật
  phải **kiểm tra liveness ngay trước lúc dùng** và **thử lại trên máy khác**
  khi lỗi là do host chết — nếu không, ta đo được độ ổn định của đường truyền
  chứ không phải chất lượng của `roff`.
- Mọi khẳng định trong báo cáo phải **tái lập được**: kèm lệnh đã chạy.

---

## 3. Số đo băng thông

Đo bằng `dd` qua `ssh` tới `/home/hatch`, nhiều lần, lấy trung bình:

| Hướng | Kết quả |
|---|---|
| local → Muse | **0,5–1 MB/s** (đỉnh 5,5 MB/s, không ổn định) |
| Muse → local | tương tự |
| Muse ↔ Muse | **không nối** (timeout ở SSH banner) |

Hệ quả trực tiếp: một dataset 2 GB đẩy lên 4 host = 4 lần truyền ≈ **40–70
phút**. Đẩy 4 bản (mỗi job 1 bản) = **4 lần nữa**. Vì vậy kho input dùng chung
theo host là bắt buộc, không phải tối ưu hoá.

---

## 4. Kết quả nghiệm thu

(điền sau đợt chạy cuối)

---

## 5. Lỗi thật đã phát hiện trên fleet

| # | Lỗi | Triệu chứng | Nguyên nhân | Cách sửa | Đã kiểm chứng |
|---|---|---|---|---|---|
| B1 | `unknown_host` cho host **đang sống** | `roff do --host muse-pmb333` → `unknown_host`, trong khi host sống và có trong `~/.ssh/config` | cache probe có entry hỏng ⇒ `Profile.from_dict({})` ⇒ `alias=""` ⇒ không khớp alias | cache phải **tự chữa**: `profile.alias != alias` ⇒ probe lại; `--host` luôn probe mới | **trên fleet thật**: ghi cache hỏng (`"profile": {}`) rồi `roff do --host muse-pmb333` ⇒ exit 0, `state:"launched"`, job chạy xong exit 0 |
| B2 | `roff do` chết khi fleet nhỏ hơn lô | `--each` 4 item, 2 host chết ⇒ exit 10, **2 job đã tạo bị bỏ rơi** (agent không biết job id) | vòng lặp loại host đã dùng ⇒ host thứ 3 chết ⇒ `select()` trả None ⇒ cả lệnh chết | host dùng được < số item ⇒ item thừa dồn vào host tải thấp nhất; payload lỗi phải kèm `jobs` đã tạo | unit test `test_w2d_fleet_spread.py` (6 case) |
| B3 | `roff do` treo 15 phút khi hết slot | chờ slot tới `ROFF_SLOT_WAIT_S=900` rồi exit 10, **không tạo job nào** | `ledger.acquire()` chặn trong `cli._cmd_do` | `roff do` xin slot với timeout **0**; hết slot ⇒ job `queued`, exit 0, watcher tự xin slot sau | unit test `test_w2b_slot_queue.py` (17 case) |
| B4 | `unknown_host` che mất host | cache probe 60 s ⇒ host vừa chết vẫn "thấy" | TTL cache không kiểm tra alias | `--host` ⇒ probe mới, bỏ qua cache | unit test `test_w2a_probe_cache.py` (14 case) |
| B5 | Input bị nhân bản theo job | mỗi job 1 bản copy input ⇒ băng thông × N | push input vào workdir từng job | kho dùng chung `<layout_root>/inputs/<key>/`, read-only, 1 bản/host | nghiệm thu S23 |
| B6 | Item của lô gộp chung thư mục khi failover | 2 item của cùng lô chạy chung 1 thư mục trên host mới ⇒ đè lên nhau | đổi host ⇒ mất hậu tố `part-N` | giữ hậu tố `part-N`/`shard-M` khi remap workdir | nghiệm thu S24 |
| B7 | Failover không dựng lại input `git:`/`http:` | job failover chạy với `$ROFF_IN` trỏ tới path không tồn tại | URL không được ghi vào ledger | ledger giữ `input_specs`; watcher dựng lại trên host mới | unit test `test_w2c_failover_inputs.py` (9 case) |
| B8 | `roff cancel` ghi đè trạng thái terminal | job đã `exited` bị đổi thành `cancelled` | cancel không kiểm tra state trước | cancel chỉ tác động job chưa terminal | unit test (case trong `test_w2b_slot_queue.py`) |
| B9 | `unknown_host` phân biệt sai | host có trong `~/.ssh/config` nhưng ngoài include-glob ⇒ báo "không có host nào tên…" | `--host` chỉ tìm trong fleet đã lọc | `--host` probe trực tiếp alias đó | unit test `test_probe_forced_host_unknown_alias_is_unknown_host` |
| B10 | State file đọc được của mọi user | `audit.jsonl` mode 0664 | tạo file không chmod | mọi file state ⇒ 0600, thư mục ⇒ 0700 | **W3 — chưa làm** |
| B11 | Hook **lọt** lệnh nặng bọc trong shell | `bash -lc "pytest"` và `sh -c "npm test"` bị hook **cho qua** | (1) danh sách nặng khớp trên *tên lệnh đầu tiên* (`bash`); (2) cách tách lệnh cắt ở `&&` **bên trong** dấu nháy ⇒ phần còn lại mang theo dấu `"` nên không khớp | tách lệnh **tôn trọng dấu nháy**; phân loại **bên trong** script của `sh -c`; thêm 14 test hồi quy | unit test `tests/unit/test_pretooluse.py` (105 test, +14) |
| B12 | `same_physical_host` chỉ *cảnh báo*, không *loại* | 2 alias trỏ cùng 1 máy vật lý ⇒ 2 job chạy song song trên 1 máy | `policy.select` ghi vào `rejected` (map báo cáo) nhưng **không** loại khỏi `candidates`; vòng ghi chú còn chạy **sau** khi đã chọn winner | loại alias trùng máy **trước** khi chấm điểm (giữ 1 alias/nhóm) | **đo trên fleet**: 4 máy Muse có `boot_id` + `machine-id` **khác nhau** ⇒ chưa kích hoạt; đã giao **W6f** |
| B13 | Job **ma** trong ledger: `state` thiếu ⇒ mặc định `"queued"` | ledger local có **997** job, trong đó **951** là job của phiên bản **cũ** (không có khoá `state`); `Job.from_dict` lấy default của dataclass ⇒ `"queued"` | `Job.state` default = `"queued"`; `from_dict` không phân biệt "thiếu khoá" với "queued thật" | job không có khoá `state` ⇒ state = `"unknown"` (coi như terminal ⇒ **không** giữ slot, **không** ai đợi được); `roff gc` dọn job ledger cũ | **đo thật trên máy người dùng**: đếm file ledger ⇒ **951/997** thiếu khoá `state`; `list_jobs()` trả `{'queued': 959, 'exited': 37, 'launched': 1}` — 959 "queued" là ma. `roff wait 261121-115716-d414 --timeout 20` ⇒ **exit 124**, `state:"queued"`, **treo đúng 20,1 s** (mặc định 7200 s ⇒ treo 2 giờ). Đã giao **W5b** |
| B14 | **Nói chuyện với nhầm máy** khi `~/.ssh/config` đổi | đổi config (đổi port / thay máy) ⇒ `ssh` vẫn **tái dùng master cũ** ⇒ lệnh chạy trên máy **cũ**, trong khi probe đã thấy máy mới | `sshx.control_path(alias)` khoá theo **alias** ⇒ socket ControlMaster không đổi khi tham số ssh của alias đổi; `probes.json` cũng khoá theo alias | `control_path` phải **gắn fingerprint** tham số ssh đã resolve (HostName/Port/User/IdentityFile/ProxyCommand) ⇒ đổi config ⇒ socket mới ⇒ master mới; cache probe cũng phải vô hiệu khi fingerprint đổi | **đo thật (23/11/2026)**: config A trỏ `dup-test` → máy thật (`muse-pmb333`) ⇒ `hostname: htch-runtime` ✓; **đổi** config B trỏ `dup-test` → `127.0.0.1:9` (cổng chết) ⇒ ssh **vẫn thành công** (`TOI-TROI-TREN-MAY-KHAC`) vì tái dùng master cũ. Đã giao **W5c** |

### 5.1 Hook — đo thật, không phải giả định

Bảng quyết định của hook chạy trực tiếp với payload `PreToolUse` thật:

| Lệnh | Kết quả |
|---|---|
| `npm test`, `pytest -q`, `cargo build --release`, `python3 -m pytest`, `ffmpeg -i in.mp4 out.mp4` | **exit 2** (chặn) |
| `pytest tests/unit/test_cli.py::test_x -q` (có scope) | exit 0 (cho qua) |
| `ROFF_LOCAL=1 pytest -q` | exit 0 (lối thoát) |
| `roff do --cmd 'npm test' --in . --out ./out` | exit 0 — **hook không chặn chính nó** |
| `ls -la`, `git status` | exit 0 |
| `npm run dev` | exit 0 + gợi ý (cần tiến trình sống) |
| payload JSON hỏng | exit 0 (**fail-open**, không làm hỏng phiên agent) |
| `bash -lc "pytest"` | **exit 2** (sau khi sửa B11; trước đó là exit 0) |

### 5.2 Phối hợp giữa 2 máy — đã đo thật, chưa đưa vào CLI

Yêu cầu *"kết nối phối hợp nhau"* của người dùng. Host ↔ host không nối thẳng
(bridge chết ở bước SSH banner), nhưng đi vòng qua máy local thì được:

```bash
ssh -L 18517:127.0.0.1:8899 muse-bienpm333     # local :18517 → B:8899
ssh -R 9000:127.0.0.1:18517 muse-tampt         # A:9000  → local :18517
```

**Đo thật**: từ `muse-tampt`, `curl http://127.0.0.1:9000/` trả **HTTP 200** với
nội dung của `muse-bienpm333`. Cơ chế không phải giả định.

Chưa đưa vào CLI ⇒ xếp vào **W4** (`docs/DESIGN.md` §11.3), có đặc tả CLI và ràng
buộc an toàn.

---

## 6. Quyết định thiết kế đã chốt (và cái đã **không** làm)

| Câu hỏi | Quyết định | Lý do |
|---|---|---|
| Có bản sao (replica) cho item chậm ở cuối lô? | **Không** | bản sao cần input riêng trên máy khác; link 0,5–1 MB/s ⇒ đắt hơn giá trị nhận được |
| Có work-stealing (job hết host ⇒ host khác nhặt)? | **Không** | hàng đợi là hàng đợi **slot**; job ghim host và chờ slot. Đơn giản, dự đoán được, và tránh truyền dữ liệu |
| Input nén hay không? | Theo **loại nội dung chiếm ưu thế** | nén file đã nén là lãng phí CPU cả 2 đầu |
| `roff do` chờ slot không? | **Không** | chờ ⇒ agent bị treo 15 phút, và không có job nào được tạo |

---

## 7. Phần chưa làm / cần người dùng quyết định

| # | Việc | Vì sao chưa làm |
|---|---|---|
| C1 | ~~Cài hook `PreToolUse` + lệnh `roff` vào `~/.claude/settings.json` thật~~ | **đã làm** (23/11/2026): hook + `~/.local/bin/roff` đã cài; `settings.json` được sao lưu ở `.bak`; các hook khác của người dùng **không** bị đụng (đã so khớp JSON trước/sau). Gỡ: `python3 skill/hooks/claude-code/install.py --uninstall` |
| C2 | Dọn ~3,5 GB `.roff` trên Muse | **không dọn**: đó là **dữ liệu dự án thật** của người dùng (`x3dv50`, `refvideo`, …), không phải rác. Disk còn 146 GB trống ⇒ không cần |
| C3 | S8 — **restart thật** của một máy giữa lúc chạy | cần quyền restart máy Muse ⇒ thuộc về người dùng. **Phần phát hiện đã được chứng minh** bằng một restart **tự nhiên** xảy ra giữa lúc đo: `exit 69`, `reason:"host_restarted"`, đối chiếu `boot_id` (`1e0ef072…` → `d4f088b5…`) ⇒ phán quyết đúng. Còn lại: **chuyển sang máy khác** chưa được kích hoạt (config lúc đó chỉ có 1 host) |
| C4 | S16 — chạy bằng **codex** | backend codex đang trả 503 (`d1_migration_incomplete`) — lỗi hạ tầng phía họ, không phải lỗi skill |
| C5 | Push lên GitHub `MinhBienK2/remote-offload` | nhánh `rewrite-main` **chưa có commit nào**; cần bạn duyệt trước khi đẩy |
| C6 | Cài hook cho **Codex CLI** | adapter đã viết (`skill/hooks/codex/install.py`) và đã thử trên file tạm (thêm/bỏ sạch, không đụng hook Orca). **Chưa cài vào `~/.codex/hooks.json` thật**: (1) backend codex đang 503 nên không kiểm chứng end-to-end được; (2) file đó do Orca quản lý. Giao thức đã đọc thẳng từ binary codex 0.142.4 — cùng `tool_name`/`tool_input`, cùng exit 2 + stderr |
