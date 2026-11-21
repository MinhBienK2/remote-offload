# remote-offload — skill build + real-host test retrospective

Date: 2026-11-21 (local +0700). Helper: `roff.py` (python3 stdlib, plain ssh/rsync/tar, no daemon).
Skill installed at `~/.agents/skills/remote-offload/` (symlink to this dir). Host aliases below are anonymized labels of a real private fleet (Colab bridge + reverse-tunnel boxes).

## What was built

A standalone, harness-agnostic skill that teaches an agent to offload compute-heavy work
(ffmpeg, builds, test suites, headless browser e2e, screenshots) onto the user's existing
`~/.ssh/config` hosts instead of loading the local machine.

- `roff.py` — single-file CLI: `hosts/probe/push/pull/run/wait/status/logs/cancel/jobs/ensure/tunnel`.
  Every command prints JSON; exit code is the contract:
  `0` ok · `1` job failed · `2` usage · `69` lost (host restarted/wiped) · `124` wait timeout · `130` cancelled · `255` unreachable.
- `SKILL.md` — decision rules + 8-step workflow + recovery + recipe pointers.
- `references/{ffmpeg,be-fe,e2e}.md` — task recipes.
- `setups/node.sh` — idempotent node bootstrap into `~/.roff/toolchain/bin`.

Design borrowed from the evaluated Kanevry skill (fail-loud-not-local, setup-once persistence,
exit-code verdicts, artifact-fetch-before-done, secrets-out-of-argv) but decoupled from any
orchestrator: no wave roles, no external `offload` binary, no plugin frontmatter.

## Bugs found by real-host testing and fixed mid-run

1. **Tilde workdir not expanded on colab bridges.** `roff run --project X` built `~/…` workdir;
   the colab ssh bridge does not shell-expand `~`, and rsync treats `~` literally → jobs died
   exit 97 "workdir does not exist", pushes created literal `~` dirs.
   Fix: probe now captures remote `home`; `remote_home()`/`abs_remote()` resolve every push/pull/run
   path to absolute `$HOME`. Verified: `--project proj-check` push→run→pull round-trips on muse.
2. **Single-session bridge contention.** colab `ProxyCommand` allows ONE active ssh session;
   concurrent workers got HTTP 429 / kex closed. Documented in SKILL.md placement step; workers
   redirected to stable `muse`.
3. **Tunnel port held by a stale ControlMaster.** A dead shared master kept the local forward
   port bound with no live connection → curl reset. roff tunnel now detects the owning master
   (`ss -ltnp`), records `master_pid`, and `tunnel stop` cancels the forward via `-O cancel`
   instead of killing the wrong pid.

## Test matrix — six Orca worker sessions on real SSH hosts

Fleet: 11 `~/.ssh/config` aliases → 6 reachable pointing at 3 physical boxes
(`comfy`/`colab-comfy` = 1 Colab box; `muse` + 2 aliases = 1 box; `muse-b` = 1 box), 5 dead.
`gpu=null` on every host → CPU libx264 only, no CUDA claims. `durable_mount=null` everywhere.

| Case | Scenario | Host (final) | Verdict | Key evidence |
|---|---|---|---|---|
| 1 | ffmpeg 1080p→720p transcode, no local encode | muse-b | PASS | job 261121-010541-2632 exit 0, 48.8s encode; output.mp4 11,316,669 B sha256-verified; local ffprobe h264 1280x720 + aac, 60.000000s |
| 2 | BE+FE setup-once + tunnels, browser render | muse | PASS | ensure node skipped=false→true; jobs 2af8/2fd5; curl /health + /api/time + FE HTML via 18000/13000; headless render of remote server time; tunnels+servers cleaned |
| 3 | headless playwright screenshot | comfy | PASS | job 261121-010719-e6fb exit 0; screenshot.png 87,389 B sha256-verified, PNG 1280x800, example.com full-page viewed; ensure playwright skipped=false→true; browser 100% remote headless. Muse confirmatory re-run dropped (CDN chromium download stalled on low-RAM host); worker looped on it ignoring stop orders → fenced via worker-stop, orphaned muse PIDs + /tmp/pwzip scratch cleaned. Comfy acceptance sufficient. |
| 4 | restart/wipe recovery + ensure re-bootstrap | comfy | PASS | ensure idempotent; wipe→instance_token changed→wiped flag→re-ensure skipped=false; restart→wait/status exit 69; unreachable exit 255 |
| 5 | long-job detach + waiter-kill + cancel | muse | PASS | wait --timeout 5 →124; SIGKILL waiter (137) job survives, progress 6→10 lines; natural exit 0, 18 contiguous lines Δ5s; cancel sleep600 →130, pgroup dead |
| 6 | host-down fail-loud + placement + flap | muse-b | PASS | dead host run →255 no local fallback; placement comfy→stable alias on flap; ffmpeg testsrc2 10s 720p job 8484 exit 0, 300 frames sha256-verified |

Reports: `/tmp/roff-case{1,2,3,4,5,6}-report.md`. Raw evidence under `/tmp/roff-case*/`.

## Cross-cutting observations

- **Flap is real and detectable.** comfy/colab-comfy served two different `instance_token`s under
  the same kernel `boot_id` within ~15s (container recreation behind a reverse tunnel). Three
  workers independently hit job-lost (69) and recovered by switching to a stable alias — exactly
  the skill's recovery path. The `boot_id`+`instance_token` pair is the right stability signal.
- **Fail-loud holds.** No worker silently fell back to local heavy work; dead hosts returned 255
  with the explicit message and nothing ran locally.
- **Setup-once + wipe recovery holds.** Stamps (script-hash:boot_id:instance_token) skip on repeat
  and auto-re-run after wipe; toolchain lands on PATH for every job.
- **Detached jobs survive agent/connection loss.** Killing the local `roff wait` process never
  touched the remote job; the ledger (`~/.local/state/roff/jobs`) survives local restarts.
- **Browser downloads can stall on weak hosts.** Case 3's muse confirmatory re-run hung 15 min at
  0 bytes fetching Chromium from `playwright.azureedge.net` (muse ~513 MB free RAM). The comfy run
  had already passed, so the redundant re-run was cancelled and its orphaned ensure PIDs cleaned.
  Lesson: bound setup downloads with a timeout and don't block acceptance on a confirmatory re-run
  when the primary host already produced a verified artifact.

## Verdict on the original question

The Kanevry `remote-offload` skill is **orchestrator glue, not a standalone how-to**: it routes
wave roles (test/ui/perf) to a declared host via an external `offload` CLI owned by a private
baseline repo, with plugin-specific frontmatter. It does not cover arbitrary ffmpeg/e2e/screenshot
offload and cannot run without that infrastructure. Its *policy ideas* are sound and were reused;
this skill is the standalone, host-agnostic implementation the user actually asked for, now
validated end-to-end on their real SSH fleet.