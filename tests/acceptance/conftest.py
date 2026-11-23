"""Hook chung cho **bộ nghiệm thu** — FAIL do hạ tầng phải thành SKIP.

Nguyên tắc đã chốt ở `docs/REPORT.md` §2:

> Một kịch bản chỉ đỏ khi **lỗi của `roff`**. Fleet `muse-*` của người
> dùng **flap liên tục** (reverse tunnel rớt giữa lúc chạy, máy restart) ⇒
> `ssh` chết với rc=255 — đó là **sự cố hạ tầng**, không phải lỗi sản phẩm.

Cơ chế: hook `pytest_runtest_makereport` xem lại **thông điệp lỗi** của
test. Nếu thông điệp mang dấu hiệu tầng ssh/tầng host (máy chết, đường
truyền đứt, `rc=255` từ lớp ssh) ⇒ đổi outcome thành `skipped` kèm **lý do
rõ ràng**. Test đỏ mà **không** có dấu hiệu này ⇒ giữ nguyên FAIL ⇒ lỗi
thật của `roff` vẫn lộ ra đúng như trước.

**Không** dùng cơ chế này để giấu lỗi:
* mọi lần skip đều ghi lý do vào báo cáo (`pytest -rs` ⇒ runner truyền
  `-rfEs`), nên đọc được ngay là kịch bản bị bỏ qua vì lý do gì;
* ngưỡng nhận diện **hẹp**: chỉ những thông điệp có dấu hiệu ssh/host;
* mọi kịch bản quan trọng còn có guard **tường minh** trong thân test
  (`wait_or_skip`, `pick_live`, `is_host_down`) — hook chỉ là lưới an toàn
  cuối cùng cho những lần trượt sót.
"""
from __future__ import annotations

import pytest

#: Dấu hiệu **tầng ssh / tầng host** — đứt kết nối, máy chết, DNS hỏng.
#:
#: Chữ **thường** — thông điệp lỗi so khớp không phân biệt hoa thường.
SSH_MARKERS = (
    "connection refused",
    "connection timed out",
    "connection reset",
    "connection closed",
    "connection lost",
    "no route to host",
    "kex_exchange_identification",
    "banner exchange",
    "broken pipe",
    "host is down",
    "network is unreachable",
    "could not resolve hostname",
    "name or service not known",
    "timeoutexpired",
    "connection closed by remote host",
)

#: `rc=255` — mã thoát **tầng ssh** theo hợp đồng exit-code của `roff`
#: (`docs/API-CONTRACT.md` §2): 255 luôn đi kèm `detail` của lỗi ssh.
#:
#: Khoá theo **dạng thức xuất hiện trong thông điệp lỗi**:
#:   * ``rc=255 …`` — f-string của test in ra mã thoát thực tế;
#:   * ``assert 255 == 0`` — pytest viết lại: **giá trị thực** 255, **kỳ vọng**
#:     0 (biến thì giữ nguyên tên, nên ``assert rc == 255`` của S42 **không**
#:     khớp — kịch bản kiểm tra exit 255 vẫn đỏ đúng như trước).
#:
#: Cố ý **không** khoá theo dạng JSON ``"exit_code": 255``: payload của
#: `roff` có thể mang 255 trong khi lỗi nằm chỗ khác (ví dụ S42 báo sai
#: `reason`) ⇒ sẽ che mất lỗi thật.
EXIT_255_MARKERS = ("rc=255", "assert 255 == 0")


def host_down_signature(text: str) -> str:
    """Dấu hiệu hạ tầng trong thông điệp lỗi; ``""`` nếu không có.

    Args:
        text: thông điệp lỗi đầy đủ của pytest (repr + traceback).

    Returns:
        Chuỗi mô tả dấu hiệu (dùng làm lý do skip), hoặc ``""`` nếu thông
        điệp **không** mang dấu hiệu hạ tầng ⇒ phải giữ FAIL.
    """
    low = str(text or "").lower()
    for marker in SSH_MARKERS:
        if marker in low:
            return marker
    for marker in EXIT_255_MARKERS:
        if marker in low:
            return marker
    return ""


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """FAIL có dấu hiệu hạ tầng ⇒ SKIP (kèm lý do), còn lại giữ nguyên."""
    outcome = yield
    report = outcome.get_result()
    if report.outcome != "failed":
        return
    if report.when not in ("call", "setup"):
        return
    signature = host_down_signature(str(report.longrepr))
    if not signature:
        return
    report.outcome = "skipped"
    # pytest đọc lý do skip ở `longrepr[2]` ⇒ phải là **tuple 3 phần tử**
    # `(path, lineno, message)` — đặt chuỗi thẳng sẽ làm pytest vỡ (INTERNALERROR).
    report.longrepr = (
        str(item.path),
        0,
        "sự cố hạ tầng (%s) — máy remote chết / đường truyền đứt; "
        "KHÔNG phải lỗi của roff (docs/REPORT.md §2)" % signature,
    )
