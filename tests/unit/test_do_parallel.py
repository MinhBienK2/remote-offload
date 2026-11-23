"""B36/C10 — ``roff do --each`` phải **gửi lô song song**, không tuần tự.

Vấn đề (đo thật 23/11/2026, ``docs/evidence/final/s18-submit-latency.txt``):
vòng lặp ``--each`` trong ``cli.py`` làm **tuần tự** mọi việc từ xa cho
từng item — ``ensure_shared_input()`` (**đẩy** input lên host) + ``launch()``
— mỗi item ~21–26 s ⇒ các job của một lô **không bao giờ chạy chồng nhau**
⇒ fleet **không** tính toán song song.

Cách sửa: ``do`` **hoãn** việc đẩy input + launch cho **watcher**
(đường dẫn đã có sẵn ở nhánh "job xếp hàng") ⇒ mỗi watcher tự đẩy + launch
**song song**, và ``do`` trả JSON **ngay**.

Các test ở đây **đo được**, không cần mạng:
  1. với N item, ``do`` tạo N job mà **không** đẩy input, **không** launch
     (đẩy là việc của watcher) — đếm lời gọi ``transfer.push``/``_run_ssh``;
  2. probe fleet **1 lần cho cả lô** (không phải 1 lần/item) — đếm lời gọi
     ``probe.probe``;
  3. input local **không tồn tại** ⇒ ``do`` **fail sớm** (exit ≠ 0) và
     **không** đụng vào fleet;
  4. watcher **thật sự** đẩy input + launch cho job mà ``do`` đã hoãn
     (không phải "không ai làm");
  5. hợp đồng cũ giữ nguyên: ``jobs`` trong JSON, mỗi item một host khác,
     kho input dùng chung **1 bản copy/host**, job hết slot vẫn **xếp hàng**.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill"))

from roffkit import cli as cli_mod  # noqa: E402
from roffkit import job as job_mod  # noqa: E402
from roffkit import probe as probe_mod  # noqa: E402
from roffkit import shared as shared_mod  # noqa: E402
from roffkit import watcher as watcher_mod  # noqa: E402


# --------------------------------------------------------------------------
# host giả: filesystem trên host mà ``shared``/``layout`` cần
# --------------------------------------------------------------------------
class FakeRemote:
    """Giả lập host (marker + lock của kho input dùng chung)."""

    def __init__(self) -> None:
        self.files: dict[str, str] = {}
        self.locks: set[str] = set()
        self.scripts: list[str] = []
        self.lock_mtime = time.time()

    def run_shell(self, host: str, script: str, timeout: float = 60.0):
        """Seam cho :func:`roffkit.shared._run_shell` (trả bộ ba)."""
        self.scripts.append(script)
        out = ""
        for part in script.split("&&"):
            rc, text = self._exec(part.strip())
            if rc != 0:
                return rc, "", ""
            out = text or out
        return 0, out, ""

    def _exec(self, part: str) -> tuple[int, str]:
        if not part:
            return 0, ""
        tokens = shlex.split(part)
        head = tokens[0] if tokens else ""
        if head == "cat" and len(tokens) > 1:
            return 0, self.files.get(tokens[1], "")
        if head == "mkdir" and "-p" in tokens[:2]:
            return 0, ""
        if head == "mkdir" and len(tokens) > 1:
            if tokens[1] in self.locks:
                return 1, ""
            self.locks.add(tokens[1])
            return 0, ""
        if head == "rmdir" and len(tokens) > 1:
            self.locks.discard(tokens[1])
            return 0, ""
        if head == "rm" and len(tokens) > 2:
            self.locks.discard(tokens[2])
            self.files.pop(tokens[2], None)
            self.files.pop(tokens[2] + "/owner", None)
            return 0, ""
        if head == "stat" and len(tokens) > 3:
            path = tokens[3]
            if path in self.locks or path in self.files:
                return 0, str(int(self.lock_mtime))
            return 1, ""
        if head == "test" and len(tokens) > 2:
            path = tokens[-1]
            exists = path in self.locks or path in self.files
            if tokens[1] == "!":
                return (0, "") if not exists else (1, "")
            if tokens[1] == "-e":
                return (0, "") if exists else (1, "")
            return 0, ""
        if head == "printf" and len(tokens) >= 5 and tokens[-2] == ">":
            self.files[tokens[-1]] = tokens[2]
            return 0, ""
        return 0, ""


class Harness:
    """Bộ đếm cho mọi việc **từ xa** mà ``do``/watcher có thể làm."""

    def __init__(self) -> None:
        self.remote = FakeRemote()
        self.probes: list[str] = []
        self.pushes: list[tuple] = []
        self.launches: list[str] = []
        self.spawned: list[str] = []
        self.shared_inputs: list[str] = []
        """Lời gọi ``shared.ensure_shared_input`` (= **đẩy input** lên host)."""

    def install(self, monkeypatch, aliases: list[str]) -> "Harness":
        monkeypatch.setattr(cli_mod, "_fleet_aliases", lambda cfg: list(aliases))
        monkeypatch.setattr(shared_mod, "_run_shell", self.remote.run_shell)
        monkeypatch.setattr(
            probe_mod, "probe", self._fake_probe
        )
        monkeypatch.setattr(shared_mod.transfer_mod, "push", self._fake_push)
        # Bọc hàm thật (để logic marker/khoá vẫn chạy) + **đếm** lần đẩy.
        self._real_ensure_shared = shared_mod.ensure_shared_input
        monkeypatch.setattr(
            shared_mod, "ensure_shared_input", self._fake_ensure_shared
        )
        monkeypatch.setattr(job_mod, "_run_ssh", self._fake_launch)
        # Không spawn watcher thật (nền, quay vòng vô hạn) — chỉ **ghi lại**.
        monkeypatch.setattr(cli_mod, "_spawn_watcher", self._spawn)
        return self

    def _spawn(self, job_id: str) -> bool:
        """Giả lập spawn watcher (thành công ⇒ watcher sẽ khởi chạy job)."""
        self.spawned.append(job_id)
        return True

    # --- seams ---
    def _fake_probe(self, alias: str, timeout: int = 30) -> probe_mod.Profile:
        self.probes.append(alias)
        return probe_mod.Profile(
            alias=alias,
            reachable=True,
            cores=4,
            mem_available_mb=4096,
            disk_free_mb=100000,
            layout_root="/home/hatch/.roff",
            home="/root",
            boot_id="boot-" + alias,
            instance_token="tok-" + alias,
            internet=True,
        )

    def _fake_ensure_shared(self, host, local_path, layout_root, *args, **kwargs):
        """Seam cho :func:`roffkit.shared.ensure_shared_input` (đếm lần đẩy).

        Gọi hàm **thật** (logic marker + khoá của kho dùng chung vẫn chạy) —
        chỉ đếm thêm số lần **đẩy input**.
        """
        self.shared_inputs.append(str(local_path))
        return self._real_ensure_shared(host, local_path, layout_root, *args, **kwargs)

    def _fake_push(self, host, local_dir, remote_dir, *args, **kwargs):
        self.pushes.append((host, local_dir, remote_dir))

        class Result:
            ok = True
            bytes_transferred = 1
            files: list = []
            error = None

        return Result()

    def _fake_launch(self, host, script, timeout, stdin=None):
        self.launches.append(host)
        return (
            0,
            "ROFF_PID=1\nROFF_BOOT_ID=boot-1\nROFF_INSTANCE_TOKEN=tok-1\n",
            "",
        )


@pytest.fixture
def harness(tmp_path, monkeypatch):
    """Env cô lập + seam giả (không mạng thật)."""
    state = tmp_path / "state"
    state.mkdir()
    config = tmp_path / "config.ini"
    config.write_text("[fleet]\ninclude = muse-*\n")
    monkeypatch.setenv("ROFF_STATE_DIR", str(state))
    monkeypatch.setenv("ROFF_CONFIG", str(config))
    monkeypatch.setenv("ROFF_SLOT_WAIT_S", "5")
    # KHÔNG bật ROFF_NO_WATCHER ⇒ dùng **đường dẫn hoãn** (deferred).
    monkeypatch.delenv("ROFF_NO_WATCHER", raising=False)
    box = Harness()
    box.install(monkeypatch, ["muse-a", "muse-b", "muse-c"])
    box.state = state
    return box


def _args(tmp_path, each: int = 3, inputs: list[str] | None = None) -> argparse.Namespace:
    """Namespace cho ``_cmd_do`` (giống hợp đồng CLI)."""
    folder = tmp_path / "items"
    folder.mkdir(exist_ok=True)
    for index in range(each):
        (folder / f"item-{index}.txt").write_text(f"item {index}")
    return argparse.Namespace(
        cmd="ffmpeg -i $ROFF_ITEM $ROFF_OUT/out.mp4",
        inputs=list(inputs or []),
        out=None,
        need=[],
        cpus=0,
        mem=None,
        disk=None,
        each=str(folder / "*.txt"),
        shards=1,
        after=None,
        name="proj",
        sync_back=[],
        checkpoint=None,
        env=[],
        retry=1,
        side_effects=False,
        timeout=600,
        priority="normal",
        prefer_same_host=True,
        host=None,
    )


def _states(state_dir: Path) -> list[str]:
    out = []
    for path in sorted((state_dir / "jobs").glob("*.json")):
        out.append(json.loads(path.read_text()).get("state"))
    return out


# --------------------------------------------------------------------------
# 1 — `do` KHÔNG đẩy input, KHÔNG launch (đẩy là việc của watcher)
# --------------------------------------------------------------------------
def test_do_each_does_not_push_input_at_call_time(harness, tmp_path):
    """N item ⇒ ``do`` tạo N job mà **không** đẩy input lên host.

    Đây là tiêu chí nghiệm thu chính của B36/C10: nếu ``do`` còn đẩy input
    (và launch) **tuần tự** cho từng item thì lô vẫn không chạy chồng nhau.
    """
    payload, code = cli_mod._cmd_do(_args(tmp_path, each=3))

    assert code == 0
    assert payload["count"] == 3
    assert len(payload["jobs"]) == 3
    assert harness.shared_inputs == [], (
        f"`do` KHÔNG được đẩy input (ensure_shared_input): {harness.shared_inputs}"
    )
    assert harness.pushes == [], f"`do` KHÔNG được đẩy input: {harness.pushes}"
    assert harness.launches == [], f"`do` KHÔNG được launch: {harness.launches}"
    # ...nhưng watcher **đã** được giao việc (mỗi job 1 watcher).
    assert len(harness.spawned) == 3, "mỗi job phải có watcher theo dõi"


def test_do_each_does_not_push_input_even_with_shared_input_dir(harness, tmp_path):
    """Có ``--in`` (thư mục) ⇒ ``do`` vẫn **không** đẩy (kể cả bản copy dùng chung)."""
    data = tmp_path / "data"
    data.mkdir()
    (data / "a.bin").write_text("x")

    payload, code = cli_mod._cmd_do(_args(tmp_path, each=2, inputs=[str(data)]))

    assert code == 0
    assert payload["count"] == 2
    assert harness.shared_inputs == [], (
        f"`do` KHÔNG được đẩy input: {harness.shared_inputs}"
    )
    assert harness.pushes == [], f"`do` KHÔNG được đẩy input: {harness.pushes}"
    assert harness.launches == [], f"`do` KHÔNG được launch: {harness.launches}"


# --------------------------------------------------------------------------
# 2 — probe fleet 1 lần cho CẢ lô
# --------------------------------------------------------------------------
def test_probe_fleet_runs_once_for_the_whole_batch(harness, tmp_path):
    """4 item, 3 host ⇒ probe **3 lần** (1 lần/host), **không** phải 12 lần."""
    payload, code = cli_mod._cmd_do(_args(tmp_path, each=4))

    assert code == 0
    assert payload["count"] == 4
    assert len(harness.probes) == 3, (
        f"phải probe 1 lần cho cả lô (3 host), thấy {len(harness.probes)}: "
        f"{harness.probes}"
    )
    assert len(set(harness.probes)) == 3


# --------------------------------------------------------------------------
# 3 — input local không tồn tại ⇒ fail SỚM (không đụng fleet)
# --------------------------------------------------------------------------
def test_missing_local_input_fails_fast(harness, tmp_path):
    """Input không có ⇒ ``do`` fail ngay (exit ≠ 0) và **không** probe host."""
    missing = str(tmp_path / "khong-co.mp4")

    with pytest.raises(Exception) as excinfo:
        cli_mod._cmd_do(_args(tmp_path, each=1, inputs=[missing]))

    code = getattr(excinfo.value, "code", 0)
    assert code != 0, "input không tồn tại ⇒ phải fail"
    assert code == 2, "input sai ⇒ exit 2 (EXIT_INPUT)"
    assert harness.probes == [], (
        f"phải fail SỚM (trước cả khi probe fleet): {harness.probes}"
    )
    assert harness.pushes == [] and harness.launches == []


def test_missing_item_input_fails_fast(harness, tmp_path):
    """``--each`` khớp item không tồn tại ⇒ fail sớm, không tạo job."""
    args = _args(tmp_path, each=1)
    args.each = str(tmp_path / "khong-co-*.txt")

    with pytest.raises(Exception) as excinfo:
        cli_mod._cmd_do(args)

    assert getattr(excinfo.value, "code", 0) == 2
    assert harness.probes == []
    assert list((harness.state / "jobs").glob("*.json")) == []


# --------------------------------------------------------------------------
# 4 — watcher THẬT SỰ đẩy input + launch cho job mà `do` đã hoãn
# --------------------------------------------------------------------------
def test_watcher_loop_starts_a_job_whose_state_is_already_launched(
    harness, tmp_path, monkeypatch
):
    """State ``launched`` (đã chiếm slot) nhưng **chưa** launch ⇒ watcher start.

    Đây là đường dẫn **sản xuất** của B36/C10: ``do` trả JSON về đã đặt
    state là ``launched`` (đã nhận slot) nhưng process trên host **chưa** chạy
    — watcher phải nhận ra điều đó và tự đẩy input + launch.
    """
    payload, code = cli_mod._cmd_do(_args(tmp_path, each=1))
    assert code == 0

    job_id = payload["job"]
    job = job_mod.load_job(job_id)
    assert job.state == "launched", "đã chiếm slot ⇒ state là launched"
    assert not job.attempts, "chưa từng launch ⇒ chưa có attempt"

    started: list[str] = []

    def fake_start(target):
        """Giả lập ``_start`` thật (launch thành công ⇒ có attempt + tắt cờ)."""
        started.append(target.id)
        target.state = "launched"
        target.attempts = list(target.attempts or []) + [
            {"attempt": 0, "host": target.host}
        ]
        job_mod.save_job(target)
        job_mod.set_job_options(target.id, deferred_start=False)
        return True

    monkeypatch.setattr(watcher_mod, "_start", fake_start)
    monkeypatch.setattr(
        watcher_mod, "remote_state", lambda *a, **k: {"reachable": False}
    )

    class _Stop(Exception):
        pass

    def _boom(_seconds):
        raise _Stop()

    monkeypatch.setattr(watcher_mod, "_sleep", _boom)

    with pytest.raises(_Stop):
        watcher_mod.watch(job_id)

    assert started == [job_id], "watcher phải start job bị hoãn"



def test_watcher_pushes_and_launches_the_deferred_job(harness, tmp_path):
    """Job bị hoãn ⇒ watcher đẩy input + launch (không phải "không ai làm")."""
    payload, code = cli_mod._cmd_do(_args(tmp_path, each=1))

    assert code == 0
    job_id = payload["job"]
    assert harness.pushes == [] and harness.launches == []

    job = job_mod.load_job(job_id)
    assert watcher_mod._start(job) is True, "watcher phải start được job bị hoãn"

    assert harness.shared_inputs, "watcher phải đẩy input lên host"
    assert harness.pushes, "watcher phải đẩy input lên host"
    assert harness.launches, "watcher phải launch job trên host"
    assert job_mod.load_job(job_id).state == "launched"


def test_each_deferred_job_is_started_independently(harness, tmp_path):
    """Mỗi job của lô có watcher riêng ⇒ đẩy input **độc lập**, không chung.

    (Nếu có 1 watcher chung thì lô lại thành hàng đợi tuần tự.)
    """
    payload, code = cli_mod._cmd_do(_args(tmp_path, each=3))

    assert code == 0
    for job_id in payload["jobs"]:
        job = job_mod.load_job(job_id)
        assert watcher_mod._start(job) is True

    # 3 item khác nhau ⇒ 3 nguồn riêng ⇒ 3 lần đẩy (mỗi item 1 bản copy).
    assert len(harness.pushes) == 3, f"mỗi item 1 bản copy: {harness.pushes}"
    assert len(harness.launches) == 3


# --------------------------------------------------------------------------
# 5 — hợp đồng cũ giữ nguyên
# --------------------------------------------------------------------------
def test_each_item_lands_on_a_different_host(harness, tmp_path):
    """Chọn host **không** đổi: mỗi item một máy khác nhau (nếu fleet đủ)."""
    payload, code = cli_mod._cmd_do(_args(tmp_path, each=3))

    assert code == 0
    hosts = [job_mod.load_job(job_id).host for job_id in payload["jobs"]]
    assert len(set(hosts)) == 3, f"mỗi item 1 host khác nhau: {hosts}"


def test_payload_still_lists_every_job(harness, tmp_path):
    """JSON phải có ``jobs`` (agent cần biết job nào đã tạo) + ``count``."""
    payload, code = cli_mod._cmd_do(_args(tmp_path, each=3))

    assert code == 0
    assert payload["ok"] is True
    assert payload["count"] == 3
    assert len(payload["jobs"]) == 3
    assert len(set(payload["jobs"])) == 3
    assert payload["job"] == payload["jobs"][0]


def test_shared_store_keeps_one_copy_per_host(harness, tmp_path):
    """Kho dùng chung: nhiều job **cùng host** ⇒ **1 bản copy** (không đẩy lại).

    Hợp đồng "1 bản copy/host" nay do **watcher** bảo đảm (marker trên host),
    không phải do ``do``.
    """
    data = tmp_path / "data"
    data.mkdir()
    (data / "a.bin").write_text("x")
    args = _args(tmp_path, each=3, inputs=[str(data)])
    args.host = "muse-a"  # cùng 1 host ⇒ cùng 1 bản copy

    payload, code = cli_mod._cmd_do(args)
    assert code == 0
    assert len(payload["jobs"]) == 3

    for job_id in payload["jobs"]:
        job = job_mod.load_job(job_id)
        assert watcher_mod._start(job) is True

    dir_pushes = [entry for entry in harness.pushes if entry[1] == str(data)]
    assert len(dir_pushes) == 1, (
        f"3 job cùng host phải chỉ có 1 bản copy trong kho: {harness.pushes}"
    )


def test_job_without_slot_stays_queued_and_is_not_launched(harness, tmp_path):
    """Hết slot ⇒ job **xếp hàng** (không launch, không vượt slot)."""
    args = _args(tmp_path, each=3)
    args.host = "muse-a"  # 1 host, 1 slot ⇒ 1 chạy + 2 xếp hàng

    payload, code = cli_mod._cmd_do(args)

    assert code == 0
    assert payload["count"] == 3
    states = _states(harness.state)
    assert states.count("launched") == 1, f"chỉ 1 job được slot: {states}"
    assert states.count("queued") == 2, f"2 job còn lại phải xếp hàng: {states}"
    assert harness.launches == [], "job xếp hàng KHÔNG được launch"


def test_deferred_job_is_marked_launched_once_slot_granted(harness, tmp_path):
    """Đã chiếm slot ⇒ state ``launched`` (hợp đồng §1.2 của SPEC-CLI)."""
    payload, code = cli_mod._cmd_do(_args(tmp_path, each=1))

    assert code == 0
    assert payload["state"] == "launched"
    options = job_mod.job_options(payload["job"])
    assert options.get("deferred_start") is True, (
        "job phải được đánh dấu hoãn (watcher sẽ đẩy input + launch)"
    )
