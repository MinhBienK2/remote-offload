# Review: tổ chức của skill `remote-offload`

Ngày: 2026-11-21. Đối tượng: `skill/SKILL.md`, `skill/references/*.md`, `skill/setups/*.sh`, `skill/roff.py`.
Trạng thái: **đã áp 5 fix đầu + refactor layout**. File này là tài liệu cho **người**, không phải cho agent.

> Bối cảnh trước khi refactor: `~/.claude/skills/remote-offload` và `~/.agents/skills/remote-offload`
> là symlink tới **repo root**, nên `README/RESEARCH/RETRO/LICENSE/.git` ship kèo vào mọi lần cài.
> Nay: symlink trỏ tới `skill/`; `docs/` dành cho người.

---

## 1. Verdict (sau khi sửa)

**Hình dạng đúng; ranh giới đóng gói đã vẽ; phần "quyết định" đã bớt cảm tính.**

- **Đúng, giữ nguyên:** `SKILL.md` là router (decision rules → workflow 8 bước → recovery → pointer), phần nặng theo task đẩy xuống `references/` theo 3 nhánh, mỗi nhánh khép kín. Progressive disclosure đúng cách, không sprawl.
- **Đã sửa:** ranh giới skill (`skill/` vs `docs/`), `description` thừa, lệnh thật + path binding, bảng recovery đọc `reason`, pointer có điều kiện.
- **Đã sửa ở tầng code:** skill **hứa** một số thứ mà `roff.py` **không làm** — secrets trong remote argv, `roff status` trả 0 cho job fail, cancel báo thành "job failed", `~/.ssh/config` parse sai, `ensure` không re-run sau restart, `push` xong `run` báo 255, pull manifest lẫn file cũ.
- **Còn mở:** `roff hosts` fan-out đụng luật single-session bridge; bảng decision cho bước "Place"; setup script Playwright còn dùng `@latest`; dedup bảng exit code giữa `README`/`RETRO`/`SKILL.md`.

---

## 2. Những gì đang đúng — đừng đụng

| Thứ | Vì sao giữ |
|---|---|
| Fail loudly, not locally (`SKILL.md`) | Không có hệ config nào confess được gotcha này. |
| Exit code là hợp đồng | Rẻ, máy đọc được, `RETRO.md` chứng minh worker thật sự dùng nó. |
| Setup-once + stamp `hash:boot_id:instance_token` | Đúng bài toán wipe-vs-restart. |
| Detached job + ledger local | Job sống sót qua disconnect và qua agent restart. |
| Artifact trước khi "done" | Câu duy nhất có completion criterion sắc nhất; giữ nguyên văn. |
| Remote stdout = data, không phải instruction | Guardrail chống prompt-injection. Giữ dạng phủ định. |
| Flap detection bằng `boot_id`+`instance_token` | 3 worker độc lập gặp flap và tự recover (`RETRO.md`). |
| Chia `references/` theo nhánh | Đúng cut; mỗi file là một workflow khép kín. |

---

## 3. Đã áp dụng

### Layout (Fix 1)

```
repo/
├── skill/                 <- symlink vào ~/.claude/skills/remote-offload
│   ├── SKILL.md
│   ├── roff.py
│   ├── references/{ffmpeg,be-fe,e2e}.md
│   └── setups/{node.sh,playwright-chromium.sh}
├── docs/{README.md,RESEARCH.md,RETRO.md,REVIEW.md}
├── LICENSE
└── .gitignore
```

Skill root giờ chỉ chứa thứ agent dùng.

### `description` (Fix 2)

Cắt mệnh đề `Covers host discovery/probe, setup-once persistence across host restarts,
detached jobs, artifact fetch, tunnels.` (~22 từ permanent load, body đã có hết).
Giữ nguyên 8 nhánh trigger; thêm `"chạy trên máy khác"`.

### Lệnh thật + path binding (Fix 3)

`SKILL.md` giờ nói rõ: `roff` = `python3 <skill-dir>/roff.py`, mọi path setup viết
`<skill-dir>/setups/…` (**trước đây là path CWD-relative → exit 2 khi agent chạy từ project dir**),
và có `--help` fallback.

### Recovery thành bảng, dạy đọc `reason` (Fix 4)

Bảng `exit | JSON field | Means | Do`, tách 3 nguyên nhân của `69`, thêm `97` (workdir thiếu),
thêm `130` (job bị cancel → không resubmit), nói rõ `255` = unreachable **hoặc** timeout,
và `reason` của OOM nói "output dở vẫn còn, pull trước".

### Pointer có điều kiện (Fix 5)

`## Recipes` giờ là "Reach for a file only when its branch applies" + 3 câu có điều kiện kích hoạt.

### Sửa ở `roff.py` (cùng lớp lỗi, đã verify bằng host thật)

| Sửa | Verify |
|---|---|
| Secrets không còn trong remote argv: payload đi qua stdin (`bash -s`, không args) | `REMOTE_ARGV=[]`; `ps` trên host không thấy secret |
| `env.sh` mode 600 (trước: 644), `cmd.sh`/`run.sh` 700 | `ls -l` trên host |
| Audit log redact `--env` (trước: plaintext) | `audit.jsonl` |
| `~/.ssh/config`: bỏ `Include` (glob, đệ quy) + không nuốt option keyword (`Host foo User root` → 1 alias) | test với config giả + config thật |
| `ensure` stamp lấy `boot_id`/`instance_token` **trực tiếp từ host** (trước: cache → không re-run sau restart) | ensure run 1 `skipped:false`, run 2 `skipped:true` |
| `ensure` validate tên setup (`[A-Za-z0-9._-]+`) → chặn path traversal | `--name ../evil` → exit 2 |
| `push` xong `run` không còn báo 255 (profile cache không có key `reachable`) | profile `{"home":"/root"}` → run OK |
| `roff status` mirror kết quả job (trước: job fail vẫn exit 0) | job exit 7 → status exit 1 |
| `roff wait`/`status` map cancel → 130 (trước: 1 = "job failed") | cancel → wait exit 130 |
| `roff pull` manifest chỉ liệt kê file **pull về** (trước: gộp file cũ trong thư mục đích) | dir có sẵn 4 file cũ → manifest chỉ 1 file mới |
| `reason` của `69` viết lại đúng nghĩa (reboot: output còn; OOM: output còn) | đọc lại `remote_state` |
| `roff hosts`/`probe all`: một host treo không còn giết cả sweep | `safe_probe` bọc lỗi |

### Vòng 2 — sau khi 4 critique của workflow review đọc lại code đã sửa

| Sửa | Vì sao |
|---|---|
| `job_exit_code` cho phép `97` đi xuyên ra (trước: mọi rc≠0 → `1`) | Bảng contract hứa exit `97` nhưng CLI **không bao giờ** phát ra mã đó → dòng recovery "project never pushed" là chết. Đây là lỗi do bản thân vòng 1 tạo ra |
| `roff run` khi 255 giờ kèm nguyên nhân thật (`error` từ probe: auth failure / timeout), không còn gọi chung là "host unreachable" | ssh trả 255 cho cả auth failure lẫn unreachable; thông báo cũ nói dối và làm rơi stderr |
| Gợi ý `next` của `roff run` giờ **chạy được**: đường dẫn thật của script, `wait --timeout 3600`, `pull --local ./out`; đổi từ chuỗi sang mảng | Trước: `roff wait <id>` (không timeout → treo vô hạn) và `--local <dir>` (placeholder không chạy được) |
| `run --help` + docstring của CLI nói rõ `$ROFF_OUT` | Biến mà **mọi** job command phụ thuộc không có trong bất kỳ `--help` nào |
| SKILL.md: **hai bảng exit → một bảng** (`exit | JSON field | Means | Do`) | Sự trùng lặp đó chính là nơi lệch: bảng trên cùng và bảng recovery không khớp nhau. Một nguồn sự thật duy nhất |
| SKILL.md: thêm prerequisites (python3, ssh alias, key auth non-interactive, rsync tuỳ chọn) | Trước đây toàn bộ giả định ngầm |
| SKILL.md: bước 6 có *Done when* | "job vẫn đang chạy" không có chỗ nào đứng |
| SKILL.md: cảnh báo single-session bridge nói **một lần** (trước: ba lần) | Lặp lại làm loãng và dễ bị bỏ qua |
| SKILL.md: bảng "Place" thêm các nhánh thực sự xảy ra (`reachable:false`, `gpu:null`, RAM thấp) và chuyển dòng "sibling alias" về đúng nơi có dữ liệu | Một dòng trước đây cần dữ liệu mà bước 1 không thu thập được |
| SKILL.md: bỏ "never bare `out/`" (phủ định nằm cạnh mệnh đề dương của nó) | Không đổi hành vi, bớt load |

### Vòng 3 — sau khi verify của workflow xác nhận finding còn sống

| Sửa | Vì sao |
|---|---|
| `roff hosts` / `roff probe all` **serial mặc định**, thêm `--parallel` opt-in | Verify của critique "hierarchy" giữ lại đúng 1 finding: guardrail single-session bridge nằm *sau* lệnh phá nó, và `roff hosts` fan-out 8 alias đồng thời → 429 trên bridge 1 session (fleet có `comfy` + `colab-comfy` = cùng 1 box). Giờ lệnh discovery tự tuân thủ guardrail của chính nó |
| `description`: gộp 3 cụm tiếng Việt thành 2 (một branch viết 3 lần), bỏ "remote host" trần (over-fire), thêm nhánh "reach a service running on a remote host (tunnel / port forward)" mà skill thực sự hỗ trợ | Theo `writing-for-agents`: synonym của một branch là một branch viết hai lần; và pointer phải nêu **mọi** branch skill phục vụ |

### Vòng 4 — quét lại "sediment" (code đổi, doc chưa theo)

| Sửa | Vì sao |
|---|---|
| `docs/README.md`: ví dụ `roff.py hosts` bỏ chữ "in parallel" | Vòng 3 đã đổi `hosts` sang serial mặc định nhưng README vẫn mô tả parallel — doc nói ngược với code |
| `docs/README.md`: **xoá** danh sách exit code inline, chỉ trỏ về `skill/SKILL.md` | Bản rút gọn của bảng là **sai**, không chỉ trùng: code `1` mang hai nghĩa tùy có field `job` hay không (`1 job failed` vs `1` pre-job failure) — đúng cái bug vòng 2 đã sửa. Rút gọn hoá đọc sai thành hiện thường. Bảng đầy đủ chỉ ở SKILL.md |
| `docs/README.md`: bullet secrets nói rõ `--cmd` được lưu **verbatim** trong audit log | Câu cũ ("never in the local audit log") chỉ đúng cho `--env`; dễ bị hiểu là "secret nào cũng không vào audit log" — sai, vì `--cmd` lưu nguyên văn |
| `docs/RETRO.md`: thêm note đầu file trỏ bảng exit code hiện hành về `skill/SKILL.md` | RETRO là bản ghi lịch sử — không sửa body, chỉ chỉ đường cho người đọc |

---

## 4. Còn mở (xếp theo payoff)

| # | Vấn đề | Chỗ | Payoff |
|---|---|---|---|
| 1 | Bước "Place" vẫn là prose dày (giờ có bảng symptom→meaning→do nhưng còn 1 đoạn dài) | `SKILL.md` bước 2 | TB |
| 2 | `setups/playwright-chromium.sh` dùng `playwright@latest` — client/server có thể lệch version | `skill/setups/playwright-chromium.sh` | TB |
| 4 | `references/` vẫn lặp lại sequence push→run→wait→pull của SKILL.md | `skill/references/*` | Thấp |
| 5 | Vài câu trong SKILL.md lặp lại thứ `--help` đã nói | `skill/SKILL.md` | Thấp |

> #3 (bảng exit code bị copy ở `docs/`) đã **đóng** ở vòng 4: `docs/README.md` chỉ trỏ về
> `skill/SKILL.md`, `docs/RETRO.md` giữ nguyên bản ghi lịch sử + note trỏ về SKILL.md.

---

## 5. Ghi chú

- `docs/RETRO.md` là bản ghi lịch sử — giữ nguyên nội dung, chỉ nhớ rằng layout đã đổi.
- `git mv` giữ history: `git log --follow skill/SKILL.md` vẫn truy được.
- Symlink đã đổi: `~/.claude/skills/remote-offload` và `~/.agents/skills/remote-offload` → `<repo>/skill`.
