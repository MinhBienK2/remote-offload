"""Unit test cho ngưỡng tuổi của ``roff gc`` (B32).

Lỗi gốc (đã đo trên fleet thật): ``cli.py`` đọc ngưỡng bằng
``float(getattr(args, "max_age_days", 7.0) or 7.0)`` ⇒ **0 bị nuốt** (0 là
falsy) ⇒ ``roff gc --max-age-days 0`` hoá 7 ngày ⇒ ``gc`` **không bao giờ**
dọn bản copy trong kho input dùng chung ở host ⇒ **kho input phía host phình
vô hạn** (S28 bắt được: mọi bản copy đều bị báo ``too_recent``).

Nguyên tắc khoá lại ở đây:

* **0 là giá trị hợp lệ** — người dùng gõ rõ ràng ⇒ phải giữ nguyên 0;
* phân biệt "không truyền" (``None``) với "truyền 0" ⇒ kiểm tra ``is None``,
  **không** dùng truthiness (``or``);
* giá trị **âm** ⇒ báo lỗi rõ ràng (exit 2) như ``--timeout``/``--cpus``;
* ``--shards 0`` ⇒ thông điệp lỗi phải nói **0** (đúng số người dùng gõ).

Test **không** chạm mạng: ``gc_shared_store``/``gc_jobs`` bị thay bằng bản
giả để bắt tham số truyền xuống.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SKILL = REPO / "skill"

if str(SKILL) not in sys.path:
    sys.path.insert(0, str(SKILL))

from conftest import parse_json, run_cli  # noqa: E402

from roffkit import cli as cli_mod  # noqa: E402
from roffkit import probe as probe_mod  # noqa: E402
from roffkit.errors import EXIT_INPUT, RoffError  # noqa: E402

SEVEN_DAYS_S = 7 * 24 * 3600


# --------------------------------------------------------------------------
# hạ tầng test
# --------------------------------------------------------------------------
@pytest.fixture
def gc_env(monkeypatch, tmp_path):
    """Env cô lập cho ``roff gc``: state/cache/config riêng, không đụng nhà.

    ``ROFF_SSH_CONFIG`` trỏ tới file **rỗng** ⇒ fleet **rỗng** ⇒ test không
    bao giờ probe máy thật, kể cả khi lệnh lỗi và đi tiếp (lỗi của test
    khác với lỗi của sản phẩm).
    """
    state = tmp_path / "state"
    cache = tmp_path / "cache"
    home = tmp_path / "home"
    for path in (state, cache, home):
        path.mkdir(parents=True, exist_ok=True)
    config = tmp_path / "config.ini"
    config.write_text("[fleet]\ninclude = *\n", encoding="utf-8")
    ssh_config = tmp_path / "ssh_config"
    ssh_config.write_text("", encoding="utf-8")
    monkeypatch.setenv("ROFF_STATE_DIR", str(state))
    monkeypatch.setenv("ROFF_CACHE_DIR", str(cache))
    monkeypatch.setenv("ROFF_CONFIG", str(config))
    monkeypatch.setenv("ROFF_HOME", str(home))
    monkeypatch.setenv("ROFF_SSH_CONFIG", str(ssh_config))
    return {
        "state": state,
        "cache": cache,
        "config": config,
        "home": home,
        "ssh_config": ssh_config,
    }


def _capture_host_gc(monkeypatch) -> dict:
    """Bắt tham số ``max_age_s`` truyền xuống ``gc_shared_store``."""
    captured: dict = {}

    def _fake_gc_shared_store(
        host, layout_root, keep_keys=(), max_age_s=0.0, now=None
    ):
        captured["max_age_s"] = max_age_s
        captured["host"] = host
        return {
            "host": host,
            "removed": [],
            "removed_locks": [],
            "skipped": [],
            "skipped_locks": [],
            "freed_mb": 0.0,
        }

    monkeypatch.setattr(cli_mod.shared_mod, "gc_shared_store", _fake_gc_shared_store)
    monkeypatch.setattr(cli_mod, "_fleet_aliases", lambda cfg: ["muse-a"])
    monkeypatch.setattr(
        cli_mod.probe_mod,
        "cached_profiles",
        lambda aliases, cfg: [
            probe_mod.Profile(
                alias="muse-a", reachable=True, layout_root="/home/hatch/.roff"
            )
        ],
    )
    return captured


def _capture_job_gc(monkeypatch) -> dict:
    """Bắt tham số ``max_age_days`` truyền xuống ``job.gc_jobs``."""
    captured: dict = {}

    def _fake_gc_jobs(state_dir=None, max_age_days=30.0):
        captured["max_age_days"] = max_age_days
        return {
            "removed": [],
            "removed_count": 0,
            "skipped": [],
            "freed_bytes": 0,
        }

    monkeypatch.setattr(cli_mod.job_mod, "gc_jobs", _fake_gc_jobs)
    return captured


def _cli_env(gc_env) -> dict:
    """Env đầy đủ cho tiến trình con (``run_cli`` thay cả env).

    ``ROFF_SSH_CONFIG`` trỏ tới file **rỗng** ⇒ không host nào để probe ⇒
    lệnh hỏng cũng không bao giờ chạm vào máy thật (test phải **hermetic**).
    """
    env = dict(os.environ)
    env.update(
        {
            "ROFF_STATE_DIR": str(gc_env["state"]),
            "ROFF_CACHE_DIR": str(gc_env["cache"]),
            "ROFF_CONFIG": str(gc_env["config"]),
            "ROFF_HOME": str(gc_env["home"]),
            "ROFF_SSH_CONFIG": str(gc_env["ssh_config"]),
            "ROFF_NO_WATCHER": "1",
        }
    )
    return env


# --------------------------------------------------------------------------
# --max-age-days
# --------------------------------------------------------------------------
def test_gc_max_age_days_zero_reaches_host_store(gc_env, monkeypatch):
    """``--max-age-days 0`` ⇒ ngưỡng xuống ``gc_shared_store`` phải là **0**.

    Nuốt 0 ⇒ ngưỡng 7 ngày ⇒ ``gc`` không dọn được kho input ⇒ kho input phía
    host phình vô hạn.
    """
    captured = _capture_host_gc(monkeypatch)

    payload, code = cli_mod._cmd_gc(
        argparse.Namespace(max_age_days=0, job_max_age_days=None)
    )

    assert code == 0
    assert captured.get("max_age_s") == 0.0, (
        f"ngưỡng phải là 0 (dọn hết), thực tế: {captured.get('max_age_s')!r}"
    )


def test_gc_max_age_days_default_is_kept_when_option_absent(gc_env, monkeypatch):
    """Không truyền ``--max-age-days`` ⇒ vẫn 7 ngày (hành vi cũ)."""
    captured = _capture_host_gc(monkeypatch)

    payload, code = cli_mod._cmd_gc(argparse.Namespace())

    assert code == 0
    assert captured.get("max_age_s") == SEVEN_DAYS_S


def test_gc_max_age_days_zero_survives_argparse(gc_env, monkeypatch):
    """Qua ``argparse`` (chuỗi "0") ⇒ 0 vẫn phải được giữ.

    ``type=float`` của argparse cho ra ``0.0`` — falsy ⇒ dễ bị ``or`` nuốt.
    """
    parser = cli_mod._build_parser()
    args = parser.parse_args(["gc", "--max-age-days", "0"])

    assert args.max_age_days == 0.0
    captured = _capture_host_gc(monkeypatch)
    payload, code = cli_mod._cmd_gc(args)

    assert code == 0
    assert captured.get("max_age_s") == 0.0


def test_gc_negative_max_age_days_is_rejected(gc_env):
    """``--max-age-days -1`` ⇒ lỗi rõ ràng, exit 2 (như ``--timeout``)."""
    with pytest.raises(RoffError) as excinfo:
        cli_mod._cmd_gc(argparse.Namespace(max_age_days=-1, job_max_age_days=None))

    assert excinfo.value.code == EXIT_INPUT
    message = str(excinfo.value.payload().get("error") or "")
    assert "--max-age-days" in message, message
    assert "-1" in message, message


# --------------------------------------------------------------------------
# --job-max-age-days
# --------------------------------------------------------------------------
def test_gc_job_max_age_days_zero_reaches_gc_jobs(gc_env, monkeypatch):
    """``--job-max-age-days 0`` ⇒ ``gc_jobs`` phải nhận **0** (không phải 30)."""
    captured = _capture_job_gc(monkeypatch)
    _capture_host_gc(monkeypatch)

    payload, code = cli_mod._cmd_gc(
        argparse.Namespace(max_age_days=None, job_max_age_days=0)
    )

    assert code == 0
    assert captured.get("max_age_days") == 0.0, (
        f"ngưỡng job phải là 0, thực tế: {captured.get('max_age_days')!r}"
    )


def test_gc_job_max_age_days_default_is_kept_when_option_absent(
    gc_env, monkeypatch
):
    """Không truyền ``--job-max-age-days`` ⇒ vẫn 30 ngày (hành vi cũ)."""
    captured = _capture_job_gc(monkeypatch)
    _capture_host_gc(monkeypatch)

    payload, code = cli_mod._cmd_gc(argparse.Namespace())

    assert code == 0
    assert captured.get("max_age_days") == 30.0


def test_gc_negative_job_max_age_days_is_rejected(gc_env):
    """``--job-max-age-days -1`` ⇒ lỗi rõ ràng, exit 2."""
    with pytest.raises(RoffError) as excinfo:
        cli_mod._cmd_gc(argparse.Namespace(max_age_days=None, job_max_age_days=-1))

    assert excinfo.value.code == EXIT_INPUT
    message = str(excinfo.value.payload().get("error") or "")
    assert "--job-max-age-days" in message, message
    assert "-1" in message, message


# --------------------------------------------------------------------------
# --shards: thông điệp lỗi phải nói đúng số người dùng gõ
# --------------------------------------------------------------------------
def test_shards_zero_error_mentions_zero():
    """``--shards 0`` ⇒ thông điệp lỗi nói **0** (không phải 1)."""
    args = argparse.Namespace(shards=0, each=None)

    with pytest.raises(RoffError) as excinfo:
        cli_mod._job_specs(args)

    assert excinfo.value.code == EXIT_INPUT
    message = str(excinfo.value.payload().get("error") or "")
    assert "0" in message, f"thông điệp phải nói số người dùng gõ: {message}"


def test_shards_default_still_one_job():
    """Không truyền ``--shards`` ⇒ vẫn 1 job (hành vi cũ)."""
    specs = cli_mod._job_specs(argparse.Namespace(each=None))

    assert len(specs) == 1
    assert specs[0]["shards"] is None


# --------------------------------------------------------------------------
# hợp đồng CLI (tiến trình con): exit code + JSON
# --------------------------------------------------------------------------
def test_cli_gc_negative_max_age_days_exits_2(gc_env):
    """``roff gc --max-age-days -1`` ⇒ exit 2 + JSON báo lỗi rõ ràng."""
    rc, out, err = run_cli(
        ["gc", "--max-age-days", "-1"], env=_cli_env(gc_env)
    )

    assert rc == EXIT_INPUT, f"rc={rc} err={err[-300:]}"
    payload = parse_json(out)
    assert payload.get("ok") is False
    assert "--max-age-days" in str(payload.get("error") or ""), payload


def test_cli_shards_zero_exits_2_and_mentions_zero(gc_env):
    """``roff do --shards 0`` ⇒ exit 2 + thông điệp nói **0**."""
    rc, out, err = run_cli(
        ["do", "--cmd", "true", "--shards", "0"], env=_cli_env(gc_env)
    )

    assert rc == EXIT_INPUT, f"rc={rc} err={err[-300:]}"
    payload = parse_json(out)
    assert payload.get("ok") is False
    assert "0" in str(payload.get("error") or ""), payload
