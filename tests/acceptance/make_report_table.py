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
}

#: Kịch bản chạy bằng **script riêng** (không phải pytest).
MANUAL_SCRIPT: dict[str, str] = {
    "S1": "skill tự bật ở dự án khác — `tests/acceptance/s1_skill_activation.py`",
    "S16": "chạy bằng codex — `tests/acceptance/s16_codex_activation.py` (backend codex đang 503)",
}

#: Kịch bản phải **đo thủ công** (không tự động hoá được).
MANUAL: dict[str, str] = {
    "S2": "thiếu tool → tự cài — cài phần mềm lên máy thật nên không tự động hoá",
    "S8": "RESTART THẬT giữa lúc chạy — cần quyền restart máy Muse",
    "S17": "đo token context chính — đo thủ công",
    "S20": "1 máy chết giữa lô — đo thủ công",
    "S21": "split → encode N host → concat — đo thủ công",
    "S22": "item chậm ở cuối lô — đo thủ công",
}


def parse(text: str) -> dict[str, str]:
    """Kết quả từng test: id → "PASSED" | "FAILED" | "SKIPPED" | "ERROR"."""
    out: dict[str, str] = {}
    # Dòng "PASSED tests/...::test_x"
    for line in text.splitlines():
        line = line.strip()
        m = re.match(r"^(PASSED|FAILED|SKIPPED|ERROR|XFAIL|XPASS)\s+(\S+)", line)
        if m:
            status, node = m.group(1), m.group(2)
            tid = node.split("::")[-1]
            out[tid] = status
    # Dòng "FAILED tests/...::test_x - Error: ..."  (đã khớp ở trên)
    for line in text.splitlines():
        m = re.match(r"^(FAILED|ERROR)\s+(\S+)\s+-\s+(.*)$", line.strip())
        if m:
            out[m.group(2).split("::")[-1]] = f"{m.group(1)}: {m.group(3)[:80]}"
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("tag")
    ap.add_argument("--md", action="store_true", help="in bảng markdown")
    args = ap.parse_args()

    path = EVIDENCE / args.tag / "acceptance.txt"
    if not path.is_file():
        print(f"không có {path}", file=sys.stderr)
        return 2
    results = parse(path.read_text(encoding="utf-8", errors="replace"))

    rows = []
    for tid, name in NAMES.items():
        status = results.get(tid, "—")
        rows.append((name.split(" — ")[0], name, status))
    for num, note in MANUAL_SCRIPT.items():
        rows.append((num, f"{num} — {note}", "script riêng"))
    for num, note in MANUAL.items():
        rows.append((num, f"{num} — {note}", "thủ công"))

    rows.sort(key=lambda r: (int(r[0].lstrip("S").split("b")[0] or 0), r[0]))

    if args.md:
        print("| # | Kịch bản | Kết quả |")
        print("|---|---|---|")
        for _num, name, status in rows:
            print(f"| {name.split(' — ', 1)[0]} | {name.split(' — ', 1)[1]} | {status} |")
    else:
        for num, name, status in rows:
            print(f"{num:6} {status:20} {name}")
    print(f"\n({len(results)} test có kết quả; "
          f"{len(rows)} kịch bản trong danh mục)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
