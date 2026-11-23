"""Test cho roffkit.config — đọc INI, default, host config.

Không cần mạng.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill"))

from roffkit.config import Config, HostConfig  # noqa: E402
from roffkit.errors import EXIT_CONFIG, RoffError  # noqa: E402

FULL_INI = """\
[fleet]
include = muse-*
include = qwen1-direct
exclude = colab-comfy
probe_cache_s = 30

[local]
max_parallel_transfers = 3
nice = 10
ionice = best-effort
cache_max_mb = 1000
cache_min_free_mb = 500

[heavy]
add = gradle
add = cargo
allow = git
allow = ls

[host.muse-pmb333]
labels = ffmpeg, shared
weight = 1.5
slots = 2
mem_budget_mb = 700
"""


def test_missing_config_file_returns_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = Config.load()
    assert cfg.path is None
    assert cfg.fleet_include == ["*"]
    assert cfg.fleet_exclude == []
    assert cfg.probe_cache_s == 60
    assert cfg.max_parallel_transfers == 2
    assert cfg.nice == 19
    assert cfg.ionice == "idle"
    assert cfg.cache_max_mb == 5120
    assert cfg.cache_min_free_mb == 3072
    assert cfg.heavy_add == []
    assert cfg.heavy_allow == []
    assert cfg.hosts == {}


def test_explicit_missing_path_returns_defaults_with_path_none(tmp_path):
    cfg = Config.load(str(tmp_path / "khong-co.ini"))
    assert cfg.path is None
    assert cfg.fleet_include == ["*"]
    assert cfg.hosts == {}


def test_default_config_file_is_read_from_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    conf_dir = tmp_path / ".config" / "roff"
    conf_dir.mkdir(parents=True)
    ini = conf_dir / "config.ini"
    ini.write_text("[fleet]\ninclude = muse-*\n", encoding="utf-8")
    cfg = Config.load()
    assert cfg.path == str(ini)
    assert cfg.fleet_include == ["muse-*"]


def test_load_reads_every_section(tmp_path):
    path = tmp_path / "config.ini"
    path.write_text(FULL_INI, encoding="utf-8")
    cfg = Config.load(str(path))
    assert cfg.path == str(path)
    assert cfg.fleet_include == ["muse-*", "qwen1-direct"]
    assert cfg.fleet_exclude == ["colab-comfy"]
    assert cfg.probe_cache_s == 30
    assert cfg.max_parallel_transfers == 3
    assert cfg.nice == 10
    assert cfg.ionice == "best-effort"
    assert cfg.cache_max_mb == 1000
    assert cfg.cache_min_free_mb == 500
    assert cfg.heavy_add == ["gradle", "cargo"]
    assert cfg.heavy_allow == ["git", "ls"]


def test_load_reads_host_sections(tmp_path):
    path = tmp_path / "config.ini"
    path.write_text(FULL_INI, encoding="utf-8")
    cfg = Config.load(str(path))
    host = cfg.host("muse-pmb333")
    assert host.name == "muse-pmb333"
    assert host.labels == ["ffmpeg", "shared"]
    assert host.weight == 1.5
    assert host.slots == 2
    assert host.mem_budget_mb == 700


def test_host_returns_defaults_for_unknown_alias():
    cfg = Config.load("/khong/ton/tai.ini")
    host = cfg.host("muse-khong-co")
    assert host == HostConfig(
        name="muse-khong-co", labels=[], weight=1.0, slots=1, mem_budget_mb=0
    )


def test_repeated_include_lines_accumulate(tmp_path):
    path = tmp_path / "config.ini"
    path.write_text("[fleet]\ninclude = a-*\ninclude = b-*\ninclude = c\n", encoding="utf-8")
    cfg = Config.load(str(path))
    assert cfg.fleet_include == ["a-*", "b-*", "c"]


def test_multiline_include_value_is_accepted(tmp_path):
    path = tmp_path / "config.ini"
    path.write_text("[fleet]\ninclude = a-*\n\tb-*\n", encoding="utf-8")
    cfg = Config.load(str(path))
    assert cfg.fleet_include == ["a-*", "b-*"]


def test_csv_list_is_split(tmp_path):
    path = tmp_path / "config.ini"
    path.write_text(
        "[fleet]\ninclude = a, b, c\nexclude = x\n\n[host.h1]\nlabels = l1, l2\n",
        encoding="utf-8",
    )
    cfg = Config.load(str(path))
    assert cfg.fleet_include == ["a", "b", "c"]
    assert cfg.fleet_exclude == ["x"]
    assert cfg.host("h1").labels == ["l1", "l2"]


def test_max_parallel_transfers_can_live_in_fleet_section(tmp_path):
    """DESIGN.md §5.10 đặt max_parallel_transfers trong [fleet]."""
    path = tmp_path / "config.ini"
    path.write_text("[fleet]\nmax_parallel_transfers = 5\n", encoding="utf-8")
    cfg = Config.load(str(path))
    assert cfg.max_parallel_transfers == 5


def test_missing_sections_keep_defaults(tmp_path):
    path = tmp_path / "config.ini"
    path.write_text("[fleet]\ninclude = muse-*\n", encoding="utf-8")
    cfg = Config.load(str(path))
    assert cfg.probe_cache_s == 60
    assert cfg.max_parallel_transfers == 2
    assert cfg.nice == 19
    assert cfg.ionice == "idle"
    assert cfg.cache_max_mb == 5120
    assert cfg.cache_min_free_mb == 3072
    assert cfg.heavy_add == []
    assert cfg.heavy_allow == []
    assert cfg.hosts == {}


def test_broken_ini_raises_config_error(tmp_path):
    path = tmp_path / "config.ini"
    path.write_text("khong phai ini\n", encoding="utf-8")
    with pytest.raises(RoffError) as exc:
        Config.load(str(path))
    assert exc.value.code == EXIT_CONFIG
    assert "config" in exc.value.payload()["error"].lower()


def test_broken_section_header_raises_config_error(tmp_path):
    path = tmp_path / "config.ini"
    path.write_text("[fleet\ninclude = x\n", encoding="utf-8")
    with pytest.raises(RoffError) as exc:
        Config.load(str(path))
    assert exc.value.code == EXIT_CONFIG


def test_invalid_value_raises_config_error(tmp_path):
    path = tmp_path / "config.ini"
    path.write_text("[host.h1]\nweight = abc\n", encoding="utf-8")
    with pytest.raises(RoffError) as exc:
        Config.load(str(path))
    assert exc.value.code == EXIT_CONFIG
    assert "weight" in exc.value.payload()["error"]


def test_invalid_int_value_raises_config_error(tmp_path):
    path = tmp_path / "config.ini"
    path.write_text("[host.h1]\nslots = hai\n", encoding="utf-8")
    with pytest.raises(RoffError) as exc:
        Config.load(str(path))
    assert exc.value.code == EXIT_CONFIG
