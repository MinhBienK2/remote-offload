# Bằng chứng nghiệm thu

Mỗi lần chạy `tests/acceptance/run_acceptance.py` tạo một thư mục theo thời điểm
(`YYYYMMDD-HHMMSS/`) chứa:

| File | Nội dung |
|---|---|
| `fleet.json` | trạng thái fleet lúc đo: host nào sống, RAM trống, disk, layout_root, có internet không |
| `acceptance.txt` | output đầy đủ của `pytest tests/acceptance -q -m acceptance -rf` |
| `measure.txt` | kết quả bộ đo tài nguyên máy local khi job chạy remote (`docs/SCENARIOS.md` §0.1) |
| `s1.txt` | kết quả kịch bản S1 — skill tự bật ở dự án khác |
| `report.json` | tóm tắt từng bước (rc + thời gian) |

**Fleet hay flap.** Tunnel của các máy Muse rớt giữa chừng, nên cùng một kịch
bản có thể xanh ở lần chạy này và đỏ ở lần sau **vì host chết**, không phải vì
`roff` sai. Đọc `acceptance.txt` kèm `fleet.json` của cùng lần chạy.

Lệnh chạy lại:

```bash
python3 tests/acceptance/run_acceptance.py
python3 tests/acceptance/run_acceptance.py --skip s1        # bỏ kịch bản tốn token
```
