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
- **Đã đóng ở vòng 3–7:** fan-out của `roff hosts` (→ serial mặc định), dedup bảng exit code (vòng 4), dòng `0` của bảng exit key theo field + cảnh báo `--timeout` của `wait` (vòng 5), hai hình dạng của exit 2 + field `detail` của 255 (vòng 6), và 5 lỗi doc + B1–B5 của synthesis (vòng 7).

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

### Vòng 5 — synthesis của workflow (verdict viết trên `636fb28`, còn sống với HEAD hiện tại)

9 verdict của một critic đã được đối chiếu lại với code hiện tại; **2 còn sống**, 7 đã được sửa ở vòng 2–3
(verdict tự nói "đã sửa trong working tree"). Hai cái còn sống:

| Sửa | Vì sao |
|---|---|
| Dòng `0` của bảng exit **key theo field** (`state`, `reachable`) thay vì `—`; thêm câu "**Exit 0 means the command ran, not that the work is done**" | `roff status` trả **exit 0** cho job *đang chạy* (`cmd_status` → `emit({..., "state": "running"}, 0)`), còn dòng bảng cũ nói "the job finished cleanly → pull the artifacts" với cột field là `—` (tức "không có gì để đọc"). Agent monitor bằng `roff status` → thấy 0 → pull `$ROFF_OUT` rỗng → báo thành công trong khi job còn chạy. Đã verify live: `status` job đang chạy → exit 0 + `state: running`; `wait --timeout 3` → 124; `cancel` → 130; `status` sau cancel → 130 + `state: exited, exit: 130` |
| Bước 6: "Always pass `--timeout`: without it `wait` blocks forever and prints nothing if the host goes down" | `roff wait` mặc định `--timeout 0` = chờ vô hạn; nhánh `unreachable` chỉ emit khi đã hết deadline → treo im lặng, không 255, không 124. Agent không có `--timeout` là treo session |

### Vòng 6 — agent synthesis (chưa chạy xong) đã probe thêm 2 lỗi hợp đồng

Agent synthesis của workflow **chưa sinh ra bản tổng hợp** (journal dừng ở `started Synthesize`),
nhưng transcript của nó có probe thực nghiệm và phát hiện 2 chỗ bảng exit nói sai. Cả hai đã
verify lại bằng code + host thật:

| Sửa | Vì sao |
|---|---|
| Tách dòng `2` thành hai: `2` + `error` (input bị từ chối trước khi chạy) và `2` + `—` (argparse usage error, **không** có JSON) | `RoffError(2, …)` in JSON (`{"ok": false, "error": "unknown job nosuchjob …"}`) — còn argparse usage error thì in usage ra stderr và **không** có JSON. Bảng cũ gộp cả hai và nói "prints no JSON", sai với nửa kia. Agent gõ sai job id sẽ đi tìm JSON không tồn tại. Verify live: `status nosuchjob` → exit 2 + JSON; `logs` thiếu tham số → exit 2 + usage trên stderr, không JSON |
| Dòng `255` đọc field `detail` (không phải `error`); decision rule 255 nói rõ "Permission denied (publickey)" là **key bị từ chối phía bạn**, không phải host chết | `error` chỉ là vỏ ("cannot resolve remote HOME on h"); nguyên nhân thật nằm ở `detail` ("Permission denied (publickey)."). Không nêu `detail` thì agent không phân biệt được "key hỏng → báo user" với "host chết → đổi host", và SKILL.md bảo "pick another host" — sai với lỗi key. Verify live (ssh giả, exit 255): `{"ok": false, "error": "cannot resolve remote HOME on h", "host": "h", "detail": "Permission denied (publickey)."}` |

Hai sửa nhỏ kèm theo sau khi đọc lại SKILL.md từ đầu: dòng `124` bỏ lại `—` (cột field chỉ lặp
lại cột "Do" — exit code 124 đã đủ để biết), và `wiped_since_last_probe` /
`restarted_since_last_probe` ghi rõ "chỉ có mặt khi đúng" (code chỉ set khi `True`).

### Vòng 7 — bản tổng hợp cuối của workflow (12 agent, 1.07M token)

Synthesis cuối cùng (chạy trên `c336177` = `main` lúc đó) ra **5 lỗi thật + 8 lỗi CLI**.
Lần này mọi claim đều được tự kiểm lại bằng code và host thật trước khi sửa.

**5 lỗi trong SKILL.md — đã sửa:**

| # | Lỗi | Sửa | Verify |
|---|---|---|---|
| 1 | Profile không hề nhắc `error`; dòng `reachable: false` trong bảng step-2 nói "the host is down" → agent đọc "key bị từ chối" thành "cả fleet chết" | Bảng exit 0 + danh sách field của profile + bảng step-2 đều nêu `error`; tách dòng `reachable: false` thành 2 dòng (auth vs host chết) | Payload có `error` sẵn (probe_host trả về) — đây là lỗi **doc**, không phải code |
| 2 | "*Done when:* của bước 6 tự nêu 124 (mà 124 = job **vẫn đang chạy**)" → agent đánh dấu xong rồi pull `$ROFF_OUT` rỗng | Done when giờ liệt kê đúng các code kết thúc (0/1/69/97/130) và nói rõ **"124 is not done"** | — |
| 3 | Dòng 255 trỏ `detail`, nhưng `logs`, `cancel` và nhánh ssh-timeout **không hề có** `detail` | Sửa **cả code lẫn doc**: B1/B2/B3 dưới đây | `logs`/`cancel` trước đây trả 255 **không** có `detail` — "Permission denied (publickey)" biến mất; `status` thì có. Doc đúng một nửa |
| 4 | Hợp đồng exit bị kể lại ở docstring của `roff.py` (in ra bởi `--help`, mà SKILL.md trỏ tới) và ở `references/e2e.md` — cả hai đã lệch | Docstring + e2e.md nói theo SKILL.md | `--help` nói "0 ok" (trong khi 0 ≠ xong) và "1 job failed" (bỏ nhánh pre-job) |
| 5 | `description` kích hoạt quá: "asks to run something on another machine" bắt được cả "run this on my laptop" — thứ SKILL.md giữ ở local; và "ML/render" không tìm thấy được trong body ("training/render") | Bỏ nhánh đó, dùng "ML training/render" ở cả description lẫn body | — |

**5 lỗi CLI — đã sửa, verify bằng harness `ssh` giả:**

| # | Lỗi | Sửa |
|---|---|---|
| B1 | `cmd_logs` vứt mất stderr của ssh khi 255 → key bị từ chối không phân biệt được với host chết | `detail=p.stderr.strip()[-400:]` |
| B2 | `cmd_cancel` mất stderr khi 255 | `detail=p.stderr.strip()[-400:]` |
| B3 | `ssh()` timeout raise 255 không kèm `detail` | kèm `detail` |
| B4 | `probe <host>` (một host) và `probe all` **trả về khác nhau** cho cùng một sự cố: một host → exit 255 không có key `reachable`; all → exit 0 với `reachable: false` | `probe <host>` đi qua `safe_probe` → hai đường giống nhau |
| B5 | `cancel` một job **đã xong** vẫn báo `cancelled`/130 → agent tưởng job bị hủy, không biết kết quả thật | Script cancel giờ trả `EXIT=$(cat $d/exit)`; code 130 ⇒ thật sự bị hủy, code khác ⇒ job đã tự xong trước đó |

Verify B5 (live, `muse-tampt`): cancel job **đã xong** → `{"state": "exited", "exit": 0, "note": "the job finished before the cancel landed; nothing was cancelled"}` exit 0; cancel job **đang chạy** → `{"state": "cancelled", "exit": 130}` exit 130 ✓.

**Cố ý KHÔNG làm** (phần "Leave it alone" của synthesis): giữ bảng exit ở SKILL.md; giữ
probe serial mặc định; giữ `$ROFF_OUT`; giữ cơ chế *Done when*; giữ mảng `next`; không
chuyển bước 8 ra `references/` (P7) và không xoá dòng định nghĩa `roff` ở 3 file
reference (P8) — hai cái này chỉ giảm context load, không có hậu quả hành vi, và P8 làm
file reference phụ thuộc việc agent đã đọc SKILL.md.

### Vòng 7b — nhánh `feat/durable-layout-root` (CỦA PHIÊN KHÁC, KHÔNG merge)

Trong lúc workflow chạy, một session khác của người dùng làm việc **cùng thư mục** và tạo
nhánh `feat/durable-layout-root` (commit `e8f162d`, 11:59). Reflog cho thấy
`checkout -b` → `reset` → `commit`; transcript của agent synthesis của tôi chỉ chạy lệnh
git chỉ-đọc (`status`/`log`/`diff`/`show`) ⇒ **không phải agent của tôi tạo nhánh**.

Nội dung nhánh: đưa layout root sang `<durable mount>/.roff` (`$ROFF_ROOT`) khi host có
mount bền (container Muse wipe `$HOME` mỗi lần refresh → `/home/hatch`), thêm
`layout_root` + `durable_local` vào profile, và sửa B6 (`node.sh` cài vào
`$HOME/.roff/toolchain` trong khi runner lấy PATH từ `$ROFF_ROOT` ⇒ `node: command not
found` trên host durable-root).

**Không merge, không sửa, không xoá.** Lý do:
- Nó làm thay đổi **ngữ nghĩa** mà `main` đang dựa vào: `wiped_since_last_probe` không còn
  đồng nghĩa với "phải re-push", và instance token cố ý nằm lại trong `$HOME` để wipe vẫn
  bị phát hiện.
- Nó ghi đè `SKILL.md` và `skill/roff.py` ở đúng những chỗ vòng 7 vừa sửa ⇒ merge sẽ phải
  giải quyết thủ công đúng những hunk đó.
- Nó **không** sửa 5 lỗi của vòng 7 (và không sửa B1–B5).

Cách đúng: các sửa vòng 7 nằm trên `main`; nhánh kia **rebase** lên `main` rồi mới merge.
Việc rebase/merge là của phiên đó, không phải của tôi — tôi không đụng vào nhánh của ai.

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
