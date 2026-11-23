"""Test cho roffkit.fleet — đọc ~/.ssh/config, Include, lọc fleet.

Không cần mạng, không gọi ssh thật.
"""

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill"))

from roffkit.config import Config  # noqa: E402
from roffkit.fleet import fleet_aliases, parse_ssh_config  # noqa: E402


def _write(path: Path, text: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return str(path)


def _ssh_home(tmp_path, monkeypatch) -> Path:
    """``$HOME/.ssh`` tạm — luật của OpenSSH cho ``Include`` **tương đối**.

    ssh coi đường dẫn không tuyệt đối trong file config *người dùng* là nằm
    trong ``~/.ssh`` (đo bằng ``strace``) ⇒ test phải dựng đúng hình dạng đó.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    ssh = tmp_path / ".ssh"
    ssh.mkdir(parents=True, exist_ok=True)
    return ssh


def test_parses_simple_host(tmp_path):
    path = _write(
        tmp_path / "config",
        "Host muse-a\n"
        "  HostName 10.0.0.1\n"
        "  Port 2222\n"
        "  User root\n",
    )
    hosts = parse_ssh_config(path)
    assert len(hosts) == 1
    host = hosts[0]
    assert host.alias == "muse-a"
    assert host.hostname == "10.0.0.1"
    assert host.port == 2222
    assert host.user == "root"
    assert host.proxy_command is None


def test_host_with_multiple_aliases_makes_several_hosts(tmp_path):
    path = _write(tmp_path / "config", "Host muse-a muse-b\n  HostName 10.0.0.1\n")
    aliases = [h.alias for h in parse_ssh_config(path)]
    assert aliases == ["muse-a", "muse-b"]


def test_star_block_is_not_a_real_host(tmp_path):
    path = _write(tmp_path / "config", "Host *\n  User root\n\nHost muse-a\n  HostName 10.0.0.1\n")
    aliases = [h.alias for h in parse_ssh_config(path)]
    assert aliases == ["muse-a"]


def test_comments_and_blank_lines_are_ignored(tmp_path):
    path = _write(
        tmp_path / "config",
        "# đây là comment\n"
        "\n"
        "   \n"
        "Host muse-a   # alias chính\n"
        "  HostName 10.0.0.1\n",
    )
    aliases = [h.alias for h in parse_ssh_config(path)]
    assert aliases == ["muse-a"]


def test_option_keywords_are_not_mistaken_for_hosts(tmp_path):
    """R15: HostName/Port/ProxyCommand/... không được biến thành alias giả."""
    path = _write(
        tmp_path / "config",
        "Host muse-a\n"
        "  HostName 10.0.0.1\n"
        "  Port 2222\n"
        "  User root\n"
        "  ProxyCommand ssh -W %h:%p jump\n"
        "  IdentityFile ~/.ssh/id_ed25519\n"
        "  StrictHostKeyChecking no\n"
        "  UserKnownHostsFile /dev/null\n"
        "  ServerAliveInterval 30\n"
        "  ServerAliveCountMax 3\n"
        "  LogLevel QUIET\n"
        "  IdentitiesOnly yes\n",
    )
    hosts = parse_ssh_config(path)
    assert [h.alias for h in hosts] == ["muse-a"]
    assert hosts[0].hostname == "10.0.0.1"
    assert hosts[0].proxy_command == "ssh -W %h:%p jump"


def test_include_is_followed(tmp_path, monkeypatch):
    """R14: alias trong file include phải được liệt kê."""
    ssh = _ssh_home(tmp_path, monkeypatch)
    _write(ssh / "config", "Include conf.d/*.conf\n\nHost main\n  HostName 1.2.3.4\n")
    _write(ssh / "conf.d" / "a.conf", "Host included\n  HostName 5.6.7.8\n")
    aliases = [h.alias for h in parse_ssh_config(str(ssh / "config"))]
    assert "included" in aliases
    assert "main" in aliases


def test_include_with_several_patterns(tmp_path, monkeypatch):
    ssh = _ssh_home(tmp_path, monkeypatch)
    _write(ssh / "config", "Include a.conf b.conf\n")
    _write(ssh / "a.conf", "Host a1\n")
    _write(ssh / "b.conf", "Host b1\n")
    aliases = [h.alias for h in parse_ssh_config(str(ssh / "config"))]
    assert set(aliases) == {"a1", "b1"}


def test_include_is_recursive(tmp_path, monkeypatch):
    ssh = _ssh_home(tmp_path, monkeypatch)
    _write(ssh / "config", "Include one.conf\n")
    _write(ssh / "one.conf", "Include two.conf\nHost one\n")
    _write(ssh / "two.conf", "Host two\n")
    aliases = [h.alias for h in parse_ssh_config(str(ssh / "config"))]
    assert set(aliases) == {"one", "two"}


def test_include_cycle_does_not_hang(tmp_path, monkeypatch):
    ssh = _ssh_home(tmp_path, monkeypatch)
    _write(ssh / "config", "Include b.conf\n")
    _write(ssh / "b.conf", "Include config\nHost b\n")
    aliases = [h.alias for h in parse_ssh_config(str(ssh / "config"))]
    assert aliases == ["b"]


def test_missing_include_file_is_ignored(tmp_path, monkeypatch):
    ssh = _ssh_home(tmp_path, monkeypatch)
    _write(ssh / "config", "Include khong-co.conf\nHost a\n")
    aliases = [h.alias for h in parse_ssh_config(str(ssh / "config"))]
    assert aliases == ["a"]


def test_port_defaults_to_22(tmp_path):
    path = _write(tmp_path / "config", "Host muse-a\n  HostName 10.0.0.1\n")
    hosts = parse_ssh_config(path)
    assert hosts[0].port == 22


def test_star_options_are_merged_into_other_blocks(tmp_path):
    path = _write(
        tmp_path / "config",
        "Host *\n  User root\n  Port 2200\n\nHost muse-a\n  HostName 10.0.0.1\n",
    )
    hosts = parse_ssh_config(path)
    assert len(hosts) == 1
    assert hosts[0].user == "root"
    assert hosts[0].port == 2200
    assert hosts[0].hostname == "10.0.0.1"


def test_host_specific_option_wins_over_star(tmp_path):
    path = _write(
        tmp_path / "config",
        "Host *\n  User root\n\nHost muse-a\n  User alice\n  HostName 10.0.0.1\n",
    )
    hosts = parse_ssh_config(path)
    assert hosts[0].user == "alice"


def test_missing_file_returns_empty_list(tmp_path):
    assert parse_ssh_config(str(tmp_path / "khong-co")) == []


def test_alias_order_follows_file(tmp_path):
    path = _write(tmp_path / "config", "Host zeta\nHost alpha\nHost mid\n")
    assert [h.alias for h in parse_ssh_config(path)] == ["zeta", "alpha", "mid"]


# ------------------------------------------------------------ fleet_aliases


def _cfg(tmp_path: Path, text: str) -> Config:
    path = tmp_path / "config.ini"
    path.write_text(text, encoding="utf-8")
    return Config.load(str(path))


def test_fleet_aliases_filters_by_include(tmp_path):
    cfg = _cfg(tmp_path, "[fleet]\ninclude = muse-*\n")
    aliases = fleet_aliases(cfg, ["muse-a", "muse-b", "qwen1-direct"])
    assert aliases == ["muse-a", "muse-b"]


def test_fleet_aliases_applies_exclude(tmp_path):
    cfg = _cfg(tmp_path, "[fleet]\ninclude = muse-*\nexclude = muse-secret\n")
    aliases = fleet_aliases(cfg, ["muse-a", "muse-secret"])
    assert aliases == ["muse-a"]


def test_fleet_aliases_default_include_is_everything(tmp_path):
    cfg = _cfg(tmp_path, "[fleet]\n")
    aliases = fleet_aliases(cfg, ["muse-a", "khac"])
    assert aliases == ["muse-a", "khac"]


def test_fleet_aliases_deduplicates(tmp_path):
    cfg = _cfg(tmp_path, "[fleet]\ninclude = *\n")
    aliases = fleet_aliases(cfg, ["muse-a", "muse-a"])
    assert aliases == ["muse-a"]


def test_fleet_aliases_with_no_aliases(tmp_path):
    cfg = _cfg(tmp_path, "[fleet]\ninclude = *\n")
    assert fleet_aliases(cfg, []) == []
