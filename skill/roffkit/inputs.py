"""Chuẩn bị **input** của 1 job trên host — dùng chung cho ``cli`` và ``watcher``.

Vì sao tách module riêng:

* ``cli`` import ``watcher`` (không được import ngược lại — import vòng);
* cả hai đều cần **dựng lại** input trên host: ``cli`` khi launch lần đầu,
  ``watcher`` khi launch job vừa được xếp hàng (slot) hoặc khi failover sang
  host khác.

Input có 4 kiểu:

=========  ==========================================================
``path``   thư mục/file local ⇒ đẩy vào **kho dùng chung** của host
``git``    ``git:<url>`` ⇒ clone **trên host**
``http``   ``https://…`` ⇒ host tự tải (nếu host có internet) hoặc
           tải qua local rồi đẩy lên
``job``    ``job:<id>`` ⇒ lấy output của job khác (chỉ lúc launch lần đầu)
=========  ==========================================================

Module này **không in ra stdout** (quy tắc chung của roff).
"""

from __future__ import annotations

import os
import shlex
import shutil
import tempfile
from typing import Any, Sequence

from . import shared as shared_mod
from . import transfer as transfer_mod
from .errors import EXIT_INPUT, EXIT_JOB_FAILED, EXIT_UNREACHABLE, RoffError

__all__ = [
    "clone_git_on_host",
    "classify_git_failure",
    "download_http_locally",
    "push_local_path",
    "prepare_inputs",
    "resolve_item_path",
    "REASON_NOT_RECREATABLE",
]

#: Reason khi input **không dựng lại được** trên host mới (vd ``--in job:``).
REASON_NOT_RECREATABLE = "input_not_recreatable"

#: Dấu hiệu lỗi git do **URL/repo** sai (⇒ exit 2, input sai).
_BAD_URL_MARKERS = (
    "not found",
    "could not read from remote repository",
    "does not exist",
    "not a git repository",
    "authentication failed",
    "permission denied",
    "invalid url",
    "no such file or directory",
    "empty repository",
    "remote branch",
)

#: Dấu hiệu lỗi **ssh** (host chết / không với được) ⇒ exit 255.
_SSH_FAILURE_MARKERS = (
    "could not resolve hostname",
    "connection refused",
    "connection reset",
    "connection timed out",
    "no route to host",
    "network is unreachable",
    "kex_exchange_identification",
    "ssh: connect to host",
    "host key verification failed",
)


def classify_git_failure(rc: int, stderr: str) -> tuple[int, str]:
    """Phân loại lỗi git ⇒ ``(exit_code, reason)`` (W3m).

    * ``rc == 255`` hoặc stderr có dấu hiệu **ssh** ⇒ host không với được
      ⇒ ``EXIT_UNREACHABLE`` (255) — đúng nghĩa "không với được host",
      và cho phép thử host khác (W3c);
    * lỗi do **URL/repo** ⇒ ``EXIT_INPUT`` (2) — input sai, không retry;
    * còn lại ⇒ ``EXIT_JOB_FAILED`` (1) — job fail (vd host không có mạng).
    """
    text = str(stderr or "").lower()
    if int(rc) == 255 or any(marker in text for marker in _SSH_FAILURE_MARKERS):
        return EXIT_UNREACHABLE, "host_unreachable"
    if any(marker in text for marker in _BAD_URL_MARKERS):
        return EXIT_INPUT, "git_bad_url"
    return EXIT_JOB_FAILED, "git_clone_failed"


# --------------------------------------------------------------------------
# từng kiểu input
# --------------------------------------------------------------------------
def clone_git_on_host(host: str, url: str, remote_dir: str) -> str:
    """Clone git **trên host** (không đi qua máy local) → trả đường dẫn remote."""
    name = _safe_name(os.path.basename(str(url).rstrip("/")) or "repo", "repo")
    target = f"{remote_dir.rstrip('/')}/{name}"
    script = (
        "mkdir -p " + shlex.quote(remote_dir) + " && "
        "git clone --depth 1 " + shlex.quote(url) + " " + shlex.quote(target)
    )
    rc, _out, err = _shell_runner(host, script)
    if rc != 0:
        detail = (err or "").strip() or f"rc={rc}"
        code, reason = classify_git_failure(rc, err)
        raise RoffError(
            code,
            reason,
            error=f"git clone thất bại trên host {host}: {url}",
            detail=detail,
            host=host,
            url=url,
        )
    return target


def download_http_locally(url: str) -> str:
    """Tải URL về máy local (chỉ dùng khi host **không** có internet)."""
    import urllib.error
    import urllib.request

    folder = tempfile.mkdtemp(prefix="roff-http-")
    name = os.path.basename(str(url).split("?")[0]) or "download.bin"
    target = os.path.join(folder, name)
    try:
        with urllib.request.urlopen(url, timeout=120) as response:  # noqa: S310
            with open(target, "wb") as handle:
                shutil.copyfileobj(response, handle)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        shutil.rmtree(folder, ignore_errors=True)
        # URL sai / tải hỏng ⇒ **input sai** (exit 2), không phải "không với
        # được host" (255) — xem W3m.
        raise RoffError(
            EXIT_INPUT,
            "download_failed",
            error=f"không tải được URL: {url}",
            detail=str(exc),
            url=url,
        ) from None
    return folder


def push_local_path(host: str, local_path: str, remote_dir: str) -> str:
    """Đẩy 1 file/thư mục local lên host; trả **đường dẫn remote** tương ứng.

    Thư mục → nội dung nằm trong ``remote_dir`` (đường dẫn remote = ``remote_dir``).
    File → nằm trong ``remote_dir/<tên>`` (đường dẫn remote = ``remote_dir/<tên>``).
    """
    absolute = os.path.abspath(os.path.expanduser(local_path))
    if os.path.isdir(absolute):
        transfer_mod.push(host, absolute, remote_dir)
        return remote_dir
    if not os.path.isfile(absolute):
        raise RoffError(
            EXIT_INPUT,
            "input_not_found",
            error=f"không có input local: {local_path}",
            input=local_path,
        )
    # File đơn lẻ: đóng gói trong 1 thư mục tạm (hardlink ⇒ không copy byte).
    staging = tempfile.mkdtemp(prefix="roff-in-")
    try:
        target = os.path.join(staging, os.path.basename(absolute))
        try:
            os.link(absolute, target)
        except OSError:
            shutil.copy2(absolute, target)
        transfer_mod.push(host, staging, remote_dir)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return f"{remote_dir.rstrip('/')}/{os.path.basename(absolute)}"


def prepare_job_input(
    host: str,
    source_job: Any,
    workdir: str,
) -> str:
    """Chuẩn bị input lấy từ job khác (``--in job:<id>``).

    Cùng host ⇒ dùng thẳng thư mục output trên host (không truyền gì).
    Khác host ⇒ stream host→host **qua pipe ở local, không ghi đĩa local**.
    """
    if source_job is None:
        raise RoffError(
            EXIT_INPUT, "invalid_input", error="--in job: không có job nguồn"
        )
    source_path = str(getattr(source_job, "out_remote", "") or "")
    if not source_path:
        raise RoffError(
            EXIT_INPUT,
            "invalid_input",
            error=f"job {source_job.id} chưa có output để dùng làm input",
            job=source_job.id,
        )
    if host == source_job.host:
        return source_path
    transfer_mod.stream_between(source_job.host, source_path, host, workdir)
    return workdir


# --------------------------------------------------------------------------
# item của --each
# --------------------------------------------------------------------------
def resolve_item_path(
    host: str,
    item_local: str,
    layout_root: str,
    source_locals: Sequence[str] = (),
    pushed: set[tuple[str, str]] | None = None,
) -> tuple[str, str, str, bool]:
    """Đường dẫn **remote** của 1 item của ``--each`` (trong kho dùng chung).

    Trả ``(đường dẫn remote, shared key, rel trong {key}/, item nằm trong input)``.

    * Item nằm trong 1 input thư mục ⇒ dùng đường dẫn tương đối trong bản copy
      của input đó (input đã nằm trong kho ⇒ **không đẩy lại**).
    * Item không nằm trong input nào ⇒ tự trở thành **1 nguồn riêng** trong kho
      (đẩy đúng 1 lần, các job sau dùng lại bản copy đó).
    """
    absolute = os.path.abspath(item_local)
    for local in source_locals or ():
        if not local:
            continue
        base = os.path.abspath(os.path.expanduser(local))
        if os.path.isdir(base) and absolute.startswith(base + os.sep):
            relative = os.path.relpath(absolute, base).replace(os.sep, "/")
            key = shared_mod.shared_key(base)
            return (
                shared_mod.shared_remote_path(layout_root, key, relative),
                key,
                relative,
                True,
            )
    key = shared_mod.shared_key(absolute)
    cache_key = (host, key)
    if pushed is not None and cache_key in pushed:
        pass
    else:
        shared_mod.ensure_shared_input(host, absolute, layout_root)
        if pushed is not None:
            pushed.add(cache_key)
    rel = shared_mod.shared_rel(absolute)
    return shared_mod.shared_remote_path(layout_root, key, rel), key, rel, False


# --------------------------------------------------------------------------
# chuẩn bị mọi input
# --------------------------------------------------------------------------
def prepare_inputs(
    specs: Sequence[dict],
    host: str,
    workdir: str,
    layout_root: str,
    has_internet: bool = True,
    source_job: Any = None,
    allow_job_input: bool = True,
) -> tuple[list[dict], str, str, dict[str, str]]:
    """Chuẩn bị **mọi** input của 1 job trên host.

    Args:
        specs: danh sách ``{"kind": "path"|"git"|"http"|"job", "value": ...}``.
        host: alias host đích.
        workdir: thư mục làm việc trên host (input ``git``/``http`` nằm trong đó).
        layout_root: layout root của host (kho dùng chung).
        has_internet: host có internet không (quyết định ai tải URL).
        source_job: job nguồn cho input kiểu ``job``.
        allow_job_input: ``False`` ⇒ input kiểu ``job`` **không** dựng lại
            được (dữ liệu nằm trên host của job nguồn) ⇒ ném lỗi
            :data:`REASON_NOT_RECREATABLE`.

    Returns:
        ``(danh sách input đã chuẩn bị, remote của input chính, kind của
        input chính, env cần thêm vào job)``.

    Raises:
        RoffError: ``EXIT_INPUT`` với reason ``input_not_recreatable`` khi
            input không dựng lại được trên host này.
    """
    prepared: list[dict] = []
    env: dict[str, str] = {}
    primary_remote = ""
    primary_kind = ""

    for spec in specs or ():
        if not isinstance(spec, dict):
            continue
        kind = str(spec.get("kind") or "path")
        value = str(spec.get("value") or "")
        entry: dict[str, Any] = {"kind": kind, "value": value}

        if kind == "path":
            local = os.path.abspath(os.path.expanduser(value))
            remote = shared_mod.ensure_shared_input(host, local, layout_root)
            entry.update(
                {
                    "local": local,
                    "key": shared_mod.shared_key(local),
                    "rel": shared_mod.shared_rel(local),
                    "remote": remote,
                }
            )
        elif kind == "git":
            entry["remote"] = clone_git_on_host(host, value, workdir)
        elif kind == "http":
            if has_internet:
                entry["remote"] = value
                env["ROFF_URL"] = value
            else:
                folder = download_http_locally(value)
                try:
                    entry["remote"] = push_local_path(host, folder, workdir)
                finally:
                    shutil.rmtree(folder, ignore_errors=True)
        elif kind == "job":
            if not allow_job_input:
                raise RoffError(
                    EXIT_INPUT,
                    REASON_NOT_RECREATABLE,
                    error=(
                        f"input kiểu job ({value}) không dựng lại được trên host "
                        f"{host}: dữ liệu nằm trên host của job nguồn"
                    ),
                    detail=f"input_not_recreatable: job:{value}",
                    input=value,
                    host=host,
                )
            job = source_job
            if job is None:
                from . import job as job_mod  # import cục bộ ⇒ tránh import vòng

                job = job_mod.load_job(value)
            entry["remote"] = prepare_job_input(host, job, workdir)
            env["ROFF_SOURCE_JOB"] = str(job.id)
        else:
            raise RoffError(
                EXIT_INPUT,
                "invalid_input",
                error=f"kiểu input không hợp lệ: {kind!r}",
                input=value,
            )

        if not primary_remote:
            primary_remote = str(entry.get("remote") or "")
            primary_kind = kind
        prepared.append(entry)

    return prepared, primary_remote, primary_kind, env


# --------------------------------------------------------------------------
# nội bộ
# --------------------------------------------------------------------------
def _safe_name(text: str, fallback: str = "project") -> str:
    """Tên project an toàn cho đường dẫn trên host."""
    cleaned = "".join(
        ch if (ch.isalnum() or ch in "._-") else "-" for ch in str(text or "").strip()
    ).strip("-.")
    return cleaned or fallback


def _shell_runner(host: str, script: str) -> tuple[int, str, str]:
    """Chạy 1 script sh trên host (dùng runner dùng chung của roff)."""
    from . import sshx

    result = sshx.run_shell(host, script, timeout=60)
    return (int(result.rc), result.stdout or "", result.stderr or "")
