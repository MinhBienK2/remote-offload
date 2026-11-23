# Headless browser (Playwright / Cypress) on a remote host

Two shapes:

- **A. Whole suite remote** — the suite runs on the host, the reports and
  screenshots come back as artifacts. Use this for anything big.
- **B. Browser remote, driver local** — a Playwright *server* runs on the host,
  a local script drives it through a tunnel. Use this for one-off checks and
  interactive-looking steps.

Headless only. These hosts have no display, and a GUI window on them is
useless anyway.

## A — whole suite remote

**1. Ensure the browser before the job.** The browser download is the slowest
step on a weak host, and it must not happen inside the job:

```bash
roff ensure <host> --setup <skill-dir>/setups/npm.sh --name npm
roff ensure <host> --setup <skill-dir>/setups/playwright-chromium.sh --name playwright
```

The setup installs Chromium into the durable toolchain area
(`$ROFF_ROOT/ms-playwright`), so it survives a restart and is reused by every
later job. The second run reports `skipped: true`.

**Browser build and Playwright package must agree.** If the project pins
`playwright@1.44`, install that browser build — edit `PLAYWRIGHT_VERSION` in
`setups/playwright-chromium.sh` to match the project's version. A mismatch
fails at launch with "Executable doesn't exist".

**2. Push and run:**

```bash
roff do --host <host> --in ./app --out ./e2e --need node --cmd '
  cd "$ROFF_WORK"
  [ -d node_modules ] || npm ci --no-audit --no-fund
  npx playwright test --reporter=line
  cp -r test-results "$ROFF_OUT/" 2>/dev/null || true
  cp -r playwright-report "$ROFF_OUT/" 2>/dev/null || true
'
```

Everything the run produces has to land under `$ROFF_OUT` — that directory is
what comes home. Screenshots and traces that stay in the working directory
stay on the host.

**3. Read the result:**

```bash
roff wait <job-id>
roff result <job-id>        # exit 1 means the tests failed; the artifacts are still there
```

Exit 1 with an `exit` field = the suite ran and failed: pull the report and
read the failures. Exit 1 with an `error` field and no `job` = a pre-job step
failed (push / ensure) — fix that step, there is no job to clean up.

Done when: the report and the screenshots are in the local `--out` directory
and `roff result` lists them with sha256.

## B — browser remote, driver local

For a quick screenshot or a scripted check that is too small to justify a full
job:

```bash
roff do --host <host> --out ./pw --cmd '
  setsid nohup npx -y playwright@latest launch-server --browser chromium \
    > "$ROFF_OUT/pw.log" 2>&1 < /dev/null & echo $! > "$ROFF_OUT/pw.pid"
  sleep 2; cat "$ROFF_OUT/pw.log"
'
```

The log prints a `ws://127.0.0.1:<port>/<guid>` endpoint. Forward that port:

```bash
roff tunnel start <host> --lport 19222 --rport <port>
```

The local driver connects to `ws://127.0.0.1:19222/<guid>`; screenshots written
by the driver travel back through the websocket — no file transfer needed.
Client and server Playwright versions must match, or the protocol handshake
fails.

Stop both when done: `roff tunnel stop <host> --lport 19222`, then kill the
server pid you recorded.

## Browser management

- One browser per job. A job that leaks a browser process holds the host's
  RAM hostage; these hosts have ~450–900 MB free.
- The browser download is the classic stall: on a weak host the CDN can sit at
  0 bytes for 15 minutes. The setup script has a hard timeout — if it fails,
  pick another host rather than waiting.
- `probe.tools.chromium` or `google-chrome` present → skip the download and
  point Playwright at it with `executablePath`.
- `--with-deps` pulls system packages through `apt-get`; it needs root and it
  is slow. Install the browser build without it first, and only add `--deps`
  when the run fails on a missing shared library.

## When not to use this recipe

- The suite is a handful of assertions that finish in seconds — a local
  browser is faster than an upload.
- The test needs the user's own browser session, cookies or extensions.
- The test needs a local service (a dev server on the user's machine) — start
  that service behind a tunnel first, then run the suite remotely.
