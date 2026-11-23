# Troubleshooting

Start here when a `roff` command failed or a job ended in a state you did not
expect.

**Step 1 — read the JSON.** Every command prints one object; the exit code says
*what*, the JSON says *why*. The two fields that matter:

- `error` — the message from the layer that failed
- `reason` — a stable code (`host_restarted`, `local_port_busy`, …)

**Step 2 — find the row below.**

## Exit codes

| Exit | Cause | Action |
|---|---|---|
| 1 | the job ran and the command failed | `roff logs <job-id> --tail 100`, fix the cause, resubmit a **changed** command. A failed job is not retried automatically — that is deliberate |
| 2 | input rejected before anything ran | fix the flag or path: unknown job id, missing `--out`, `--timeout 0`. Nothing started, nothing to clean up |
| 3 | `~/.config/roff/config.ini` unreadable | fix the INI; without it everything still works with defaults, so deleting it is also a fix |
| 10 | no host usable — the fleet is exhausted, every host is dead, or every host was filtered out | report it to the user. **Do not run the command locally.** Read `rejected` (why each host was dropped) and `jobs` (jobs already created — wait for those too). A host that is merely **busy** does *not* land here: it produces `state:"queued"` with exit 0 |
| 69 | the job was lost — the machine died under it | read `reason`: `host_restarted` (reboot), `host_wiped` (home erased), `oom_killed` (out of memory), `vanished`. Then: `roff pull <job-id> --local ./partial` for whatever survived, `roff ensure` again if a toolchain is missing, `roff push` again if the project was wiped, resubmit |
| 97 | the working directory was never pushed | `roff push <host> <dir> --name <project>` with the same project name, then resubmit |
| 124 | the wait timed out | **not a failure** — the job is still running **or still queued**. `roff wait <job-id>` again, or `roff status <job-id>` |
| 130 | the job was cancelled | do not resubmit; tell the user who cancelled it |
| 255 | the host could not be reached | read `detail`. "Permission denied" / "publickey" = the key was refused — that is the user's key, report it. "Connection refused" / timeout = the host is down, pick another one |

Exit 1 and exit 69 look alike and are not. **1** = your command was wrong.
**69** = the machine died before the command finished.

## A job that seems stuck

```bash
roff status <job-id>      # state + heartbeat, local ledger only
roff logs <job-id> --tail 50
```

State `running` with a heartbeat that advances → the job is alive; wait.
State `running` with a heartbeat that froze and the host unreachable → the
tunnel dropped, not necessarily the job (a restart is a different `reason`).

## The job is queued, not started

`state: "queued"` means the host had **no free slot** when you submitted. The
job exists, it holds its place in the queue, and the watcher starts it by
itself as soon as a slot frees up. This is **not** an error:

- do **not** resubmit — that would queue a second copy of the same work;
- do **not** run the command locally — the whole point is that it runs off
  your machine;
- just keep waiting: `roff wait <job-id> --timeout 7200`.

`roff wait` returns exit 124 while a job is still queued. That is a timeout,
not a failure — wait again.

## The host is unreachable but the job may still be running

A dropped tunnel is not a dead job. Do not resubmit on the first unreachable
answer — check `reason` first: only `host_restarted`, `host_wiped` or
`oom_killed` mean the work is gone. A tunnel that comes back with the same
`boot_id` means the job is still running.

## Tunnel problems

| Message | Cause | Action |
|---|---|---|
| `remote_listening: false` | nothing is listening on the remote port | start the service on the host first, then open the tunnel. The check runs `ss`, then `netstat`, then `/proc/net/tcp` |
| `remote_listening: null` (unknown) | the host has none of those tools, so nobody can tell | fine — the tunnel opens, but verify by hitting the endpoint yourself |
| `local_port_busy` | another process holds the local port | roff names the pid and will **not** kill it — these hosts run the user's other services. Free the port or pick another one |
| `control_master_failed` | the master could not be created (key refused, host down) | read `detail`; fix the ssh connection first |
| tunnel opens but the connection resets | an old ssh master still holds the port and its forward was never cancelled | roff cancels forwards it owns; a master started outside roff has to be released by hand (`ssh -O cancel` with that master's own ControlPath) |

## The hook blocks a command you wanted to run

The hook stops known-heavy commands on the local machine. Options, in order:

1. Run it on a host: `roff do --cmd '…'`.
2. Run one specific command locally on purpose: prefix it with `ROFF_LOCAL=1`.
3. Change the rules: `[heavy] allow = <command>` in
   `~/.config/roff/config.ini`, or `[heavy] add = <command>` to block something
   new. A broken config never blocks anything — it falls back to the defaults.

## A tool is missing on the host

```bash
roff probe <host>                       # tools.* shows what the host has
roff ensure <host> --setup <skill-dir>/setups/npm.sh --name npm
```

`ensure` is idempotent: the second call reports `skipped: true`, and it re-runs
by itself after a host restart or wipe.

The Muse hosts have `node` but **no `npm`** — that is what `setups/npm.sh` is
for.

## The Chromium download stalls

The browser CDN can sit at 0 bytes for 15 minutes on a weak host. The setup
script has a hard timeout and fails loudly instead of hanging. When it fails:
use another host, or point Playwright at an existing browser
(`probe.tools.chromium` → `executablePath`).

## The job produced no artifact

- `$ROFF_OUT` is the only directory that comes home. Output written elsewhere
  on the host stays there.
- Check the exit code first: 1 means the command failed (the logs say why),
  69 means the machine died mid-run (partial output may still be there — pull
  it before resubmitting).
- `roff result <job-id>` lists the artifacts with sha256.

## Nothing local runs at all

`roff` keeps the local machine light: remote commands run under
`nice -n 19 ionice -c3`, transfers are capped, and the watcher polls the local
ledger rather than the hosts. If the local machine is busy anyway, check
`pgrep -af 'roff|ssh'` — a tunnel you opened on purpose is expected to be
there; anything else is leftover from a run that did not finish.
