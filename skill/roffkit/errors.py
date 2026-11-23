"""Hợp đồng exit code + payload JSON cho toàn bộ roff.

Bất biến (xem docs/API-CONTRACT.md §2):
  * mọi lệnh in đúng **1 JSON object** ra stdout rồi `exit(code)`;
  * JSON luôn có `ok` (bool); lỗi luôn có `error` hoặc `reason`;
  * exit 255 (không với được host) **luôn** kèm `detail` (text lỗi ssh gốc).

Module này không tự in gì ra stdout ngoài hàm :func:`emit`.
"""

from __future__ import annotations

import json
import sys
from typing import Any

EXIT_OK = 0
"""Thành công."""

EXIT_JOB_FAILED = 1
"""Job đã chạy và fail (không retry mù)."""

EXIT_INPUT = 2
"""Input sai, chưa có gì chạy."""

EXIT_CONFIG = 3
"""Config sai / không đọc được."""

EXIT_NO_HOST = 10
"""Không còn host nào dùng được (hết slot / hết fleet)."""

EXIT_JOB_LOST = 69
"""Job mất: restart / wipe / OOM / vanished."""

EXIT_NO_WORKDIR = 97
"""Workdir không tồn tại trên host."""

EXIT_TIMEOUT = 124
"""Wait hết giờ — KHÔNG phải lỗi, job vẫn chạy."""

EXIT_CANCELLED = 130
"""Bị huỷ có chủ đích."""

EXIT_UNREACHABLE = 255
"""Không với được host."""

REASON_RESTARTED = "host_restarted"
REASON_WIPED = "host_wiped"
REASON_OOM = "oom_killed"
REASON_VANISHED = "vanished"


class RoffError(Exception):
    """Mang exit code + payload JSON. ``cli.main()`` bắt và in ra.

    :param code: exit code theo bảng trong API-CONTRACT §2.
    :param reason: mã lý do ngắn (vd ``host_restarted``), hoặc chuỗi rỗng.
    :param fields: thêm khoá tuỳ ý vào payload (vd ``error=``, ``detail=``).
    """

    def __init__(self, code: int, reason: str = "", **fields: Any) -> None:
        super().__init__(reason or f"exit {code}")
        self.code = code
        self.reason = reason
        self.fields = fields

    def payload(self) -> dict:
        """Trả payload JSON chuẩn của lỗi.

        Luôn có ``ok: False`` và ``exit_code``; có ``reason`` nếu có, ``error`` nếu
        có; mọi khoá trong ``fields`` được gộp thêm vào. Với ``code == 255``
        (``EXIT_UNREACHABLE``) ``detail`` là bắt buộc — nếu không có sẽ tự sinh.
        """
        payload: dict = {"ok": False}
        if self.reason:
            payload["reason"] = self.reason
        error = self.fields.get("error")
        if error is not None:
            payload["error"] = error if isinstance(error, str) else str(error)
        payload["exit_code"] = self.code
        for key, value in self.fields.items():
            if key == "error":
                continue
            payload[key] = value
        if self.code == EXIT_UNREACHABLE and not payload.get("detail"):
            payload["detail"] = self.reason or f"exit {self.code}"
        return payload


def emit(obj: dict, code: int = 0) -> None:
    """In ``obj`` thành 1 JSON object ra stdout rồi ``sys.exit(code)``.

    Dùng ``ensure_ascii=False`` (giữ chữ tiếng Việt) và ``sort_keys=False``
    (giữ thứ tự khoá do caller dựng).
    """
    print(json.dumps(obj, ensure_ascii=False, sort_keys=False))
    sys.exit(code)
