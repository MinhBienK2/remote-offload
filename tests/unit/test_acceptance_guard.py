"""Bộ lọc "hạ tầng ⇒ SKIP" của bộ nghiệm thu — phải **không** che lỗi thật.

Cơ chế nằm ở `tests/acceptance/conftest.py` (hook `pytest_runtest_makereport`).
Hàm thuần `host_down_signature()` được thử ở đây để bảo đảm:
* lỗi **hạ tầng** (máy chết, đường truyền đứt) ⇒ nhận diện được;
* lỗi **thật của `roff`** ⇒ **không** được nhận diện (nếu không, báo cáo
  nghiệm thu sẽ nói dối).
"""
import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
_CONFTEST = REPO / "tests" / "acceptance" / "conftest.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("roff_acceptance_conftest", _CONFTEST)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


signature = _load_module().host_down_signature


@pytest.mark.parametrize("text", [
    "ssh: connect to host 127.0.0.1 port 2222: Connection refused",
    "ssh: connect to host 127.0.0.1 port 2226: Connection timed out",
    "kex_exchange_identification: Connection closed by remote host",
    "No route to host",
    "ssh: Could not resolve hostname muse-x: Name or service not known",
    "subprocess.TimeoutExpired: Command '['ssh', ...]' timed out after 60 seconds",
    "roff do thất bại: rc=255 khi mkdir /home/hatch/.roff/jobs/261123-160500-6464",
    ">       assert 255 == 0",
])
def test_infra_signatures_are_detected(text):
    assert signature(text), f"phải nhận diện là hạ tầng: {text!r}"


@pytest.mark.parametrize("text", [
    # Lỗi thật của roff — **không** được che.
    "phải có 3 file shard: ['s0.txt', 's2.txt']",
    "sai lý do: {'ok': False, 'exit_code': 255, 'reason': 'no_host'}",
    "phải exit 255 (không với được host), thấy rc=10: {'exit_code': 10}",
    "job mất vì host chết (reason=host_restarted) — sự cố hạ tầng, không phải lỗi roff",
    "không tạo được job giữ slot: {'ok': False, 'exit_code': 10}",
    "",
])
def test_real_bugs_are_not_masked(text):
    assert not signature(text), f"KHÔNG được che lỗi thật: {text!r}"
