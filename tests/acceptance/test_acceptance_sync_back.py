"""S46 — `--sync-back` phải **thực sự** kéo file về project (sự cố B26).

Bối cảnh (đo thật 23/11/2026): `--sync-back` được **quảng cáo** trong
`skill/SKILL.md` ("pull specific paths back into the working tree") và trong
`docs/SPEC-CLI.md` §1, nhưng **không mã nào thực sự kéo** các path đó về local:
ledger chỉ **ghi lại** đường dẫn remote, watcher chỉ **remap** khi failover, còn
chỗ duy nhất gọi `pull_path()` là `roff pull` (thủ công).

⇒ `roff do --sync-back X` là lệnh **rỗng (no-op)**: job báo thành công,
`pulled: true`, nhưng file **không** về project ⇒ agent tin là đã có file.

Đo thật: job `261123-171305-88ac` (host `muse-phamminhbien3333`) exit 0,
`pulled: true`, ledger có `sync_back = ['/tmp/sbtest/proj/report.md']` — nhưng
`/tmp/sbtest/proj/` **rỗng**.

Kịch bản này **cần fleet thật**. Test tự `skip` khi không đủ máy — không được
báo xanh khi không kiểm chứng được (nguyên tắc `docs/REPORT.md` §2).

Mô tả đầy đủ: `docs/SCENARIOS.md` §12c (mục S46).
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as common  # noqa: E402

from _common import pick_live  # noqa: E402
from _common import roff_json  # noqa: E402
from _common import wait_or_skip  # noqa: E402

pytestmark = pytest.mark.acceptance

MUSE = ["muse-pmb333", "muse-tampt", "muse-bienpm333", "muse-phamminhbien3333"]


@pytest.fixture(scope="module")
def fleet():
    """Các máy Muse **đang sống** lúc module bắt đầu (probe thật)."""
    live = common.pick_live_all(MUSE)
    if not live:
        pytest.skip("không có máy muse nào sống — bật lại tunnel trước khi nghiệm thu")
    return live


def test_s46_sync_back_pulls_file_into_project(fleet, tmp_path):
    """S46: `--sync-back` phải **kéo file về project** sau khi job xong.

    Cách làm deterministic:
      * `--sync-back report.md` (đường dẫn **tương đối** trong project) ⇒
        remote tương ứng là `$ROFF_WORK/report.md` trên máy;
      * job ghi đúng file đó ⇒ sau khi job xong, file phải xuất hiện ở
        `<project>/report.md` với đúng nội dung.

    Đạt khi:
      * `roff wait` trả rc 0 (job chạy xong, không có lỗi pull);
      * `<project>/report.md` **tồn tại** và có đúng nội dung;
      * ledger **không** có `sync_back_error`.
    """
    alias = pick_live(fleet)
    if alias is None:
        pytest.skip("mọi máy muse đã chết — sự cố hạ tầng")

    project = tmp_path / "proj"
    project.mkdir()

    rc, obj, err = roff_json([
        "do", "--cmd", 'printf "report-content" > "$ROFF_WORK/report.md"',
        "--out", str(tmp_path / "out"),
        "--sync-back", "report.md",
    ], timeout=300, cwd=project)
    if common.is_host_down(rc, obj, err):
        pytest.skip(f"máy remote chết lúc gửi job — sự cố hạ tầng: {err[-200:]}")
    assert rc == 0, (
        f"roff do thất bại: rc={rc} {json.dumps(obj, ensure_ascii=False)} "
        f"{err[-300:]}")
    jid = obj.get("job")
    assert jid, f"thiếu job id: {json.dumps(obj, ensure_ascii=False)}"

    rc2, res, err2 = wait_or_skip(jid, timeout=360, wait_timeout=300)
    assert rc2 == 0, (
        f"job phải thành công: rc={rc2} {json.dumps(res, ensure_ascii=False)}")

    target = project / "report.md"
    assert target.exists(), (
        f"--sync-back KHÔNG kéo file về project: {sorted(p.name for p in project.iterdir())} "
        f"— lệnh đã được quảng cáo trong SKILL.md nhưng không bao giờ chạy")
    assert target.read_text(encoding="utf-8") == "report-content", (
        f"nội dung file kéo về sai: {target.read_text(encoding='utf-8')!r}")

    # Ledger không được ghi "lỗi sync-back" khi mọi thứ đã ổn.
    ledger = common.ledger_of(jid)
    if ledger is not None:
        options = ledger.get("_roff") or {}
        assert not options.get("sync_back_error"), (
            f"ledger ghi lỗi sync-back dù file đã về: {options.get('sync_back_error')!r}")
