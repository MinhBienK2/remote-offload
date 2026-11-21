# Headless browser e2e + screenshots on a remote host

Commands below use `roff` = `python3 <skill-dir>/roff.py` (see SKILL.md).

Two shapes; pick by what you need:

## A. Whole suite remote (preferred for heavy suites)

Push the project, ensure node + a browser, run the suite as a job, pull reports/screenshots.

Browser bootstrap is a setup script, like node — run it with `roff ensure` **before** the job
(`--ensure` on `roff run` takes a single script, so it cannot carry both):

```bash
roff ensure <host> --setup <skill-dir>/setups/node.sh --name node
roff ensure <host> --setup <skill-dir>/setups/playwright-chromium.sh --name playwright
```

If the project pins its own playwright version, edit `playwright-chromium.sh` to install that
version — the browser build and the playwright package must agree.

`--with-deps` needs root (these hosts are root). If apt is unavailable, install chromium
via the distro package and point Playwright at it (`executablePath`). Xvfb is present on
these images but headless chromium does not need it.

```bash
roff push <host> ./app --name app
roff run <host> --project app --cmd '
  [ -d node_modules ] || npm ci --no-audit --no-fund
  npx playwright test --reporter=line
  cp -r test-results "$ROFF_OUT/" 2>/dev/null || true
'
roff wait <id> --timeout 1800
roff pull <id> --local ./e2e-results
```

Screenshots in `test-results/` come back with the pull. Exit 1 = tests failed: read logs,
pull the traces, fix, resubmit.

## B. Browser remote, driver local (quick screenshots / interactive checks)

Run a Playwright server on the host, connect over a tunnel:

```bash
roff run <host> --cmd 'setsid nohup npx -y playwright@latest launch-server --browser chromium > "$ROFF_OUT/pw.log" 2>&1 < /dev/null & sleep 2; cat "$ROFF_OUT/pw.log"'
# log prints ws://127.0.0.1:<port>/<guid>
roff tunnel start <host> --lport <L> --rport <port>
```

Local driver: `chromium.connect("ws://127.0.0.1:<L>/<guid>")`; `page.screenshot({path})`
writes the file **locally through the websocket** — no scp needed. Client and server
Playwright versions must match. Stop the tunnel and kill the server pid when done.

## Rules

- `probe.tools.chromium`/`google-chrome` present → you can skip the browser download and
  use `executablePath`; probe first, don't assume.
- Headless only. Never try to open a GUI window on the host for the user.
- Screenshots/videos/traces are artifacts: they must land locally via pull (A) or the
  websocket (B) before you report success.