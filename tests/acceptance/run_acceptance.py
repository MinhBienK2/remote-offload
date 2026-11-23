#!/usr/bin/env python3
"""Chạy trọn đợt nghiệm thu cuối và gom bằng chứng vào `docs/evidence/`.

Các bước (bỏ qua được bằng cờ):
  1. chụp trạng thái fleet (ai sống, RAM, disk)
  2. `pytest tests/acceptance -q -m acceptance` — kịch bản S3–S24 tự động
  3. `measure_job.py` — đo tài nguyên máy local khi job chạy remote (SCENARIOS §0.1)
  4. `s1_skill_activation.py` — skill tự bật ở dự án khác (S1)

Mọi thứ ghi ra `docs/evidence/<ngày>/`.

Dùng:
    python3 tests/acceptance/run_acceptance.py
    python3 tests/acceptance/run_acceptance.py --skip s1
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
EVIDENCE = REPO / "docs" / "evidence"


def run(argv, cwd=REPO, timeout=3600, env=None):
    e = dict(os.environ)
    if env:
        e.update(env)
    started = time.time()
    p = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True,
                       timeout=timeout, env=e)
    return {
        "argv": argv,
        "rc": p.returncode,
        "seconds": round(time.time() - started, 1),
        "stdout": p.stdout[-20000:],
        "stderr": p.stderr[-20000:],
    }


def fleet_snapshot():
    p = run([sys.executable, str(REPO / "skill" / "roff.py"), "hosts", "--json"], timeout=300)
    try:
        obj = json.loads(p["stdout"].strip().splitlines()[-1])
    except Exception:
        return {"raw": p["stdout"][-2000:]}
    out = []
    for h in obj.get("hosts", []):
        out.append({
            "alias": h.get("alias"),
            "reachable": h.get("reachable"),
            "error": h.get("error"),
            "cores": h.get("cores"),
            "mem_available_mb": h.get("mem_available_mb"),
            "disk_free_mb": h.get("disk_free_mb"),
            "durable": h.get("durable"),
            "layout_root": h.get("layout_root"),
            "internet": h.get("internet"),
        })
    return {"hosts": out, "count": obj.get("count")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip", action="append", default=[],
                    choices=["acceptance", "measure", "s1"])
    ap.add_argument("--tag", default=None)
    args = ap.parse_args()

    tag = args.tag or time.strftime("%Y%m%d-%H%M%S")
    outdir = EVIDENCE / tag
    outdir.mkdir(parents=True, exist_ok=True)
    print(f"[nghiệm thu] thư mục bằng chứng: {outdir}")

    report = {"tag": tag, "started_at": time.strftime("%Y-%m-%d %H:%M:%S"), "steps": {}}

    print("[1/4] chụp trạng thái fleet…")
    snap = fleet_snapshot()
    (outdir / "fleet.json").write_text(
        json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
    report["steps"]["fleet"] = {"ok": bool(snap.get("hosts"))}
    for h in (snap.get("hosts") or []):
        print(f"    {h['alias']:24} reachable={h['reachable']} mem={h['mem_available_mb']}MB")

    if "acceptance" not in args.skip:
        print("[2/4] pytest tests/acceptance …")
        res = run([sys.executable, "-m", "pytest", "tests/acceptance", "-q", "-m",
                   "acceptance", "-rf"], timeout=5400)
        (outdir / "acceptance.txt").write_text(
            res["stdout"] + "\n----- stderr -----\n" + res["stderr"], encoding="utf-8")
        report["steps"]["acceptance"] = {
            "rc": res["rc"], "seconds": res["seconds"],
            "tail": res["stdout"][-3000:],
        }
        print(f"    rc={res['rc']} trong {res['seconds']}s")

    if "measure" not in args.skip:
        print("[3/4] đo tài nguyên máy local khi job chạy remote…")
        res = run([sys.executable, str(HERE / "measure_job.py")], timeout=1800)
        (outdir / "measure.txt").write_text(
            res["stdout"] + "\n----- stderr -----\n" + res["stderr"], encoding="utf-8")
        report["steps"]["measure"] = {"rc": res["rc"], "seconds": res["seconds"]}
        print(f"    rc={res['rc']}")

    if "s1" not in args.skip:
        print("[4/4] S1 — skill tự bật ở dự án khác…")
        res = run([sys.executable, str(HERE / "s1_skill_activation.py")], timeout=1200)
        (outdir / "s1.txt").write_text(
            res["stdout"] + "\n----- stderr -----\n" + res["stderr"], encoding="utf-8")
        report["steps"]["s1"] = {"rc": res["rc"], "seconds": res["seconds"],
                                 "tail": res["stdout"][-2000:]}
        print(f"    rc={res['rc']}")

    report["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    (outdir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[nghiệm thu] xong. Bằng chứng: {outdir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
