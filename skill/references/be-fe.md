# BE + FE on a remote host (setup-once, survives restarts)

Commands below use `roff` = `python3 <skill-dir>/roff.py` (see SKILL.md).

## 1. Toolchain once, not per job

`roff probe <host>` — if `tools.node`/`tools.npm` missing:

```bash
roff ensure <host> --setup <skill-dir>/setups/node.sh --name node
```

Installs node+npm into `~/.roff/toolchain/bin` (on PATH for all jobs). The stamp is
script-hash + boot_id + instance_token: second call skips; after a host restart/wipe the
stamp is gone and the next `ensure` (or `run --ensure`) re-installs automatically.
Python deps: same pattern — a setup script doing `pip3 install --user …` or a venv under
`~/.roff/toolchain`, guarded by an import check. Never reinstall inside every job.

## 2. Push the project, install deps once per project

```bash
roff push <host> ./myapp --name myapp            # node_modules excluded by default
roff run <host> --project myapp --ensure <skill-dir>/setups/node.sh --ensure-name node --cmd '
  [ -d node_modules ] || npm ci --no-audit --no-fund
  npm run build
'
```

`node_modules` lives in `~/.roff/projects/myapp` on the host and persists across jobs and
ssh disconnects. After a wipe, re-push + the `[ -d node_modules ] || npm ci` guard rebuilds it.

## 3. Run BE and FE as detached jobs

```bash
roff run <host> --project myapp --cmd 'setsid nohup npm run start:be -- --port 8000 > "$ROFF_OUT/be.log" 2>&1 < /dev/null & echo be-pid=$!'
roff run <host> --project myapp --cmd 'setsid nohup npm run start:fe -- --port 3000 > "$ROFF_OUT/fe.log" 2>&1 < /dev/null & echo fe-pid=$!'
```

These jobs "exit" immediately (the launcher backgrounds the server); the servers keep
running. Check health through a tunnel, not by job status.

## 4. Tunnel to local

```bash
roff tunnel start <host> --lport 18000 --rport 8000   # BE → http://127.0.0.1:18000
roff tunnel start <host> --lport 13000 --rport 3000   # FE → http://127.0.0.1:13000
curl -sf http://127.0.0.1:18000/health
```

`tunnel start` also checks the remote port: exit 1 (`remote_listening: false`) means the
server isn't up yet — go back to step 3, don't retry the tunnel blindly. `remote_listening:
null` means the check couldn't run; the curl above is then the proof.

FE must proxy API calls to the BE's remote port (or bind 0.0.0.0 and use the host's
internal address) — the browser hits the tunnel, the FE server talks to BE server-side.

## 5. Cleanup

Stop tunnels you opened (`roff tunnel stop <host> --lport …`). Kill only the server pids
you recorded (`kill <pid>` over ssh) — never broad pkill. After a host restart the servers
are dead anyway; re-run step 3 (deps survive if only a restart, not a wipe).