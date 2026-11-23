"""Test cho roffkit.errors — hợp đồng exit code + payload JSON.

Không cần mạng, không chạy ssh thật.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill"))

from roffkit.errors import (  # noqa: E402
    EXIT_CANCELLED,
    EXIT_CONFIG,
    EXIT_INPUT,
    EXIT_JOB_FAILED,
    EXIT_JOB_LOST,
    EXIT_NO_HOST,
    EXIT_NO_WORKDIR,
    EXIT_OK,
    EXIT_TIMEOUT,
    EXIT_UNREACHABLE,
    REASON_OOM,
    REASON_RESTARTED,
    REASON_VANISHED,
    REASON_WIPED,
    RoffError,
    emit,
)


def test_exit_codes_match_the_contract():
    assert EXIT_OK == 0
    assert EXIT_JOB_FAILED == 1
    assert EXIT_INPUT == 2
    assert EXIT_CONFIG == 3
    assert EXIT_NO_HOST == 10
    assert EXIT_JOB_LOST == 69
    assert EXIT_NO_WORKDIR == 97
    assert EXIT_TIMEOUT == 124
    assert EXIT_CANCELLED == 130
    assert EXIT_UNREACHABLE == 255


def test_reason_constants_match_the_contract():
    assert REASON_RESTARTED == "host_restarted"
    assert REASON_WIPED == "host_wiped"
    assert REASON_OOM == "oom_killed"
    assert REASON_VANISHED == "vanished"


def test_roff_error_keeps_code_reason_and_fields():
    err = RoffError(EXIT_NO_HOST, "no slot on muse-a", host="muse-a")
    assert err.code == EXIT_NO_HOST
    assert err.reason == "no slot on muse-a"
    assert err.fields == {"host": "muse-a"}


def test_roff_error_message_is_reason_or_exit_code():
    assert str(RoffError(EXIT_CONFIG, "bad ini")) == "bad ini"
    assert str(RoffError(EXIT_CONFIG)) == "exit 3"


def test_payload_always_has_ok_false_and_exit_code():
    payload = RoffError(EXIT_NO_HOST, "no host").payload()
    assert payload["ok"] is False
    assert payload["exit_code"] == EXIT_NO_HOST
    assert payload["reason"] == "no host"


def test_payload_omits_reason_when_empty():
    payload = RoffError(EXIT_INPUT).payload()
    assert "reason" not in payload
    assert payload["ok"] is False


def test_payload_contains_error_when_given():
    payload = RoffError(EXIT_INPUT, error="đường dẫn sai").payload()
    assert payload["error"] == "đường dẫn sai"


def test_payload_contains_extra_fields():
    payload = RoffError(EXIT_NO_HOST, "no host", host="muse-a", best="muse-b").payload()
    assert payload["host"] == "muse-a"
    assert payload["best"] == "muse-b"


def test_payload_for_255_always_has_detail():
    payload = RoffError(EXIT_UNREACHABLE, detail="Permission denied (publickey)").payload()
    assert payload["detail"] == "Permission denied (publickey)"
    assert payload["exit_code"] == EXIT_UNREACHABLE


def test_payload_for_255_synthesises_detail_when_missing():
    payload = RoffError(EXIT_UNREACHABLE).payload()
    assert payload["detail"]
    payload = RoffError(EXIT_UNREACHABLE, "ssh: connect refused").payload()
    assert payload["detail"] == "ssh: connect refused"


def test_emit_prints_one_json_object_and_exits(capsys):
    with pytest.raises(SystemExit) as exc:
        emit({"ok": True, "state": "exited"}, 0)
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert out.endswith("\n")
    assert json.loads(out) == {"ok": True, "state": "exited"}


def test_emit_keeps_key_order_and_non_ascii(capsys):
    with pytest.raises(SystemExit):
        emit({"b": 1, "a": "héllo"}, 3)
    out = capsys.readouterr().out.strip()
    assert out == '{"b": 1, "a": "héllo"}'
    assert json.loads(out)["a"] == "héllo"


def test_emit_exit_code_is_propagated():
    with pytest.raises(SystemExit) as exc:
        emit({"ok": False}, EXIT_UNREACHABLE)
    assert exc.value.code == EXIT_UNREACHABLE
