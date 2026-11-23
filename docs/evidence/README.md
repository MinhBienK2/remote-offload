# Bằng chứng nghiệm thu

Mỗi lần chạy `tests/acceptance/run_acceptance.py` tạo một thư mục theo thời điểm
(`YYYYMMDD-HHMMSS/`) chứa:

| File | Nội dung |
|---|---|
| `fleet.json` | trạng thái fleet lúc đo: host nào sống, RAM trống, disk, layout_root, có internet không |
| `acceptance.txt` | output đầy đủ của `pytest tests/acceptance -v -m acceptance -rfEs` |
| `measure.txt` | kết quả bộ đo tài nguyên máy local khi job chạy remote (`docs/SCENARIOS.md` §0.1) |
| `s1.txt` | kết quả kịch bản S1 — skill tự bật ở dự án khác |
| `report.json` | tóm tắt từng bước (rc + thời gian) |

> `acceptance.txt` chạy pytest ở chế độ **`-v`** (không phải `-q`) — mỗi test có
> **một dòng kết quả** để `make_report_table.py` đọc được trạng thái từng kịch bản.
> Cờ `-rfEs` in thêm **lý do** của mỗi lần skip ⇒ đọc `acceptance.txt` là biết
> ngay kịch bản bị bỏ qua vì **hạ tầng** hay vì **lỗi của `roff`**.

## Các lần đo đã lưu

| Thư mục | Là gì |
|---|---|
| `rehearsal/` | đợt **diễn tập** (23/11/2026) — chạy trước đợt cuối để bắt lỗi hạ tầng; `acceptance.txt` ở đây chứa **lỗi thật** của bộ test (và 1 sự cố của `roff`) đã sửa sau đó |
| `failover-live-20261123/` | bằng chứng **failover tự nhiên**: job `261123-153312-4c46` chạy trên `muse-pmb333` thì máy chết ⇒ `roff` chuyển sang `muse-bienpm333` ⇒ job chạy tiếp (xem `docs/REPORT.md` B17, `docs/SCENARIOS.md` S20) |
| `final-20261123/` | đợt nghiệm thu **cuối của đợt 23/11** (46 PASSED / 0 FAILED / 6 SKIPPED) — bằng chứng của lỗi **B36** (`s18-submit-latency.txt`: đo **trước** khi sửa đường gửi job) |
| `final/` | đợt nghiệm thu **cuối — 24/11 lần 3** (49 PASSED / **0 FAILED** / 7 SKIPPED, 52 phút) — đợt này **xanh sạch**: cả 7 lần skip đều là **hạ tầng** (máy Muse chết giữa lúc chạy, hoặc fleet chỉ còn 1 máy sống nên không đo được tăng tốc) ⇒ **không** còn lỗi sản phẩm nào lộ ra. Bằng chứng dùng cho `docs/REPORT.md` §4 + §3.1 |
| `final-20261124a/` | đợt nghiệm thu **24/11 lần 1** (48 PASSED / **2 FAILED** / 6 SKIPPED) — đợt này **tìm ra 2 lỗi KỊCH BẢN** (không phải lỗi sản phẩm): **S28** đo kho input phía host *ngay lúc* `roff do` vừa trả về, nhưng sau B36/C10 thì **watcher** mới đẩy input ⇒ đo quá sớm; **S49c** báo đỏ vì `RoffError 255` lúc launch (host `muse-tampt` chết đúng giây đó) ⇒ phải `skip` chứ không `fail`. Cả 2 đã sửa và chạy lại ⇒ **5 passed** |
| `final-20261124b/` | đợt nghiệm thu **24/11 lần 2** (48 PASSED / **1 FAILED** / 7 SKIPPED) — lỗi đỏ còn lại là **S24**: kịch bản chờ pid rồi **dừng vòng lờ** ở job đầu tiên ⇒ chỉ **1/2** job bị giết ⇒ job còn lại chạy tới hết (không có event `failover`). Trước B36/C10, `do` launch **tuần tự** nên khi `do` trả JSON thì cả 2 job đã có pid ⇒ lỗi này không lộ ra được. Sửa xong ⇒ **xanh**. Đợt này cũng là nơi phát hiện **B42** (verdict `vanished` có thể sai ⇒ watcher failover job còn sống) |
| `c10-after-20261124/` | đo **sau khi sửa B36/C10** (đường gửi job song song) + đo **C9** trên fleet thật (S48/S49/S49b/S49c **PASSED**) — bằng chứng cho `docs/REPORT.md` §5 (B34, B36) và §7 (C9, C10) |
| `retest-20261124/` | chạy lại **riêng** 4 kịch bản đã sửa trong đợt 24/11 — **S23 / S24 / S28 / S49c ⇒ 4 PASSED** (lần chạy đầu tiên: S23 + S28 **SKIPPED** vì `muse-pmb333` chết giữa chừng — `Connection refused` ở port 2222 ⇒ **hạ tầng**, không phải lỗi `roff`; chạy lại khi cả 4 máy sống ⇒ xanh). Bằng chứng cho `docs/REPORT.md` §5 (B40, B41, B42) |
| `s1-attempt-20261123/` | lần chạy **S1** (skill tự bật ở dự án khác) — phiên Claude Code **hết giờ** (600 s) vì lúc đó fleet đang bận và có lô đo tài nguyên chạy song song ⇒ **hạ tầng**, không phải lỗi skill; script đã sửa để lần sau in ra phần log đã có khi timeout (xem `docs/REPORT.md` §7) |

> **Đợt mới ghi đè `final/`, không trộn với đợt cũ.** `run_acceptance.py` tạo
> thư mục theo tag mà **không** dọn thư mục đã có ⇒ chạy lại `--tag final` sẽ
> **trộn** file của hai đợt. Vì vậy đợt 23/11 đã được tách sang
> `final-20261123/`.
>
> `final/` là đợt nghiệm thu **cuối** (`run_acceptance.py --tag final --skip s1`):
> 2 bước (nghiệm thu + đo tài nguyên) ⇒ `report.json` ghi trạng thái từng bước.
> `acceptance.txt` giữ **toàn bộ** stdout của pytest (không cắt) để
> `make_report_table.py` đọc được kết quả của **mọi** test.
>
> Bước S1 chạy **riêng** (`docs/evidence/s1-attempt-20261123/`) vì nó mở một
> phiên Claude Code **mới** ⇒ tốn token; xem `docs/REPORT.md` §7.

**Fleet hay flap.** Tunnel của các máy Muse rớt giữa chừng, nên cùng một kịch
bản có thể xanh ở lần chạy này và đỏ ở lần sau **vì host chết**, không phải vì
`roff` sai. Đọc `acceptance.txt` kèm `fleet.json` của cùng lần chạy.

Lệnh chạy lại:

```bash
python3 tests/acceptance/run_acceptance.py
python3 tests/acceptance/run_acceptance.py --skip s1        # bỏ kịch bản tốn token
python3 tests/acceptance/run_acceptance.py --tag final      # thư mục tên cố định
```

Bảng kết quả cho `docs/REPORT.md` §4 sinh tự động từ `acceptance.txt`:

```bash
python3 tests/acceptance/make_report_table.py final          # xem trên màn hình
python3 tests/acceptance/make_report_table.py final --md     # ra markdown
python3 tests/acceptance/make_report_table.py final --section # trọn §4 (dán thẳng vào REPORT.md)
```

> **Công cụ sinh bảng cũng có test riêng** (`tests/unit/test_report_table.py`):
> đếm sai số skip thì báo cáo §4 nói dối — mà báo cáo đó là thứ người đọc
> dùng để kết luận "roff còn lỗi hay không". (Đo thật 23/11/2026: lý do skip
> gắn vào kết quả dạng `"SKIPPED — …"` mà bộ đếm tách theo `":"` ⇒ bảng ghi
> **"0 SKIPPED"** trong khi chính bảng liệt kê **8** lần skip.)

> **Bộ nghiệm thu không ghi vào ledger thật của bạn.** Mọi test dùng state dir
> tạm riêng (`ROFF_STATE_DIR`), tự dọn khi kết thúc — chạy nghiệm thu không
> làm phình `~/.local/state/roff/jobs/`.

> **Chạy nghiệm thu một mình.** Không được chạy song song với bộ `pytest`
> khác hay với job `roff` khác trên cùng máy:
>
> * `pytest` giữ **3** thư mục tạm đánh số mới nhất (`/tmp/pytest-<pid>/...`)
>   ⇒ một `pytest` chạy song song sẽ **xoá** thư mục tạm của lần trước ⇒ input
>   của test đang chạy biến mất giữa chừng ⇒ `input_not_found` (đo thật
>   23/11/2026). ⇒ `run_acceptance.py` giờ truyền **`--basetemp` riêng theo tag**
>   (`/tmp/pytest-roff-acceptance-<tag>`) ⇒ cô lập tuyệt đối với mọi lần chạy
>   `pytest` khác;
> * bộ **đo tài nguyên** (`measure.txt`) đếm tiến trình `roff` còn sót ⇒ job
>   `roff` khác đang chạy làm bộ đo báo **FAIL** giả. ⇒ `measure_job.py` giờ
>   **chờ tiến trình ổn định** (tối đa 20 s) trước khi kết luận "rò" — watcher
>   cần vài giây để tự thoát, `sleep 3` cứng có thể báo rò **oán**.
>
> Chạy từng bước riêng:
>
> ```bash
> python3 tests/acceptance/run_acceptance.py --tag final --only acceptance
> python3 tests/acceptance/run_acceptance.py --tag final --only measure
> python3 tests/acceptance/run_acceptance.py --tag final --only s1
> ```

> **Host chết giữa lúc chạy ⇒ test `skip`, không `fail`.** Chỗ chờ job đều đi
> qua `tests/acceptance/_common.py::wait_or_skip()`: nếu job mất vì máy chết
> (`host_restarted`/`host_wiped`/`oom_killed`/`vanished`) thì test **bỏ qua**
> kèm lý do — vì đó là sự cố hạ tầng, không phải lỗi của `roff`
> (nguyên tắc ở `docs/REPORT.md` §2).
>
> **Ngược lại: hạ tầng phải `skip`, không được để đỏ oan.** Đo thật 24/11/2026:
> `S49c` đỏ vì `RoffError 255` lúc launch — `muse-tampt` chết đúng giây đó;
> chạy lại ⇒ **PASS**. ⇒ mọi chỗ gọi ssh phải bắt 255 ⇒ `skip` kèm lý do
> ("host chết lúc …"), **không** để đỏ. Lỗi **khác** 255 (vd 97 = thiếu
> workdir) vẫn đỏ đúng như cũ.
>
> **Kịch bản phải CHỜ trạng thái, không được ĐOÁN trạng thái.** Sau B36/C10,
> `roff do` **không** đẩy input nữa ⇒ **watcher** đẩy ⇒ lúc `do` trả về,
> kho input phía host **có thể chưa** có. `S28` đo kho input ngay lúc đó ⇒
> **đỏ oan ở 2 máy khác nhau** (đợt `final-20261124a/`). Sửa: chờ tối đa 240 s
> (`wait_dir_state()`), rồi mới kết luận. Cùng nguyên tắc với S23: workdir
> phía host **được giữ lại giữa các lần chạy** ⇒ phải dùng `--name` riêng
> cho mỗi lần chạy, nếu không sẽ đo nhầm **rác của lần chạy cũ**.
>
> Ngoài ra còn **hai lớp phòng thủ**:
>
> * **guard tường minh** trong thân từng test — `pick_live()` (chọn máy **đang**
>   sống lúc chạy), `is_host_down()` + `pytest.skip` trước mỗi `assert` phụ
>   thuộc máy remote, `dir_state()` trả **3 trạng thái** (có / không có /
>   *không xác định* — máy chết) ⇒ trạng thái cuối ⇒ skip;
> * **lưới an toàn cuối cùng** — `tests/acceptance/conftest.py` đọc thông điệp
>   lỗi của test; nếu có dấu hiệu tầng ssh/tầng host (`Connection refused`,
>   `No route to host`, `timed out after …`, `rc=255`) thì đổi FAIL thành
>   **SKIP kèm lý do**. Ngưỡng nhận diện **hẹp** và có test riêng
>   (`tests/unit/test_acceptance_guard.py`): kịch bản **cố ý** kiểm tra exit
>   255 (S42) hay báo sai `reason` vẫn đỏ đúng như trước — lưới này **không**
>   được che lỗi thật của `roff`.
