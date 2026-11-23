# Backend + frontend on a remote host

Shape: two long-running servers on one host, reached from the local machine
through tunnels, torn down cleanly afterwards.

The servers are **not** jobs. A job that starts a server exits immediately and
the server keeps running. Reach it through a tunnel, not through job status.

## 1 — toolchain, once per host

```bash
roff probe <host>
```

Read `tools.node` and `tools.npm`. The Muse hosts have `node` but no `npm`;
install it once, before the first job:

```bash
roff ensure <host> --setup <skill-dir>/setups/npm.sh --name npm
```

The second `ensure` reports `skipped: true` — the setup is idempotent and
re-runs automatically after the host restarts or gets wiped.

## 2 — push the project, once

```bash
roff push <host> ./myapp --name myapp
```

`node_modules` is excluded from the push by default. Install it once, inside
the first job, and it stays on the host for every later job:

```bash
'  [ -d node_modules ] || npm ci --no-audit --no-fund'
```

## 3 — start both servers

```bash
roff do --host <host> --in ./myapp --out ./out --cmd '
  cd "$ROFF_WORK"
  setsid nohup npm run start:be -- --port 8000 > "$ROFF_OUT/be.log" 2>&1 < /dev/null &
  echo $! > "$ROFF_OUT/be.pid"
  setsid nohup npm run start:fe -- --port 3000 > "$ROFF_OUT/fe.log" 2>&1 < /dev/null &
  echo $! > "$ROFF_OUT/fe.pid"
'
```

`setsid nohup … &` detaches the server from the ssh session: the job exits, the
server lives. **Record the pids** — they are the only handle you have for
cleanup.

The job itself finishing with exit 0 means "the servers were launched", not
"the servers work". Check them through the tunnel.

## 4 — tunnel each port to localhost

```bash
roff tunnel start <host> --lport 18000 --rport 8000   # BE
roff tunnel start <host> --lport 13000 --rport 3000   # FE
curl -sf http://127.0.0.1:18000/health
```

Order matters: **the remote port must already be LISTENing** when the tunnel
starts. `roff tunnel start` checks that first and refuses with
`remote_listening: false` when nothing is listening — start the server, then
the tunnel.

The frontend must reach the backend over the **host's** address
(`http://127.0.0.1:8000` on the host), not the local one: the browser talks to
the FE through the tunnel, the FE talks to the BE server-side.

If the local port is already taken, `roff tunnel start` refuses with
`local_port_busy` and names the pid holding it. Release that port yourself —
roff does not kill processes it did not start.

## 5 — tear down

```bash
roff tunnel stop <host> --lport 18000
roff tunnel stop <host> --lport 13000
```

Then kill the servers by the pids you recorded:

```bash
roff do --host <host> --in job:<job-id> --cmd '
  kill "$(cat "$ROFF_IN/be.pid")" "$(cat "$ROFF_IN/fe.pid")" 2>/dev/null || true
'
```

`kill <pid>` for the pids you started. These hosts run the user's other
services — a `pkill -f node` there kills someone else's work.

Done when: `roff tunnel list` shows no tunnel for that host, `curl` through the
old port fails to connect, and the two pids are gone.

## After a host restart

The servers are dead, the pushed project and `node_modules` are not (they live
on the durable mount). Re-run step 3 — skip steps 1 and 2. After a host *wipe*,
start again at step 1.

## Two hosts — backend on A, frontend on B

When the two servers live on **different** hosts, host↔host does not connect
directly on this fleet (the bridge dies at the SSH banner). `roff tunnel link`
routes the traffic through the local machine:

```bash
# B phục vụ (backend). Dịch vụ trên B phải LISTEN trước.
roff tunnel link --from <aliasA> --to <aliasB> --rport 8000 --to-port 8000
```

Now a process **on A** that calls `127.0.0.1:8000` reaches the service on B:

```
A :8000  ──(ssh -R)──▶  local :18500  ──(ssh -L)──▶  B :8000
```

* `--rport` is the port **A** listens on. `--to-port` is the port the service
  listens on **B** (defaults to `--rport`). `--via-lport` is the intermediate
  port on the local machine — auto-chosen in 18500–18599 when you omit it.
* The link checks that **both** ends are LISTENing before reporting success
  (`a_listening`, `b_listening` in the JSON). `b_listening: false` means the
  service on B was not up and **nothing was opened**.
* Start the frontend on A **after** the link, pointing it at
  `127.0.0.1:<rport>` — not at B's address.

Tear it down (clears both branches, not just one):

```bash
roff tunnel stop --from <aliasA> --rport 8000
```

`roff tunnel list` shows the links that are open.

## When not to use this recipe

- The user wants the dev server on their own machine (HMR, their own GPU, a
  port they already have open) — run it locally.
- The server needs the user's browser session or local files — tunnel it
  instead of offloading it.
- One short-lived command that happens to start a server — use `roff do`
  directly, not the tunnel dance.
