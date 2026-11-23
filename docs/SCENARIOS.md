# Kịch bản nghiệm thu (S1–S47) và test hồi quy (R1–R27)

> Mọi kịch bản chạy trên fleet thật: `muse-pmb333`, `muse-tampt`, `muse-bienpm333`,
> `muse-phamminhbien3333`, `qwen1-direct`.
> Người dùng bật lại tunnel và restart máy thật khi được yêu cầu (Q26).

> **Quy ước đánh số.** Kịch bản **tự động** (có test trong
> `tests/acceptance/`) viết số 2 chữ số: `S02`, `S03`, … `S47` — cùng cách viết
> với bảng `docs/REPORT.md` §4. Kịch bản **biến thể** thêm hậu tố: `S02b`,
> `S13b`, `S14b`, `S31b`, `S33b`, `S33c`, `S34b`, `S34c`, `S35b`, `S35c`,
> `S37b`, `S39b`, `S47b` — mỗi biến thể là **một test riêng** và **một dòng
> riêng** trong bảng §4. Kịch bản **đo tay** (S1, S2, S8, S16, S17, S20, S21,
> S22) giữ dạng viết gốc.
> **`S44` không dùng** — mã bị bỏ trống, không phải kịch bản bị mất.

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
- **CPU**: CPU% **trung bình** của (roff + ssh + rsync) **< 2% một core** trong suốt lúc job chạy remote (đỉnh theo cửa sổ 0,5 s không làm ngưỡng: nó bắt cả khởi động interpreter của watcher — đo thật 23/11/2026: đỉnh 3,0% nhưng trung bình 0,06% và lệnh tương tác không chậm)
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
- Log bộ đo: CPU roff (trung bình) < 2% một core, RAM < 100 MB.
- Đây là kịch bản quan trọng nhất — nó chứng minh tính **tổng quát**.

**Bằng chứng**: transcript hook, `ps` không có vitest local, artifact test report về local.

---

### S02 — Vòng đời job trên host thật

**Bối cảnh**: một job đơn giản chạy trên một máy Muse thật.

**Đạt khi**:
- `roff do` trả JSON có `job`, `host`, `state` (`queued` khi hết slot,
  `launched` khi đã lên host) và exit 0.
- `roff wait <job>` trả `state: "exited"`, `exit: 0`, `pulled: true` ⇒ artifact
  **thật sự** nằm trong `--out` (không chỉ "job báo thành công").
- Job đóng gói: `sha256` của artifact local khớp `sha256` ghi trong ledger.

Vì sao nguy hiểm: đây là **hợp đồng cơ bản** — mọi kịch bản còn lại đo trên
nền này. Một job "thành công" mà artifact không về thì agent đọc thư mục rỗng
⇒ tin nhầm (xem B23).

### S02b — Job fail ⇒ exit 1

**Đạt khi**:
- Job cố ý hỏng (`exit 1` trên host) ⇒ `roff wait` trả **exit code 1** (không
  phải 0, không phải 69) và JSON nói rõ `state: "exited"`, `exit: 1`.
- Job **có** `--side-effects` thì **không** retry (xem S09).

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

### S03 — ffmpeg 500 MB

**Bối cảnh**: file video ~500 MB ở local.

**Cách chạy**: `roff do --cmd 'ffmpeg -i "$ROFF_IN" -vf scale=1280:-2 -c:v libx264 "$ROFF_OUT/out.mp4"' --in ./movie.mp4 --out ./out --need ffmpeg`

**Đạt khi**:
- Artifact về local, có sha256 khớp với sha256 tính trên remote.
- `ffprobe` local xác nhận 1280×720, đúng duration.
- Đạt ngưỡng tài nguyên local 0.1.
- Nếu file đã nén (mp4) thì **không** nén lại khi truyền.

### S04 — Input là URL, máy không có internet

**Bối cảnh**: `muse-tampt` **không** có internet (curl github timeout 10 s).
Input là URL công khai.

**Cách chạy**: `roff do --cmd 'curl -sSL "$ROFF_URL" -o "$ROFF_OUT/data.bin"' --in http://<url>`

**Đạt khi**:
- Scheduler **ưu tiên** máy có internet (`muse-pmb333`) — lý do chọn host in ra phải nói rõ.
- Ép chạy trên `muse-tampt` (`--host muse-tampt`) → phải tải qua local rồi đẩy lên,
  và báo cáo phải nói rõ đã đi vòng qua local (không giấu).

### S05 — RAM không đủ

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

### S06 — Đứt lúc push

**Cách chạy**: push 1 GB, ngang giữa chừng **ngắt tunnel** (kill tiến trình client ssh phía host,
hoặc chặn port), rồi để watcher nối lại.

**Đạt khi**:
- Transfer **resume**, không chuyển lại từ đầu. Đo: số byte thực sự truyền lần 2 < 50% tổng.
- Dùng `rsync --partial` để giữ phần đã tải.

### S07 — Đứt tunnel 3 phút, máy vẫn sống (KHÔNG failover)

**Bối cảnh**: đây là kịch bản dễ làm sai nhất. Mất kết nối **không** đồng nghĩa job chết.

**Cách chạy**: job đang chạy 10 phút → ngắt tunnel 3 phút → để nguyên.

**Đạt khi**:
- Watcher **không** failover, **không** báo lỗi cho agent.
- Khi tunnel sống lại: `boot_id` và `instance_token` **không đổi** → job vẫn sống.
- Job chạy tiếp tới khi xong, exit 0, artifact về đủ.
- Ledger ghi lại một mục `disconnected` (thời điểm, số giây) để agent đọc được.

> **Điều kiện tiên quyết (sửa 23/11/2026 — B28)**: phải **chờ job lên `launched`**
> rồi mới cắt kết nối. Job còn xếp hàng (hết slot) thì cửa sổ "mất kết nối"
> trôi qua **trước khi job chạy** ⇒ watcher không có gì để mất ⇒ không ghi
> `disconnected` ⇒ kịch bản fail **oan**. Bản nghiệm thu chờ tối đa 120 s và
> `skip` nếu job không lên `launched` (host quá tải / flap).

### S8 — RESTART THẬT giữa lúc chạy

**Cách chạy**: người dùng **bấm restart máy Muse** (Q26) khi job đang chạy.

**Đạt khi**:
- `boot_id` đổi → watcher biết máy restart.
- Exit **69**, `reason=host_restarted`.
- Nếu job cho phép retry (mặc định `--retry 1`) → failover sang máy khác, chạy lại tới khi xong.
- Nếu có `--checkpoint` → job mới nhận `$ROFF_RESUME=1` và có checkpoint được khôi phục.
- Nếu job có `--side-effects` → **không** retry, báo rõ cho agent.

### S09 — `--side-effects` không retry

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
- **Mỗi shard `exit == 0`** (sửa 23/11/2026 — B28): `roff wait` trả `rc=0` cả khi
  job **remote** exit khác 0 ⇒ chỉ kiểm `rc` là một shard chết âm thầm vẫn xanh
  rồi fail ở bước "đủ 3 file" — khó hiểu và dễ quy nhầm sang hạ tầng.

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

### S13b — Mọi host chết ⇒ **không** chạy local

**Đạt khi**: cùng bối cảnh S13, nhưng phải kiểm thêm rằng `roff` **không** tự
chạy lệnh ở máy local khi không còn host nào. Không được có tiến trình con nào
của lệnh đó trên máy local (kiểm bằng `ps` trước/sau).

Vì sao nguy hiểm: "không có host" là lúc dễ nhất để rơi vào bẫy "chạy tại chỗ
cho xong" — đúng cái điều skill sinh ra để tránh.

---

## 6. Nhóm an toàn

### S14 — Secret không lộ

**Cách chạy**: `roff do --cmd './app --token "$MY_SECRET"' --env MY_SECRET=s3cr3t-value-xyz`

**Đạt khi**:
- `ps` trên host (chạy song song lúc job) **không** chứa `s3cr3t-value-xyz`.
- Audit log local **không** chứa giá trị đó.
- File env trên host có mode 600.

### S14b — Secret không nằm trong **argv** trên remote

**Đạt khi**: cùng bối cảnh S14, nhưng phải kiểm thêm: giá trị secret **không**
xuất hiện trong `/proc/<pid>/cmdline` của tiến trình trên host (chỉ nằm trong
file env mode 600).

Vì sao nguy hiểm: `--cmd` được lưu **nguyên văn** trong audit log local ⇒ secret
trong `--cmd` là secret **đã rò** (về máy local). Mà `--env` thì không.

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

> **Cái gì kiểm chứng được ở đây là phần TÍNH TOÁN, không phải wall-clock.**
> Lý do (đo thật 23/11/2026): đường lên local → host chỉ **0,5–1 MB/s**
> (`docs/REPORT.md` §3). Video 1280x720/6 s = **5,6 MB** ⇒ **đẩy** ~25 s, còn
> **chạy** chỉ ~17 s ⇒ các job **không bao giờ chạy chồng nhau** (đo thật: job 1
> chạy 19:33:47–19:34:04, job 2 chạy 19:34:15–19:34:33 — **không giao nhau**)
> ⇒ tăng tốc wall-clock của lô **không** phản ánh scheduler, mà phản ánh
> đường lên. ⇒ Kịch bản dùng **input nhỏ** (~200 KB ⇒ đẩy ~0,3 s) + **lệnh
> nặng** (lặp 6 lần encode) để đo được phần tính toán có phân tán hay không.

**Đạt khi**:
- Phía **song song** chạy trên **≥ 2 máy khác nhau** (nếu fleet hẹp lúc đo thì
  **không** kiểm chứng được ⇒ bỏ qua, không báo đỏ).
- Các job **chạy chồng nhau**: cửa sổ chạy remote của chúng có giao nhau.
- Tăng tốc **tính toán** ≥ **0,6 × N** (tổng thời gian chạy remote ÷ thời gian
  của đoạn chạy chồng nhau).
- Tăng tốc **wall-clock** vẫn được **đo và in ra** để đọc (không khẳng định —
> xem lý do ở trên).
- Mọi output có sha256 đúng (so với encode chuẩn 1 máy).
- Không có artifact trùng lặp do bản sao (Q36).

> **"Không chạy chồng nhau" là lỗi SẢN PHẨM hay HẠ TẦNG?** (B37, 23/11/2026 —
> bằng chứng: `docs/evidence/final/s18-submit-latency.txt`). Phân biệt bằng
> **mốc gửi** (lúc `do` tạo job), đọc từ ledger:
> - các job được gửi **lệch nhau > 10 s** ⇒ `roff do --each` gửi **tuần tự**
>   ⇒ lỗi **sản phẩm** (B36) ⇒ **FAIL**, **không** được `skip` — nếu không,
>   lỗi sản phẩm sẽ bị báo xanh mỗi đợt nghiệm thu;
> - các job được gửi **cùng lúc** mà vẫn không chạy chồng nhau ⇒ fleet **hẹp**
>   / host chết giữa lô ⇒ **hạ tầng** ⇒ `skip` (kèm lý do đúng).
>
> Đợt nghiệm thu 23/11/2026 22:24 rơi vào ca **thứ nhất**: các job gửi lúc
> 22:51:01 / 22:52:43 / 22:53:11 (lệch 102 s rồi 28 s) ⇒ **FAIL**. Lý do skip
> mà kịch bản cũ ghi ("đường lên 0,5–1 MB/s là nút thắt") **sai**: input chỉ
> 196 KB ⇒ đẩy mất ~0,3 s.

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

> **Đã đo một phần — 23/11/2026.** Phần "**một job** đang chạy thì máy chết" xảy
> ra **tự nhiên** giữa lúc nghiệm thu: job `261123-153312-4c46` chạy trên
> `muse-pmb333` (attempt 0, `boot_id fe60056a…`) ⇒ tunnel rớt (6 sự kiện
> `disconnected`) ⇒ `roff` phát hiện `host_restarted` ⇒ **failover** sang
> `muse-bienpm333` (attempt 1, `boot_id 9fd59921…`, `pid` mới) ⇒ job **chạy tiếp**
> (`state: running`), không mất. Bằng chứng đầy đủ:
> `docs/evidence/failover-live-20261123/`.
>
> **Còn chưa đo**: phần "**lô** nhiều item" (chia lại item của máy chết, không
> chạy lại item đã xong) — cần cố ý ngắt tunnel giữa lô S18, tức là cần quyền
> can thiệp vào tunnel của máy Muse (thuộc người dùng).

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

### S31b — Mốc đối chiếu

**Đạt khi**: (mốc đối chiếu của S31, chạy **trước** để chắc là lỗi do config
thay thế chứ không phải do môi trường) — cùng lệnh probe, **không** có
`ROFF_SSH_CONFIG` ⇒ alias `roff-s31-check` phải báo *"không phân giải được tên"*,
**không** phải *"connection refused"*.

Vì sao nguy hiểm: nếu cả hai nhánh đều ra cùng một lỗi thì S31 không chứng minh
được gì — kịch bản phải **phân biệt được** 2 nguyên nhân.

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

## 12. Nhóm phối hợp 2 máy & chẩn đoán (bổ sung 23/11/2026, wave W4/W5)

Các kịch bản này **cần fleet thật** (S33) hoặc chạy tức thì (S34/S35).
Test tự `skip` nếu không đủ máy — không được báo xanh khi không kiểm chứng được.

### S33 — `roff tunnel link` nối 2 máy thật

Backend ở máy B, consumer ở máy A. `roff tunnel link --from A --to B --rport P
--to-port Q` ⇒ mở 2 nhánh: `ssh -N -L <via>:Q B` và `ssh -N -R P:127.0.0.1:<via> A`.

**Đạt khi**:
- JSON có đủ khoá: `from`, `to`, `rport`, `to_port`, `via_lport` (trong dải
  18500–18599), `a_listening`, `b_listening` — cả hai phải `true`.
- **Link thật sự thông**: từ A, `curl http://127.0.0.1:<rport>/` trả HTTP 200
  từ service đang chạy ở B (không phải chỉ "cổng đã mở").
- `roff tunnel stop --from A --rport P` ⇒ **không** còn listener nào ở A, cũng
  không còn listener local ở `via_lport` (nếu còn ⇒ rò).

**Đạt khi B không LISTEN** (S33b): lệnh phải **fail** với `reason: remote_not_listening`
và **không** được để lại listener ở A.

**S33c — `tunnel list` ẩn link đã stop**: sau khi `roff tunnel stop`, `roff tunnel list`
**không** còn liệt kê link đó ⇒ nếu còn, agent tưởng link còn sống và điều hướng
vào một cổng đã chết.

Vì sao nguy hiểm: "cổng đã mở" ≠ "thông". Nếu chỉ kiểm tra listener mà không
gọi thử, một link chết vẫn được báo thành công ⇒ agent tin là đã xong.

### S34 — `roff doctor`

**Đạt khi**:
- JSON có `checks` (list) + `problems` (int) + `ok` (bool).
- `problems` == số check `ok:false`; `ok` == (`problems == 0`); exit code **1**
  khi có vấn đề, **0** khi sạch.
- Mỗi check có `name`, `ok`, `detail`; có ít nhất `config`, `fleet`, `state_perms`.
- **S34b** — `doctor` **chỉ báo, không ghi file**: so sánh cây state dir
  trước/sau phải giống hệt (cũng vậy với `~/.config/roff`).
- **S34c** — mặc định **không** probe mạng ⇒ chạy < 5 s (đo bằng đồng hồ;
  `--probe` mới được phép đo thật).

Vì sao nguy hiểm: `doctor` là lệnh người dùng chạy để "xem tình trạng" — nếu nó
tự ghi file hoặc tự probe mỗi host thì nó **tự làm nặng** máy người dùng, đúng
thứ mà skill sinh ra để tránh.

### S35 — Ledger bẩn: job ma và job terminal cũ

**Đạt khi**:
- File job **không có khoá `state`** (định dạng cũ) ⇒ `Job.from_dict` cho
  `state == "unknown"`, và `"unknown"` **là** terminal: không giữ slot, không
  bị `reconcile_slots` coi là còn sống.
- `roff wait <job-ma>` **trả về ngay** (không treo tới hết `--timeout`).
- `roff gc` **xoá** job định dạng cũ + job terminal quá 30 ngày, **giữ** job
  đang chạy / đang xếp hàng, và trả `removed_jobs` trong JSON.
- **S35b** — job định dạng cũ **không** được giữ slot: sau `gc`, `reconcile_slots`
  không tính job ma là đang chạy (slot trống phải được dùng lại được).
- **S35c** — `roff wait` với một job định dạng cũ **trả ngay** với
  `reason: "unknown_state"` (exit 1), không treo tới hết `--timeout`.

Vì sao nguy hiểm: 951 job ma trong ledger thật làm `roff wait` treo 2 giờ và
`roff jobs` ra 997 dòng rác — agent nhìn vào đó và không tin được trạng thái.

### S36 — Đổi `ssh config` thì không được tái dùng master cũ

Cùng một alias, nhưng `ROFF_SSH_CONFIG` trỏ sang cổng **chết**.

**Đạt khi**:
- Probe với config **thật** ⇒ `reachable: true`, exit 0.
- Probe lại với config **đã đổi** (cùng alias, cổng chết) ⇒ `reachable: false`
  và exit ≠ 0 — **không** được trả về thông tin của **máy cũ**.

Vì sao nguy hiểm: socket `ControlMaster` trước đây khoá theo **alias** ⇒ lần
probe thứ hai **tái dùng master cũ** ⇒ `roff` tin là máy còn sống ⇒ job giao
cho một máy **không còn tồn tại**, và mọi lần failover cũng đi qua master chết
⇒ job chết với lý do sai.

### S37 — Tự suy ra tool từ câu lệnh (không cần `--need`)

`roff do --cmd 'ffmpeg -i in.mp4 out.mp4'` mà **không** có `--need`.

**Đạt khi**:
- JSON của `roff do` có khoá **`inferred_needs`**: `list[str]`, tên tool **đã
  chuẩn hoá** (`npm` ⇒ `node`), không trùng, thứ tự ổn định.
- Với `ffmpeg -version` ⇒ `inferred_needs == ["ffmpeg"]`.
- Với `echo roff-acceptance` ⇒ `inferred_needs == []` (`echo` là builtin).
- `--need` ghi rõ vẫn là tiêu chí **cứng** (không bị suy ra ghi đè) — **S37b**.
- Job dùng tool suy ra vẫn chạy tới **kết thúc** (`roff wait` ⇒ rc 0) — **S39b**.

Vì sao nguy hiểm: agent phải biết trước tool mà ghi `--need`; nếu quên thì job
được giao cho máy **không có** tool ⇒ chết ngay với exit 127, lãng phí một vòng
giao dịch. Suy ra tự động giúp, **nhưng phải mềm** (xem S38).

### S37b — `--need` vẫn là tiêu chí **cứng**

**Đạt khi**: `roff do --cmd 'pytest -q' --need pytest` ⇒ host **không** có
`pytest` bị **loại khỏi** danh sách ứng viên (không phải hạ điểm) — kể cả khi
`inferred_needs` rỗng.

Vì sao nguy hiểm: nếu `--need` cũng chỉ là "mềm" thì lệnh **bắt buộc** có tool
đó sẽ chạy trên máy không có ⇒ exit 127 ngay. Người dùng ghi `--need` là để
**loại** host, không phải để xin ưu tiên.

### S38 — Suy ra là tiêu chí **MỀM**, không phải lệnh cài tool

Lệnh cần một tool mà **không host nào** có (vd `zzz-roff-khong-ton-tai-xyz`).

**Đạt khi**:
- Job **vẫn được giao** — không phải exit 10 với lý do "thiếu tool".
- `ensured` **rỗng** ⇒ suy ra **không** kích hoạt bước cài toolchain.
- Tool suy ra vẫn xuất hiện trong JSON (minh bạch), và nếu bị loại host thì lý do
  loại **không được** nhắc tới tool suy ra.

Vì sao nguy hiểm: suy ra là **phỏng đoán** (`sh build.sh` không ai biết nó cần
gì). Nếu suy ra thành tiêu chí cứng thì một lệnh lạ làm mất fleet; nếu suy ra
dẫn tới cài tool thì agent tự cài cả ứng dụng lên máy người dùng — rủi ro hơn
nhiều so với việc job chết vì thiếu tool.

### S39 — Suy ra không làm thay đổi hành vi cũ

**Đạt khi**: `policy.select()` không truyền `inferred_needs` (hoặc rỗng) ⇒
**kết quả y hệt** trước W6b: cùng host được chọn, cùng điểm, cùng danh sách
`rejected`.

### S39b — Job dùng tool **suy ra** vẫn kết thúc

**Đạt khi**: `roff do --cmd 'pytest -q'` (không `--need`) trên máy **có**
`pytest` ⇒ job chạy tới kết thúc (`roff wait` ⇒ rc 0, `state: "exited"`),
`inferred_needs == ["pytest"]`.

Vì sao nguy hiểm: "suy ra tool" chỉ là **mềm** — nhưng nếu nó làm hỏng luôn
đường chạy thì mềm cũng hỏng. Suy ra phải **không** đổi hành vi, chỉ xếp hạng.

---

## 12a. Nhóm kẹt kho input dùng chung (bổ sung 23/11/2026, wave W7)

Ba kịch bản này sinh ra từ **sự cố đo thật** (B17): ngày 23/11/2026 một job
treo **28,5 phút** trên fleet Muse vì thư mục `<key>.lock` của kho input dùng
chung bị **mồ côi** (tiến trình chết giữa lúc đẩy) ⇒ mọi lần đẩy sau đó đều
fail `shared_input_busy` ⇒ watcher quay vòng **vô hạn** trên cùng một host ⇒
job treo tới hết `timeout` (7200 + 600 = **2 giờ 10 phút**) mà **không**
failover.

### S40 — Kho input bị kẹt thì phải **tự dọn**, không treo

Tạo lock mồ côi trên host (giả lập đúng trạng thái B17: `<key>.lock` có,
`<key>.complete` chưa có) rồi chạy `roff do` với đúng input đó.

**Đạt khi**:
- Job **vẫn chạy được** (exit 0, có artifact) — tức là roff đã dọn lock chết
  và đẩy lại được, **không** báo `shared_input_busy` vĩnh viễn.
- Thời gian từ lúc `roff do` tới khi job vào trạng thái `launched` **< 120 s**
  (trước W7: phải quay vòng ~30 lần ≈ **30 phút** mới dọn được 1 lock mồ côi).
- Lock **không** còn sót sau khi job xong (không có `<key>.lock` mà không có
  `<key>.complete`).

Vì sao nguy hiểm: kho input kẹt là trạng thái **tự lan truyền** — một lần đẩy
bị giết giữa chừng làm **mọi** job sau đó của nguồn đó treo, kể cả job của
phiên agent khác. Người dùng nhìn thấy "máy remote treo" chứ không phải "kho
input của mình bị kẹt".

### S41 — Input local biến mất giữa lúc chạy ⇒ kết thúc với lý do **rõ ràng**

Xoá file input local **sau khi** job đã được tạo nhưng **trước** khi watcher
đẩy nó lên host.

**Đạt khi**:
- Job kết thúc ở state `lost` với `reason` **`input_not_found`** (không phải
  `timeout`, không phải `unknown_state`).
- **Không** failover: không có job thứ 2 được tạo (failover sang máy nào cũng
  hỏng — file local đã mất).
- Không quay vòng: job kết thúc trong **< 120 s** kể từ lúc input bị xoá.

Vì sao nguy hiệm: lỗi này trước đây bị coi là lỗi hạ tầng ⇒ được failover
⇒ mỗi lần failover lại đẩy input lần nữa ⇒ đốt thêm băng thông rồi cuối cùng
vẫn `lost` với lý do **sai** ("host chết") ⇒ agent báo cáo sai nguyên nhân cho
người dùng.

### S42 — Job **ghim** host (`--host`) mà host đó không với được ⇒ **fail to**

Chạy `roff do --host <alias>` với alias **chắc chắn** không với được (config
riêng trỏ về cổng SSH không có gì nghe).

**Đạt khi**:
- exit **255** với lý do rõ ràng (`host_unreachable` hoặc `unknown_host`) —
  **không** phải `timeout`, không phải `no_host`.
- **Không** có gì chạy ở máy local (marker phải không tồn tại) — nguyên tắc
  "fail loud, không tự chạy ở máy người dùng".
- **Không** có job nào được tạo trên máy khác (không failover âm thầm).

Vì sao nguy hiểm: `--host` là quyền của người dùng ("chạy **đúng** ở máy này").
Tự failover sang máy khác là vi phạm ý chí đó — nhưng quay vòng vô hạn thì
lại làm máy người dùng bật cốc.

**Phần watcher** (host ghim hỏng *lúc đẩy* ⇒ `lost` + `input_push_failed`,
không failover) **không** kiểm chứng được trên fleet thật mà không phá máy
người dùng ⇒ kiểm chứng bằng unit test
`tests/unit/test_w7_lock_recovery.py::test_watch_marks_lost_when_host_is_pinned`.


---

## 12b. Nhóm dọn kho input (bổ sung 23/11/2026, B22)

### S43 — `roff gc` dọn lock mồ côi

Tạo trên máy thật 2 thư mục lock: một **mồ côi** (không có file `owner`,
mtime cũ) và một **còn chủ** (`owner` là tiến trình đang sống, mtime cũ), rồi
chạy `roff gc`.

**Đạt khi**:
- Lock **mồ côi** bị **xoá**, và JSON của `gc` báo nó trong `removed_locks`.
- Lock **còn chủ** **được giữ** — `gc` không được cướp khoá đang dùng.
- Test tự dọn đúng 2 lock nó tạo (không đụng dữ liệu khác của người dùng).

Vì sao nguy hiểm: lock mồ côi là **rác tích tụ** — mỗi lần đẩy bị giết giữa
lúc chạy để lại một thư mục `.lock`, và không ai dọn ⇒ kho input trên host
phình mãi. Ngược lại, nếu `gc` xoá nhầm lock **còn chủ** thì hai máy cùng
đẩy một input ⇒ đẩy trùng, tốn băng thông (0,5–1 MB/s) và có thể ghi đè
nhau giữa chừng.


---

## 12c. Nhóm lỗi *âm thầm* kéo artifact (bổ sung 23/11/2026, B23)

### S45 — pull hỏng phải **báo lỗi**, không được báo thành công

Cách làm **deterministic** (không cần đụng mạng): cho `--out` trỏ vào một
thư mục **không ghi được** ⇒ `pull` chắc chắn hỏng (`PermissionError`).

**Đạt khi**:
- Job **remote** chạy xong (`exit == 0` — công việc trên máy đã xong).
- `roff wait` trả **exit ≠ 0** với `reason == "pull_failed"`, `pulled == false`,
  `pull_error` khác rỗng, `ok == false`.
- `exit` trong JSON vẫn là **0** — không được báo nhầm là *job* fail.
- Sau khi mở lại quyền ghi, `roff pull --local <thư mục>` **kéo lại được**
  artifact ⇒ có đường cứu, không phải mất data.

Vì sao nguy hiểm: `wait` báo exit 0 ⇒ agent tin là đã có artifact ⇒ đọc
thư mục output **rỗng** ⇒ tưởng job chạy mà không ra kết quả, và **không có
cảnh báo nào**. Đo thật 23/11/2026: 1 trong 3 shard của S11 mất file
(`['s0.txt','s2.txt']`) trong khi `roff wait` trả rc 0 cho cả 3 — nguyên nhân là
pull hỏng lúc fleet flap **bị nuốt**.

Exit code dành riêng: **74** — phải phân biệt với **1** (*job* fail ⇒ đừng
chạy lại job). Với 74 thì **kéo lại** bằng `roff pull`, **không** cần chạy lại
job.

### B25 — `roff` và `ssh` phải đọc **cùng** file ssh-config

Không có kịch bản riêng: **S07** là kịch bản nghiệm thu cho B25 (đổi
`~/.ssh/config` sang cổng chết ⇒ watcher phải thấy mất kết nối ⇒ ghi
`disconnected`), và **S31** cho phần `ROFF_SSH_CONFIG` tới tay ssh.

Bằng chứng đo thật 23/11/2026: `roff` đọc `~/.ssh/config` theo `$HOME`, còn
**OpenSSH đọc theo passwd home** (`getpwuid`) ⇒ ssh đọc file khác với file mà
roff fingerprint ⇒ đổi config **không** làm ssh mất kết nối ⇒ watcher không
ghi `disconnected` ⇒ S07 đỏ. Lệnh khắc phục: `roff` **luôn** truyền
`-F <đường dẫn>` cho ssh ⇒ hai bên đọc đúng một file.

### S46 — `--sync-back` phải **thực sự** kéo file về project

Chạy `roff do` **trong** một thư mục project tạm, với
`--sync-back report.md` (đường dẫn **tương đối** trong project) và lệnh ghi
`$ROFF_WORK/report.md` trên máy.

**Đạt khi**:
- `roff wait` trả rc 0;
- `<project>/report.md` **tồn tại** và có **đúng nội dung**;
- ledger **không** ghi `sync_back_error`.

Vì sao nguy hiểm: `--sync-back` được **quảng cáo** trong `skill/SKILL.md` và
`docs/SPEC-CLI.md`, nên agent tin là file đã về project ⇒ đọc file cũ ⇒ mất
công. Đo thật 23/11/2026: job `261123-171305-88ac` exit 0, `pulled: true`,
ledger có `sync_back = ['/tmp/sbtest/proj/report.md']` — nhưng thư mục đó
**rỗng**: không mã nào gọi `pull_path()` ngoài `roff pull` thủ công.

**Đã kiểm chứng (23/11/2026, sau khi sửa B26)**:
`tests/acceptance/test_acceptance_sync_back.py::test_s46_sync_back_pulls_file_into_project`
→ **1 passed in 78.20s** trên fleet thật (`muse-phamminhbien3333`).


---

## 12d. Nhóm an toàn fleet (bổ sung 23/11/2026, B27)

### S47 — `ROFF_CONFIG` trỏ tới file không tồn tại ⇒ **fail to**, không nới fleet

Chạy `roff` với `ROFF_CONFIG` trỏ tới một file **không tồn tại**.

**Đạt khi**:
- `roff hosts` trả rc **3**, `reason == "config_missing"`, và lỗi **nêu đường dẫn**;
- `roff do` trả rc **3** và **không tạo job nào** trong ledger;
- (đối chứng) config **có thật** với `include = muse-*` + `exclude = colab-comfy`
  ⇒ fleet **đúng** như khai báo (`colab-comfy` vắng mặt), JSON **không** mang
  cờ `fleet_unrestricted`. (**S47b** — đây là nhánh đối chứng của S47.)

Vì sao nguy hiểm: config thật của người dùng khai báo `exclude = colab-comfy`
(máy Colab GPU, chuyên dụng) và `include = muse-*`. Nhưng `Config.load()` trả
về config **rỗng** khi file không tồn tại, mà **không báo lỗi** ⇒ `fleet_include`
mặc định `["*"]` ⇒ fleet **8 máy** thay vì 4.

Đo thật 23/11/2026 (trước khi sửa):

```
$ ROFF_CONFIG=/tmp/khong-co-that-abc123/config.ini roff hosts
  fleet gồm: ['comfy', 'colab-comfy', 'qwen1', 'qwen1-direct',
              'muse-pmb333', 'muse-tampt', 'muse-bienpm333', 'muse-phamminhbien3333']
  count = 8
```

⇒ `roff` sẵn sàng đẩy job **và dữ liệu** lên máy Colab GPU của người dùng.
Không phải sự cố ngẫu nhiên: một subagent đã **vô tình chạy job thật**
(`echo hi`) lên host `comfy` vì đúng lý do này.

Kịch bản này **không cần fleet thật** — lỗi phải xảy ra **trước** khi probe
mạng — nên nó vẫn kiểm chứng được ngay cả khi cả 4 máy Muse đang chết.

**Đã kiểm chứng (23/11/2026, sau khi sửa B27)**:
`tests/acceptance/test_acceptance_config_safety.py` →
**2 passed in 56.28s** (S47 + S47b).


## 12e. Bộ nghiệm thu tự phân biệt "máy chết" với "lỗi của `roff`" (bổ sung 23/11/2026, B28)

Không phải kịch bản mới — mà là **cách chạy** các kịch bản trên, để báo cáo
không đo nhầm chất lượng sản phẩm.

Fleet `muse-*` restart giữa lúc đo là chuyện **bình thường** (4 máy đã restart
nhiều lần trong một đợt). Ba lớp phòng thủ:

| Lớp | Ở đâu | Việc |
|---|---|---|
| Guard tường minh | trong từng test | `pick_live()` chọn máy **đang sống lúc chạy**; `is_host_down()` + `pytest.skip` trước mỗi `assert` phụ thuộc máy remote; `dir_state()` trả **3 trạng thái** (có / không có / *không xác định* — máy chết) ⇒ trạng thái cuối ⇒ **skip** |
| Lưới an toàn cuối cùng | `tests/acceptance/conftest.py` | FAIL mà thông điệp có dấu hiệu tầng ssh/tầng host (`Connection refused`, `No route to host`, `timed out after …`, `rc=255`) ⇒ đổi thành **SKIP kèm lý do** |
| Bằng chứng mang lý do | `run_acceptance.py` | pytest chạy với `-rfEs` ⇒ `acceptance.txt` in **lý do** của từng lần skip ⇒ `make_report_table.py` đưa vào cột *Ghi chú* của §4 |

**Ngưỡng của lưới an toàn là hẹp, và có test riêng**
(`tests/unit/test_acceptance_guard.py`): kịch bản **cố ý** kiểm exit **255**
(S42) hay báo sai `reason` vẫn phải **đỏ** — lưới này không được che lỗi thật.

Đợt chạy 23/11/2026 16:37 (trước khi có lớp này) cho thấy vì sao cần: **12
FAILED** — 8 do `Connection refused`, và **2 lỗi nằm trong chính bộ test**
(S07 cắt kết nối *trước khi* job lên `launched`; S11 không kiểm `exit` của
shard) ⇒ mỗi lần đỏ phải hỏi: *lỗi ở sản phẩm hay ở kịch bản?*


# Test hồi quy (R1–R27)

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
| R21 | ngưỡng RAM | phải **cộng dồn** (`need` + `reserve`), không phải `max(512, need)` |
| R22 | độ dài ControlPath | **≤ 88 byte** (giới hạn thật của Unix socket là 108 byte, nhưng khi socket cũ còn sót, OpenSSH thử bind lại với hậu tố ngẫu nhiên tới **17 byte** ⇒ 88 + 17 = 105 vẫn vừa) |
| R23 | socket ControlMaster sót | phải được **dọn**, không để `ssh` chết với socket cũ |
| R24 | chuyển host | mọi đường dẫn remote phải tính lại theo host mới |
| R25 | workdir hiện tại | lấy từ **ledger**, KHÔNG lấy từ attempt cũ |
| R26 | kéo artifact | job phải **kéo xong** mới được đánh dấu terminal |
| R27 | kho input dùng chung **không được kẹt vĩnh viễn** (B17) | lock mồ côi ⇒ **dọn rồi chiếm lại** trong **1** lần chờ ⇒ đẩy **thành công** ⇒ `.complete` được ghi ⇒ lần sau **không** đẩy lại; lock của tiến trình **còn sống** thì **không** bị cướp; nhả lock bằng `rm -rf` + kiểm tra lại, còn thì **cảnh báo** ra stderr |

## Chạy

```bash
# test hồi quy, không cần fleet
pytest tests/unit tests/regression -q

# test nghiệm thu, cần fleet thật — tự bỏ qua nếu host không sống
pytest tests/acceptance -v -m acceptance -rf

# nghiệm thu trọn vẹn (ghi bằng chứng vào docs/evidence/<tag>/)
python3 tests/acceptance/run_acceptance.py --tag final

# bảng kết quả cho docs/REPORT.md §4
python3 tests/acceptance/make_report_table.py final --md

# §4 trọn vẹn (tiêu đề + tóm tắt + bảng) — dán thẳng vào docs/REPORT.md
python3 tests/acceptance/make_report_table.py final --section
```

Marker `acceptance` = cần host thật. Test tự `skip` với lý do rõ ràng nếu host không sống,
**không** được báo pass khi bỏ qua.

> **Chạy một mình.** Không chạy song song với `pytest` khác hay job `roff` khác:
> `pytest` giữ 3 thư mục tạm mới nhất nên sẽ **xoá** thư mục tạm của lần chạy
> đang diễn ra ⇒ input biến mất giữa chừng; bộ **đo tài nguyên** cũng đếm
> tiến trình `roff` của người khác ⇒ báo rò giả. Chi tiết: `docs/evidence/README.md`.
