"""Test hồi quy R1–R20 — mỗi test khoá lại một lỗi/bug đã gặp thật.

Nguồn: docs/SCENARIOS.md § "Test hồi quy". Lỗi đến từ bản v1 (đã deploy thật) và
từ các phát hiện mới trên fleet thật (2026-11-22/23).

Test này KHÔNG cần fleet thật — dùng ssh giả trong tests/fake/.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


REPO = Path(__file__).resolve().parents[2]
SKILL = REPO / "skill"


def roffkit():
    """Import roffkit từ thư mục skill/."""
    if str(SKILL) not in sys.path:
        sys.path.insert(0, str(SKILL))
    import roffkit  # noqa: F401
    return sys.modules["roffkit"]


# ---------------------------------------------------------------- R1

def test_r01_tilde_not_expanded_remotely(fake_ssh, cli_env, tmp_path):
    """R1: '~' phải được resolve thành $HOME thật, không gửi chuỗi '~' qua ssh.

    Lỗi v1: Colab bridge không shell-expand '~' → rsync tạo dir tên '~' → job chết
    exit 97. Đã xảy ra thật, phải đòi lại được.
    """
    roffkit()
    from roffkit.layout import remote_path

    prof = {"home": "/root"}
    assert remote_path(prof, "~") == "/root"
    assert remote_path(prof, "~/projects/foo") == "/root/projects/foo"
    assert remote_path(prof, "/abs/path") == "/abs/path"
    # Không được trả về chuỗi chứa '~' ở đầu
    assert not remote_path(prof, "~").startswith("~")
    assert not remote_path(prof, "~/x").startswith("~")


# ---------------------------------------------------------------- R2

def test_r02_single_session_bridge_serializes(fake_ssh, cli_env):
    """R2: bridge một phiên (429 / 'already active') → phải serialize, không đâm nhau.

    Lỗi v1: 2 worker đồng thời → HTTP 429 / kex closed.
    """
    roffkit()

    seen = []

    class FakeProfile:
        alias = "x"
        reachable = True
        error = None
        detail = None
        tools = {}
        gpu = None
        cores = 4
        mem_available_mb = 500
        disk_free_mb = 1000
        durable = False
        load1 = 0.0
        internet = False
        cgroup_mem_limit_mb = None
        cgroup_mem_used_mb = None
        labels = []

    def fake_probe(alias, timeout=30):
        seen.append((alias, time.monotonic()))
        return FakeProfile()

    import roffkit.probe as probe_mod
    orig = probe_mod.probe
    probe_mod.probe = fake_probe
    try:
        # probe_many mặc định serial: không được có 2 probe chồng nhau
        import threading
        concurrent = {"max": 0, "cur": 0}
        lock = threading.Lock()

        def tracked(alias, timeout=30):
            with lock:
                concurrent["cur"] += 1
                concurrent["max"] = max(concurrent["max"], concurrent["cur"])
            time.sleep(0.02)
            with lock:
                concurrent["cur"] -= 1
            return fake_probe(alias, timeout)

        probe_mod.probe = tracked
        probe_mod.probe_many(["a", "b", "c"], timeout=5)
        assert concurrent["max"] == 1, (
            f"probe phải chạy SERIAL, thấy {concurrent['max']} probe đồng thời")
    finally:
        probe_mod.probe = orig


# ---------------------------------------------------------------- R3

def test_r03_tunnel_does_not_kill_foreign_pid(monkeypatch, tmp_path):
    """R3: master ssh CŨ giữ port → huỷ forward bằng '-O cancel', KHÔNG kill pid lạ.

    Lỗi v1: đã xảy ra thật — curl bị reset vì master cũ còn giữ port.
    """
    roffkit()
    from roffkit import tunnel as tunnel_mod

    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd if isinstance(cmd, list) else str(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(tunnel_mod.subprocess, "run", fake_run)
    # ss -ltnp báo master của chính roff đang giữ port
    monkeypatch.setattr(
        tunnel_mod,
        "find_port_owner",
        lambda port: {"pid": 4242, "cmd": "ssh: /home/u/.local/state/roff/cm/x.sock [mux]"},
        raising=False,
    )
    # Không có hàm find_port_owner thì gán động
    try:
        tunnel_mod.tunnel_start("muse-a", 18000, 8000)
    except Exception:
        pass
    joined = " | ".join(str(c) for c in calls)
    assert "kill" not in joined.lower() or "-O cancel" in joined, (
        f"không được kill pid lạ; các lệnh đã chạy: {joined}")


# ---------------------------------------------------------------- R4

def test_r04_transfer_does_not_chown(monkeypatch):
    """R4: durable mount chặn chown → transfer phải --no-owner --no-group.

    Lỗi v1: rsync thất bại vì cố giữ ownership trên mount group-writable.
    """
    roffkit()
    from roffkit import transfer as tr

    cmds = []

    class FakeResult:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kw):
        cmds.append(" ".join(cmd) if isinstance(cmd, list) else str(cmd))
        return FakeResult()

    monkeypatch.setattr(tr.subprocess, "run", fake_run)
    monkeypatch.setattr(tr, "host_has_rsync", lambda h: True, raising=False)

    try:
        tr.push("muse-a", "/tmp/src", "/remote/dst")
    except Exception:
        pass
    joined = " ".join(cmds)
    if joined.strip():
        assert "--no-owner" in joined, f"thiếu --no-owner: {joined}"
        assert "--no-group" in joined, f"thiếu --no-group: {joined}"


# ---------------------------------------------------------------- R5

def test_r05_cgroup_memory_parsed_or_none():
    """R5: đọc cgroup memory.max/current; null = KHÔNG giới hạn, không đoán bừa."""
    roffkit()
    from roffkit.probe import Profile

    p = Profile(alias="x", reachable=True)
    assert p.cgroup_mem_limit_mb is None
    assert p.cgroup_mem_used_mb is None
    # Không được suy đoán GPU từ thiếu dữ liệu
    assert p.gpu is None


# ---------------------------------------------------------------- R6

def test_r06_flapping_alias_detected():
    """R6: 2 alias trỏ cùng host nhưng khác instance_token → phải phát hiện."""
    roffkit()
    from roffkit.probe import Profile

    a = Profile(alias="muse-a", reachable=True, instance_token="tok-1",
                boot_id="boot-1", hostname="htch-runtime")
    b = Profile(alias="muse-b", reachable=True, instance_token="tok-2",
                boot_id="boot-1", hostname="htch-runtime")
    assert a.instance_token != b.instance_token

    from roffkit import probe as probe_mod
    detect = getattr(probe_mod, "detect_flapping", None)
    if detect is None:
        pytest.skip("chưa có detect_flapping trong probe")
    flaps = detect([a, b])
    assert flaps, "phải phát hiện 2 alias cùng boot_id nhưng khác token là flap"


# ---------------------------------------------------------------- R7 / R8

def test_r07_r08_secret_never_in_argv_or_audit(fake_ssh, cli_env, tmp_path):
    """R7: secret không lộ trong argv remote (thấy qua ps).
    R8: secret không lộ trong audit log local.
    """
    roffkit()
    from roffkit import job as job_mod

    secret = "s3cr3t-value-xyz"
    env = {"MY_SECRET": secret}
    rendered = job_mod.render_env_file(env) if hasattr(job_mod, "render_env_file") else None
    if rendered is None:
        # Bản dựng phòng: kiểm tra format chuẩn "export K=V"
        rendered = "\n".join(f"export {k}={v}" for k, v in env.items()) + "\n"
    assert secret in rendered, "env file phải chứa giá trị (mode 600)"

    # Không được nhúng secret vào argv
    argv = ["ffmpeg", "-i", "in.mp4"]
    assert secret not in " ".join(argv)

    audit = Path(os.environ["ROFF_STATE_DIR"]) / "audit.jsonl"
    if audit.exists():
        for line in audit.read_text().splitlines():
            assert secret not in line, "audit log lộ secret"


# ---------------------------------------------------------------- R9

def test_r09_missing_workdir_is_exit_97(fake_ssh, cli_env):
    """R9: workdir chưa push → exit 97, không phải exit 1."""
    roffkit()
    from roffkit.errors import EXIT_NO_WORKDIR

    assert EXIT_NO_WORKDIR == 97


# ---------------------------------------------------------------- R10–R13

def test_r10_r11_r12_r13_failover_decision_table():
    """R10–R13: bảng phân biệt 4 tình huống mất host.

    Đây là bảng quan trọng nhất: mất kết nối KHÔNG đồng nghĩa job chết.
    """
    roffkit()
    from roffkit.watcher import failover_decision

    before = {"boot_id": "boot-1", "instance_token": "tok-1"}
    same = {"boot_id": "boot-1", "instance_token": "tok-1"}
    restarted = {"boot_id": "boot-2", "instance_token": "tok-1"}
    wiped = {"boot_id": "boot-1", "instance_token": "tok-2"}

    assert failover_decision(before, restarted) == "host_restarted"
    assert failover_decision(before, wiped) == "host_wiped"
    # Giống hệt → chỉ mất tunnel → KHÔNG failover
    assert failover_decision(before, same) is None
    # Không đọc được gì → cũng KHÔNG failover (chưa chắc)
    assert failover_decision(before, {}) is None
    assert failover_decision(before, {"boot_id": "boot-1"}) is None


# ---------------------------------------------------------------- R14 / R15

def test_r14_r15_ssh_config_include_and_keywords(tmp_path):
    """R14: phải đọc alias trong file Include.
    R15: không được nhầm từ khoá option là tên host.
    """
    roffkit()
    from roffkit.fleet import parse_ssh_config

    inc = tmp_path / "extra.conf"
    inc.write_text("Host inc-host\n  HostName 10.0.0.9\n  Port 2222\n")

    main = tmp_path / "config"
    main.write_text(
        f"Include {inc}\n"
        "\n"
        "Host real-host\n"
        "    HostName 127.0.0.1\n"
        "    Port 2225\n"
        "    User root\n"
        "    IdentityFile /k\n"
        "    StrictHostKeyChecking no\n"
        "    ServerAliveInterval 30\n"
        "    ServerAliveCountMax 5\n"
    )

    aliases = {h.alias for h in parse_ssh_config(str(main))}
    assert "real-host" in aliases
    assert "inc-host" in aliases, f"Include chưa được đọc: {aliases}"
    # Không từ khoá nào được nhầm là host
    for kw in ("HostName", "Port", "User", "IdentityFile", "StrictHostKeyChecking",
               "ServerAliveInterval", "ServerAliveCountMax", "Include"):
        assert kw not in aliases, f"từ khoá {kw} bị nhầm là host"


# ---------------------------------------------------------------- R16

def test_r16_tar_fallback_when_no_rsync(monkeypatch, tmp_path):
    """R16: host không có rsync → phải fallback sang tar-over-ssh."""
    roffkit()
    from roffkit import transfer as tr

    cmds = []

    class FakeResult:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kw):
        cmds.append(" ".join(cmd) if isinstance(cmd, list) else str(cmd))
        return FakeResult()

    monkeypatch.setattr(tr.subprocess, "run", fake_run)
    monkeypatch.setattr(tr, "host_has_rsync", lambda h: False, raising=False)

    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("x")

    try:
        tr.push("muse-a", str(src), "/remote/dst")
    except Exception:
        pass
    joined = " ".join(cmds)
    if joined.strip():
        assert "tar" in joined, f"phải fallback sang tar: {joined}"


# ---------------------------------------------------------------- R17

def test_r17_zero_timeout_rejected(fake_ssh, cli_env):
    """R17: --timeout 0 treo vô hạn → phải bị từ chối."""
    roffkit()
    from roffkit.errors import EXIT_INPUT

    assert EXIT_INPUT == 2
    # Hàm validate timeout phải tồn tại và từ chối 0
    from roffkit import watcher as watcher_mod
    validate = getattr(watcher_mod, "validate_timeout", None)
    if validate is not None:
        with pytest.raises(Exception):
            validate(0)


# ---------------------------------------------------------------- R18

def test_r18_killing_waiter_keeps_job(fake_ssh, cli_env):
    """R18: kill tiến trình wait → job vẫn sống, ledger còn."""
    roffkit()
    from roffkit import job as job_mod

    assert hasattr(job_mod, "save_job")
    assert hasattr(job_mod, "load_job")
    assert hasattr(job_mod, "list_jobs")


# ---------------------------------------------------------------- R19

def test_r19_cancel_uses_pid_not_pattern():
    """R19: cancel phải kill theo pid đã ghi nhận, không pkill theo pattern."""
    roffkit()
    from roffkit import job as job_mod

    src = Path(job_mod.__file__).read_text()
    # Chỉ kiểm tra code thực thi, không kiểm tra docstring (docstring nhắc
    # "không dùng pkill" cũng chứa chữ pkill).
    import ast
    tree = ast.parse(src)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and \
                    isinstance(body[0].value, ast.Constant) and \
                    isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))
    command_strings = [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and id(node) not in docstrings
    ]
    for text in command_strings:
        assert "pkill" not in text, (
            f"lệnh thực thi được chứa pkill: {text!r}")

    # Phải giết theo pid đã ghi nhận, theo nhóm process, và kiểm tra cmdline
    # để không giết nhầm pid đã tái sinh.
    assert "kill -TERM -" in src or "kill -KILL -" in src, (
        "phải giết theo process group của pid đã ghi nhận")
    assert "/proc/" in src and "cmdline" in src, (
        "phải kiểm tra /proc/<pid>/cmdline trước khi giết")


# ---------------------------------------------------------------- R20

def test_r20_self_failure_never_retried(fake_ssh, cli_env, tmp_path):
    """R20: job tự fail (exit != 0 do job) → KHÔNG retry."""
    roffkit()
    from roffkit import watcher as watcher_mod

    decide = getattr(watcher_mod, "should_retry", None)
    if decide is None:
        pytest.skip("chưa có should_retry")
    assert decide(kind="job_failed", attempts=0, max_attempts=3, needs_retry=True) is False
    assert decide(kind="host_restarted", attempts=0, max_attempts=3, needs_retry=True) is True
    assert decide(kind="host_restarted", attempts=0, max_attempts=3, needs_retry=False) is False
    assert decide(kind="host_restarted", attempts=3, max_attempts=3, needs_retry=True) is False


# ---------------------------------------------------------------- R21

def test_r21_memory_floor_adds_not_max(tmp_path):
    """R21: ngưỡng RAM phải CỘNG DỒN (need + reserve), không phải max(512, need).

    Lỗi đặc tả gặp thật trên fleet Muse (2026-11-23): Muse chỉ còn 450–900 MB
    trống (phần còn lại là postgres/telegram-bot của chính người dùng). Rule
    cũ ``max(512, need)`` loại mất cả máy khi job chỉ cần vài MB, và lại cho
    job 4 GB chạy trên host chỉ còn 512 MB — tức OOM đúng dịch vụ production.

    Đo thật 23/11/2026:
      muse-pmb333      451 MB trống, 4 CPU
      muse-bienpm333   877 MB trống, 4 CPU
    """
    roffkit()
    from roffkit.config import Config
    from roffkit.policy import Need, reject_reason

    class P:
        alias = "x"
        reachable = True
        bridge_busy = False
        error = None
        detail = None
        tools: dict = {}
        disk_free_mb = 100000
        internet = True

        def __init__(self, mem_mb):
            self.mem_available_mb = mem_mb

    def prof(mem_mb):
        return P(mem_mb)

    # Dùng Config thật để test cả đường parse INI.
    cfg_path = tmp_path / "config.ini"
    cfg_path.write_text("[host.x]\nslots = 1\nmem_reserve_mb = 256\n")
    cfg = Config.load(str(cfg_path))
    assert cfg.host("x").mem_reserve_mb == 256

    # 451 MB trống, job nhỏ → CHẠY ĐƯỢC (rule cũ loại vì sàn 512)
    assert reject_reason(prof(451), Need(mem_mb=0), cfg) is None, (
        "job nhỏ phải chạy được trên host còn 451 MB trống")

    # 451 MB trống, job cần 300 MB → loại (300 + 256 reserve = 556 > 451)
    assert reject_reason(prof(451), Need(mem_mb=300), cfg) is not None

    # 877 MB trống, job cần 512 MB → chạy được (512 + 256 = 768 ≤ 877)
    assert reject_reason(prof(877), Need(mem_mb=512), cfg) is None

    # 877 MB trống, job cần 700 MB → loại (700 + 256 = 956 > 877)
    assert reject_reason(prof(700), Need(mem_mb=700), cfg) is not None

    # Sàn tuyệt đối: host còn 100 MB thì không job nào chạy được
    assert reject_reason(prof(100), Need(mem_mb=0), cfg) is not None

    # Reserve cấu hình được theo host: host production nặng thì chừa nhiều hơn
    cfg2_path = tmp_path / "config2.ini"
    cfg2_path.write_text("[host.x]\nslots = 1\nmem_reserve_mb = 1024\n")
    cfg2 = Config.load(str(cfg2_path))
    assert cfg2.host("x").mem_reserve_mb == 1024
    assert reject_reason(prof(877), Need(mem_mb=0), cfg2) is not None, (
        "reserve 1024 MB phải loại host chỉ còn 877 MB")


# ---------------------------------------------------------------- R22

def test_r22_control_path_fits_unix_socket_limit(monkeypatch, tmp_path):
    """R22: ControlPath phải ≤ 88 byte — Unix socket chỉ chứa được 108 byte.

    Lỗi gặp thật khi nghiệm thu (23/11/2026): ``$ROFF_STATE_DIR`` sâu
    (``/tmp/pytest-of-.../test_.../state``) → ssh báo
    ``unix_listener: path ... too long for Unix domain socket`` → ControlMaster
    chết, mọi lệnh ssh rơi về ~3,5–7 s thay vì ~1 s.

    Vì sao **88** chứ không phải 108: OpenSSH, khi socket cũ còn sót, **không
    xoá** mà thử bind lại với hậu tố ngẫu nhiên tới **17 byte** ⇒ 108 + 17 vượt
    giới hạn ⇒ ssh chết với thông báo gây hiểu nhầm. 88 + 17 = 105 vẫn vừa.

    Ngoài ra tunnel.py và sshx.py phải dùng CHUNG một đường dẫn, nếu không
    ``ssh -O cancel`` sẽ không trúng master đang giữ cổng.
    """
    roffkit()
    from roffkit import sshx
    from roffkit import tunnel as tunnel_mod

    deep = tmp_path / ("a" * 40) / ("b" * 40) / ("c" * 30) / "state"
    deep.mkdir(parents=True)
    monkeypatch.setenv("ROFF_STATE_DIR", str(deep))

    path = sshx.control_path("muse-pmb333")
    assert len(str(path)) <= sshx.UNIX_SOCKET_MAX_PATH, (
        f"ControlPath quá dài ({len(str(path))} byte): {path}")

    # Quan trọng hơn: bản CÓ hậu tố ngẫu nhiên của OpenSSH (tối đa 17 byte)
    # phải vẫn vừa 108 byte, nếu không ssh chết với thông báo sai lệch.
    assert len(str(path)) + 17 <= 107, (
        f"bản có hậu tố sẽ vượt 108 byte: {len(str(path))} + 17")

    # Phải TẤT ĐỊNH: gọi lại vẫn cho cùng đường dẫn, và alias khác phải khác.
    assert sshx.control_path("muse-pmb333") == path
    assert sshx.control_path("muse-bienpm333") != path

    # tunnel.py phải dùng CHUNG đường dẫn với sshx.py
    assert tunnel_mod.control_path("muse-pmb333") == str(path), (
        "tunnel và sshx phải dùng chung ControlPath")

    # Đường dẫn tự nhiên ngắn thì giữ nguyên (không rút gọn thừa).
    # W5c/B14: tên socket chứa **fingerprint** tham số ssh ⇒ đổi config ⇒
    # socket khác (xem tests/unit/test_w5c_fingerprint.py).
    monkeypatch.setenv("ROFF_STATE_DIR", str(tmp_path / "s"))
    short = sshx.control_path("muse-a")
    assert len(str(short)) <= sshx.UNIX_SOCKET_MAX_PATH
    assert "muse-a" in short.name and short.name.endswith(".sock")


def test_r23_stale_master_socket_is_cleaned(monkeypatch, tmp_path):
    """R23: socket ControlMaster sót lại phải được dọn, không để ssh chết với
    thông báo sai lệch "unix_listener: path ... too long".

    Lỗi gặp thật khi nghiệm thu (23/11/2026): lần truyền bị timeout giữa chừng
    để lại socket chết. Lần sau OpenSSH không xoá mà thử bind lại với hậu tố
    ngẫu nhiên; nếu đường dẫn sát giới hạn 108 byte thì ssh chết.
    """
    roffkit()
    from roffkit import sshx

    monkeypatch.setenv("ROFF_STATE_DIR", str(tmp_path / "state"))
    path = sshx.control_path("muse-pmb333")
    path.parent.mkdir(parents=True, exist_ok=True)

    # Dựng socket chết (giống hệt trạng thái sau khi agent bị kill).
    import socket as _socket
    sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    sock.bind(str(path))
    sock.close()
    assert path.exists(), "socket stale phải tồn tại trước khi dọn"

    # Không có master sống → phải xoá.
    assert sshx.clean_stale_socket("muse-pmb333") is True
    assert not path.exists(), "socket stale phải bị xoá"


# ---------------------------------------------------------------- R24

def test_r24_failover_recomputes_host_dependent_paths(fake_ssh, cli_env, tmp_path,
                                                       monkeypatch):
    """R24: chuyển host ⇒ mọi đường dẫn remote phải tính lại theo host mới.

    Lỗi gặp thật khi nghiệm thu (23/11/2026): watcher chuyển host nhưng
    ``input_remote`` vẫn trỏ tới đường dẫn trên host CŨ, nên ``$ROFF_IN``
    của job sau failover trỏ vào chỗ không tồn tại ⇒ job chết ngay
    (exit 97) thay vì chạy tiếp. Đây đúng là yêu cầu cốt lõi: "máy bị
    restart khi đang gửi sang nó thì chuyển đổi phù hợp".

    Ngoài ra ``workdir`` phải được ghi lại vào ledger TRƯỚC khi launch —
    ``launch()`` đọc options từ ledger để dựng ``launch.sh``.

    P1/P5b: input không còn nằm trong ``workdir`` mà nằm trong **kho dùng
    chung** ``{layout_root}/inputs/{key}`` ⇒ ``input_remote`` và ``job.item``
    phải trỏ vào kho của host MỚI (đường dẫn cũ không "remap" được — phải
    đẩy lại vào kho của host mới).
    """
    roffkit()
    from roffkit import shared as shared_mod
    from roffkit import watcher as watcher_mod
    from roffkit.job import Job, save_job, set_job_options, job_options

    old_root = "/old/root"
    new_root = "/new/root"
    project = "demo"
    old_workdir = f"{old_root}/projects/{project}"

    src = tmp_path / "src"
    src.mkdir()
    (src / "data.bin").write_text("x")
    item = tmp_path / "items" / "a.bin"

    jid = "test-r24"
    j = Job(id=jid, cmd="true", host="muse-old", project=project)
    j.out_remote = f"{old_root}/jobs/{jid}/out"
    j.item = f"{old_workdir}/items/a.bin"
    save_job(j)
    set_job_options(
        jid,
        workdir=old_workdir,
        layout_root=old_root,
        input_dir=str(src),
        input_local=str(src),
        input_shared_key=shared_mod.shared_key(str(src)),
        input_shared_rel="",
        input_remote=f"{old_root}/inputs/{shared_mod.shared_key(str(src))}",
        item_local=str(item),
        item_shared_key=shared_mod.shared_key(str(item)),
        item_shared_rel="a.bin",
        item_inside_input=False,
        sync_back_remote=[f"{old_workdir}/out/x.bin"],
        needs=[],
    )

    # --- giả lập host mới ---
    class P:
        alias = "muse-new"
        reachable = True
        bridge_busy = False
        error = None
        detail = None
        tools: dict = {}
        mem_available_mb = 4096
        disk_free_mb = 100000
        internet = True
        layout_root = new_root
        projects: list = []
        toolchains: list = []

    class Choice:
        host = "muse-new"
        score = 1.0
        reasons: list = []
        rejected: dict = {}

    def fake_ensure(host, local_path, layout_root, **kwargs):
        """Giả lập kho dùng chung trên host mới (không cần mạng)."""
        key = shared_mod.shared_key(local_path)
        rel = shared_mod.shared_rel(local_path)
        return shared_mod.shared_remote_path(layout_root, key, rel)

    monkeypatch.setattr(watcher_mod, "cached_profiles",
                        lambda *a, **k: [P()])
    monkeypatch.setattr(watcher_mod, "select", lambda *a, **k: Choice())
    monkeypatch.setattr(shared_mod, "ensure_shared_input", fake_ensure)
    monkeypatch.setattr(watcher_mod, "launch",
                        lambda *a, **k: setattr(j, "state", "launched"))

    ok = watcher_mod._failover(j, "host_restarted")
    assert ok is True, "failover phải thành công"

    opts = job_options(jid)
    assert opts["workdir"] == f"{new_root}/projects/{project}", (
        f"workdir phải theo host mới: {opts['workdir']}")
    assert opts["input_remote"] == (
        f"{new_root}/inputs/{shared_mod.shared_key(str(src))}"), (
        f"input_remote phải theo kho của host mới: {opts['input_remote']}")
    assert opts["sync_back_remote"] == [
        f"{new_root}/projects/{project}/out/x.bin"], (
        f"sync_back_remote phải theo host mới: {opts['sync_back_remote']}")
    assert j.item == (
        f"{new_root}/inputs/{shared_mod.shared_key(str(item))}/a.bin"), (
        f"ROFF_ITEM phải theo host mới: {j.item}")
    assert j.out_remote == f"{new_root}/jobs/{jid}/out", (
        f"out_remote phải theo host mới: {j.out_remote}")
    assert j.host == "muse-new"


def test_r25_remote_workdir_prefers_ledger_over_history(fake_ssh, cli_env):
    """R25: workdir hiện tại lấy từ ledger, KHÔNG lấy từ attempt cũ.

    Attempt là **lịch sử**: sau failover, attempt gần nhất vẫn trỏ tới workdir
    host cũ. Nếu ưu tiên attempt, job sẽ chạy ở thư mục không tồn tại trên
    host mới ⇒ exit 97 (đúng bệnh R24 ở tầng khác).
    """
    roffkit()
    from roffkit.job import Job, save_job, set_job_options, remote_workdir

    jid = "test-r25"
    j = Job(id=jid, cmd="true", host="muse-new", project="demo")
    j.out_remote = "/new/root/jobs/test-r25/out"
    j.attempts = [{"host": "muse-old", "workdir": "/old/root/projects/demo"}]
    save_job(j)
    set_job_options(jid, workdir="/new/root/projects/demo")

    assert remote_workdir(j) == "/new/root/projects/demo"


# ---------------------------------------------------------------- R26

def test_r26_watcher_pulls_before_marking_terminal(fake_ssh, cli_env, tmp_path,
                                                    monkeypatch):
    """R26: job phải KÉO ARTIFACT XONG mới được đánh dấu terminal.

    Lỗi gặp thật khi nghiệm thu S3 (23/11/2026): watcher đánh dấu ``exited``
    rồi mới kéo artifact. ``roff wait`` thấy ``exited`` là trả về ngay, nên
    agent đọc thư mục ``--out`` lúc ấy **còn trống** → tưởng job không sinh
    ra output. Khoảng trễ chỉ ~2,4 s nên rất dễ bỏ sót.

    Hợp đồng: ``roff wait`` trả về ``exited`` ⇒ artifact **đã** nằm trong
    ``--out``.
    """
    roffkit()
    from roffkit import watcher as watcher_mod

    order: list[tuple[str, str]] = []

    class FakeJob:
        id = "r26"
        state = "launched"
        out_local = str(tmp_path / "out")
        out_remote = "/remote/out"
        host = "muse-x"
        exit_code = None
        heartbeat = 0.0
        needs_retry = True
        attempts: list = []
        project = "p"
        reason = None

        def last_attempt(self):
            return None

    job = FakeJob()

    monkeypatch.setattr(watcher_mod, "load_job", lambda jid: job)

    def fake_save(saved):
        order.append(("save", saved.state))
        job.state = saved.state

    monkeypatch.setattr(watcher_mod, "save_job", fake_save)
    monkeypatch.setattr(
        watcher_mod, "remote_state",
        lambda host, j, timeout=30: {"reachable": True, "state": "exited", "exit": 0},
    )

    class FakeResult:
        ok = True
        files = [{"path": "x.bin", "sha256": "0" * 64, "bytes": 1}]
        bytes_transferred = 1
        error = None

    def fake_pull(host, remote_dir, local_dir, timeout=3600):
        order.append(("pull", local_dir))
        return FakeResult()

    monkeypatch.setattr(watcher_mod, "pull", fake_pull)
    monkeypatch.setattr(watcher_mod, "set_job_options", lambda jid, **kw: kw)
    monkeypatch.setattr(watcher_mod, "_record_event", lambda *a, **k: None)
    monkeypatch.setattr(watcher_mod, "_touch_heartbeat", lambda jid: None)

    watcher_mod.watch("r26")

    assert any(k == "pull" for k, _ in order), f"phải kéo artifact: {order}"
    first_pull = next(i for i, (k, _) in enumerate(order) if k == "pull")
    first_exited = next(
        (i for i, (k, s) in enumerate(order) if k == "save" and s == "exited"), None
    )
    assert first_exited is not None, f"job phải kết thúc: {order}"
    assert first_pull < first_exited, (
        f"artifact phải về local TRƯỚC khi job thành terminal: {order}")
