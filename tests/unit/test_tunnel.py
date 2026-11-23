"""Unit test cho `roffkit.tunnel` — hợp đồng API-CONTRACT §12, hồi quy R3.

Không network thật: **mọi** lệnh local (ssh, ss) đi qua seam `tunnel._run`,
nên test nhanh và xác định.

Trọng tâm:
  * remote phải đang LISTEN ở rport trước khi mở tunnel;
  * phân biệt "không có gì nghe" (False) với "không kiểm tra được" (None);
  * master ssh CŨ giữ lport → huỷ forward bằng `-O cancel`, **không** kill pid;
  * pid lạ giữ port → **không** kill, báo lỗi rõ;
  * ControlMaster của roff: ControlPath riêng, ControlPersist=300.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

# Tự thêm `skill/` vào sys.path: test này phải chạy được dù conftest dùng
# đường dẫn khác (không phụ thuộc file của agent khác).
_SKILL_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "skill"
)
if _SKILL_DIR not in sys.path:
    sys.path.insert(0, _SKILL_DIR)

from roffkit import tunnel  # noqa: E402
from roffkit.errors import (  # noqa: E402
    EXIT_INPUT,
    EXIT_JOB_FAILED,
    EXIT_UNREACHABLE,
    RoffError,
)

ALIAS = "muse-a"
LPORT = 18000
RPORT = 8000

SS_LISTEN = (
    "State  Recv-Q Send-Q Local Address:Port Peer Address:Port Process\n"
    "LISTEN 0      128    127.0.0.1:8000      0.0.0.0:*\n"
)


# --------------------------------------------------------------------------------------
# seam
# --------------------------------------------------------------------------------------
class Fake:
    """Thay `tunnel._run`; ghi lại mọi argv để assert."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.handlers: list[tuple] = []

    def add(self, pred, response):
        self.handlers.append((pred, response))
        return self

    def __call__(self, argv, timeout=30):
        argv = list(argv)
        self.calls.append(argv)
        joined = " ".join(argv)
        for pred, response in self.handlers:
            if pred(argv, joined):
                if callable(response):
                    response = response(argv, joined)
                return response
        return (0, "", "")

    # -- tiện ích assert ---------------------------------------------------------------
    def joined(self) -> str:
        return "\n".join(" ".join(c) for c in self.calls)

    def killed(self) -> bool:
        """Có lệnh kill tiến trình nào không? (R3: TUYỆT ĐỐI KHÔNG)"""
        killers = {"kill", "killall", "pkill", "fuser"}
        for call in self.calls:
            for arg in call:
                if arg in killers or arg.startswith("kill"):
                    return True
        return False

    def cancelled(self) -> bool:
        for call in self.calls:
            if "-O" in call and "cancel" in call:
                return True
        return False


def is_remote_check(argv, joined) -> bool:
    return "__ROFF_SRC__" in joined


def is_ss(argv, joined) -> bool:
    return "-ltnp" in argv


def mux_cmd(argv) -> str:
    if "-O" not in argv:
        return ""
    return argv[argv.index("-O") + 1] if argv.index("-O") + 1 < len(argv) else ""


def is_mux(argv, joined) -> bool:
    return mux_cmd(argv) in {"check", "cancel", "forward", "exit"}


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    d = tmp_path / "state"
    monkeypatch.setenv("ROFF_STATE_DIR", str(d))
    return d


@pytest.fixture
def no_kill(monkeypatch):
    """Bất kỳ ai gọi os.kill trong lúc test đều là bug (R3)."""

    def boom(*_a, **_kw):
        raise AssertionError("tunnel.py không được kill tiến trình nào")

    monkeypatch.setattr(tunnel.os, "kill", boom)
    return boom


# --------------------------------------------------------------------------------------
# remote có LISTEN ở rport không
# --------------------------------------------------------------------------------------
def test_remote_not_listening_raises_with_false(state_dir, monkeypatch):
    fake = Fake()
    fake.add(is_remote_check, (0, "LISTEN 0 128 127.0.0.1:22 0.0.0.0:*\n", ""))
    monkeypatch.setattr(tunnel, "_run", fake)

    with pytest.raises(RoffError) as exc:
        tunnel.tunnel_start(ALIAS, LPORT, RPORT)

    err = exc.value
    assert err.code == EXIT_JOB_FAILED
    assert err.reason == "remote_listening"
    assert err.fields.get("remote_listening") is False
    payload = err.payload()
    assert payload["ok"] is False
    assert payload["remote_listening"] is False
    assert payload["exit_code"] == EXIT_JOB_FAILED


def test_remote_listening_true_allows_start(state_dir, monkeypatch):
    fake = Fake()
    fake.add(is_remote_check, (0, SS_LISTEN, ""))
    fake.add(is_ss, (0, "", ""))
    fake.add(lambda a, j: mux_cmd(a) == "check", (0, "Master running (pid=4242)\n", ""))
    monkeypatch.setattr(tunnel, "_run", fake)

    t = tunnel.tunnel_start(ALIAS, LPORT, RPORT)
    assert t.alias == ALIAS
    assert t.lport == LPORT
    assert t.rport == RPORT
    assert t.master_pid == 4242
    assert "remote_listening=True" in (t.note or "")


def test_remote_listening_unknown_when_no_tools(state_dir, monkeypatch):
    """Không có ss/netstat và không đọc được /proc/net/tcp → None, KHÔNG phải False."""
    fake = Fake()
    fake.add(is_remote_check, (0, "__ROFF_SRC__=none\n", ""))
    fake.add(is_ss, (0, "", ""))
    fake.add(lambda a, j: mux_cmd(a) == "check", (0, "Master running (pid=99)\n", ""))
    monkeypatch.setattr(tunnel, "_run", fake)

    t = tunnel.tunnel_start(ALIAS, LPORT, RPORT)
    assert "remote_listening=unknown" in (t.note or "")
    assert "False" not in (t.note or "")


def test_remote_listening_detected_from_proc(state_dir, monkeypatch):
    proc = (
        "__ROFF_SRC__=proc\n"
        "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt"
        "   uid  timeout inode\n"
        "   0: 00000000:1F40 00000000:0000 0A 00000000:00000000 00:00000000 00000000"
        "     0        0 12345 1 0000 100 0 0 10 0\n"
    )
    fake = Fake()
    fake.add(is_remote_check, (0, proc, ""))
    fake.add(is_ss, (0, "", ""))
    fake.add(lambda a, j: mux_cmd(a) == "check", (0, "Master running (pid=7)\n", ""))
    monkeypatch.setattr(tunnel, "_run", fake)

    t = tunnel.tunnel_start(ALIAS, LPORT, RPORT)
    assert "remote_listening=True" in (t.note or "")


def test_ssh_failure_is_unreachable_not_remote_listening(state_dir, monkeypatch):
    fake = Fake()
    fake.add(is_remote_check, (255, "", "ssh: connect to host port 2222: Connection refused"))
    monkeypatch.setattr(tunnel, "_run", fake)

    with pytest.raises(RoffError) as exc:
        tunnel.tunnel_start(ALIAS, LPORT, RPORT)
    assert exc.value.code == EXIT_UNREACHABLE
    assert exc.value.fields.get("detail")


# --------------------------------------------------------------------------------------
# R3 — master ssh cũ giữ port
# --------------------------------------------------------------------------------------
def test_stale_roff_master_holding_port_is_cancelled_not_killed(state_dir, monkeypatch, no_kill):
    """Master cũ CỦA ROFF giữ lport → `ssh -O cancel`; không kill pid (bug R3 thật)."""
    calls = {"ss": 0}

    def ss_response(argv, joined):
        calls["ss"] += 1
        if calls["ss"] == 1:
            return (
                0,
                "LISTEN 0 128 127.0.0.1:18000 0.0.0.0:* users:((\"ssh\",pid=4242,fd=7))\n",
                "",
            )
        return (0, "", "")

    fake = Fake()
    fake.add(is_remote_check, (0, SS_LISTEN, ""))
    fake.add(is_ss, ss_response)
    fake.add(lambda a, j: mux_cmd(a) == "check", (0, "Master running (pid=4242)\n", ""))
    monkeypatch.setattr(tunnel, "_run", fake)
    # pid 4242 là master của chính roff (ControlPath trùng)
    monkeypatch.setattr(
        tunnel,
        "_pid_cmdline",
        lambda pid: f"ssh -o ControlPath={tunnel.control_path(ALIAS)} -f -N {ALIAS}",
    )

    t = tunnel.tunnel_start(ALIAS, LPORT, RPORT)

    assert fake.cancelled(), "phải huỷ forward bằng ssh -O cancel"
    assert not fake.killed(), "TUYỆT ĐỐI KHÔNG kill pid"
    assert t.lport == LPORT


def test_foreign_process_holding_port_is_never_killed(state_dir, monkeypatch, no_kill):
    """Pid của người dùng giữ port → báo lỗi, không kill, không cancel của người ta."""
    fake = Fake()
    fake.add(is_remote_check, (0, SS_LISTEN, ""))
    fake.add(
        is_ss,
        (
            0,
            "LISTEN 0 128 0.0.0.0:18000 0.0.0.0:* users:((\"node\",pid=31337,fd=20))\n",
            "",
        ),
    )
    monkeypatch.setattr(tunnel, "_run", fake)
    monkeypatch.setattr(tunnel, "_pid_cmdline", lambda pid: "node server.js")

    with pytest.raises(RoffError) as exc:
        tunnel.tunnel_start(ALIAS, LPORT, RPORT)

    assert exc.value.reason == "local_port_busy"
    assert not fake.killed()
    assert not fake.cancelled(), "không được đụng forward của người khác"


# --------------------------------------------------------------------------------------
# ControlMaster của roff
# --------------------------------------------------------------------------------------
def test_control_master_options_are_used(state_dir, monkeypatch):
    fake = Fake()
    fake.add(is_remote_check, (0, SS_LISTEN, ""))
    fake.add(is_ss, (0, "", ""))
    fake.add(lambda a, j: mux_cmd(a) == "check", (0, "Master running (pid=555)\n", ""))
    monkeypatch.setattr(tunnel, "_run", fake)

    tunnel.tunnel_start(ALIAS, LPORT, RPORT)

    joined = fake.joined()
    assert f"ControlPath={tunnel.control_path(ALIAS)}" in joined
    assert "ControlMaster=auto" in joined
    assert "ControlPersist=300" in joined


def test_control_path_is_per_alias_and_private(state_dir, monkeypatch):
    fake = Fake()
    fake.add(is_remote_check, (0, SS_LISTEN, ""))
    fake.add(is_ss, (0, "", ""))
    fake.add(lambda a, j: mux_cmd(a) == "check", (0, "Master running (pid=1)\n", ""))
    monkeypatch.setattr(tunnel, "_run", fake)

    tunnel.tunnel_start(ALIAS, LPORT, RPORT)

    path = Path(tunnel.control_path(ALIAS))
    name = path.name
    # Tên socket **gắn fingerprint** tham số ssh đã resolve (sửa B14):
    # `<alias>-<8 hex>.sock` — đổi `~/.ssh/config` ⇒ socket khác ⇒ master khác.
    #
    # Nhưng đường dẫn **phải** ≤ 88 byte (`sun_path` của Unix socket) ⇒ khi
    # `$ROFF_STATE_DIR` nằm sâu trong `/tmp` (đúng trường hợp của pytest
    # `tmp_path`), `control_path` **rút gọn** thành `cm-<hash>-<alias>.sock`.
    # ⇒ kiểm tra **hợp đồng** (có alias, đủ dài, ≤ giới hạn socket), **không**
    # kiểm tra tên cụ thể — nếu không thì test đỏ vì lý do **độ dài đường dẫn**
    # chứ không phải vì lỗi sản phẩm (đo thật 23/11/2026, sau B25).
    assert name.endswith(".sock")
    assert ALIAS in name
    assert len(name) > len(ALIAS) + len(".sock")
    assert len(str(path)) <= tunnel.sshx.UNIX_SOCKET_MAX_PATH
    assert "cm" in str(path)
    assert tunnel.control_path("muse-b") != path
    # Socket phải **riêng tư**: thư mục chứa nó chỉ owner đọc/ghi được.
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


# --------------------------------------------------------------------------------------
# state: list / stop
# --------------------------------------------------------------------------------------
def test_start_records_tunnel_and_list_returns_it(state_dir, monkeypatch):
    fake = Fake()
    fake.add(is_remote_check, (0, SS_LISTEN, ""))
    fake.add(is_ss, (0, "", ""))
    fake.add(lambda a, j: mux_cmd(a) == "check", (0, "Master running (pid=321)\n", ""))
    monkeypatch.setattr(tunnel, "_run", fake)

    tunnel.tunnel_start(ALIAS, LPORT, RPORT)

    tunnels = tunnel.tunnel_list()
    assert len(tunnels) == 1
    assert tunnels[0].alias == ALIAS
    assert tunnels[0].lport == LPORT
    assert tunnels[0].rport == RPORT


def test_stop_cancels_forward_and_forgets_record(state_dir, monkeypatch, no_kill):
    fake = Fake()
    fake.add(is_remote_check, (0, SS_LISTEN, ""))
    fake.add(is_ss, (0, "", ""))
    fake.add(lambda a, j: mux_cmd(a) == "check", (0, "Master running (pid=321)\n", ""))
    monkeypatch.setattr(tunnel, "_run", fake)

    tunnel.tunnel_start(ALIAS, LPORT, RPORT)
    result = tunnel.tunnel_stop(ALIAS, LPORT)

    assert result.get("ok") is True
    assert result.get("stopped") is True
    assert fake.cancelled()
    assert not fake.killed()
    assert tunnel.tunnel_list() == []


def test_stop_unknown_tunnel_is_noop(state_dir, monkeypatch):
    fake = Fake()
    monkeypatch.setattr(tunnel, "_run", fake)
    result = tunnel.tunnel_stop(ALIAS, LPORT)
    assert result.get("ok") is True
    assert result.get("stopped") is False


def test_tunnel_list_empty_without_state(state_dir):
    assert tunnel.tunnel_list() == []


# --------------------------------------------------------------------------------------
# input
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize("lport,rport", [(0, 8000), (70000, 8000), (18000, 0), (18000, 70000)])
def test_invalid_ports_are_input_error(state_dir, monkeypatch, lport, rport):
    fake = Fake()
    monkeypatch.setattr(tunnel, "_run", fake)
    with pytest.raises(RoffError) as exc:
        tunnel.tunnel_start(ALIAS, lport, rport)
    assert exc.value.code == EXIT_INPUT
    assert not fake.calls, "phải validate trước khi gọi ssh"


def test_module_prints_nothing_to_stdout(state_dir, monkeypatch, capsys):
    fake = Fake()
    fake.add(is_remote_check, (0, SS_LISTEN, ""))
    fake.add(is_ss, (0, "", ""))
    fake.add(lambda a, j: mux_cmd(a) == "check", (0, "Master running (pid=1)\n", ""))
    monkeypatch.setattr(tunnel, "_run", fake)

    tunnel.tunnel_start(ALIAS, LPORT, RPORT)
    tunnel.tunnel_list()
    out = capsys.readouterr()
    assert out.out == "", "module không được in ra stdout (chỉ cli mới in JSON)"
