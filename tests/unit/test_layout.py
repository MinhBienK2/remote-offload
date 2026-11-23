"""Test cho roffkit.layout — layout root bền vững, path tuyệt đối, instance token.

Không cần mạng: runner ssh được thay bằng runner giả chạy script bằng /bin/sh.
"""

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill"))

from roffkit import layout  # noqa: E402
from roffkit.errors import EXIT_INPUT, EXIT_UNREACHABLE, RoffError  # noqa: E402


class _LocalShellRunner:
    """Giả lập ssh: chạy script bằng /bin/sh ngay trên máy local (không mạng)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, host: str, script: str) -> tuple[int, str, str]:
        self.calls.append((host, script))
        proc = subprocess.run(
            ["/bin/sh", "-c", script], capture_output=True, text=True, check=False
        )
        return proc.returncode, proc.stdout, proc.stderr


class _FailingRunner:
    def __init__(self, rc: int = 1, out: str = "", err: str = "boom") -> None:
        self.rc = rc
        self.out = out
        self.err = err

    def __call__(self, host: str, script: str) -> tuple[int, str, str]:
        return self.rc, self.out, self.err


# ---------------------------------------------------------------- remote_path


def test_remote_path_expands_bare_tilde():
    assert layout.remote_path({"home": "/root"}, "~") == "/root"


def test_remote_path_expands_tilde_slash():
    assert layout.remote_path({"home": "/root"}, "~/work/dir") == "/root/work/dir"


def test_remote_path_keeps_absolute_path():
    assert layout.remote_path({"home": "/root"}, "/var/tmp/x") == "/var/tmp/x"


def test_remote_path_result_is_always_absolute():
    for path in ("~", "~/a/b", "/etc/passwd"):
        assert os.path.isabs(layout.remote_path({"home": "/root"}, path))


def test_remote_path_rejects_relative_path():
    with pytest.raises(RoffError) as exc:
        layout.remote_path({"home": "/root"}, "work/dir")
    assert exc.value.code == EXIT_INPUT


def test_remote_path_rejects_other_user_tilde():
    with pytest.raises(RoffError) as exc:
        layout.remote_path({"home": "/root"}, "~someone/else")
    assert exc.value.code == EXIT_INPUT


def test_remote_path_requires_home_in_profile():
    with pytest.raises(RoffError) as exc:
        layout.remote_path({}, "~/x")
    assert exc.value.code == EXIT_INPUT


def test_remote_path_rejects_relative_home():
    with pytest.raises(RoffError) as exc:
        layout.remote_path({"home": "root"}, "~/x")
    assert exc.value.code == EXIT_INPUT


# ---------------------------------------------------------------- layout_for


def test_layout_for_uses_first_durable_candidate():
    lay = layout.layout_for(
        "muse-a",
        "/root",
        probe_result={"durable_candidates": ["/home/hatch", "/mnt/data"], "home_fs": "devA"},
    )
    assert lay.root == "/home/hatch/.roff"
    assert lay.durable is True
    assert lay.home == "/root"


def test_layout_for_skips_candidate_on_same_filesystem_as_home():
    lay = layout.layout_for(
        "muse-a",
        "/root",
        probe_result={
            "durable_candidates": ["/home/hatch"],
            "home_fs": "devA",
            "candidate_fs": {"/home/hatch": "devA"},
        },
    )
    assert lay.durable is False
    assert lay.root == "/root/.roff"


def test_layout_for_falls_back_to_home_when_no_candidate():
    lay = layout.layout_for(
        "muse-a", "/root", probe_result={"durable_candidates": [], "home_fs": "devA"}
    )
    assert lay.durable is False
    assert lay.root == "/root/.roff"


def test_layout_for_probe_candidate_equal_to_home_is_skipped():
    lay = layout.layout_for(
        "muse-a",
        "/root",
        probe_result={"durable_candidates": ["/root", "/data"], "home_fs": "devA"},
    )
    assert lay.root == "/data/.roff"
    assert lay.durable is True


def test_layout_for_local_same_filesystem_is_not_durable(tmp_path, monkeypatch):
    durable = tmp_path / "durable"
    durable.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(layout, "DURABLE_CANDIDATES", (str(durable),))
    lay = layout.layout_for("muse-a", str(home))
    assert lay.durable is False
    assert lay.root == str(home) + "/.roff"


def test_layout_for_local_different_filesystem_is_durable(tmp_path, monkeypatch):
    durable = tmp_path / "durable"
    durable.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(layout, "DURABLE_CANDIDATES", (str(durable),))

    def fake_fs_id(path: str) -> str:
        if os.path.abspath(path) == os.path.abspath(str(durable)):
            return "devB"
        return "devA"

    monkeypatch.setattr(layout, "_fs_id", fake_fs_id)
    lay = layout.layout_for("muse-a", str(home))
    assert lay.durable is True
    assert lay.root == str(durable) + "/.roff"


def test_layout_for_local_missing_candidate_is_not_durable(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(layout, "DURABLE_CANDIDATES", (str(tmp_path / "khong-co"),))
    lay = layout.layout_for("muse-a", str(home))
    assert lay.durable is False
    assert lay.root == str(home) + "/.roff"


def test_layout_root_is_always_absolute(tmp_path):
    lay = layout.layout_for("muse-a", str(tmp_path))
    assert os.path.isabs(lay.root)


# ------------------------------------------------------------ instance_token


def test_instance_token_creates_token_file_mode_600(tmp_path):
    runner = _LocalShellRunner()
    token = layout.instance_token("muse-a", str(tmp_path), run=runner)
    assert len(token) == 32
    assert all(c in "0123456789abcdef" for c in token)
    path = tmp_path / ".roff-instance-token"
    assert path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_text(encoding="utf-8").strip() == token


def test_instance_token_is_stable_across_calls(tmp_path):
    runner = _LocalShellRunner()
    first = layout.instance_token("muse-a", str(tmp_path), run=runner)
    second = layout.instance_token("muse-a", str(tmp_path), run=runner)
    assert first == second


def test_instance_token_reads_existing_file(tmp_path):
    (tmp_path / ".roff-instance-token").write_text("deadbeef\n", encoding="utf-8")
    runner = _LocalShellRunner()
    token = layout.instance_token("muse-a", str(tmp_path), run=runner)
    assert token == "deadbeef"


def test_instance_token_without_runner_raises_not_implemented(tmp_path):
    with pytest.raises(NotImplementedError) as exc:
        layout.instance_token("muse-a", str(tmp_path))
    assert "run=" in str(exc.value)


def test_instance_token_runner_receives_host_and_script(tmp_path):
    runner = _LocalShellRunner()
    layout.instance_token("muse-a", str(tmp_path), run=runner)
    host, script = runner.calls[0]
    assert host == "muse-a"
    assert ".roff-instance-token" in script


def test_create_instance_token_writes_mode_600(tmp_path):
    path = tmp_path / "token"
    token = layout.create_instance_token(str(path))
    assert len(token) == 32
    assert all(c in "0123456789abcdef" for c in token)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_text(encoding="utf-8").strip() == token


# ------------------------------------------------------------ remote_mkdirs


def test_remote_mkdirs_creates_directory(tmp_path):
    runner = _LocalShellRunner()
    target = tmp_path / "a" / "b" / "c"
    layout.remote_mkdirs("muse-a", str(target), run=runner)
    assert target.is_dir()


def test_remote_mkdirs_raises_on_failure():
    with pytest.raises(RoffError) as exc:
        layout.remote_mkdirs("muse-a", "/tmp/x", run=_FailingRunner(err="permission denied"))
    assert exc.value.code == EXIT_UNREACHABLE
    assert "permission denied" in exc.value.payload()["detail"]


def test_remote_mkdirs_rejects_relative_path():
    with pytest.raises(RoffError) as exc:
        layout.remote_mkdirs("muse-a", "x/y", run=_LocalShellRunner())
    assert exc.value.code == EXIT_INPUT
