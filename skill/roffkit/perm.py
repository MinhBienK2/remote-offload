"""Quyền truy cập cho **file state** của roff (chứa dữ liệu người dùng).

Đo thật (23/11/2026): ``~/.local/state/roff/audit.jsonl`` từng là mode **0664**
⇒ mọi user trên máy đọc được lệnh đã chạy (có thể chứa tên file, path, tên
project). ``probes.json``/``slots.json`` cũng vậy.

Quy tắc:

* **file** state (chứa dữ liệu người dùng) ⇒ mode **0600**;
* **thư mục** chứa chúng ⇒ mode **0700**.

Module này không in ra stdout (quy tắc chung của roff).
"""

from __future__ import annotations

import os

__all__ = [
    "FILE_MODE",
    "DIR_MODE",
    "secure_file",
    "secure_dir",
    "normalize_tree",
]

FILE_MODE = 0o600
"""Mode của file state chứa dữ liệu người dùng."""

DIR_MODE = 0o700
"""Mode của thư mục chứa file state."""


def secure_file(path: str) -> None:
    """Đặt mode 0600 cho file (bỏ qua lỗi — không để lỗi quyền làm hỏng lệnh)."""
    if not path:
        return
    try:
        os.chmod(path, FILE_MODE)
    except OSError:
        pass


def secure_dir(path: str) -> None:
    """Đặt mode 0700 cho thư mục (bỏ qua lỗi)."""
    if not path:
        return
    try:
        os.chmod(path, DIR_MODE)
    except OSError:
        pass


def normalize_tree(root: str) -> int:
    """Chuẩn hoá quyền cho **cả cây** state: file 0600, thư mục 0700.

    Dùng để nâng quyền file đã tồn tại từ trước (mode 0664 do umask của máy
    tạo ra). Trả số entry đã chuẩn hoá.

    An toàn: chỉ đổi quyền, không đụng nội dung; lỗi nào cũng bỏ qua.
    """
    if not root or not os.path.isdir(root):
        return 0
    changed = 0
    secure_dir(root)
    for dirpath, dirnames, filenames in os.walk(root):
        secure_dir(dirpath)
        changed += 1
        for name in filenames:
            full = os.path.join(dirpath, name)
            try:
                mode = os.stat(full).st_mode & 0o777
            except OSError:
                continue
            if mode != FILE_MODE:
                secure_file(full)
                changed += 1
        dirnames[:] = list(dirnames)
    return changed
