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

---

## 4. Còn mở (xếp theo payoff)

| # | Vấn đề | Chỗ | Payoff |
|---|---|---|---|
| 1 | `roff hosts` probe song song 8 alias → đụng single-session bridge (fleet có `comfy`+`colab-comfy` = cùng 1 box) | `roff.py` `cmd_hosts` | Cao |
| 2 | Bước "Place" vẫn là prose dày (giờ có bảng symptom→meaning→do nhưng còn 1 đoạn dài) | `SKILL.md` bước 2 | TB |
| 3 | `setups/playwright-chromium.sh` dùng `playwright@latest` — client/server có thể lệch version | `skill/setups/playwright-chromium.sh` | TB |
| 4 | Bảng exit code còn copy ở `docs/README.md` + `docs/RETRO.md` (SKILL.md là bản agent-facing duy nhất) | `docs/` | Thấp |
| 5 | `references/` vẫn lặp lại sequence push→run→wait→pull của SKILL.md | `skill/references/*` | Thấp |
| 6 | `docs/RETRO.md` mô tả `setups/node.sh` ở root (đã đổi layout) | `docs/RETRO.md` | Thấp |

---

## 5. Ghi chú

- `docs/RETRO.md` là bản ghi lịch sử — giữ nguyên nội dung, chỉ nhớ rằng layout đã đổi.
- `git mv` giữ history: `git log --follow skill/SKILL.md` vẫn truy được.
- Symlink đã đổi: `~/.claude/skills/remote-offload` và `~/.agents/skills/remote-offload` → `<repo>/skill`.
