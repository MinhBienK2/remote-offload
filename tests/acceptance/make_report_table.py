#!/usr/bin/env python3
"""Biến output pytest nghiệm thu thành bảng markdown cho `docs/REPORT.md` §4.

Đọc `docs/evidence/<tag>/acceptance.txt` (do `run_acceptance.py` ghi ra) và in
bảng: | # | kịch bản | kết quả | ghi chú |.

Dùng:
    python3 tests/acceptance/make_report_table.py final
    python3 tests/acceptance/make_report_table.py final --md   # in ra markdown
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
EVIDENCE = REPO / "docs" / "evidence"

#: Tên kịch bản (theo `docs/SCENARIOS.md`) cho từng test id.
NAMES: dict[str, str] = {
    "test_s02_job_lifecycle_on_real_host": "S02 — vòng đời job trên host thật",
    "test_s02b_job_failure_exit_code_on_real_host": "S02b — job fail ⇒ exit 1",
    "test_s03_ffmpeg_transcode_artifacts": "S03 — ffmpeg 500 MB",
    "test_s04_url_input_prefers_internet_host": "S04 — input là URL",
    "test_s05_insufficient_ram_never_runs_local": "S05 — RAM không đủ",
    "test_s06_resume_partial_transfer_on_real_host": "S06 — đứt lúc push",
    "test_s07_tunnel_drop_does_not_failover": "S07 — đứt tunnel, máy vẫn sống",
    "test_s09_side_effects_never_retried": "S09 — `--side-effects` không retry",
    "test_s10_job_survives_agent_death": "S10 — kill agent giữa lúc chạy",
    "test_s11_shards_spread_over_hosts": "S11 — `--in job:` + `--shards`",
    "test_s12_tunnel_serves_and_stops_cleanly": "S12 — BE + FE qua tunnel",
    "test_s13_all_hosts_dead": "S13 — mọi host đều chết",
    "test_s13_all_hosts_dead_never_runs_local": "S13b — mọi host chết ⇒ không chạy local",
    "test_s14_secret_never_leaks": "S14 — secret không lộ",
    "test_s14_secret_not_in_remote_argv": "S14b — secret không nằm trong argv remote",
    "test_s15_stale_master_does_not_kill_foreign_pid": "S15 — master ssh cũ giữ port",
    "test_s18_parallel_speedup": "S18 — 20 video trên cả fleet",
    "test_s19_three_sessions_share_the_fleet": "S19 — 3 phiên agent cùng lúc",
    "test_s23_input_pushed_once_per_host": "S23 — input đẩy 1 lần cho mỗi host",
    "test_s24_failover_keeps_item_and_workdir": "S24 — failover giữ item & thư mục",
    "test_s25_leaked_slot_does_not_block_host_forever": "S25 — slot rò không kẹt host",
    "test_s26_queued_job_auto_launches_when_slot_frees": "S26 — job xếp hàng tự chạy",
    "test_s27_state_files_are_private": "S27 — file state không đọc được của user khác",
    "test_s28_gc_cleans_host_input_store_but_not_running_jobs": "S28 — `roff gc` dọn kho input",
    "test_s29_error_exit_codes_are_not_confused": "S29 — exit code phân biệt đúng",
    "test_s30_job_routed_to_host_that_has_the_tool": "S30 — job giao cho máy có tool",
    "test_s31_ssh_actually_uses_replacement_ssh_config": "S31 — `ROFF_SSH_CONFIG` tới tay ssh",
    "test_s31b_without_replacement_config_alias_is_unknown": "S31b — mốc đối chiếu",
    "test_s32_broken_ledger_does_not_wipe_the_slot_queue": "S32 — ledger hỏng không dọn hàng đợi",
    "test_s33_tunnel_link_end_to_end_on_real_fleet": "S33 — `tunnel link` nối 2 máy thật",
    "test_s33b_tunnel_link_reports_remote_not_listening": "S33b — B không LISTEN ⇒ không mở forward",
    "test_s33c_tunnel_list_shows_then_hides_the_link": "S33c — `tunnel list` ẩn link đã stop",
    "test_s34_doctor_reports_json_and_exit_code": "S34 — `roff doctor` JSON + exit code",
    "test_s34b_doctor_does_not_write_anything": "S34b — `doctor` chỉ báo, không ghi file",
    "test_s34c_doctor_default_does_not_probe_network": "S34c — `doctor` mặc định không probe mạng",
    "test_s35_gc_removes_legacy_and_old_terminal_jobs": "S35 — `roff gc` dọn job ma",
    "test_s35b_legacy_job_is_not_held_as_running": "S35b — job định dạng cũ không giữ slot",
    "test_s35c_wait_on_legacy_job_returns_immediately": "S35c — `wait` job định dạng cũ không treo",
    "test_s36_changing_ssh_config_does_not_reuse_the_old_master": "S36 — đổi ssh config ⇒ không nói chuyện với máy cũ",
    "test_s37_infers_tool_from_command": "S37 — tự suy ra tool từ câu lệnh",
    "test_s37_explicit_need_is_not_guessed_away": "S37b — `--need` vẫn là tiêu chí cứng",
    "test_s38_inferred_tool_never_rejects_or_installs": "S38 — suy ra là mềm: không loại host, không cài tool",
    "test_s39_builtin_command_infers_nothing": "S39 — `echo` không suy ra tool nào",
    "test_s39_waits_for_inferred_job_to_finish": "S39b — job dùng tool suy ra vẫn kết thúc",
    "test_s40_orphan_shared_lock_is_cleaned_and_job_runs": "S40 — kho input bị kẹt thì tự dọn, không treo",
    "test_s41_missing_local_input_ends_with_clear_reason": "S41 — input local mất ⇒ lý do rõ ràng",
    "test_s42_pinned_host_unreachable_fails_loud_without_failover": "S42 — host ghim không với được ⇒ fail to",
    "test_s43_gc_removes_orphan_lock_but_keeps_live_lock": "S43 — gc dọn lock mồ côi, giữ lock còn sống",
    "test_s45_pull_failure_is_reported_not_swallowed": "S45 — pull hỏng ⇒ báo lỗi, không báo thành công",
    "test_s46_sync_back_pulls_file_into_project": "S46 — `--sync-back` thực sự kéo file về project",
    "test_s47_missing_config_fails_loud_and_creates_no_job": "S47 — config chỉ định mà thiếu ⇒ fail to, không tạo job",
    "test_s47_config_present_keeps_fleet_restricted": "S47b — config có thật ⇒ `exclude` được giữ",
}

#: Kịch bản chạy bằng **script riêng** (không phải pytest).
MANUAL_SCRIPT: dict[str, str] = {
    "S1": "skill tự bật ở dự án khác — `tests/acceptance/s1_skill_activation.py`",
    "S16": "chạy bằng codex — `tests/acceptance/s16_codex_activation.py` (backend codex đang 503)",
}

#: Kịch bản đã **đo thật** nhưng **không** phải bằng một test pytest riêng —
#: sự cố xảy ra **tự nhiên** giữa lúc chạy và được ghi lại đầy đủ vào ledger.
MEASURED: dict[str, str] = {
    "S20": "1 máy chết giữa lô ⇒ chuyển sang máy khác — đo thật 23/11/2026 "
            "(`docs/evidence/failover-live-20261123/`)",
}


#: Kịch bản phải **đo thủ công** (không tự động hoá được).
MANUAL: dict[str, str] = {
    "S2": "thiếu tool → tự cài — cài phần mềm lên máy thật nên không tự động hoá",
    "S8": "RESTART THẬT giữa lúc chạy — cần quyền restart máy Muse",
    "S17": "đo token context chính — đo thủ công",
    "S21": "split → encode N host → concat — đo thủ công",
    "S22": "item chậm ở cuối lô — đo thủ công",
}


def parse(text: str) -> dict[str, str]:
    """Kết quả từng test: id → "PASSED" | "FAILED" | "SKIPPED" | "ERROR".

    Chấp nhận **cả hai** thứ tự của pytest:
      * ``PASSED tests/x.py::test_y``   (dạng ``-rA``/``--tb``)
      * ``tests/x.py::test_y PASSED``   (dạng ``-v``)
    """
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        # "PASSED tests/...::test_x"
        m = re.match(r"^(PASSED|FAILED|SKIPPED|ERROR|XFAIL|XPASS)\s+(\S+)", line)
        if m:
            status, node = m.group(1), m.group(2)
            # Dòng **tóm tắt** của pytest ("SKIPPED [1] file:0: lý do") **không**
            # phải test nào — bỏ qua, nếu không nó sẽ lọt vào kết quả như một
            # test ma (và làm lệch số lượng skip ⇒ mất lý do).
            if node.startswith("["):
                continue
            out[node.split("::")[-1]] = status
            continue
        # "tests/...::test_x PASSED"
        m = re.match(r"^(\S+::\S+)\s+(PASSED|FAILED|SKIPPED|ERROR|XFAIL|XPASS)\b", line)
        if m:
            out[m.group(1).split("::")[-1]] = m.group(2)
            continue
        # "FAILED tests/...::test_x - Error: ..."  (đã khớp ở trên)
        m = re.match(r"^(FAILED|ERROR)\s+(\S+)\s+-\s+(.*)$", line)
        if m:
            out[m.group(2).split("::")[-1]] = f"{m.group(1)}: {m.group(3)[:80]}"
    return out


#: Dòng tóm tắt của pytest: ``SKIPPED [1] <file>:<line>: <lý do>``.
#:
#: pytest in ra **lý do skip** ở "short test summary info" — nhưng **không**
#: kèm id của test ⇒ phải **zip theo thứ tự** với danh sách test bị skip
#: (thứ tự chạy của pytest ổn định và giống nhau ở cả hai danh sách).
SKIP_REASON_RE = re.compile(r"^SKIPPED \[\d+\] \S+:\d+: (.*)$")


def parse_skip_reasons(text: str) -> list[str]:
    """Lý do của các lần skip, **theo thứ tự** xuất hiện trong bằng chứng."""
    reasons: list[str] = []
    for line in text.splitlines():
        m = SKIP_REASON_RE.match(line.strip())
        if m:
            reasons.append(m.group(1).strip())
    return reasons


def attach_skip_reasons(text: str, results: dict[str, str]) -> dict[str, str]:
    """Gắn lý do skip vào kết quả: ``SKIPPED — <lý do>``.

    Không khớp số lượng ⇒ **không** đoán bừa: giữ nguyên ``SKIPPED`` không lý do.
    """
    reasons = parse_skip_reasons(text)
    if not reasons:
        return results
    skipped_ids = [tid for tid, st in results.items() if st == "SKIPPED"]
    if len(skipped_ids) != len(reasons):
        return results
    out = dict(results)
    for tid, reason in zip(skipped_ids, reasons):
        out[tid] = "SKIPPED — " + reason
    return out


def _num_key(num: str) -> int:
    """Số thứ tự của mã kịch bản: ``S2``→2, ``S02b``→2, ``S33c``→33.

    Trước đây dùng ``split("b")`` ⇒ ``"33c"`` không parse được ⇒ **crash**
    đúng lúc cần lấy bảng §4 (đo thật 23/11/2026).
    """
    m = re.match(r"\d+", num.lstrip("S"))
    return int(m.group()) if m else 0


def _sort_key(num: str):
    """Thứ tự hiển thị: theo số, rồi hậu tố (S02 → S02b → S02c)."""
    m = re.match(r"\d+", num.lstrip("S"))
    n = int(m.group()) if m else 0
    suffix = num.lstrip("S")[len(m.group()):] if m else num
    return (n, suffix, num)


def _fleet_line(tag: str) -> str:
    """Một dòng tóm tắt trạng thái fleet lúc đo (từ ``fleet.json``)."""
    path = EVIDENCE / tag / "fleet.json"
    if not path.is_file():
        return "- Fleet: **không có** `fleet.json`"
    try:
        snap = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "- Fleet: **không đọc được** `fleet.json`"
    hosts = snap.get("hosts") or []
    alive = [h for h in hosts if h.get("reachable")]
    mems = [int(h.get("mem_available_mb") or 0) for h in alive]
    avg = round(sum(mems) / len(mems)) if mems else 0
    return (f"- Fleet lúc đo: **{len(alive)}/{len(hosts)}** máy sống"
            + (f", RAM trống trung bình **{avg} MB**" if mems else "")
            + " — xem `fleet.json`")


def _counts(results: dict[str, str]) -> tuple[int, int, int, int]:
    """(PASSED, FAILED, SKIPPED, khác)."""
    passed = failed = skipped = other = 0
    for status in results.values():
        head, _note = _split_status(status)
        head = head.upper()
        if head == "PASSED":
            passed += 1
        elif head == "FAILED":
            failed += 1
        elif head == "SKIPPED":
            skipped += 1
        else:
            other += 1
    return passed, failed, skipped, other


def _split_status(status: str) -> tuple[str, str]:
    """Tách kết quả thành ``("PASSED"|"SKIPPED"|…, lý do)``.

    Có **hai** kiểu kết quả có lý do:
      * ``"SKIPPED — <lý do>"``  (do `attach_skip_reasons` gắn vào)
      * ``"FAILED: <thông điệp>"`` (dòng ``FAILED … - …`` của pytest)

    Trước đây chỉ tách theo ``" — "`` ⇒ ``"FAILED: …"`` bị giữ nguyên, và
    `_counts` tách theo ``":"`` ⇒ ``"SKIPPED — …"`` **không** còn là ``SKIPPED``
    ⇒ bảng hiện "0 SKIPPED" trong khi chính bảng liệt kê 8 lần skip
    (đo thật 23/11/2026). Giờ tách theo ``" — "`` **trước**, rồi mới tới ``":"``.
    """
    if " — " in status:
        head, tail = status.split(" — ", 1)
        return head.strip(), tail.strip()
    if ":" in status:
        head, tail = status.split(":", 1)
        return head.strip(), tail.strip()
    return status.strip(), ""


#: Ký tự ``|`` phá vỡ bảng markdown ⇒ phải escape.
#: Lý do skip có thể rất dài (S12 in ra nguyên lệnh ssh) ⇒ cắt bớt, bản đầy
#: đủ vẫn nằm trong `acceptance.txt`.
NOTE_MAX = 200


def _note_cell(note: str) -> str:
    """Lý do (ô "Ghi chú") ⇒ ô markdown an toàn: 1 dòng, không `|`, có độ dài.

    Escape ``|`` **trước** khi cắt ⇒ ô luôn ≤ ``NOTE_MAX + 1`` ký tự (nếu
    escape sau, mỗi ``|`` thêm 1 ký tự ⇒ độ dài vượt ngưỡng).
    """
    text = " ".join((note or "").split()).replace("|", "\\|")
    if len(text) > NOTE_MAX:
        text = text[:NOTE_MAX].rstrip() + "…"
    return text


def _section(tag: str, results: dict[str, str], rows: list) -> str:
    """Cả §4 của `docs/REPORT.md` — tự sinh từ bằng chứng."""
    passed, failed, skipped, other = _counts(results)
    out = [
        f"Đợt nghiệm thu cuối — bằng chứng đầy đủ: `docs/evidence/{tag}/`.",
        "",
        _fleet_line(tag),
        f"- Kết quả pytest: **{passed} PASSED**, **{failed} FAILED**, "
        f"**{skipped} SKIPPED**"
        + (f", {other} kết quả khác" if other else "")
        + f" (tổng {len(results)} test có kết quả).",
        "- Test `SKIPPED` = **sự cố hạ tầng** (máy Muse chết giữa lúc chạy), "
        "không phải lỗi của `roff` — xem `docs/REPORT.md` §2. Lý do cụ thể của "
        "từng lần skip nằm ở cột *Ghi chú*.",
        "",
        "| # | Kịch bản | Kết quả | Ghi chú |",
        "|---|---|---|---|",
    ]
    for _num, name, status in rows:
        head, note = _split_status(status)
        out.append(f"| {name.split(' — ', 1)[0]} | {name.split(' — ', 1)[1]} | "
                   f"{head} | {_note_cell(note)} |")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("tag")
    ap.add_argument("--md", action="store_true", help="in bảng markdown")
    ap.add_argument("--section", action="store_true",
                    help="in trọn §4 của docs/REPORT.md (tiêu đề + bảng)")
    args = ap.parse_args()

    path = EVIDENCE / args.tag / "acceptance.txt"
    if not path.is_file():
        print(f"không có {path}", file=sys.stderr)
        return 2
    text = path.read_text(encoding="utf-8", errors="replace")
    results = attach_skip_reasons(text, parse(text))

    rows = []
    for tid, name in NAMES.items():
        status = results.get(tid, "—")
        rows.append((name.split(" — ")[0], name, status))
    for num, note in MANUAL_SCRIPT.items():
        rows.append((num, f"{num} — {note}", "script riêng"))
    for num, note in MEASURED.items():
        rows.append((num, f"{num} — {note}", "đã đo (bằng chứng)"))
    for num, note in MANUAL.items():
        rows.append((num, f"{num} — {note}", "thủ công"))

    rows.sort(key=lambda r: (_num_key(r[0]), r[0]))

    if args.section:
        print(_section(args.tag, results, rows))
    elif args.md:
        print("| # | Kịch bản | Kết quả | Ghi chú |")
        print("|---|---|---|---|")
        for _num, name, status in rows:
            head, note = _split_status(status)
            print(f"| {name.split(' — ', 1)[0]} | {name.split(' — ', 1)[1]} | "
                  f"{head} | {_note_cell(note)} |")
    else:
        for num, name, status in rows:
            print(f"{num:6} {status:20} {name}")
    print(f"\n({len(results)} test có kết quả; "
          f"{len(rows)} kịch bản trong danh mục)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
