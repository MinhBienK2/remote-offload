"""S47 — `ROFF_CONFIG` trỏ tới file không tồn tại ⇒ **fail to**, không nới fleet.

Bối cảnh (đo thật 23/11/2026, sự cố B27):

Config thật của người dùng ghi rõ::

    [fleet]
    include = muse-*
    exclude = colab-comfy

Nhưng `Config.load()` trả về config **rỗng** khi file không tồn tại, mà không
báo lỗi ⇒ `fleet_include` mặc định `["*"]` ⇒ fleet **8 máy** thay vì 4,
gồm `comfy` / `colab-comfy` (đã bị `exclude`) / `qwen1` / `qwen1-direct`.

Đo thật trước khi sửa::

    $ ROFF_CONFIG=/tmp/khong-co-that-abc123/config.ini roff hosts
    fleet: comfy, colab-comfy, qwen1, qwen1-direct, muse-pmb333, muse-tampt,
           muse-bienpm333, muse-phamminhbien3333

⇒ `roff` sẵn sàng đẩy job **và dữ liệu** lên máy Colab GPU của người dùng.

Sau khi sửa: `roff` **dừng lại** (exit 3, `reason=config_missing`) và **không
tạo job nào**.

Kịch bản này **không** cần fleet thật (lỗi phải xảy ra **trước** khi probe
mạng) — nên nó chạy được ngay cả khi toàn bộ máy Muse đang chết.

Mô tả đầy đủ: `docs/SCENARIOS.md` §12d.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as common  # noqa: E402

from _common import roff_json  # noqa: E402

pytestmark = pytest.mark.acceptance


def test_s47_missing_config_fails_loud_and_creates_no_job(tmp_path):
    """S47: config chỉ định mà không tồn tại ⇒ exit 3, **không** tạo job.

    Đạt khi:
      * `roff hosts` trả rc **3** với `reason == "config_missing"`;
      * `roff do` trả rc **3** (không phải 10/255) và **không** tạo job nào
        trong ledger — đây là điểm mấu chốt: nếu job được tạo, nó đã có thể
        bị đẩy lên một máy ngoài ý muốn.
    """
    missing = tmp_path / "khong-co-that-abc123" / "config.ini"
    assert not missing.exists(), "fixture sai: file config phải KHÔNG tồn tại"
    env = {"ROFF_CONFIG": str(missing)}

    # --- 1. `roff hosts` phải fail to, nói rõ lý do ------------------------
    rc, obj, err = roff_json(["hosts"], timeout=120, env=env)
    assert rc == 3, (
        f"config thiếu mà `roff hosts` trả rc={rc} (mong đợi 3): "
        f"{json.dumps(obj, ensure_ascii=False)[:300]} {err[-200:]}")
    assert obj.get("reason") == "config_missing", (
        f"reason sai: {obj.get('reason')!r} — mong đợi 'config_missing'")
    assert str(missing) in json.dumps(obj, ensure_ascii=False), (
        "lỗi phải **nêu đường dẫn** config để người dùng biết sửa ở đâu")

    # --- 2. `roff do` phải dừng TRƯỚC khi tạo job -------------------------
    jobs_dir = Path(common.DEFAULT_STATE) / "jobs"
    before = set(jobs_dir.glob("*.json")) if jobs_dir.is_dir() else set()

    rc2, obj2, err2 = roff_json(
        ["do", "--cmd", "echo hien-nen-co-job-nao", "--out", str(tmp_path / "out")],
        timeout=180, env=env,
    )
    assert rc2 == 3, (
        f"config thiếu mà `roff do` trả rc={rc2} (mong đợi 3, KHÔNG phải 10/255): "
        f"{json.dumps(obj2, ensure_ascii=False)[:300]} {err2[-200:]}")
    assert obj2.get("reason") == "config_missing", (
        f"reason sai: {obj2.get('reason')!r}")

    after = set(jobs_dir.glob("*.json")) if jobs_dir.is_dir() else set()
    assert after == before, (
        f"`roff do` ĐÃ TẠO JOB dù config lỗi: {sorted(p.name for p in after - before)} "
        f"— job đó có thể đã bị đẩy lên máy ngoài ý muốn")


def test_s47_config_present_keeps_fleet_restricted(tmp_path):
    """S47b: config **có thật** ⇒ `exclude` được giữ, không nới fleet.

    Đối chứng với case trên: cùng một `ROFF_CONFIG`, nhưng file tồn tại và
    khai báo `include`/`exclude` ⇒ fleet phải **đúng** như khai báo, và JSON
    **không** mang cờ `fleet_unrestricted`.
    """
    cfg = tmp_path / "config.ini"
    cfg.write_text(
        "[fleet]\n"
        "include = muse-*\n"
        "exclude = colab-comfy\n",
        encoding="utf-8",
    )
    env = {"ROFF_CONFIG": str(cfg)}

    rc, obj, err = roff_json(["hosts"], timeout=180, env=env)
    if common.is_host_down(rc, obj, err):
        pytest.skip(f"fleet không hào hứng đo lúc này — sự cố hạ tầng: {err[-200:]}")
    assert rc == 0, (
        f"config hợp lệ mà `roff hosts` trả rc={rc}: "
        f"{json.dumps(obj, ensure_ascii=False)[:300]} {err[-200:]}")

    aliases = [h.get("alias") for h in (obj.get("hosts") or [])]
    assert "colab-comfy" not in aliases, (
        f"`exclude = colab-comfy` bị bỏ qua: {aliases}")
    # `include = muse-*` ⇒ chỉ được phép có alias muse-*.
    stray = [a for a in aliases if a and not str(a).startswith("muse-")]
    assert not stray, f"fleet có alias ngoài `include = muse-*`: {stray}"
    assert not obj.get("fleet_unrestricted"), (
        "config **có** giới hạn fleet mà JSON vẫn báo `fleet_unrestricted`")
