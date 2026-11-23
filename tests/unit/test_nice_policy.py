"""Unit test cho chính sách ``nice``/``ionice`` — **B34** (một nguồn sự thật)
+ **C9** (ưu tiên job trên máy remote).

Không cần mạng: seam ``job._run_ssh`` được monkeypatch để bắt payload tar
(có ``run.sh``) mà không gọi ssh thật.

**S48 (B34)** — gọi hàm tiền tố của **cả 5 module** (``probe``/``sshx``/
``tunnel``/``job``/``transfer``) với **cùng** env + **cùng** config ⇒
**5 kết quả phải bằng nhau**. Trước B34 chúng **khác nhau** (3 nguồn sự
thật: hằng / env / config) ⇒ đây chính là test bắt lỗi hồi quy.

**S49 (C9)** — chạy **thật** script ``run.sh`` (đúng bản ship lên host) bằng
``/bin/sh`` trên máy đang chạy test và **đo** nice của tiến trình job qua
``/proc/self/stat`` (trường 19) — **đo**, không phải đọc code.

Bảng ca kiểm (brie S48):

| Môi trường | Config | Kỳ vọng |
|---|---|---|
| không env | không có | ``nice -n 19`` + ``ionice -c3`` (mặc định) |
| ``ROFF_NICE=5`` | không có | ``nice -n 5`` (env **thắng**) |
| ``ROFF_NICE=5`` + ``[local] nice=7`` | có | ``nice -n 5`` (env **thắng** config) |
| không env | ``[local] nice=7`` | ``nice -n 7`` (config dùng được) |
| ``ROFF_NICE=0`` | không có | **không** bọc ``nice`` (0 = ưu tiên cao) |
| ``ROFF_NICE=0`` + ``[local] nice=7`` | có | **không** bọc ``nice`` |
| ``ROFF_IONICE=none`` | không có | **không** bọc ``ionice`` |
| ``ROFF_IONICE=realtime`` | không có | ``ionice -c1`` |
| ``ROFF_IONICE=best-effort`` | không có | ``ionice -c2`` |
| ``ROFF_IONICE=idle`` | không có | ``ionice -c3`` |
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill"))

from roffkit import config as config_mod  # noqa: E402
from roffkit import job as job_mod  # noqa: E402
from roffkit import probe, sshx, transfer, tunnel  # noqa: E402

HAS_NICE = shutil.which("nice") is not None
HAS_IONICE = shutil.which("ionice") is not None

requires_nice_ionice = pytest.mark.skipif(
    not (HAS_NICE and HAS_IONICE),
    reason="máy chạy test không có nice/ionice ⇒ không đo được tiền tố đầy đủ",
)

#: Hàm tiền tố của **5 module** — tất cả phải cho ra **cùng** một kết quả.
PREFIX_FUNCS = {
    "probe": lambda: probe._prefix_nice(),
    "sshx": lambda: sshx._prefix(),
    "tunnel": lambda: tunnel._prefix(),
    "job": lambda: job_mod._prefix_nice(),
    "transfer": lambda: transfer._prefix_nice(),
}

#: (env, nội dung config, tiền tố kỳ vọng)
#: Bảng ca kiểm (brie S48). **Thứ tự** `nice`/`ionice` trong tiền tố **không
#: phải hợp đồng** — `nice` và `ionice` đặt hai thuộc tính độc lập (CPU / I-O),
#: cả hai đều được kế thừa qua fork+exec. Bảng này chốt **nội dung**
#: (mức/lớp đúng, `0`/`none` ⇒ không bọc), không chốt thứ tự.
CASES = [
    ({}, None, ["nice", "-n", "19", "ionice", "-c3"]),
    ({"ROFF_NICE": "5"}, None, ["nice", "-n", "5", "ionice", "-c3"]),
    ({"ROFF_NICE": "5"}, "[local]\nnice = 7\n", ["nice", "-n", "5", "ionice", "-c3"]),
    ({}, "[local]\nnice = 7\n", ["nice", "-n", "7", "ionice", "-c3"]),
    ({"ROFF_NICE": "0"}, None, ["ionice", "-c3"]),
    ({"ROFF_NICE": "0"}, "[local]\nnice = 7\n", ["ionice", "-c3"]),
    ({"ROFF_IONICE": "none"}, None, ["nice", "-n", "19"]),
    ({"ROFF_IONICE": "realtime"}, None, ["nice", "-n", "19", "ionice", "-c1"]),
    ({"ROFF_IONICE": "best-effort"}, None, ["nice", "-n", "19", "ionice", "-c2"]),
    ({"ROFF_IONICE": "idle"}, None, ["nice", "-n", "19", "ionice", "-c3"]),
]

CASE_IDS = [str(i) for i in range(len(CASES))]


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Cô lập khỏi config/env thật của máy người dùng."""
    monkeypatch.setenv("ROFF_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ROFF_CONFIG", str(tmp_path / "config.ini"))
    for name in (
        "ROFF_NICE",
        "ROFF_IONICE",
        "ROFF_REMOTE_NICE",
        "ROFF_REMOTE_IONICE",
    ):
        monkeypatch.delenv(name, raising=False)


def _write_config(tmp_path: Path, body: str | None) -> None:
    path = tmp_path / "config.ini"
    if body is None:
        path.unlink(missing_ok=True)
    else:
        path.write_text(body, encoding="utf-8")


# --------------------------------------------------------------------------
# S48 — 5 module, 1 nguồn sự thật
# --------------------------------------------------------------------------
@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_five_modules_give_the_same_prefix(case, tmp_path, monkeypatch):
    """**Bài test bắt lỗi hồi quy của B34**: 5 module, cùng env + cùng config
    ⇒ **5 kết quả phải bằng nhau**.

    Trước B34: ``probe`` dùng hằng, ``sshx``/``tunnel`` dùng env, ``job``
    hardcode, ``transfer`` đọc config ⇒ chúng **khác nhau**.
    """
    env, body, _expected = case
    _write_config(tmp_path, body)
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    results = {name: func() for name, func in PREFIX_FUNCS.items()}
    distinct = {tuple(v) for v in results.values()}
    assert len(distinct) == 1, f"5 module cho ra {len(distinct)} kết quả khác nhau: {results}"
    # ... và kết quả đó phải là kết quả của nguồn sự thật duy nhất.
    assert results["probe"] == config_mod.local_nice_prefix()


@requires_nice_ionice
@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_prefix_matches_expected_table(case, tmp_path, monkeypatch):
    """Bảng ca kiểm của brie S48 — env > config > mặc định."""
    env, body, expected = case
    _write_config(tmp_path, body)
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    assert config_mod.local_nice_prefix() == expected


def test_env_nice_beats_config_nice(tmp_path, monkeypatch):
    _write_config(tmp_path, "[local]\nnice = 7\n")
    monkeypatch.setenv("ROFF_NICE", "5")
    assert config_mod.local_nice_policy().nice == 5


def test_config_nice_used_when_no_env(tmp_path):
    _write_config(tmp_path, "[local]\nnice = 7\n")
    assert config_mod.local_nice_policy().nice == 7


def test_env_ionice_beats_config_ionice(tmp_path, monkeypatch):
    _write_config(tmp_path, "[local]\nionice = best-effort\n")
    monkeypatch.setenv("ROFF_IONICE", "realtime")
    assert config_mod.local_nice_policy().ionice == "1"


def test_config_ionice_used_when_no_env(tmp_path):
    _write_config(tmp_path, "[local]\nionice = best-effort\n")
    assert config_mod.local_nice_policy().ionice == "2"


def test_nice_zero_means_no_wrapper(tmp_path, monkeypatch):
    """``nice = 0`` ⇒ **không** bọc ``nice`` (người dùng muốn ưu tiên cao)."""
    monkeypatch.setenv("ROFF_NICE", "0")
    assert "nice" not in config_mod.local_nice_prefix()


def test_nice_zero_beats_config_nice(tmp_path, monkeypatch):
    _write_config(tmp_path, "[local]\nnice = 7\n")
    monkeypatch.setenv("ROFF_NICE", "0")
    assert "nice" not in config_mod.local_nice_prefix()


def test_ionice_none_means_no_wrapper(tmp_path, monkeypatch):
    monkeypatch.setenv("ROFF_IONICE", "none")
    assert "ionice" not in config_mod.local_nice_prefix()


def test_ionice_empty_means_no_wrapper(tmp_path, monkeypatch):
    monkeypatch.setenv("ROFF_IONICE", "")
    assert "ionice" not in config_mod.local_nice_prefix()


@pytest.mark.parametrize(
    "value,expected",
    [
        ("idle", "3"),
        ("best-effort", "2"),
        ("be", "2"),
        ("realtime", "1"),
        ("3", "3"),
        ("2", "2"),
        ("none", None),
        ("", None),
        (None, None),
    ],
)
def test_ionice_class_mapping(value, expected):
    assert config_mod.ionice_class(value) == expected


def test_broken_config_does_not_kill_the_process(tmp_path, monkeypatch):
    """Config **sai cú pháp** ⇒ **mặc định**, **không** raise (B27).

    Đọc config ở đường sau khi job đã ghim host không được làm chết tiến
    trình — cùng nguyên tắc với ``transfer._config()`` (strict=False).
    """
    _write_config(tmp_path, "[local\nnice = 5\n")  # INI hỏng
    assert config_mod.load_config() is None
    assert config_mod.local_nice_prefix() == ["nice", "-n", "19", "ionice", "-c3"]


def test_config_with_bad_value_does_not_kill_the_process(tmp_path):
    _write_config(tmp_path, "[local]\nnice = khong-phai-so\n")
    assert config_mod.load_config() is None
    assert config_mod.local_nice_policy().nice == config_mod.DEFAULT_NICE


def test_invalid_env_is_ignored_not_fatal(tmp_path, monkeypatch):
    """Env rác ⇒ **bỏ qua** (đọc tiếp config), không làm chết tiến trình."""
    _write_config(tmp_path, "[local]\nnice = 7\n")
    monkeypatch.setenv("ROFF_NICE", "khong-phai-so")
    assert config_mod.local_nice_policy().nice == 7


def test_nice_above_range_is_clamped(tmp_path, monkeypatch):
    monkeypatch.setenv("ROFF_NICE", "99")
    assert config_mod.local_nice_policy().nice == 19


def test_negative_nice_is_treated_as_zero(tmp_path, monkeypatch):
    """nice âm cần quyền đặc biệt ⇒ roff **không** tự nâng ưu tiên."""
    monkeypatch.setenv("ROFF_NICE", "-5")
    assert config_mod.local_nice_policy().nice == 0
    assert "nice" not in config_mod.local_nice_prefix()


# --------------------------------------------------------------------------
# C9 — ưu tiên job trên máy remote
# --------------------------------------------------------------------------
def test_run_sh_never_uses_exec():
    """**Cấm** ``exec``: ``run.sh`` phải còn chạy để ghi ``$JOBDIR/exit``.

    Sau ``exec``, dòng ``rc=$?`` không bao giờ chạy ⇒ job không bao giờ có
    exit code ⇒ ``roff wait`` treo.
    """
    assert "exec " not in job_mod._RUN_SH
    assert "exec\n" not in job_mod._RUN_SH
    assert 'printf \'%s\\n\' "$rc" > "$JOBDIR/exit"' in job_mod._RUN_SH


def test_run_sh_checks_command_v_on_the_host():
    """``nice``/``ionice`` phải được kiểm tra **trên host**, không ở local."""
    assert "command -v nice" in job_mod._RUN_SH
    assert "command -v ionice" in job_mod._RUN_SH


def test_run_sh_keeps_user_command_verbatim():
    """Lệnh của người dùng phải còn **nguyên văn** ⇒ ``$ROFF_ITEM``,
    ``$ROFF_OUT``… vẫn expand (không quote lại lần nữa)."""
    assert 'sh -c "$(cat "$JOBDIR/cmd")"' in job_mod._RUN_SH


def test_remote_policy_defaults_to_nice_19(tmp_path):
    """Mặc định: job trên host chạy ở ``nice 19`` + I/O ``idle``."""
    _write_config(tmp_path, "[fleet]\ninclude = *\n")
    policy = config_mod.remote_nice_policy("muse-a")
    assert policy.nice == 19
    assert policy.ionice == "3"
    assert config_mod.remote_nice_args("muse-a") == ("19", "3")


def test_remote_section_overrides_default(tmp_path):
    _write_config(tmp_path, "[remote]\nnice = 10\nionice = best-effort\n")
    policy = config_mod.remote_nice_policy("muse-a")
    assert policy.nice == 10
    assert policy.ionice == "2"


def test_remote_host_section_overrides_remote_section(tmp_path):
    _write_config(tmp_path, "[remote]\nnice = 10\n\n[host.muse-a]\nnice = 3\n")
    assert config_mod.remote_nice_policy("muse-a").nice == 3
    assert config_mod.remote_nice_policy("muse-b").nice == 10


def test_remote_nice_zero_means_no_wrapper(tmp_path):
    _write_config(tmp_path, "[remote]\nnice = 0\n")
    nice, ionice = config_mod.remote_nice_args("muse-a")
    assert nice == ""
    assert ionice == "3"


def test_remote_env_beats_config(tmp_path, monkeypatch):
    _write_config(tmp_path, "[remote]\nnice = 10\n")
    monkeypatch.setenv("ROFF_REMOTE_NICE", "4")
    assert config_mod.remote_nice_policy("muse-a").nice == 4


def test_broken_config_remote_policy_still_safe(tmp_path):
    _write_config(tmp_path, "[remote\nnice = 3\n")
    nice, ionice = config_mod.remote_nice_args("muse-a")
    assert (nice, ionice) == ("19", "3")


# --------------------------------------------------------------------------
# C9 — đo **thật** trên máy đang chạy test
# --------------------------------------------------------------------------
def _tar_members(data: bytes) -> dict[str, tuple[int, bytes]]:
    out: dict[str, tuple[int, bytes]] = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tf:
        for member in tf.getmembers():
            payload = tf.extractfile(member)
            out[member.name] = (member.mode, payload.read() if payload else b"")
    return out


class _FakeSsh:
    """Bắt payload tar của ``launch`` mà không gọi ssh thật."""

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, alias, script, timeout=30, stdin=None):
        self.calls.append({"alias": alias, "script": script, "stdin": stdin})
        return 0, "ROFF_PID=4242\nROFF_BOOT_ID=b\nROFF_INSTANCE_TOKEN=t\n", ""


def _launch_run_sh(monkeypatch, tmp_path, host="muse-a") -> str:
    """Chạy ``job.launch`` (seam giả) và trả nội dung ``run.sh`` ship lên host."""
    fake = _FakeSsh()
    monkeypatch.setattr(job_mod, "_run_ssh", fake)
    job = job_mod.Job(
        id="261124-101530-ab12",
        cmd="echo hello",
        host=host,
        project="proj",
        created_at=1_700_000_000.0,
        out_remote="/home/hatch/.roff/jobs/261124-101530-ab12/out",
    )
    job_mod.launch(host, job, env={}, cpus=1, mem_mb=256)
    members = _tar_members(fake.calls[-1]["stdin"])
    return members["run.sh"][1].decode("utf-8")


def test_launch_fills_nice_placeholders(monkeypatch, tmp_path):
    """Placeholder phải được điền từ **local** (giống ``__CPUS__``)."""
    run_sh = _launch_run_sh(monkeypatch, tmp_path)
    assert "__NICE__" not in run_sh
    assert "__IONICE__" not in run_sh
    assert "nice -n 19" in run_sh
    assert "ionice -c3" in run_sh


def _prepare_jobdir(tmp_path: Path, cmd: str, nice: str, ionice: str) -> Path:
    """Dựng ``<jobdir>`` giống hệt bản ship lên host (run.sh + cmd)."""
    jobdir = tmp_path / "job"
    jobdir.mkdir(exist_ok=True)
    script = job_mod._RUN_SH.replace("__NICE__", nice).replace("__IONICE__", ionice)
    (jobdir / "run.sh").write_text(script, encoding="utf-8")
    (jobdir / "run.sh").chmod(0o700)
    (jobdir / "cmd").write_text(cmd, encoding="utf-8")
    return jobdir


def _run_remote_script(jobdir: Path, path: str | None = None) -> subprocess.CompletedProcess:
    """Chạy ``run.sh`` **thật** (giống host) và trả kết quả."""
    env = dict(os.environ)
    env["ROFF_WORK"] = str(jobdir)
    if path is not None:
        env["PATH"] = path
    return subprocess.run(
        ["/bin/sh", str(jobdir / "run.sh")],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )


#: Lệnh in ra nice của chính nó — trường 19 của ``/proc/self/stat``.
#: (1-based: pid=1, comm=2, state=3, …, nice=19) ⇒ không phụ thuộc `nice`
#: có sẵn trên host hay không.
PRINT_NICE = "awk '{print $19}' /proc/self/stat"


@requires_nice_ionice
def test_job_on_host_runs_at_nice_19(tmp_path):
    """**Đo thật**: lệnh job trên host phải chạy ở ``nice 19``."""
    jobdir = _prepare_jobdir(tmp_path, PRINT_NICE, "19", "3")
    proc = _run_remote_script(jobdir)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().splitlines()[-1] == "19"


def test_job_on_host_writes_exit_code(tmp_path):
    """``$JOBDIR/exit`` phải được ghi ⇒ job có exit code đúng."""
    jobdir = _prepare_jobdir(tmp_path, "exit 7", "19", "3")
    proc = _run_remote_script(jobdir)
    assert proc.returncode == 7
    assert (jobdir / "exit").read_text(encoding="utf-8").strip() == "7"


def test_job_on_host_without_nice_wrapper_keeps_exit_code(tmp_path):
    """``nice = 0`` ⇒ **không** bọc ``nice`` ⇒ job chạy ở nice 0 (ưu tiên cao)."""
    jobdir = _prepare_jobdir(tmp_path, PRINT_NICE, "", "3")
    proc = _run_remote_script(jobdir)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().splitlines()[-1] == "0"
    assert (jobdir / "exit").read_text(encoding="utf-8").strip() == "0"


def _path_without(*tools: str) -> Path:
    """PATH tạm **giống hệt** PATH thật, nhưng **bỏ** các lệnh nêu tên.

    Dùng để giả lập "host không có ``nice``" mà vẫn giữ các lệnh mà
    ``run.sh`` cần (``dirname``, ``cat``, ``awk``…) — host thật luôn có
    coreutils; bỏ hết thì test đo sai nguyên nhân.
    """
    bindir = Path(tempfile.mkdtemp(prefix="roff-nobin-"))
    dropped = set(tools)
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory or not os.path.isdir(directory):
            continue
        for entry in os.listdir(directory):
            if entry in dropped or entry.startswith("."):
                continue
            source = os.path.join(directory, entry)
            target = bindir / entry
            if not os.path.exists(target) and os.access(source, os.X_OK):
                try:
                    target.symlink_to(source)
                except OSError:
                    continue
    return bindir


def test_job_on_host_without_nice_binary_still_runs(tmp_path):
    """Host **không có** ``nice`` ⇒ job vẫn chạy, exit code vẫn đúng, không fail."""
    bindir = _path_without("nice", "ionice")
    jobdir = _prepare_jobdir(tmp_path, "echo ok", "19", "3")
    proc = _run_remote_script(jobdir, path=str(bindir))
    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout
    assert (jobdir / "exit").read_text(encoding="utf-8").strip() == "0"


def test_job_on_host_without_any_priority_tool_still_runs(tmp_path):
    """Host không có cả ``nice`` lẫn ``ionice`` ⇒ job vẫn chạy bình thường."""
    bindir = _path_without("nice", "ionice")
    jobdir = _prepare_jobdir(tmp_path, PRINT_NICE, "19", "3")
    proc = _run_remote_script(jobdir, path=str(bindir))
    assert proc.returncode == 0, proc.stderr
    # Không có `nice` ⇒ job chạy ở mức mặc định của host (0) — nhưng **chạy
    # được** và exit code vẫn đúng (đây mới là điều quan trọng).
    assert proc.stdout.strip().splitlines()[-1] == "0"
    assert (jobdir / "exit").read_text(encoding="utf-8").strip() == "0"


def test_job_env_expands_inside_the_command(tmp_path):
    """``$ROFF_ITEM``/``$ROFF_OUT``… phải expand bên trong lệnh của người dùng."""
    jobdir = _prepare_jobdir(tmp_path, "echo item=$ROFF_ITEM", "19", "3")
    env = dict(os.environ)
    env["ROFF_WORK"] = str(jobdir)
    env["ROFF_ITEM"] = "video-01.mp4"
    proc = subprocess.run(
        ["/bin/sh", str(jobdir / "run.sh")],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert "item=video-01.mp4" in proc.stdout
