"""W5c (bug **B14**) — đổi ``~/.ssh/config`` không được làm roff nói chuyện với
**nhầm máy**.

Sự cố đã đo thật trên máy người dùng (23/11/2026)::

    config A: Host dup-test → 127.0.0.1:2222
      ssh -o ControlPath=<cm>/dup-test.sock -o ControlMaster=auto … dup-test 'hostname'
      ⇒ htch-runtime                       (master đã tạo)
    config B: Host dup-test → 127.0.0.1:9    (cổng chết)
      ssh -o ControlPath=<cm>/dup-test.sock … dup-test 'echo X'
      ⇒ X                                 ← VẪN THÀNH CÔNG, chưa hề nối port 9

Nguyên nhân: socket ControlMaster khoá theo **alias** ⇒ đổi config không đổi
tên socket ⇒ ssh tái dùng master cũ trong suốt ``ControlPersist``.

Cách sửa (W5c): tên socket chứa **fingerprint** của tham số ssh đã resolve
(``HostName``/``Port``/``User``/``IdentityFile``/``ProxyCommand``/``ProxyJump``)
⇒ đổi config ⇒ socket khác ⇒ master mới.

Các test ở đây **không cần mạng**: fingerprint tính từ file config local.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest

_SKILL_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "skill"
)
if _SKILL_DIR not in sys.path:
    sys.path.insert(0, _SKILL_DIR)

from roffkit import fleet  # noqa: E402
from roffkit import probe as probe_mod  # noqa: E402
from roffkit import sshx  # noqa: E402

ALIAS = "dup-test"

CONFIG_A = """\
Host dup-test
    HostName 127.0.0.1
    Port 2222
    User root
    IdentityFile ~/.ssh/id_ed25519
"""

CONFIG_B = """\
Host dup-test
    HostName 127.0.0.1
    Port 9
    User root
    IdentityFile ~/.ssh/id_ed25519
"""


def _write_config(path, text: str) -> str:
    path.write_text(text, encoding="utf-8")
    return str(path)


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    d = tmp_path / "state"
    monkeypatch.setenv("ROFF_STATE_DIR", str(d))
    return d


@pytest.fixture
def short_state(monkeypatch):
    """State dir **ngắn** ⇒ socket nằm ở ``<state>/cm/<alias>-<fp>.sock``.

    ``tmp_path`` của pytest quá dài ⇒ ``control_path`` rút gọn sang thư mục
    runtime; test cần cả hai kiểu nên tách riêng.
    """
    import shutil
    import tempfile

    d = tempfile.mkdtemp(prefix="r", dir="/tmp")
    monkeypatch.setenv("ROFF_STATE_DIR", d)
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


# --------------------------------------------------------------------------
# fingerprint
# --------------------------------------------------------------------------
def test_fingerprint_changes_when_config_changes(tmp_path, monkeypatch):
    cfg_a = _write_config(tmp_path / "a.conf", CONFIG_A)
    cfg_b = _write_config(tmp_path / "b.conf", CONFIG_B)

    monkeypatch.setenv("ROFF_SSH_CONFIG", cfg_a)
    fp_a = fleet.ssh_fingerprint(ALIAS)
    monkeypatch.setenv("ROFF_SSH_CONFIG", cfg_b)
    fp_b = fleet.ssh_fingerprint(ALIAS)

    assert fp_a != fp_b, "đổi Port ⇒ fingerprint phải đổi"
    assert len(fp_a) == 8


def test_fingerprint_stable_for_same_config(tmp_path, monkeypatch):
    cfg_a = _write_config(tmp_path / "a.conf", CONFIG_A)
    monkeypatch.setenv("ROFF_SSH_CONFIG", cfg_a)
    assert fleet.ssh_fingerprint(ALIAS) == fleet.ssh_fingerprint(ALIAS)


def test_fingerprint_needs_no_ssh(tmp_path, monkeypatch):
    """Fingerprint tính từ **file config local** — không gọi ssh (không round-trip)."""
    cfg_a = _write_config(tmp_path / "a.conf", CONFIG_A)
    monkeypatch.setenv("ROFF_SSH_CONFIG", cfg_a)
    monkeypatch.setenv("PATH", "")  # không có `ssh` ⇒ vẫn phải tính được
    assert fleet.ssh_fingerprint(ALIAS)


def test_ssh_host_params_resolves_connection_options(tmp_path, monkeypatch):
    """Resolve đủ: HostName, Port, User, IdentityFile, ProxyJump."""
    cfg = _write_config(tmp_path / "c.conf", """\
Host *
    User roffuser
    IdentityFile ~/.ssh/id_default

Host muse-*
    Port 2200

Host muse-a
    HostName 10.0.0.9
    IdentityFile ~/.ssh/id_a
    ProxyJump jump.example
""")
    monkeypatch.setenv("ROFF_SSH_CONFIG", cfg)

    params = fleet.ssh_host_params("muse-a")
    assert params["hostname"] == "10.0.0.9"
    assert params["port"] == 2200
    assert params["user"] == "roffuser"
    assert params["proxyjump"] == "jump.example"
    assert params["identityfile"] == ["~/.ssh/id_default", "~/.ssh/id_a"]


def test_ssh_host_params_defaults_port_22(tmp_path, monkeypatch):
    cfg = _write_config(tmp_path / "c.conf", "Host h1\n    HostName 10.0.0.1\n")
    monkeypatch.setenv("ROFF_SSH_CONFIG", cfg)
    assert fleet.ssh_host_params("h1")["port"] == 22


# --------------------------------------------------------------------------
# control_path
# --------------------------------------------------------------------------
def test_control_path_changes_when_config_changes(tmp_path, monkeypatch, short_state):
    cfg_a = _write_config(tmp_path / "a.conf", CONFIG_A)
    cfg_b = _write_config(tmp_path / "b.conf", CONFIG_B)

    monkeypatch.setenv("ROFF_SSH_CONFIG", cfg_a)
    path_a = sshx.control_path(ALIAS)
    monkeypatch.setenv("ROFF_SSH_CONFIG", cfg_b)
    path_b = sshx.control_path(ALIAS)

    assert path_a != path_b, (
        "đổi config mà socket ControlMaster giữ nguyên ⇒ roff nói chuyện "
        "với máy cũ (B14)"
    )
    assert path_a.name.startswith("dup-test-")
    assert path_a.name.endswith(".sock")
    assert path_a.parent.name == "cm"


def test_control_path_stable_for_same_config(tmp_path, monkeypatch, state_dir):
    cfg_a = _write_config(tmp_path / "a.conf", CONFIG_A)
    monkeypatch.setenv("ROFF_SSH_CONFIG", cfg_a)
    assert sshx.control_path(ALIAS) == sshx.control_path(ALIAS)


def test_control_path_fits_unix_socket_limit(tmp_path, monkeypatch):
    """Alias dài + state dir sâu ⇒ vẫn ≤ 88 byte (và bản có hậu tố ≤ 107)."""
    deep = tmp_path / ("x" * 40) / ("y" * 40) / ("z" * 30) / "state"
    deep.mkdir(parents=True)
    monkeypatch.setenv("ROFF_STATE_DIR", str(deep))
    monkeypatch.setenv("ROFF_SSH_CONFIG", str(tmp_path / "a.conf"))
    _write_config(tmp_path / "a.conf", CONFIG_A)

    path = sshx.control_path("m-" + "a" * 60)
    assert len(str(path)) <= sshx.UNIX_SOCKET_MAX_PATH
    assert len(str(path)) + 17 <= 107


def test_stale_fingerprint_socket_is_cleaned(tmp_path, monkeypatch, short_state):
    """Socket của fingerprint **cũ** phải được dọn (chỉ trong cm của roff)."""
    cfg_a = _write_config(tmp_path / "a.conf", CONFIG_A)
    cfg_b = _write_config(tmp_path / "b.conf", CONFIG_B)
    monkeypatch.setenv("ROFF_SSH_CONFIG", cfg_a)
    old_path = sshx.control_path(ALIAS)
    old_path.parent.mkdir(parents=True, exist_ok=True)
    old_path.write_text("", encoding="utf-8")

    # Socket của alias KHÁC trong cùng thư mục ⇒ không được đụng.
    other = old_path.parent / "muse-khác-1234abcd.sock"
    other.write_text("", encoding="utf-8")

    monkeypatch.setenv("ROFF_SSH_CONFIG", cfg_b)
    assert sshx.clean_stale_socket(ALIAS) is True
    assert not old_path.exists(), "socket fingerprint cũ phải bị dọn"
    assert other.exists(), "socket của alias khác không được đụng"


# --------------------------------------------------------------------------
# cache probe
# --------------------------------------------------------------------------
class _Cfg:
    def __init__(self, probe_cache_s: int = 60) -> None:
        self.probe_cache_s = probe_cache_s


def _fake_ssh():
    calls: list[str] = []

    def _run(alias, script, timeout):
        calls.append(alias)
        return 0, "", ""

    _run.calls = calls  # type: ignore[attr-defined]
    return _run


PROBE_OUTPUT = "\n".join(
    [
        "ROFF_HOSTNAME=htch-runtime",
        "ROFF_BOOT_ID=boot-1",
        "ROFF_HOME=/root",
        "ROFF_CORES=4",
        "ROFF_MEM_TOTAL_KB=8388608",
        "ROFF_MEM_AVAILABLE_KB=921600",
        "ROFF_LAYOUT_ROOT=/home/hatch/.roff",
        "ROFF_DURABLE=1",
        "ROFF_DISK_FREE_MB=102400",
        "ROFF_INSTANCE_TOKEN=tok-1",
        "",
    ]
)


def test_probe_cache_with_old_fingerprint_is_reprobed(tmp_path, monkeypatch):
    """Entry cache có fingerprint của config CŨ ⇒ phải probe lại (B14)."""
    cfg_a = _write_config(tmp_path / "a.conf", CONFIG_A)
    cfg_b = _write_config(tmp_path / "b.conf", CONFIG_B)
    monkeypatch.setenv("ROFF_SSH_CONFIG", cfg_a)

    state = tmp_path / "state"
    state.mkdir()
    cache_path = state / "probes.json"
    cache = {
        ALIAS: {
            "ts": 9e9,
            "fingerprint": fleet.ssh_fingerprint(ALIAS),
            "profile": {"alias": ALIAS, "cores": 99},
        }
    }
    cache_path.write_text(json.dumps(cache), encoding="utf-8")

    fake = _fake_ssh()
    monkeypatch.setattr(probe_mod, "_run_ssh", fake)

    # Đổi config ⇒ fingerprint đổi ⇒ cache hết hạn ⇒ probe lại.
    monkeypatch.setenv("ROFF_SSH_CONFIG", cfg_b)
    probe_mod.cached_profiles([ALIAS], _Cfg(60), state_dir=str(state))
    assert len(fake.calls) == 1, "cache có fingerprint cũ phải được probe lại"

    # Cache **không** bị xoá (chỉ entry của alias này bị thay).
    data = json.loads(cache_path.read_text(encoding="utf-8"))
    assert ALIAS in data


def test_probe_cache_with_same_fingerprint_is_reused(tmp_path, monkeypatch):
    cfg_a = _write_config(tmp_path / "a.conf", CONFIG_A)
    monkeypatch.setenv("ROFF_SSH_CONFIG", cfg_a)

    state = tmp_path / "state"
    state.mkdir()
    cache_path = state / "probes.json"
    cache_path.write_text(
        json.dumps(
            {
                ALIAS: {
                    "ts": 9e9,
                    "fingerprint": fleet.ssh_fingerprint(ALIAS),
                    "profile": {"alias": ALIAS, "cores": 4},
                }
            }
        ),
        encoding="utf-8",
    )

    fake = _fake_ssh()
    monkeypatch.setattr(probe_mod, "_run_ssh", fake)
    profiles = probe_mod.cached_profiles([ALIAS], _Cfg(60), state_dir=str(state))
    assert not fake.calls, "cùng config ⇒ cache còn hạn ⇒ không probe lại"
    assert profiles[0].cores == 4


def test_probe_cache_without_fingerprint_is_reprobed(tmp_path, monkeypatch):
    """Entry cache kiểu cũ (không có fingerprint) ⇒ coi như hết hạn."""
    cfg_a = _write_config(tmp_path / "a.conf", CONFIG_A)
    monkeypatch.setenv("ROFF_SSH_CONFIG", cfg_a)

    state = tmp_path / "state"
    state.mkdir()
    (state / "probes.json").write_text(
        json.dumps({ALIAS: {"ts": 9e9, "profile": {"alias": ALIAS, "cores": 4}}}),
        encoding="utf-8",
    )
    fake = _fake_ssh()
    monkeypatch.setattr(probe_mod, "_run_ssh", fake)
    probe_mod.cached_profiles([ALIAS], _Cfg(60), state_dir=str(state))
    assert len(fake.calls) == 1


# --------------------------------------------------------------------------
# B25 — fingerprint phải theo ĐÚNG file mà ssh thật sự đọc
#
# B14 dựa vào giả định "đổi file ssh-config ⇒ đổi fingerprint". Giả định đó
# chỉ đúng khi roff và ssh đọc **cùng một file**: ssh mặc định đọc
# ``~/.ssh/config`` theo **passwd entry** (``getpwuid``), còn roff đọc theo
# ``$HOME``. Lệch ⇒ fingerprint không phản ánh thực tế ⇒ master cũ vẫn được
# tái dùng ⇒ roff nói chuyện với máy cũ.
# --------------------------------------------------------------------------
def _home_config(tmp_path, text: str):
    """Dựng ``$HOME`` tạm có ``.ssh/config`` ⇒ trả về path của file config."""
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True, exist_ok=True)
    cfg = home / ".ssh" / "config"
    cfg.write_text(text, encoding="utf-8")
    return cfg


def test_fingerprint_follows_temp_home_config(tmp_path, monkeypatch):
    """``$HOME`` trỏ sang thư mục tạm ⇒ fingerprint theo config **trong** thư mục
    tạm; đổi nội dung file đó ⇒ fingerprint phải đổi."""
    cfg = _home_config(tmp_path, CONFIG_A)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("ROFF_SSH_CONFIG", raising=False)

    assert fleet.ssh_config_file() == str(cfg)
    fp_a = fleet.ssh_fingerprint(ALIAS)

    cfg.write_text(CONFIG_B, encoding="utf-8")
    fp_b = fleet.ssh_fingerprint(ALIAS)

    assert fp_a != fp_b, "đổi config trong $HOME tạm ⇒ fingerprint phải đổi"
    assert len(fp_a) == 8


def test_control_path_follows_temp_home_config(tmp_path, monkeypatch, short_state):
    """B14 + B25: đổi config trong ``$HOME`` tạm ⇒ **tên socket** đổi ⇒ master
    cũ không được tái dùng (nếu không, roff nói chuyện với máy cũ)."""
    cfg = _home_config(tmp_path, CONFIG_A)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("ROFF_SSH_CONFIG", raising=False)

    path_a = sshx.control_path(ALIAS)
    cfg.write_text(CONFIG_B, encoding="utf-8")
    path_b = sshx.control_path(ALIAS)

    assert path_a != path_b, (
        "đổi config mà socket ControlMaster giữ nguyên ⇒ roff nói chuyện "
        "với máy cũ (B14)"
    )


def test_ssh_config_args_point_at_fingerprinted_file(tmp_path, monkeypatch):
    """File roff fingerprint và file truyền cho ssh (``-F``) phải là MỘT."""
    cfg = _home_config(tmp_path, CONFIG_A)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("ROFF_SSH_CONFIG", raising=False)

    assert sshx.ssh_config_args() == ["-F", str(cfg)]
    assert sshx.ssh_config_path() == fleet.ssh_config_file()


# --------------------------------------------------------------------------
# B25 ở mức *nội dung* config — parser của fleet và ssh thật phải thấy CÙNG
# host (kể cả host nằm trong file ``Include``).
#
# Luật của OpenSSH (đo bằng ``strace`` trên OpenSSH 8.9p1 Ubuntu):
#   * ``Include ~/x``   ⇒ ``$HOME/x``
#   * ``Include rel``   ⇒ ``$HOME/.ssh/rel``  (file config *người dùng*)
#   * config mặc định  ⇒ ``<passwd home>/.ssh/config``
# ⇒ fleet phải theo đúng luật này, nếu không roff thấy host mà ssh không
# thấy ⇒ fingerprint lệch ⇒ có thể nối nhầm máy.
# --------------------------------------------------------------------------
def _real_ssh_hostname(ssh_bin: str, cfg_path, alias: str) -> str | None:
    """``HostName`` mà **ssh thật** resolve được từ file config (không nối mạng)."""
    proc = subprocess.run([ssh_bin, "-F", str(cfg_path), "-G", alias],
                          capture_output=True, text=True, timeout=30)
    out: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            out[parts[0].lower()] = parts[1].strip()
    return out.get("hostname")


def _fleet_hosts(cfg_path) -> dict[str, str | None]:
    return {h.alias: h.hostname for h in fleet.parse_ssh_config(str(cfg_path))}


def test_include_tilde_same_host_as_real_ssh(tmp_path, monkeypatch):
    """``Include ~/inc.conf`` trong config của ``$HOME`` tạm ⇒ ssh thật và
    parser của fleet phải thấy **cùng một host**."""
    ssh_bin = shutil.which("ssh")
    if not ssh_bin:
        pytest.skip("máy này không có ssh")
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    cfg = home / ".ssh" / "config"
    cfg.write_text("Include ~/inc.conf\n", encoding="utf-8")
    (home / "inc.conf").write_text("Host inc-a\n  HostName 10.9.9.9\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("ROFF_SSH_CONFIG", raising=False)

    assert _real_ssh_hostname(ssh_bin, cfg, "inc-a") == "10.9.9.9"
    assert _fleet_hosts(cfg).get("inc-a") == "10.9.9.9"


def test_relative_include_same_host_as_real_ssh(tmp_path, monkeypatch):
    """``Include inc.conf`` (tương đối) ⇒ ssh tìm trong ``$HOME/.ssh`` ⇒ fleet
    phải tìm **đúng chỗ đó**."""
    ssh_bin = shutil.which("ssh")
    if not ssh_bin:
        pytest.skip("máy này không có ssh")
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    cfg = home / ".ssh" / "config"
    cfg.write_text("Include inc.conf\n", encoding="utf-8")
    (home / ".ssh" / "inc.conf").write_text(
        "Host inc-b\n  HostName 10.8.8.8\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("ROFF_SSH_CONFIG", raising=False)

    assert _real_ssh_hostname(ssh_bin, cfg, "inc-b") == "10.8.8.8"
    assert _fleet_hosts(cfg).get("inc-b") == "10.8.8.8"


def test_relative_include_of_replacement_config_same_host_as_real_ssh(tmp_path, monkeypatch):
    """Config **thay thế** nằm ngoài ``$HOME/.ssh`` ⇒ ssh vẫn tìm include tương
    đối trong ``$HOME/.ssh`` ⇒ fleet phải vậy.

    Trước đây fleet tìm include **cạnh file config** ⇒ nó thấy host mà ssh
    **không** thấy ⇒ fingerprint lệch ⇒ có thể nối nhầm máy (B25 mức nội dung).
    """
    ssh_bin = shutil.which("ssh")
    if not ssh_bin:
        pytest.skip("máy này không có ssh")
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    cfg = tmp_path / "thay-the.conf"  # nằm NGOÀI $HOME/.ssh
    cfg.write_text("Include inc.conf\n", encoding="utf-8")
    (home / ".ssh" / "inc.conf").write_text(
        "Host inc-c\n  HostName 10.7.7.7\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("ROFF_SSH_CONFIG", raising=False)

    assert _real_ssh_hostname(ssh_bin, cfg, "inc-c") == "10.7.7.7"
    assert _fleet_hosts(cfg).get("inc-c") == "10.7.7.7"


def test_fingerprint_changes_when_included_file_changes(tmp_path, monkeypatch):
    """Host lấy từ file include ⇒ đổi file include ⇒ fingerprint phải đổi
    (nếu không, master cũ vẫn được tái dùng ⇒ nói chuyện với máy cũ)."""
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    cfg = home / ".ssh" / "config"
    cfg.write_text("Include inc.conf\n", encoding="utf-8")
    inc = home / ".ssh" / "inc.conf"
    inc.write_text("Host inc-d\n  HostName 10.6.6.6\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("ROFF_SSH_CONFIG", raising=False)

    fp_a = fleet.ssh_fingerprint("inc-d")
    inc.write_text("Host inc-d\n  HostName 10.5.5.5\n", encoding="utf-8")
    fp_b = fleet.ssh_fingerprint("inc-d")

    assert fp_a != fp_b, "đổi file include ⇒ fingerprint phải đổi"


def test_fingerprint_follows_include_of_replacement_config(tmp_path, monkeypatch):
    """Config **thay thế** nằm ngoài ``$HOME/.ssh`` ⇒ ``Include`` tương đối vẫn
    nằm ở ``$HOME/.ssh`` (đúng luật ssh) ⇒ fingerprint phải thấy host đó.

    Trước đây fingerprint tìm include **cạnh file config** ⇒ không thấy ⇒
    fingerprint tính theo nội dung khác với ssh ⇒ nối nhầm máy (B25).
    """
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    cfg = tmp_path / "thay-the.conf"  # nằm NGOÀI $HOME/.ssh
    cfg.write_text("Include inc.conf\n", encoding="utf-8")
    inc = home / ".ssh" / "inc.conf"
    inc.write_text("Host inc-e\n  HostName 10.4.4.4\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ROFF_SSH_CONFIG", str(cfg))

    assert fleet.ssh_host_params("inc-e")["hostname"] == "10.4.4.4"
    fp_a = fleet.ssh_fingerprint("inc-e")
    inc.write_text("Host inc-e\n  HostName 10.3.3.3\n", encoding="utf-8")
    assert fleet.ssh_fingerprint("inc-e") != fp_a, (
        "đổi file include mà fingerprint không đổi ⇒ master cũ vẫn được tái dùng"
    )
