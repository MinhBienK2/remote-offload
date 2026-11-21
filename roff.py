#!/usr/bin/env python3
"""roff — remote-offload helper. Plain ssh/rsync, python3 stdlib only, no daemon.

Contract: every command prints one JSON object; exit codes are machine truth:
  0 ok | 1 job failed (remote exit in .exit) | 2 usage/contract error
  69 job lost (host restarted/wiped) | 124 wait timeout | 130 cancelled
  255 host unreachable (fail loudly; NEVER silently fall back to local)
State: local ledger ~/.local/state/roff (jobs, host profiles, tunnels, audit).
Remote layout: ~/.roff/{jobs/<id>,projects,setup,toolchain,env.sh,instance}.
"""
import argparse
import base64
import concurrent.futures
import hashlib
import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

STATE = Path(os.environ.get("ROFF_STATE", str(Path.home() / ".local/state/roff")))
JOBS = STATE / "jobs"
HOSTS = STATE / "hosts"
TUNNELS = STATE / "tunnels"
CM = STATE / "cm"
for _d in (STATE, JOBS, HOSTS, TUNNELS, CM):
    _d.mkdir(parents=True, exist_ok=True)

SSH_OPTS = [
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=10",
    "-o", "ServerAliveInterval=15",
    "-o", "ServerAliveCountMax=3",
    "-o", "ControlMaster=auto",
    "-o", "ControlPath=" + str(CM / "%C"),
    "-o", "ControlPersist=120",
]
DEFAULT_EXCLUDES = [".git", "node_modules", ".venv", "venv", "__pycache__",
                    ".next", "dist", "build", ".cache", "*.pyc"]
TRANSPORT = "auto"  # auto|rsync|tar (global --transport)


class RoffError(Exception):
    def __init__(self, code, error, **extra):
        super().__init__(error)
        self.code = code
        self.payload = {"ok": False, "error": error, **extra}


def emit(obj, code=0):
    print(json.dumps(obj, ensure_ascii=False))
    sys.exit(code)


def audit(argv, code):
    try:
        with open(STATE / "audit.jsonl", "a") as f:
            f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                                "argv": argv, "exit": code}) + "\n")
    except OSError:
        pass


def ssh_base(host):
    return ["ssh", *SSH_OPTS, host]


def ssh(host, script, timeout=60, args=None):
    """Run script via `ssh host bash -s -- args`. Returns CompletedProcess."""
    remote = "bash -s --" + ("".join(" " + shlex.quote(x) for x in args) if args else "")
    try:
        return subprocess.run(ssh_base(host) + [remote], input=script,
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RoffError(255, f"ssh {host} timed out after {timeout}s", host=host)


def ssh_ok(host, script, timeout=60, what="ssh"):
    p = ssh(host, script, timeout)
    if p.returncode == 255:
        raise RoffError(255, f"{what}: host unreachable: {host}",
                        host=host, detail=p.stderr.strip()[-400:])
    if p.returncode != 0:
        raise RoffError(1, f"{what} failed on {host} (exit {p.returncode})",
                        host=host, detail=(p.stderr or p.stdout).strip()[-600:])
    return p


def have_rsync(host):
    prof = load_profile(host)
    if prof and prof.get("reachable"):
        return "rsync" in prof.get("tools", {})
    p = ssh(host, "command -v rsync >/dev/null && echo y")
    if p.returncode == 255:
        raise RoffError(255, f"host unreachable: {host}", host=host,
                        detail=p.stderr.strip()[-300:])
    return p.stdout.strip() == "y"


def rsync_cmd():
    return ["rsync", "-az", "--checksum", "-e", "ssh " + " ".join(shlex.quote(o) for o in SSH_OPTS)]


def load_profile(host):
    p = HOSTS / f"{host}.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except (OSError, ValueError):
            return None
    return None


def save_profile(host, prof):
    (HOSTS / f"{host}.json").write_text(json.dumps(prof, indent=1))


def load_job(jid):
    p = JOBS / f"{jid}.json"
    if not p.exists():
        raise RoffError(2, f"unknown job {jid} (ledger: {JOBS})", job=jid)
    return json.loads(p.read_text())


def save_job(job):
    (JOBS / f"{job['id']}.json").write_text(json.dumps(job, indent=1))


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


PROBE_SCRIPT = r'''
set -u
export PATH="$HOME/.roff/toolchain/bin:$HOME/.local/bin:$PATH"
boot=$(cat /proc/sys/kernel/random/boot_id 2>/dev/null || echo unknown)
if [ ! -f "$HOME/.roff/instance" ]; then
  mkdir -p "$HOME/.roff"
  (uuidgen 2>/dev/null || cat /proc/sys/kernel/random/uuid) > "$HOME/.roff/instance"
fi
inst=$(cat "$HOME/.roff/instance")
cores=$(nproc 2>/dev/null || echo 0)
mem=$(awk '/MemTotal/{printf "%d", $2/1024}' /proc/meminfo 2>/dev/null || echo 0)
memav=$(awk '/MemAvailable/{printf "%d", $2/1024}' /proc/meminfo 2>/dev/null || echo 0)
load=$(cut -d' ' -f1-3 /proc/loadavg 2>/dev/null || echo "")
disk=$(df -Pm "$HOME" 2>/dev/null | awk 'NR==2{print $4}')
up=$(cut -d. -f1 /proc/uptime 2>/dev/null || echo 0)
tools=""
for c in ffmpeg ffprobe node npm npx python3 pip3 git rsync docker chromium chromium-browser google-chrome Xvfb uv; do
  p=$(command -v "$c" 2>/dev/null) || continue
  v=$("$c" --version 2>&1 | head -1 | cut -c1-60 | tr -d '"\\')
  tools="$tools\"$c\":\"$v\","
done
tools="{${tools%,}}"
gpu=null
if command -v nvidia-smi >/dev/null 2>&1; then
  g=$(nvidia-smi --query-gpu=name,memory.total,memory.used --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -d '"')
  [ -n "$g" ] && gpu="\"$g\""
fi
durable=null
for d in /content/drive/MyDrive "$HOME/persistent" /persistent /data; do
  if [ -d "$d" ] && touch "$d/.roff-w" 2>/dev/null; then rm -f "$d/.roff-w"; durable="\"$d\""; break; fi
done
os=$(. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME" || uname -s)
printf '{"boot_id":"%s","instance_token":"%s","home":"%s","os":"%s","arch":"%s","cores":%s,"mem_mb":%s,"mem_available_mb":%s,"load":"%s","disk_free_mb":%s,"uptime_s":%s,"tools":%s,"gpu":%s,"durable_mount":%s}\n' \
  "$boot" "$inst" "$HOME" "$os" "$(uname -m)" "$cores" "$mem" "$memav" "$load" "${disk:-0}" "$up" "$tools" "$gpu" "$durable"
'''


def probe_host(host, timeout=30):
    p = ssh(host, PROBE_SCRIPT, timeout)
    if p.returncode != 0:
        return {"host": host, "reachable": False,
                "error": (p.stderr or p.stdout).strip()[-200:],
                "probed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    prof = None
    for line in reversed(p.stdout.strip().splitlines()):
        if line.startswith("{"):
            try:
                prof = json.loads(line)
                break
            except ValueError:
                continue
    if prof is None:
        return {"host": host, "reachable": False,
                "error": "probe output not parseable: " + p.stdout.strip()[-200:],
                "probed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    prof.update(host=host, reachable=True,
                probed_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    old = load_profile(host)
    if old and old.get("instance_token") and old["instance_token"] != prof["instance_token"]:
        prof["wiped_since_last_probe"] = True
    elif old and old.get("boot_id") and old["boot_id"] != prof["boot_id"]:
        prof["restarted_since_last_probe"] = True
    save_profile(host, prof)
    return prof


def ssh_aliases():
    cfg = Path.home() / ".ssh/config"
    aliases = []
    if cfg.exists():
        for ln in cfg.read_text().splitlines():
            s = ln.strip()
            if s.lower().startswith("host ") and not s.startswith("#"):
                for tok in s.split()[1:]:
                    if "*" not in tok and "?" not in tok and tok not in aliases:
                        aliases.append(tok)
    return aliases


def cmd_hosts(a):
    aliases = ssh_aliases()
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        profs = list(ex.map(lambda h: probe_host(h, a.timeout), aliases))
    emit({"ok": True, "hosts": profs})


def cmd_probe(a):
    if a.host == "all":
        targets = ssh_aliases()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            profs = list(ex.map(lambda h: probe_host(h, a.timeout), targets))
        emit({"ok": True, "hosts": profs})
    if a.cached:
        prof = load_profile(a.host)
        if not prof:
            raise RoffError(2, f"no cached profile for {a.host}; run probe first")
        emit({"ok": True, **prof})
    emit({"ok": True, **probe_host(a.host, a.timeout)})


def remote_home(host):
    """Absolute remote HOME (colab bridges may not tilde-expand rsync paths)."""
    prof = load_profile(host)
    if prof and prof.get("home"):
        return prof["home"]
    p = ssh(host, 'printf "%s" "$HOME"')
    if p.returncode != 0 or not p.stdout.strip().startswith("/"):
        raise RoffError(255, f"cannot resolve remote HOME on {host}", host=host,
                        detail=p.stderr.strip()[-200:])
    home = p.stdout.strip()
    prof = prof or {}
    prof["home"] = home
    save_profile(host, prof)
    return home


def abs_remote(host, remote):
    if remote.startswith("/"):
        return remote
    return remote_home(host) + "/" + remote.removeprefix("~/")


def rpath(remote):
    """~ -> $HOME for remote shell contexts (bash expands it)."""
    return remote if remote.startswith("/") else '$HOME/' + remote.removeprefix("~/")


def transfer_up(host, local, remote, excludes, timeout):
    local = str(local)
    absr = abs_remote(host, remote)
    ssh_ok(host, f'mkdir -p "{absr}"', 30, "push mkdir")
    use_rsync = TRANSPORT != "tar" and have_rsync(host)
    if use_rsync:
        cmd = rsync_cmd()
        for e in excludes:
            cmd += ["--exclude", e]
        cmd += [local.rstrip("/") + "/", f"{host}:{absr.rstrip('/')}/"]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if p.returncode != 0:
            raise RoffError(255 if p.returncode in (12, 255) else 1,
                            f"rsync push failed (exit {p.returncode})",
                            detail=p.stderr.strip()[-400:])
        return "rsync"
    cmd = ["tar", "-C", local, "-cf", "-"]
    for e in excludes:
        cmd += ["--exclude", e]
    cmd += ["."]
    rp_cmd = f'mkdir -p "{absr}" && tar -C "{absr}" -xf -'
    rp = subprocess.Popen(ssh_base(host) + [rp_cmd],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    tp = subprocess.Popen(cmd, stdout=rp.stdin, stderr=subprocess.PIPE)
    rp.stdin.close()
    tp_err = tp.stderr.read()
    tp.wait(timeout=timeout)
    out = rp.stdout.read()
    err = rp.stderr.read()
    rp.wait(timeout=timeout)
    if tp.returncode != 0 or rp.returncode != 0:
        raise RoffError(255 if rp.returncode == 255 else 1,
                        f"tar push failed (tar {tp.returncode}, ssh {rp.returncode})",
                        detail=(err + tp_err).decode()[-400:])
    return "tar"


def transfer_down(host, remote, local, timeout):
    local = Path(local)
    local.mkdir(parents=True, exist_ok=True)
    absr = abs_remote(host, remote)
    use_rsync = TRANSPORT != "tar" and have_rsync(host)
    if use_rsync:
        cmd = rsync_cmd() + [f"{host}:{absr.rstrip('/')}/", str(local).rstrip("/") + "/"]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if p.returncode != 0:
            raise RoffError(255 if p.returncode in (12, 255) else 1,
                            f"rsync pull failed (exit {p.returncode})",
                            detail=p.stderr.strip()[-400:])
        mode = "rsync"
    else:
        rp = subprocess.Popen(ssh_base(host) + [f'tar -C "{absr}" -cf - .'],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        tp = subprocess.Popen(["tar", "-C", str(local), "-xf", "-"], stdin=rp.stdout,
                              stderr=subprocess.PIPE)
        rp.stdout.close()
        tp_err = tp.stderr.read()
        tp.wait(timeout=timeout)
        err = rp.stderr.read()
        rp.wait(timeout=timeout)
        if tp.returncode != 0 or rp.returncode != 0:
            raise RoffError(255 if rp.returncode == 255 else 1,
                            f"tar pull failed (tar {tp.returncode}, ssh {rp.returncode})",
                            detail=(err + tp_err).decode()[-400:])
        mode = "tar"
    files = [{"path": str(f.relative_to(local)), "bytes": f.stat().st_size,
              "sha256": sha256_file(f)}
             for f in sorted(local.rglob("*")) if f.is_file()]
    return mode, files


def cmd_push(a):
    local = Path(a.local).expanduser().resolve()
    if not local.is_dir():
        raise RoffError(2, f"local dir not found: {local}")
    remote = a.remote or f"{remote_home(a.host)}/.roff/projects/{a.name or local.name}"
    excludes = [] if a.no_default_excludes else list(DEFAULT_EXCLUDES)
    excludes += a.exclude or []
    mode = transfer_up(a.host, local, remote, excludes, a.timeout)
    emit({"ok": True, "host": a.host, "remote": remote, "transport": mode,
          "excludes": excludes})


def cmd_pull(a):
    job = load_job(a.job) if a.job else None
    host = a.host or (job["host"] if job else None)
    remote = a.remote
    if not remote and job:
        remote = f"{job['remote']}/out"
    if not host or not remote:
        raise RoffError(2, "pull needs --job or both --host and --remote")
    local = Path(a.local).expanduser().resolve()
    mode, files = transfer_down(host, remote, local, a.timeout)
    emit({"ok": True, "host": host, "remote": remote, "local": str(local),
          "transport": mode, "files": files})


ENSURE_HEADER = 'mkdir -p "$HOME/.roff/toolchain/bin"\nexport PATH="$HOME/.roff/toolchain/bin:$HOME/.local/bin:$PATH"\n'


def ensure_setup(host, setup_path, name, force=False, timeout=1800):
    """Idempotent bootstrap, stamped by script-hash + boot_id + instance_token.
    Skip ONLY on the remote stamp: a reboot keeps ~/.roff but kills processes;
    a wipe removes both, and the local profile copy is informational only."""
    setup_path = Path(setup_path).expanduser().resolve()
    if not setup_path.is_file():
        raise RoffError(2, f"setup script not found: {setup_path}")
    h = sha256_file(setup_path)
    prof = load_profile(host) or probe_host(host)
    if not prof.get("reachable"):
        raise RoffError(255, f"ensure: host unreachable: {host}", host=host)
    stamp = f"{h}:{prof['boot_id']}:{prof['instance_token']}"
    rstamp = f"~/.roff/setup/{name}.stamp"
    p = ssh(host, f"cat {rstamp} 2>/dev/null")
    if not force and p.stdout.strip() == stamp:
        return {"ensured": True, "skipped": True, "stamp": stamp}
    body = ENSURE_HEADER + setup_path.read_text()
    b64 = base64.b64encode(body.encode()).decode()
    script = (
        'set -e\nmkdir -p ~/.roff/setup\n'
        f'printf "%s" "$1" | base64 -d > ~/.roff/setup/{name}.sh\n'
        f"bash ~/.roff/setup/{name}.sh > ~/.roff/setup/{name}.log 2>&1\n"
        f"printf '%s' {shlex.quote(stamp)} > {rstamp}\n"
        "echo ROFF_ENSURE_OK\n"
    )
    p = ssh(host, script, timeout, args=[b64])
    if p.returncode != 0 or "ROFF_ENSURE_OK" not in p.stdout:
        tail = ssh(host, f"tail -15 ~/.roff/setup/{name}.log 2>/dev/null").stdout
        raise RoffError(1, f"ensure '{name}' failed on {host} (exit {p.returncode}); fix the setup script, do not retry blindly",
                        host=host, log_tail=tail.strip())
    prof = load_profile(host) or {}
    prof.setdefault("setups", {})[name] = stamp
    save_profile(host, prof)
    return {"ensured": True, "skipped": False, "stamp": stamp,
            "log": f"~/.roff/setup/{name}.log"}


def cmd_ensure(a):
    emit({"ok": True, "host": a.host, "name": a.name,
          **ensure_setup(a.host, a.setup, a.name, a.force, a.timeout)})


RUNNER = r'''#!/usr/bin/env bash
export ROFF_OUT="$ROFF_JOB/out"
export PATH="$HOME/.roff/toolchain/bin:$HOME/.local/bin:$PATH"
if ! cd "$ROFF_WORKDIR"; then
  echo "roff: workdir does not exist: $ROFF_WORKDIR (push the project first?)" > "$ROFF_JOB/log"
  echo 97 > "$ROFF_JOB/exit"; exit 97
fi
[ -f "$HOME/.roff/env.sh" ] && . "$HOME/.roff/env.sh"
[ -f "$ROFF_JOB/env.sh" ] && . "$ROFF_JOB/env.sh"
bash "$ROFF_JOB/cmd.sh" > "$ROFF_JOB/log" 2>&1
rc=$?
printf '%s' "$rc" > "$ROFF_JOB/exit.tmp" && mv "$ROFF_JOB/exit.tmp" "$ROFF_JOB/exit"
'''

LAUNCHER = r'''
set -e
d="$HOME/.roff/jobs/$1"
mkdir -p "$d/out"
printf '%s' "$2" | base64 -d > "$d/cmd.sh"
printf '%s' "$3" | base64 -d > "$d/run.sh"
printf '%s' "$4" | base64 -d > "$d/env.sh"
boot=$(cat /proc/sys/kernel/random/boot_id)
printf '%s' "$boot" > "$d/boot"
wd="$5"; case "$wd" in "~") wd="$HOME";; "~/"*) wd="$HOME/${wd#\~/}";; esac
ROFF_JOB="$d" ROFF_WORKDIR="$wd" setsid nohup bash "$d/run.sh" >/dev/null 2>&1 < /dev/null &
pid=$!
printf '%s' "$pid" > "$d/pid"
echo "ROFF_PID=$pid"
'''


def cmd_run(a):
    host = a.host
    prof = load_profile(host) or probe_host(host)
    if not prof.get("reachable"):
        raise RoffError(255, f"run: host unreachable: {host} — fail loudly, do not run locally", host=host)
    setup_info = None
    if a.ensure:
        setup_info = ensure_setup(host, a.ensure, a.ensure_name or Path(a.ensure).stem,
                                  timeout=a.setup_timeout)
        prof = load_profile(host)  # ensure may have refreshed
    jid = time.strftime("%y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
    home = remote_home(host)
    remote = f"{home}/.roff/jobs/{jid}"
    if a.workdir:
        workdir = abs_remote(host, a.workdir)
    elif a.project:
        workdir = f"{home}/.roff/projects/{a.project}"
    else:
        workdir = home
    env_lines = []
    for kv in a.env or []:
        if "=" not in kv:
            raise RoffError(2, f"--env expects KEY=VALUE, got {kv!r}")
        k, v = kv.split("=", 1)
        env_lines.append(f"export {k}={shlex.quote(v)}")
    env_block = ("\n".join(env_lines) + "\n") if env_lines else ""
    wd = workdir.replace("~/", "$HOME/", 1) if workdir.startswith("~") else workdir
    def b64(s):
        return base64.b64encode(s.encode()).decode()
    script = LAUNCHER
    p = ssh(host, script, a.submit_timeout,
            args=[jid, b64(a.cmd), b64(RUNNER), b64(env_block), wd])
    if p.returncode == 255:
        raise RoffError(255, f"run: host unreachable: {host}", host=host,
                        detail=p.stderr.strip()[-400:])
    if p.returncode != 0 or "ROFF_PID=" not in p.stdout:
        raise RoffError(1, f"run: launch failed on {host} (exit {p.returncode})",
                        host=host, detail=(p.stderr or p.stdout).strip()[-600:])
    pid = int(p.stdout.split("ROFF_PID=")[1].split()[0])
    job = {"id": jid, "host": host, "remote": remote, "pid": pid,
           "cmd": a.cmd, "workdir": workdir, "project": a.project,
           "boot_id": prof.get("boot_id"), "instance_token": prof.get("instance_token"),
           "submitted_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
           "ensure": setup_info}
    save_job(job)
    emit({"ok": True, "job": jid, "host": host, "pid": pid, "remote": remote,
          "workdir": workdir, "ensure": setup_info,
          "next": f"roff wait {jid} | roff logs {jid} | roff pull {jid} --local <dir>"})


def remote_state(host, job, timeout=30):
    d = rpath(job["remote"])
    script = (
        f'd="{d}"\n'
        'if [ ! -d "$d" ]; then echo STATE=missing; exit 0; fi\n'
        'b=$(cat /proc/sys/kernel/random/boot_id)\n'
        'sb=$(cat "$d/boot" 2>/dev/null)\n'
        'if [ "$b" != "$sb" ]; then echo STATE=rebooted; exit 0; fi\n'
        'if [ -f "$d/exit" ]; then echo "STATE=exited EXIT=$(cat "$d/exit")"; exit 0; fi\n'
        'pid=$(cat "$d/pid" 2>/dev/null)\n'
        'if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then echo STATE=running; else echo STATE=vanished; fi\n'
    )
    p = ssh(host, script, timeout)
    if p.returncode == 255:
        return {"state": "unreachable", "detail": p.stderr.strip()[-200:]}
    out = p.stdout.strip()
    if out.startswith("STATE=exited"):
        return {"state": "exited", "exit": int(out.split("EXIT=")[1])}
    if out == "STATE=running":
        return {"state": "running"}
    if out == "STATE=rebooted":
        return {"state": "lost", "reason": "host restarted since submit (boot_id changed); job and possibly toolchain are gone"}
    if out == "STATE=missing":
        return {"state": "lost", "reason": "job dir missing — host wiped/recreated; re-probe and re-ensure, then resubmit"}
    return {"state": "lost", "reason": "process vanished without exit file (OOM kill or wipe)"}


def cmd_status(a):
    job = load_job(a.job)
    st = remote_state(job["host"], job)
    emit({"ok": st["state"] in ("running", "exited"), "job": a.job,
          "host": job["host"], **st},
         0 if st["state"] in ("running", "exited") else (255 if st["state"] == "unreachable" else 69))


def cmd_logs(a):
    job = load_job(a.job)
    d = rpath(job["remote"])
    p = ssh(job["host"], f'tail -n {int(a.tail)} "{d}/log" 2>/dev/null; echo; wc -l < "{d}/log" 2>/dev/null')
    if p.returncode == 255:
        raise RoffError(255, f"logs: host unreachable: {job['host']}", host=job["host"])
    lines = p.stdout.splitlines()
    total = lines[-1].strip() if lines and lines[-1].strip().isdigit() else "0"
    emit({"ok": True, "job": a.job, "total_lines": int(total),
          "lines": lines[:-1] if total != "0" else lines})


def cmd_wait(a):
    job = load_job(a.job)
    deadline = time.time() + a.timeout if a.timeout else None
    delay = 1.0
    while True:
        st = remote_state(job["host"], job)
        s = st["state"]
        if s == "exited":
            rc = st["exit"]
            emit({"ok": rc == 0, "job": a.job, "host": job["host"], **st},
                 0 if rc == 0 else 1)
        if s == "lost":
            emit({"ok": False, "job": a.job, "host": job["host"], **st}, 69)
        if s == "unreachable":
            if deadline and time.time() >= deadline:
                emit({"ok": False, "job": a.job, "host": job["host"], **st,
                      "note": "still unreachable at timeout; job may still be running — contact loss is not process death"}, 255)
            # keep polling through transient network loss
        if deadline and time.time() >= deadline:
            emit({"ok": False, "job": a.job, "host": job["host"], **st,
                  "note": "wait timed out; job keeps running remotely"}, 124)
        time.sleep(delay)
        delay = min(delay * 1.7, 15.0)


def cmd_cancel(a):
    job = load_job(a.job)
    d = rpath(job["remote"])
    script = (
        f'd="{d}"\npid=$(cat "$d/pid" 2>/dev/null)\n'
        'if [ -z "$pid" ]; then echo NOPID; exit 0; fi\n'
        'kill -TERM -- "-$pid" 2>/dev/null\n'
        'for i in $(seq 1 10); do [ -f "$d/exit" ] && break; kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done\n'
        'kill -KILL -- "-$pid" 2>/dev/null\n'
        '[ -f "$d/exit" ] || printf 130 > "$d/exit"\n'
        'echo CANCELLED\n'
    )
    p = ssh(job["host"], script, 40)
    if p.returncode == 255:
        raise RoffError(255, f"cancel: host unreachable: {job['host']} — job may still run; retry when reachable",
                        host=job["host"])
    emit({"ok": True, "job": a.job, "host": job["host"], "state": "cancelled", "exit": 130}, 130)


def cmd_jobs(a):
    jobs = []
    for f in sorted(JOBS.glob("*.json")):
        try:
            j = json.loads(f.read_text())
            jobs.append({k: j.get(k) for k in ("id", "host", "cmd", "submitted_at", "project")})
        except (OSError, ValueError):
            continue
    emit({"ok": True, "jobs": jobs[-a.limit:]})


def cmd_tunnel(a):
    if a.action == "list":
        rows = []
        for f in TUNNELS.glob("*.json"):
            t = json.loads(f.read_text())
            try:
                with socket.create_connection(("127.0.0.1", t["lport"]), timeout=1):
                    t["alive"] = True
            except OSError:
                t["alive"] = False
            rows.append(t)
        emit({"ok": True, "tunnels": rows})
    if a.action == "stop":
        key = f"{a.host}-{a.lport}"
        f = TUNNELS / f"{key}.json"
        if not f.exists():
            raise RoffError(2, f"no tunnel {key}")
        t = json.loads(f.read_text())
        if t.get("master_pid"):
            subprocess.run(["ssh", *SSH_OPTS, "-O", "cancel",
                            "-L", f"{t['lport']}:127.0.0.1:{t['rport']}", t["host"]],
                           capture_output=True, timeout=15)
        else:
            try:
                os.kill(t["pid"], signal.SIGTERM)
            except OSError:
                pass
        f.unlink()
        emit({"ok": True, "stopped": key})
    # start
    if not a.rport:
        raise RoffError(2, "tunnel start needs --rport")
    key = f"{a.host}-{a.lport}"
    f = TUNNELS / f"{key}.json"
    if f.exists():
        t = json.loads(f.read_text())
        try:
            os.kill(t["pid"], 0)
            emit({"ok": True, "reused": True, **t})
        except OSError:
            f.unlink()
    cmd = ["ssh", *SSH_OPTS, "-o", "ExitOnForwardFailure=yes",
           "-o", "ControlMaster=no", "-o", "ControlPath=none", "-N",
           "-L", f"{a.lport}:127.0.0.1:{a.rport}", a.host]
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)

    deadline = time.time() + 15
    up = False
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        try:
            with socket.create_connection(("127.0.0.1", a.lport), timeout=1):
                up = True
                break
        except OSError:
            time.sleep(0.5)
    if not up:
        proc.terminate()
        raise RoffError(255, f"tunnel failed to establish (ssh exit {proc.poll()}); is the remote service listening on {a.rport}?",
                        host=a.host)
    # If a live ControlMaster serves this host, it may have adopted the -L
    # request: the listener then lives in the master, and killing proc does
    # nothing. Detect ownership and tell the caller how to stop it.
    owner = None
    try:
        out = subprocess.run(["ss", "-ltnp"], capture_output=True, text=True, timeout=5).stdout
        for ln in out.splitlines():
            if f":{a.lport} " in ln and "pid=" in ln:
                owner = int(ln.split("pid=")[1].split(",")[0])
                break
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    if owner is not None and owner != proc.pid:
        proc.terminate()
        t_note = (f"forward is served by shared ssh master pid {owner}; "
                  f"stop it with: ssh -O cancel -L {a.lport}:127.0.0.1:{a.rport} {a.host}")
    else:
        t_note = None
    t = {"host": a.host, "lport": a.lport, "rport": a.rport,
         "pid": owner if (owner is not None and owner != proc.pid) else proc.pid,
         "master_pid": owner if (owner is not None and owner != proc.pid) else None,
         "url": f"http://127.0.0.1:{a.lport}",
         "note": t_note,
         "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    f.write_text(json.dumps(t, indent=1))
    emit({"ok": True, **t})


def main():
    ap = argparse.ArgumentParser(prog="roff", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--transport", choices=["auto", "rsync", "tar"], default="auto")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("hosts", help="enumerate ~/.ssh/config aliases + probe all (parallel)")
    p.add_argument("--timeout", type=int, default=25)
    p.set_defaults(fn=cmd_hosts)

    p = sub.add_parser("probe", help="capability contract for one host (or 'all'); null gpu = no CUDA claims")
    p.add_argument("host")
    p.add_argument("--cached", action="store_true")
    p.add_argument("--timeout", type=int, default=30)
    p.set_defaults(fn=cmd_probe)

    p = sub.add_parser("push", help="sync local dir -> remote project dir")
    p.add_argument("host")
    p.add_argument("local")
    p.add_argument("--remote")
    p.add_argument("--name")
    p.add_argument("--exclude", action="append")
    p.add_argument("--no-default-excludes", action="store_true")
    p.add_argument("--timeout", type=int, default=600)
    p.set_defaults(fn=cmd_push)

    p = sub.add_parser("ensure", help="run idempotent setup script once per (script,boot,instance)")
    p.add_argument("host")
    p.add_argument("--setup", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--force", action="store_true")
    p.add_argument("--timeout", type=int, default=1800)
    p.set_defaults(fn=cmd_ensure)

    p = sub.add_parser("run", help="submit detached job; survives ssh disconnect; ledger survives agent restart")
    p.add_argument("host")
    p.add_argument("--cmd", required=True)
    p.add_argument("--project", help="workdir = ~/.roff/projects/<project>")
    p.add_argument("--workdir", help="explicit remote workdir (overrides --project)")
    p.add_argument("--ensure", help="setup script to ensure before launch")
    p.add_argument("--ensure-name")
    p.add_argument("--setup-timeout", type=int, default=1800)
    p.add_argument("--submit-timeout", type=int, default=120)
    p.add_argument("--env", action="append", help="KEY=VALUE (written to job env file, never remote argv)")
    p.set_defaults(fn=cmd_run)

    for name, fn, helpt in [("status", cmd_status, "job state: running/exited/lost/unreachable"),
                            ("cancel", cmd_cancel, "kill job process group (TERM then KILL)")]:
        p = sub.add_parser(name, help=helpt)
        p.add_argument("job")
        p.set_defaults(fn=fn)

    p = sub.add_parser("logs", help="tail job log")
    p.add_argument("job")
    p.add_argument("--tail", type=int, default=50)
    p.set_defaults(fn=cmd_logs)

    p = sub.add_parser("wait", help="poll until terminal state; exit mirrors job outcome")
    p.add_argument("job")
    p.add_argument("--timeout", type=int, default=0, help="seconds; 0 = wait forever")
    p.set_defaults(fn=cmd_wait)

    p = sub.add_parser("pull", help="fetch artifacts (default: job out/) with sha256 manifest")
    p.add_argument("job", nargs="?")
    p.add_argument("--host")
    p.add_argument("--remote")
    p.add_argument("--local", required=True)
    p.add_argument("--timeout", type=int, default=900)
    p.set_defaults(fn=cmd_pull)

    p = sub.add_parser("jobs", help="list local job ledger")
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(fn=cmd_jobs)

    p = sub.add_parser("tunnel", help="managed ssh -L forwards")
    p.add_argument("action", choices=["start", "list", "stop"])
    p.add_argument("host", nargs="?")
    p.add_argument("--lport", type=int)
    p.add_argument("--rport", type=int)
    p.set_defaults(fn=cmd_tunnel)

    a = ap.parse_args()
    global TRANSPORT
    TRANSPORT = a.transport
    if a.cmd == "tunnel" and a.action != "list" and (not a.host or not a.lport):
        ap.error("tunnel start/stop needs host and --lport")
    code = 0
    try:
        a.fn(a)
    except RoffError as e:
        code = e.code
        print(json.dumps(e.payload, ensure_ascii=False))
    except subprocess.TimeoutExpired as e:
        code = 255
        print(json.dumps({"ok": False, "error": f"local subprocess timeout: {e.cmd[:3]}"}))
    except KeyboardInterrupt:
        code = 130
        print(json.dumps({"ok": False, "error": "interrupted locally; remote job keeps running"}))
    finally:
        audit(sys.argv[1:], code)
    sys.exit(code)


if __name__ == "__main__":
    main()