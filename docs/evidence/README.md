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
| `final/` | đợt nghiệm thu **cuối** (23/11/2026) — bằng chứng dùng cho `docs/REPORT.md` §4 |
| `s1-attempt-20261123/` | lần chạy **S1** (skill tự bật ở dự án khác) — phiên Claude Code **hết giờ** (600 s) vì lúc đó fleet đang bận và có lô đo tài nguyên chạy song song ⇒ **hạ tầng**, không phải lỗi skill; script đã sửa để lần sau in ra phần log đã có khi timeout (xem `docs/REPORT.md` §7) |

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
