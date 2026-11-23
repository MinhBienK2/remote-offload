"""Unit test cho `tests/acceptance/make_report_table.py` (công cụ sinh §4).

Bộ nghiệm thu phải **tự kiểm tra bộ đếm của chính nó**: nếu `make_report_table`
đếm sai thì `docs/REPORT.md` §4 nói dối — và người đọc báo cáo đó là người
duy nhất quyết định "roff còn lỗi hay không".

Case thật đã gặp (23/11/2026): lý do skip được gắn vào kết quả dưới dạng
``"SKIPPED — <lý do>"`` nhưng `_counts` tách theo ``":"`` ⇒ **"0 SKIPPED"**
trong khi chính bảng liệt kê 8 lần skip.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tests" / "acceptance" / "make_report_table.py"


def _load():
    spec = importlib.util.spec_from_file_location("make_report_table", TOOL)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["make_report_table"] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load()


#: Mẫu pytest -v + short summary info (-rfEs) rút gọn.
SAMPLE = """
============================= test session starts ==============================
tests/acceptance/test_acceptance_fleet.py::test_s02_job_lifecycle_on_real_host PASSED
tests/acceptance/test_acceptance_fleet.py::test_s03_ffmpeg_transcode_artifacts SKIPPED
tests/acceptance/test_acceptance_fleet.py::test_s09_side_effects_never_retried SKIPPED
tests/acceptance/test_acceptance_fleet.py::test_s28_gc_cleans_host_input_store FAILED
tests/acceptance/test_acceptance_fleet.py::test_s29_error_exit_codes_are_not_confused PASSED

=========================== short test summary info ============================
SKIPPED [1] tests/acceptance/test_acceptance_fleet.py:12: sự cố hạ tầng (connection refused)
SKIPPED [1] tests/acceptance/test_acceptance_fleet.py:30: máy remote chết lúc gửi job — sự cố hạ tầng:
FAILED [1] tests/acceptance/test_acceptance_fleet.py:44: assert 0 == 1
========================= 2 passed, 1 failed, 2 skipped in 12.34s =====================
"""


def test_parse_reads_verbose_statuses():
    results = mod.parse(SAMPLE)
    assert results["test_s02_job_lifecycle_on_real_host"] == "PASSED"
    assert results["test_s28_gc_cleans_host_input_store"] == "FAILED"
    assert results["test_s03_ffmpeg_transcode_artifacts"] == "SKIPPED"


def test_parse_ignores_pytest_summary_lines():
    """Dòng ``SKIPPED [1] file:line: lý do`` **không** phải là một test.

    Nếu nuốt vào kết quả ⇒ bảng §4 có thêm một "test ma" và số skip lệch.
    """
    results = mod.parse(SAMPLE)
    assert "[1]" not in results
    assert len(results) == 5


def test_skip_reasons_attached_in_order():
    results = mod.attach_skip_reasons(SAMPLE, mod.parse(SAMPLE))
    assert results["test_s03_ffmpeg_transcode_artifacts"].startswith("SKIPPED — ")
    assert "connection refused" in results["test_s03_ffmpeg_transcode_artifacts"]
    assert "máy remote chết" in results["test_s09_side_effects_never_retried"]


def test_counts_skip_when_reason_present():
    """**Case hồi quy (23/11/2026)**: lý do có dấu ``:`` vẫn phải tính là SKIP."""
    results = mod.attach_skip_reasons(SAMPLE, mod.parse(SAMPLE))
    passed, failed, skipped, other = mod._counts(results)
    assert (passed, failed, skipped, other) == (2, 1, 2, 0)


def test_counts_split_status_handles_both_separators():
    assert mod._split_status("SKIPPED — lý do") == ("SKIPPED", "lý do")
    assert mod._split_status("FAILED: thông điệp") == ("FAILED", "thông điệp")
    assert mod._split_status("PASSED") == ("PASSED", "")


def test_note_cell_is_one_line_and_escapes_pipe():
    note = "dòng 1\ndòng 2 | có dấu |  " + "x" * 500
    cell = mod._note_cell(note)
    assert "\n" not in cell
    assert "|" not in cell.replace("\\|", "")
    assert len(cell) <= mod.NOTE_MAX + 1


def test_mismatch_between_skips_and_reasons_does_not_guess():
    """Lệch số lượng ⇒ **không** đoán bừa ⇒ giữ "SKIPPED" trần."""
    text = SAMPLE.replace(
        "SKIPPED [1] tests/acceptance/test_acceptance_fleet.py:30: "
        "máy remote chết lúc gửi job — sự cố hạ tầng:\n", "")
    results = mod.attach_skip_reasons(text, mod.parse(text))
    assert results["test_s09_side_effects_never_retried"] == "SKIPPED"


@pytest.mark.parametrize("reason", [
    "sự cố hạ tầng (connection refused) — máy remote chết",
    "máy remote chết lúc gửi job — sự cố hạ tầng:",
    "không lấy được layout_root của host",
])
def test_table_row_is_valid_markdown(reason):
    """Mỗi dòng bảng phải có đúng 4 ô (3 dấu `|`)."""
    results = {"test_x": "SKIPPED — " + reason}
    rows = [("S99", "S99 — ví dụ", results["test_x"])]
    section = mod._section("final", results, rows)
    row = [ln for ln in section.splitlines() if ln.startswith("| S99")][0]
    assert row.count("|") == 5  # 4 ô ⇒ 5 dấu "|" kể cả 2 đầu


# --------------------------------------------------------------------------
# Danh mục §4 không được **rơi** khỏi đồng nghĩa với bộ test và tài liệu
# --------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parents[2]


def _catalogue_ids() -> list[str]:
    ids = [name.split(" — ")[0] for name in mod.NAMES.values()]
    ids += list(mod.MANUAL_SCRIPT)
    ids += list(mod.MEASURED)
    ids += list(mod.MANUAL)
    return ids


def test_catalogue_matches_acceptance_tests():
    """Mỗi mục trong danh mục §4 phải là **một test thật** trong `tests/acceptance/`.

    Nếu một test bị đổi tên mà danh mục không cập nhật ⇒ bảng §4 hiện dòng
    trống (``—``) ⇒ báo cáo **âm thầm** bỏ sót kịch bản.
    """
    source = ""
    for path in sorted((ROOT / "tests" / "acceptance").glob("test_*.py")):
        source += path.read_text(encoding="utf-8")
    defined = set(re.findall(r"^def (test_\w+)", source, re.MULTILINE))
    assert not [k for k in mod.NAMES if k not in defined], (
        "danh mục §4 có mục không còn test tương ứng")
    assert not [t for t in defined if t not in mod.NAMES], (
        "có test trong tests/acceptance mà danh mục §4 không liệt kê")


def test_catalogue_ids_all_documented_in_scenarios():
    """Mọi mã kịch bản phải xuất hiện trong `docs/SCENARIOS.md`."""
    doc = (ROOT / "docs" / "SCENARIOS.md").read_text(encoding="utf-8")
    missing = [i for i in _catalogue_ids()
               if not re.search(r"\b%s\b" % re.escape(i), doc)]
    assert not missing, f"mã kịch bản chưa có trong SCENARIOS.md: {missing}"
