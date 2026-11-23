"""Hồi quy W4 — ``roff tunnel link`` / ``roff tunnel stop`` (link phối hợp A↔B).

Không cần mạng thật: test dùng một **ssh giả** tự viết (stub Python) mô phỏng
đủ 3 thứ:

1. ``ControlPath`` mà ssh **không** tự tạo thư mục ⇒ nếu thư mục socket thiếu
   thì ssh chết với ``unix_listener: cannot bind to path ...`` (sự cố thật
   ngày 23/11/2026 — người nghiệm thu gặp trên fleet Muse);
2. mux client (``-O check/forward/cancel/exit``);
3. lệnh thường (script kiểm tra LISTEN) — trả về ``ss -ltn`` giả.

Mọi lệnh ssh được ghi vào log JSONL để test khẳng định **đúng** nhánh nào
được mở/huỷ (và không đụng nhánh của tunnel khác).
"""

from __future__ import annotations

import json
import os
import socket
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(_HERE))
_SKILL = os.path.join(_REPO, "skill")
if _SKILL not in sys.path:
    sys.path.insert(0, _SKILL)

from conftest import parse_json, run_cli  # noqa: E402

# --------------------------------------------------------------------------
# ssh giả
# --------------------------------------------------------------------------
_STUB_SSH = r'''#!/usr/bin/env python3
"""ssh giả cho test tunnel — mô phỏng mux + hành vi "không tự tạo thư mục"."""
import json
import os
import sys

VAL_OPTS = {"-o", "-p", "-i", "-l", "-F", "-E", "-J", "-b", "-c", "-D", "-e",
            "-I", "-L", "-R", "-W", "-w", "-m", "-Q", "-S", "-t", "-O"}


def main():
    argv = sys.argv[1:]
    with open(os.environ["ROFF_FAKE_TUNNEL"]) as fh:
        spec = json.load(fh)

    control_path = ""
    mux = ""
    forward = ""
    alias = ""
    rest = []
    background = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "-o" and i + 1 < len(argv):
            val = argv[i + 1]
            i += 2
            if val.startswith("ControlPath="):
                control_path = val.split("=", 1)[1]
            continue
        if a == "-O" and i + 1 < len(argv):
            mux = argv[i + 1]
            i += 2
            continue
        if a in ("-L", "-R") and i + 1 < len(argv):
            forward = argv[i + 1]
            i += 2
            continue
        if a == "-f":
            background = True
            i += 1
            continue
        if a in VAL_OPTS:
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        alias = a
        rest = argv[i + 1:]
        break

    log = spec.get("log")
    if log:
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "alias": alias,
                "mux": mux,
                "forward": forward,
                "cmd": " ".join(rest),
            }) + "\n")

    # ssh KHÔNG tự tạo thư mục của ControlPath ⇒ chết nếu thư mục thiếu.
    if control_path:
        parent = os.path.dirname(control_path)
        if not os.path.isdir(parent):
            sys.stderr.write(
                "unix_listener: cannot bind to path %s.AbCdEf: "
                "No such file or directory\n" % control_path)
            return 255

    host = spec.get("hosts", {}).get(alias)
    if host is None:
        sys.stderr.write("ssh: Could not resolve hostname %s\n" % alias)
        return 255
    if host.get("ssh_error"):
        sys.stderr.write(host["ssh_error"] + "\n")
        return 255

    if mux == "check":
        if host.get("master"):
            sys.stdout.write("Master running (pid=%d)\n"
                             % int(host.get("master_pid", 4242)))
            return 0
        sys.stderr.write("Control socket connect(%s): No such file or directory\n"
                         % control_path)
        return 255
    if mux in ("forward", "cancel", "exit"):
        if host.get("fail_" + mux):
            sys.stderr.write("mux %s failed\n" % mux)
            return 255
        return 0
    if background:
        # `ssh -f -N` (mở master): không in gì.
        return 0
    out = host.get("stdout", "")
    if out:
        sys.stdout.write(out)
    return int(host.get("exit", 0))


if __name__ == "__main__":
    sys.exit(main())
'''


def _ss(listen_ports):
    """Output giả của ``ss -ltn`` (có marker để roff nhận đúng nguồn)."""
    lines = ["__ROFF_SRC__=ss"]
    for port in listen_ports:
        lines.append(
            "LISTEN 0      128    127.0.0.1:%d      0.0.0.0:*" % int(port)
        )
    return "\n".join(lines) + "\n"


@pytest.fixture
def fake_tunnel(tmp_path, monkeypatch):
    """ssh giả + state dir riêng. Trả về hàm cấu hình host."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "ssh"
    stub.write_text(_STUB_SSH)
    stub.chmod(0o755)

    spec_path = tmp_path / "tunnel_spec.json"
    log_path = tmp_path / "ssh_log.jsonl"
    spec: dict = {"log": str(log_path), "hosts": {}}
    spec_path.write_text(json.dumps(spec))

    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.setenv("ROFF_FAKE_TUNNEL", str(spec_path))
    monkeypatch.setenv("ROFF_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ROFF_CONFIG", str(tmp_path / "config.ini"))

    def configure(alias: str, **kwargs):
        spec["hosts"][alias] = kwargs
        spec_path.write_text(json.dumps(spec))
        return kwargs

    def save():
        """Ghi lại spec (thay đổi trực tiếp dict trong ``spec``)."""
        spec_path.write_text(json.dumps(spec))

    def calls() -> list[dict]:
        if not log_path.exists():
            return []
        return [
            json.loads(line)
            for line in log_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    configure.calls = calls
    configure.spec = spec
    configure.save = save
    return configure


def _forwards(calls: list[dict], alias: str, mux: str = "forward") -> list[dict]:
    """Các lệnh mux `mux` gửi cho `alias`."""
    return [
        call for call in calls
        if call["alias"] == alias and call["mux"] == mux
    ]


# --------------------------------------------------------------------------
# Sự cố thật 23/11/2026: <state>/cm/ chưa tồn tại ⇒ ssh chết ngay
# --------------------------------------------------------------------------
def test_link_creates_socket_dir_when_state_dir_missing(fake_tunnel, tmp_path,
                                                        monkeypatch):
    """State dir mới ⇒ roff phải **tự tạo** ``<state>/cm/``.

    Trước khi sửa: ``ssh`` chết với ``unix_listener: cannot bind to path ...:
    No such file or directory`` ⇒ ``tunnel link`` chết với exit 255.
    """
    state = tmp_path / "state-chua-co"
    assert not state.exists()
    monkeypatch.setenv("ROFF_STATE_DIR", str(state))
    fake_tunnel("muse-a", master=True, stdout=_ss([19000]))
    fake_tunnel("muse-b", master=True, stdout=_ss([18899]))

    rc, out, _err = run_cli([
        "tunnel", "link",
        "--from", "muse-a", "--to", "muse-b",
        "--rport", "19000", "--to-port", "18899",
    ])

    assert "cannot bind" not in out, out
    assert (state / "cm").is_dir(), "phải tự tạo thư mục socket"
    assert rc == 0, out
    payload = parse_json(out)
    assert payload["ok"] is True
    assert payload["a_listening"] is True
    assert payload["b_listening"] is True


def test_link_reports_socket_dir_created(fake_tunnel, tmp_path, monkeypatch):
    """Lỗi bind socket **không** được báo nhầm là "host chết"."""
    state = tmp_path / "state-chua-co-2"
    monkeypatch.setenv("ROFF_STATE_DIR", str(state))
    # B không có gì nghe ⇒ lỗi nghiệp vụ (exit 1), không phải lỗi socket.
    fake_tunnel("muse-a", master=True, stdout=_ss([19000]))
    fake_tunnel("muse-b", master=True, stdout=_ss([]))

    rc, out, _err = run_cli([
        "tunnel", "link",
        "--from", "muse-a", "--to", "muse-b",
        "--rport", "19000", "--to-port", "18899",
    ])

    assert "cannot bind" not in out, out
    assert (state / "cm").is_dir()
    assert rc == 1, out
    payload = parse_json(out)
    assert payload["reason"] == "remote_not_listening"


# --------------------------------------------------------------------------
# Hợp đồng JSON của link
# --------------------------------------------------------------------------
def test_link_opens_both_branches_and_reports_keys(fake_tunnel):
    """Link mở **hai** nhánh: ``-L`` (qua B) và ``-R`` (trên A)."""
    fake_tunnel("muse-a", master=True, stdout=_ss([19000]))
    fake_tunnel("muse-b", master=True, stdout=_ss([18899]))

    rc, out, _err = run_cli([
        "tunnel", "link",
        "--from", "muse-a", "--to", "muse-b",
        "--rport", "19000", "--to-port", "18899",
    ])

    assert rc == 0, out
    payload = parse_json(out)
    for key in ("ok", "exit_code", "from", "to", "rport", "to_port",
                "via_lport", "a_listening", "b_listening", "note"):
        assert key in payload, f"thiếu khoá {key}: {payload}"
    assert payload["from"] == "muse-a"
    assert payload["to"] == "muse-b"
    assert payload["rport"] == 19000
    assert payload["to_port"] == 18899
    assert 18500 <= payload["via_lport"] <= 18599

    calls = fake_tunnel.calls()
    l_forwards = _forwards(calls, "muse-b", "forward")
    r_forwards = _forwards(calls, "muse-a", "forward")
    assert len(l_forwards) == 1, calls
    assert len(r_forwards) == 1, calls
    assert l_forwards[0]["forward"].endswith(":18899"), l_forwards
    assert r_forwards[0]["forward"].endswith(
        ":%d" % payload["via_lport"]), r_forwards


def test_link_fails_without_opening_forward_when_b_not_listening(fake_tunnel):
    """B không LISTEN ⇒ lỗi ``remote_not_listening`` và **không** mở forward."""
    fake_tunnel("muse-a", master=True, stdout=_ss([19000]))
    fake_tunnel("muse-b", master=True, stdout=_ss([]))

    rc, out, _err = run_cli([
        "tunnel", "link",
        "--from", "muse-a", "--to", "muse-b",
        "--rport", "19000", "--to-port", "18899",
    ])

    assert rc == 1, out
    payload = parse_json(out)
    assert payload["ok"] is False
    assert payload["reason"] == "remote_not_listening"
    assert payload["remote_listening"] is False
    assert _forwards(fake_tunnel.calls(), "muse-a") == []
    assert _forwards(fake_tunnel.calls(), "muse-b") == []


def test_link_rejects_same_host(fake_tunnel):
    """``--from`` và ``--to`` phải là 2 máy khác nhau."""
    fake_tunnel("muse-a", master=True, stdout=_ss([19000]))
    rc, out, _err = run_cli([
        "tunnel", "link",
        "--from", "muse-a", "--to", "muse-a",
        "--rport", "19000", "--to-port", "18899",
    ])
    assert rc != 0
    payload = parse_json(out)
    assert payload["reason"] == "same_host"


# --------------------------------------------------------------------------
# stop: dọn CẢ HAI nhánh
# --------------------------------------------------------------------------
def test_stop_cancels_both_branches(fake_tunnel):
    """``stop --from A --rport P`` phải huỷ forward trên A **và** trên B."""
    fake_tunnel("muse-a", master=True, stdout=_ss([19000]))
    fake_tunnel("muse-b", master=True, stdout=_ss([18899]))

    rc, out, _err = run_cli([
        "tunnel", "link",
        "--from", "muse-a", "--to", "muse-b",
        "--rport", "19000", "--to-port", "18899",
    ])
    assert rc == 0, out
    payload = parse_json(out)

    rc, out, _err = run_cli(
        ["tunnel", "stop", "--from", "muse-a", "--rport", "19000"]
    )
    assert rc == 0, out

    calls = fake_tunnel.calls()
    cancels_a = _forwards(calls, "muse-a", "cancel")
    cancels_b = _forwards(calls, "muse-b", "cancel")
    assert cancels_a, "phải huỷ forward -R trên máy nhận"
    assert cancels_b, "phải huỷ forward -L qua máy phục vụ"
    assert cancels_a[0]["forward"].endswith(":%d" % payload["via_lport"])

    # Không còn link nào trong state.
    rc, out, _err = run_cli(["tunnel", "list"])
    links = parse_json(out).get("links") or []
    assert links == [], links


def test_stop_does_not_cancel_foreign_forwards(fake_tunnel):
    """Không được huỷ forward của tunnel **khác** trên cùng master."""
    fake_tunnel("muse-a", master=True, stdout=_ss([19000]))
    fake_tunnel("muse-b", master=True, stdout=_ss([18899]))

    rc, out, _err = run_cli([
        "tunnel", "link",
        "--from", "muse-a", "--to", "muse-b",
        "--rport", "19000", "--to-port", "18899",
    ])
    assert rc == 0, out

    # Thêm 1 tunnel **khác** của roff trên cùng host B (cổng 18000).
    state = os.environ["ROFF_STATE_DIR"]
    state_file = os.path.join(state, "tunnels.json")
    data = json.loads(open(state_file).read())
    data["tunnels"].append({
        "alias": "muse-b", "lport": 18000, "rport": 8000,
        "master_pid": 1, "created_at": 0, "note": "tunnel khác",
    })
    with open(state_file, "w") as fh:
        json.dump(data, fh)

    # Huỷ cụ thể bị fail ⇒ code phải **không** huỷ toàn bộ master (tránh
    # giết forward của tunnel kia).
    fake_tunnel.spec["hosts"]["muse-b"]["fail_cancel"] = True
    fake_tunnel.save()
    rc, out, _err = run_cli(
        ["tunnel", "stop", "--from", "muse-a", "--rport", "19000"]
    )
    assert rc == 0, out

    blanket = [
        call for call in fake_tunnel.calls()
        if call["alias"] == "muse-b" and call["mux"] == "cancel"
        and not call["forward"]
    ]
    assert blanket == [], f"không được huỷ toàn bộ master: {blanket}"


def test_stop_cleans_all_when_no_other_tunnel(fake_tunnel):
    """Không còn tunnel nào khác ⇒ được phép huỷ toàn bộ forward của master."""
    fake_tunnel("muse-a", master=True, stdout=_ss([19000]))
    fake_tunnel("muse-b", master=True, stdout=_ss([18899]))
    fake_tunnel.spec["hosts"]["muse-b"]["fail_cancel"] = True
    fake_tunnel.save()

    rc, out, _err = run_cli([
        "tunnel", "link",
        "--from", "muse-a", "--to", "muse-b",
        "--rport", "19000", "--to-port", "18899",
    ])
    assert rc == 0, out

    rc, out, _err = run_cli(
        ["tunnel", "stop", "--from", "muse-a", "--rport", "19000"]
    )
    assert rc == 0, out
    blanket = [
        call for call in fake_tunnel.calls()
        if call["alias"] == "muse-b" and call["mux"] == "cancel"
        and not call["forward"]
    ]
    assert blanket, "phải có bước dọn toàn bộ forward của master"


# --------------------------------------------------------------------------
# Cổng trung gian: chọn bằng cách bind thật
# --------------------------------------------------------------------------
def test_auto_via_port_skips_busy_port(fake_tunnel):
    """Cổng trung gian phải **thử bind thật** — không đoán bừa."""
    fake_tunnel("muse-a", master=True, stdout=_ss([19000]))
    fake_tunnel("muse-b", master=True, stdout=_ss([18899]))

    # Chiếm 1 cổng trong dải 18500-18599.
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    holder.bind(("127.0.0.1", 18500))
    holder.listen(1)
    try:
        rc, out, _err = run_cli([
            "tunnel", "link",
            "--from", "muse-a", "--to", "muse-b",
            "--rport", "19000", "--to-port", "18899",
        ])
    finally:
        holder.close()

    assert rc == 0, out
    payload = parse_json(out)
    assert payload["via_lport"] != 18500, payload
    assert 18500 <= payload["via_lport"] <= 18599


def test_link_refuses_busy_via_port(fake_tunnel):
    """``--via-lport`` đang bị chiếm ⇒ báo lỗi, không mở gì."""
    fake_tunnel("muse-a", master=True, stdout=_ss([19000]))
    fake_tunnel("muse-b", master=True, stdout=_ss([18899]))

    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    holder.bind(("127.0.0.1", 18510))
    holder.listen(1)
    try:
        rc, out, _err = run_cli([
            "tunnel", "link",
            "--from", "muse-a", "--to", "muse-b",
            "--rport", "19000", "--to-port", "18899",
            "--via-lport", "18510",
        ])
    finally:
        holder.close()

    assert rc == 1, out
    payload = parse_json(out)
    assert payload["reason"] == "local_port_busy"
    assert _forwards(fake_tunnel.calls(), "muse-a") == []
    assert _forwards(fake_tunnel.calls(), "muse-b") == []
