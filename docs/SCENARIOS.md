# Kịch bản nghiệm thu (S1–S22) và test hồi quy (R1–R20)

> Mọi kịch bản chạy trên fleet thật: `muse-pmb333`, `muse-tampt`, `muse-bienpm333`,
> `muse-phamminhbien3333`, `qwen1-direct`.
> Người dùng bật lại tunnel và restart máy thật khi được yêu cầu (Q26).

## 0. Quy ước đo

### 0.1 Đo tài nguyên máy local

Mọi kịch bản đều chạy một **bộ đo** đi kèm:

```bash
# tạo: /tmp/roff-probe-local.sh — lấy mẫu mỗi 0,5 s
# - CPU% của các tiến trình roff/ssh/rsync (từ /proc/<pid>/stat)
# - RSS của chúng
# - %iowait và %idle từ /proc/stat
```

Đạt khi:
- **CPU**: tổng CPU% của (roff + ssh + rsync) **< 2% một core** trong suốt lúc job chạy remote
- **RAM**: tổng RSS của chúng **< 100 MB**
- **Mượt**: một lệnh benchmark tương tác chạy song song chậm thêm **≤ 20%**
  (đo: `bash -c 'for i in $(seq 200); do :; done'` × 20 lần, lấy trung vị; so sánh có/không roff)
- **Không sót**: sau khi kịch bản xong, `pgrep -f 'roff|ssh.*muse'` không còn tiến trình
  nào do roff tạo (trừ tunnel được mở có chủ đích)

### 0.2 Đo tăng tốc song song

- **Tăng tốc tính toán** = (thời gian encode trên 1 máy) / (thời gian encode trên N máy),
  **không tính** thời gian push/pull. Đạt: **≥ 0,6 × N**.
- **Tăng tốc toàn cục** = (thời gian từ lúc gọi `roff do` tới lúc có artifact cuối, trên 1 máy) /
  (cùng đo trên N máy). Đạt: **≥ 1,3×** với N=2, **≥ 1,8×** với N=4.

Hai ngưỡng này tách bạch: ngưỡng toàn cục thấp hơn nhiều vì băng thông qua reverse tunnel
chỉ ~1 MB/s (đo thật) — đây là giới hạn vật lý, không phải lỗi thiết kế.

### 0.3 Bằng chứng bắt buộc

Mỗi kịch bản phải lưu: exit code của mọi lệnh roff, JSON đầy đủ, `sha256sum` artifact,
ảnh chụp màn hình terminal nếu có, và log của bộ đo tài nguyên local.

---

## 1. Nhóm kích hoạt — skill phải tự bật

### S1 — Khác skill, khác dự án, phiên mới

**Bối cảnh**: phiên Claude Code mới, `cd` vào một dự án **không nằm trong repo này**, yêu cầu
"chạy test suite của dự án này".

**Cách chạy**:
1. `cd /tmp/roff-s1-proj` — dự án node tối giản với `vitest`, 12 test.
2. Hook `PreToolUse` được cài (`~/.claude/settings.json`).
3. Agent được yêu cầu chạy `npm test`.

**Đạt khi**:
- Hook chặn lệnh `npm test` và trả về hướng dẫn dùng `roff do`.
- Agent chuyển sang `roff do --cmd 'npm test'` và hoàn thành.
- **Không có** tiến trình vitest nào chạy trên máy local.
- Log bộ đo: CPU roff < 2%, RAM < 100 MB.
- Đây là kịch bản quan trọng nhất — nó chứng minh tính **tổng quát**.

**Bằng chứng**: transcript hook, `ps` không có vitest local, artifact test report về local.

---

### S2 — Thiếu tool → tự cài

**Bối cảnh**: `muse-*` có `node` nhưng **không có `npm`**.

**Cách chạy**:
1. Job cần `npm`: `roff do --cmd 'npm test' --need npm`.
2. Chọn một máy Muse.

**Đạt khi**:
- Scheduler **không** chọn máy thiếu `npm` ngay từ đầu mà không có setup script; nó cài trước.
- `ensure` chạy script `setups/npm.sh`, idempotent: lần 2 báo `skipped: true`.
- Job chạy được sau khi cài, exit 0, output test về local.
- Nếu chạy lại `ensure` sau khi máy restart → `skipped: false` (cài lại).

**Bằng chứng**: JSON `ensure` có `ensured`/`skipped`, log setup, sha256 output.

---

## 2. Nhóm dữ liệu

### S3 — ffmpeg 500 MB

**Bối cảnh**: file video ~500 MB ở local.

**Cách chạy**: `roff do --cmd 'ffmpeg -i "$ROFF_IN" -vf scale=1280:-2 -c:v libx264 "$ROFF_OUT/out.mp4"' --in ./movie.mp4 --out ./out --need ffmpeg`

**Đạt khi**:
- Artifact về local, có sha256 khớp với sha256 tính trên remote.
- `ffprobe` local xác nhận 1280×720, đúng duration.
- Đạt ngưỡng tài nguyên local 0.1.
- Nếu file đã nén (mp4) thì **không** nén lại khi truyền.

### S4 — Input là URL, máy không có internet

**Bối cảnh**: `muse-tampt` **không** có internet (curl github timeout 10 s).
Input là URL công khai.

**Cách chạy**: `roff do --cmd 'curl -sSL "$ROFF_URL" -o "$ROFF_OUT/data.bin"' --in http://<url>`

**Đạt khi**:
- Scheduler **ưu tiên** máy có internet (`muse-pmb333`) — lý do chọn host in ra phải nói rõ.
- Ép chạy trên `muse-tampt` (`--host muse-tampt`) → phải tải qua local rồi đẩy lên,
  và báo cáo phải nói rõ đã đi vòng qua local (không giấu).

### S5 — RAM không đủ

**Bối cảnh**: cả 3 máy Muse còn ~450–900 MB trống.

**Cách chạy**: `roff do --cmd 'python3 -c "x=bytearray(4*1024**3)"' --mem 6G`

**Đạt khi**:
- Không host nào đạt → exit **10** với `reason` nói rõ ("no host with ≥ 6144 MB available;
  best is 842 MB on muse-tampt").
- **Tuyệt đối không** chạy lệnh trên máy local.
- Với `--mem 600M` → phải chạy được, và nếu bị OOM thì exit 69 với `reason=oom_killed`,
  failover sang máy khác nếu còn retry.

---

## 3. Nhóm lỗi hạ tầng — phần khó nhất

### S6 — Đứt lúc push

**Cách chạy**: push 1 GB, ngang giữa chừng **ngắt tunnel** (kill tiến trình client ssh phía host,
hoặc chặn port), rồi để watcher nối lại.

**Đạt khi**:
- Transfer **resume**, không chuyển lại từ đầu. Đo: số byte thực sự truyền lần 2 < 50% tổng.
- Dùng `rsync --partial` để giữ phần đã tải.

### S7 — Đứt tunnel 3 phút, máy vẫn sống (KHÔNG failover)

**Bối cảnh**: đây là kịch bản dễ làm sai nhất. Mất kết nối **không** đồng nghĩa job chết.

**Cách chạy**: job đang chạy 10 phút → ngắt tunnel 3 phút → để nguyên.

**Đạt khi**:
- Watcher **không** failover, **không** báo lỗi cho agent.
- Khi tunnel sống lại: `boot_id` và `instance_token` **không đổi** → job vẫn sống.
- Job chạy tiếp tới khi xong, exit 0, artifact về đủ.
- Ledger ghi lại một mục `disconnected` (thời điểm, số giây) để agent đọc được.

### S8 — RESTART THẬT giữa lúc chạy

**Cách chạy**: người dùng **bấm restart máy Muse** (Q26) khi job đang chạy.

**Đạt khi**:
- `boot_id` đổi → watcher biết máy restart.
- Exit **69**, `reason=host_restarted`.
- Nếu job cho phép retry (mặc định `--retry 1`) → failover sang máy khác, chạy lại tới khi xong.
- Nếu có `--checkpoint` → job mới nhận `$ROFF_RESUME=1` và có checkpoint được khôi phục.
- Nếu job có `--side-effects` → **không** retry, báo rõ cho agent.

### S9 — `--side-effects` không retry

**Đạt khi**: host chết → exit 69, `reason` nói rõ "not retried: job declared side effects",
không có job thứ hai chạy.

---

## 4. Nhóm vòng đời

### S10 — Kill agent giữa lúc chạy

**Cách chạy**: `roff do` → vài giây sau kill phiên Claude (hoặc `kill -9` tiến trình agent).

**Đạt khi**:
- Watcher tiếp tục chạy, job vẫn chạy trên remote.
- Phiên mới: `roff jobs` thấy job đang chạy; `roff wait <id>` trả kết quả cuối.
- Không có job nào bị chạy lại trùng.

### S11 — `--in job:` + `--shards`

**Cách chạy**: job A sinh ra nhiều file → job B dùng `--in job:<A>` → `--shards 3`.

**Đạt khi**:
- Dữ liệu **không ghi xuống đĩa local** (kiểm tra: `du` thư mục local trước/sau không đổi).
- Ba shard chạy trên ≥ 2 máy khác nhau.
- `$ROFF_SHARD` / `$ROFF_SHARDS` đúng trong mỗi shard.

---

## 5. Nhóm dịch vụ và không-lùi

### S12 — BE + FE qua tunnel

**Đạt khi**: gọi được qua `http://127.0.0.1:<L>`; `tunnel stop` xong **không còn** tiến trình
ssh nào của roff; không kill nhầm tiến trình khác của người dùng.

### S13 — Mọi host đều chết

**Cách chạy**: chặn port 2222/2225/2226/2227 + `qwen1-direct`.

**Đạt khi**:
- Exit **10** (không còn host) hoặc **255** — tuỳ vào mức lỗi — với JSON nói rõ host nào chết và vì sao.
- **Không có** tiến trình nặng nào chạy trên máy local.
- Hook vẫn chặn lệnh nặng (không "thấy host chết thì cho qua").

---

## 6. Nhóm an toàn

### S14 — Secret không lộ

**Cách chạy**: `roff do --cmd './app --token "$MY_SECRET"' --env MY_SECRET=s3cr3t-value-xyz`

**Đạt khi**:
- `ps` trên host (chạy song song lúc job) **không** chứa `s3cr3t-value-xyz`.
- Audit log local **không** chứa giá trị đó.
- File env trên host có mode 600.

### S15 — Master ssh cũ giữ port

**Cách chạy**: để lại một ssh master cũ giữ port 18000, rồi `tunnel start` cùng port.

**Đạt khi**:
- roff phát hiện master cũ (qua `ssh -O check` và `ss -ltnp`), **chỉ** huỷ forward của chính nó
  (`-O cancel`), **không** kill tiến trình ssh khác của người dùng.
- Tunnel hoạt động.

---

## 7. Nhóm harness và token

### S16 — Chạy bằng codex

**Cách chạy**: dùng `codex` CLI với cùng SKILL.md (đọc từ `~/.agents/skills/remote-offload`).

**Đạt khi**: job chạy xong, artifact về, không cần hook Claude Code.
Chứng minh lõi **không phụ thuộc harness**.

### S17 — Đo token của context chính

**Cách chạy**: phiên Claude Code thật chạy S2, đo số tool call liên quan tới roff.

**Đạt khi**:
- Context chính chỉ nhận **kết quả cuối** (JSON), không nhận log thô.
- **≤ 3 tool call** liên quan tới roff.
- Nếu log dài (> 200 dòng) thì SKILL.md phải hướng dẫn giao việc đọc log cho **subagent**
  (Q6 — gợi ý, không gò bó).

---

## 8. Nhóm song song (yêu cầu chính của người dùng)

### S18 — 20 video trên cả fleet

**Cách chạy**: `roff do --each './vids/*.mp4' --cmd 'ffmpeg -i "$ROFF_ITEM" …' --out ./out`

**Đạt khi**:
- Tăng tốc **tính toán** ≥ 0,6 × N.
- Tăng tốc **toàn cục** ≥ 1,3× (N=2), ≥ 1,8× (N=4).
- Mọi output có sha256 đúng (so với encode chuẩn 1 máy).
- Không có artifact trùng lặp do bản sao (Q36).

### S19 — 3 phiên agent cùng lúc

**Cách chạy**: mở 3 phiên Claude Code song song, mỗi phiên gửi một lô ffmpeg khác nhau.

**Đạt khi**:
- Không host nào bị nhồi quá số slot (`slots` trong config).
- Có **xếp hàng** thật sự (log phải cho thấy "queued at position N").
- Cả 3 lô đều xong.
- Không vi phạm trần 2 luồng truyền đồng thời.

### S20 — 1 máy chết giữa lô

**Cách chạy**: giữa lô S18, ngắt tunnel của một máy đang chạy job.

**Đạt khi**:
- **Chỉ** các item của máy đó được chia lại máy khác.
- Item đã xong **không** chạy lại (kiểm tra bằng mtime/sha256 + log không có job trùng).
- Lô vẫn hoàn thành đủ số output.

### S21 — 1 video dài, split → encode N host → concat

**Đạt khi**:
- Output phát được (ffprobe OK).
- Số frame và duration khớp bản gốc (sai số ±1 frame là chấp nhận được do keyframe).
- Nhanh hơn encode trên 1 máy (đo cả 2 phép đo như S18).

### S22 — Item chậm ở cuối lô

**Cách chạy**: làm chậm một máy (ví dụ `nice -n 19` một job trên đó, hoặc dùng `--cpus 1`).

**Đạt khi**:
- Item chậm **vẫn chạy xong**; lô hoàn tất đủ số output, không mất item nào.
- Các item nhanh không bị chặn bởi item chậm (mỗi host chạy `slots` job song song).
- Không có output trùng lặp trong thư mục đích.

> **Chưa triển khai**: bản sao (replica) của item chậm trên máy rảnh (Q36). Thiết kế
> cũ hứa "watcher chạy bản sao, bản nào xong trước thì lấy". Bản sao làm tăng tải
> băng thông (mỗi bản sao cần input riêng trên máy khác) — trên fleet có 0,5–1 MB/s
> điều đó đắt hơn giá trị nhận được. Đã bỏ khỏi phạm vi; xem `references/fleet.md`.

---

## 9. Nhóm dữ liệu & failover (bổ sung sau đợt nghiệm thu 23/11/2026)

### S23 — Input đẩy **một lần cho mỗi host**

**Bối cảnh**: `--each` với N item trên H host, mỗi item ~5 MB.

**Cách chạy**: `roff do --each '<5 file *.bin>' --out ./out --cmd 'cp "$ROFF_ITEM" "$ROFF_OUT/"'`.

**Đạt khi**:
- Trên **mỗi** host, thư mục input dùng chung (`<layout_root>/inputs/`) có **đúng 1 bản
  copy** của input, không phải N bản (kiểm bằng `du -sb` trên host + đếm job trên host).
- Tổng byte đẩy lên ≤ H × (kích thước input) × 1,05.
- Mọi output đúng nội dung item tương ứng (sha256 khớp).

### S24 — Failover giữ nguyên item & thư mục làm việc

**Bối cảnh**: job `--each` đang chạy trên host A; A bị wipe (xoá `$HOME`) hoặc restart.

**Cách chạy**: xoá `$HOME/.roff-instance-token` trên A (tương đương host bị wipe ⇒ token
đổi) trong lúc job đang chạy ⇒ watcher phải failover sang host B.

**Đạt khi**:
- Job **không** chết: có event `failover` trong log, `host` đổi từ A sang B.
- Thư mục làm việc trên B **vẫn giữ hậu tố** `part-N` (không được gộp các item của
  cùng lô vào một thư mục).
- `$ROFF_ITEM` trên B **tồn tại** và trỏ đúng item (job exit 0, output đúng sha256).
- Host mới được **ensure toolchain** trước khi launch (nếu host mới thiếu tool thì
  phải chạy setup script).

---

## 10. Nhóm độ bền của hàng đợi slot & dọn dẹp (bổ sung 23/11/2026, wave W3)

Cả 5 kịch bản này sinh ra từ **lỗi đã tái hiện được trên code**, không phải giả
định. Test: `tests/acceptance/test_acceptance_w3_resilience.py`.

### S25 — Slot rò (watcher chết) không kẹt host vĩnh viễn

**Bối cảnh**: `request_slot` ghi ticket **vĩnh viễn**, `try_acquire` chỉ cấp slot cho
**đầu hàng** ⇒ một ticket mồ côi ở đầu hàng làm **mọi** job sau của host đó kẹt
`queued` mãi mãi. Watcher chết (agent bị kill, máy local restart) là một đường tạo
ticket mồ côi.

**Cách chạy**: host `slots = 1` → job1 `launched` (giữ slot) → job2 `queued` →
huỷ job2 → **giết watcher của job1** → giết process group remote của job1 →
`roff do` lần 3.

**Đạt khi**:
- Job3 có `state == "launched"` (không phải `queued`) — slot rò phải được thu hồi.
- Job2 vẫn `queued` đúng như FIFO (không được âm thầm chạy).
- Job3 chạy xong exit 0 và có output.

### S26 — Job xếp hàng tự chạy khi slot trống

**Cách chạy**: host `slots = 1` → job1 (`sleep 25`) `launched` → job2 `queued`.

**Đạt khi**: cả hai job **cùng** về exit 0, output của job2 có mặt ở máy local.
`queued` không phải "trả exit 0 rồi quên" — watcher phải tự xin slot và launch.

### S27 — File state không đọc được của user khác

**Đo thật trên máy người dùng**: `audit.jsonl` / `probes.json` / `slots.json` bị tạo
với mode **0664** ⇒ mọi user trên máy đọc được log lệnh (có thể chứa đường dẫn dự án).

**Đạt khi**: mọi file trong thư mục state mode **0600**, mọi thư mục state **0700**.

### S28 — `roff gc` dọn kho input phía host, nhưng không dọn kho job đang chạy

**Bối cảnh**: kho input trên host (`<layout_root>/inputs/<key>/`) trước đây **không có
đường dọn** ⇒ phình vô hạn.

**Đạt khi**:
- `roff gc` **khi job đang chạy** ⇒ kho input của job đó **còn nguyên**.
- `roff gc` **sau khi job xong** ⇒ kho input đó **bị xoá**.

### S29 — Exit code phải phân biệt đúng nguyên nhân

**Đạt khi**:
- Input sai (path không tồn tại / `--timeout 0` / job id lạ) ⇒ **exit 2**.
- Config hỏng ⇒ **exit 3**.
- Host không với được ⇒ **exit 255** và **bắt buộc có `detail`** (lỗi ssh gốc).

Lý do: exit 255 cho một việc chỉ cần sửa ở máy local là **nguy hiểm** — agent tưởng
máy remote chết rồi chạy lại ở local ⇒ đúng cái điều skill sinh ra để tránh.

### S30 — Job được giao cho máy **có** đúng công cụ

Yêu cầu của người dùng: *"task sẽ phù hợp hơn với công việc mà máy kia có"*.

**Đạt khi**:
- `--need <tool không có ở đâu cả, không có script setup>` ⇒ **mọi** host bị loại
  **với lý do nói rõ** ("thiếu tool …"), exit **10** — không phải 255, không
  phải chạy local.
- `--need ffmpeg` ⇒ host được chọn **thật sự** có `ffmpeg`, kiểm bằng
  `command -v ffmpeg` trên chính máy đó (không tin lời khai của probe).

---

## 11. Nhóm lỗi *âm thầm* (bổ sung 23/11/2026, review wave W3)

Hai kịch bản dưới đây không sinh ra từ lỗi lộ trên fleet, mà từ **review code**.
Cả hai đều thuộc loại nguy hiểm nhất: **không báo lỗi, chỉ làm hỏng việc**.

### S31 — `ROFF_SSH_CONFIG` phải tới tay `ssh` thật

`cli.py` tài liệu hoá `ROFF_SSH_CONFIG` là *"đường dẫn `~/.ssh/config` thay thế"*.
Nhưng trước đó: `fleet.parse_ssh_config()` **có** dùng nó, còn `sshx.ssh_base()`
**không** truyền `-F` cho ssh ⇒ fleet được liệt kê theo config thay thế, lệnh
ssh thật vẫn đọc `~/.ssh/config`.

**Đạt khi**:
- Dựng config thay thế: alias `roff-s31-check` → `127.0.0.1:59999` (cổng đóng).
- `ROFF_SSH_CONFIG=<file> roff probe roff-s31-check` ⇒ lỗi phải là
  **connection refused** (đã đọc config thay thế), **không** phải
  *"could not resolve hostname"* (bỏ qua config thay thế).
- Mốc đối chiếu: **không** có `ROFF_SSH_CONFIG` ⇒ alias lạ phải là lỗi
  "không phân giải được tên".

Vì sao nguy hiểm: alias trùng tên giữa 2 config ⇒ roff lên kế hoạch trên máy A
nhưng lệnh chạy trên máy B — sai máy, không báo lỗi.

### S32 — Ledger hỏng thì **không** được dọn hàng đợi slot

`reconcile_slots` dọn ticket mồ côi theo "owner còn sống không". Nếu ledger job
**đọc lỗi**, danh sách đó phải là *"không biết"* (`None`), **không phải**
*"không ai còn sống"* (`set()` rỗng).

**Đạt khi**:
- Hàng đợi có 1 ticket (`owner` nào đó), thư mục `jobs/` bị `chmod 000` ⇒
  `list_jobs()` lỗi.
- `reconcile_slots(...)` ⇒ ticket **vẫn còn** trong hàng đợi, `removed` không
  chứa nó với reason `owner_dead`.

Vì sao nguy hiểm: nếu dọn oan thì **mọi** job đang xếp hàng chết âm thầm — không
ai báo lỗi, agent chỉ thấy job "đợi mãi không chạy".

---

# Test hồi quy (R1–R20)

Chạy bằng `pytest`, không cần mạng thật (dùng ssh giả) — trừ R1/R6/R7/R10–R13 chạy trên host thật.

| # | Test | Kỳ vọng |
|---|---|---|
| R1 | `~` trong path | path resolve thành absolute `$HOME`, không có literal `~` |
| R2 | bridge một phiên | 2 watcher 1 host → xếp hàng, không lỗi 429 |
| R3 | master cũ giữ port | detect + `-O cancel`, không kill pid không liên quan |
| R4 | chown trên durable mount | transfer dùng `--no-owner --no-group`, không lỗi |
| R5 | cgroup memory | đọc `memory.max`/`memory.current`; null khi không có |
| R6 | flap alias | 2 alias khác `instance_token` → cảnh báo, dùng alias ổn định |
| R7 | secret trong argv | `ps` remote không thấy giá trị `--env` |
| R8 | secret trong audit log | audit log không chứa giá trị env |
| R9 | workdir thiếu | exit 97 khi chưa push project |
| R10 | host restart | exit 69, `reason=host_restarted` |
| R11 | host wipe | exit 69, `reason=host_wiped` |
| R12 | OOM | exit 69, `reason=oom_killed` |
| R13 | tunnel chết, máy sống | **không** failover; job chạy tiếp |
| R14 | `Include` trong ssh config | alias trong file include được liệt kê |
| R15 | option keyword bị nhầm host | không có host giả trong output |
| R16 | không có rsync | fallback tar-over-ssh hoạt động |
| R17 | `--timeout 0` | bị từ chối (exit 2), không treo |
| R18 | kill tiến trình `wait` | job vẫn sống, ledger còn |
| R19 | cancel | exit 130, process group chết |
| R20 | job tự fail | exit 1, **không** retry (không có job thứ 2) |

## Chạy

```bash
# test hồi quy, không cần fleet
pytest tests/regression -q

# test hồi thu, cần fleet thật — tự bỏ qua nếu host không sống
pytest tests/acceptance -q -m acceptance
```

Marker `acceptance` = cần host thật. Test tự `skip` với lý do rõ ràng nếu host không sống,
**không** được báo pass khi bỏ qua.
