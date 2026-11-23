#!/usr/bin/env python3
"""Chạy 1 job `roff do` **thật** kèm bộ đo tài nguyên máy local.

Đây là bằng chứng cho `docs/SCENARIOS.md` §0.1: trong lúc job chạy remote, máy
local phải nhẹ (CPU < 2 % một core, RSS < 100 MB, iowait không nhảy) và lệnh
tương tác không chậm thêm quá 20 %.

Dùng:
    python3 tests/acceptance/measure_job.py
    python3 tests/acceptance/measure_job.py --cmd 'sleep 60' --seconds 90
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LAUNCHER = REPO / "skill" / "roff.py"
sys.path.insert(0, str(Path(__file__).resolve().parent))

import measure  # noqa: E402  (dùng chung bộ đo)


def roff_json(args, timeout=600, env=None):
    e = dict(os.environ)
    if env:
        e.update(env)
    p = subprocess.run(
        [sys.executable, str(LAUNCHER)] + list(args),
        capture_output=True, text=True, timeout=timeout, env=e,
    )
    lines = [ln for ln in p.stdout.splitlines() if ln.strip()]
    obj = json.loads(lines[-1]) if lines else {}
    return p.returncode, obj, p.stderr


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cmd", default="sleep 45")
    ap.add_argument("--seconds", type=float, default=70.0)
    ap.add_argument("--need", action="append", default=[])
    args = ap.parse_args()

    workdir = Path(tempfile.mkdtemp(prefix="roff-measure-"))
    src = workdir / "in"
    src.mkdir()
    payload = b"x" * (3 * 1024 * 1024)
    (src / "blob.bin").write_bytes(payload)

    env = {"ROFF_STATE_DIR": str(workdir / "state")}

    print("[đo] chạy lệnh tương tác trước khi có roff (baseline)…")
    base = measure._bench_loop(20)

    argv = [
        "do",
        "--cmd", args.cmd,
        "--in", str(src),
        "--out", str(workdir / "out"),
    ]
    for need in args.need:
        argv += ["--need", need]

    print("[đo] gọi roff do…")
    t0 = time.time()
    rc, obj, err = roff_json(argv, timeout=900, env=env)
    print(f"[đo] roff do -> rc={rc}")
    print(json.dumps(obj, ensure_ascii=False, indent=2)[:1200])

    job_id = obj.get("job") or ""
    jobs = [j for j in (obj.get("jobs") or []) if j]
    if jobs:
        job_id = jobs[0]

    m = measure.measure(args.seconds)
    print("[đo] trong lúc job chạy:", json.dumps(m.as_dict(), ensure_ascii=False, indent=2))

    print("[đo] chạy lệnh tương tác khi roff đang chạy…")
    during = measure._bench_loop(20)

    if job_id:
        w_rc, w_obj, _ = roff_json(["wait", job_id, "--timeout", "600"], timeout=700,
                                   env=env)
        print(f"[đo] wait -> rc={w_rc} {json.dumps(w_obj, ensure_ascii=False)[:400]}")

    # Rò tiến trình phải đo **sau khi** job xong: trong lúc job chạy thì watcher
    # còn sống là **đúng**, không phải rò.
    time.sleep(3)
    m.leaked = measure.leaked_processes()

    slow = (during - base) / base * 100 if base else 0.0
    budget_ok, budget_problems = m.meets_budget()
    verdict = {
        "cpu_pct_max": round(m.max_cpu_pct, 2),
        "cpu_ok": m.max_cpu_pct < 2.0,
        "rss_mb_max": round(m.max_rss_mb, 1),
        "rss_ok": m.max_rss_mb < 100,
        "idle_pct_min": round(m.idle_pct, 1),
        "iowait_pct_max": round(m.iowait_pct, 1),
        "leaked": m.leaked,
        "bench_baseline_s": round(base, 4),
        "bench_during_s": round(during, 4),
        "slowdown_pct": round(slow, 1),
        "slowdown_ok": slow <= 20.0,
        "budget_problems": budget_problems,
    }
    print(json.dumps(verdict, ensure_ascii=False, indent=2))
    ok = all([verdict["cpu_ok"], verdict["rss_ok"], verdict["slowdown_ok"],
              not verdict["leaked"], budget_ok])
    print("[đo] KẾT QUẢ:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
