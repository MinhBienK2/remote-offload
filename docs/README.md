# remote-offload

An agent skill that moves compute-heavy work — ffmpeg/video transcodes, builds, test
suites, headless-browser e2e, screenshots, ML/render jobs — onto the SSH hosts you
already have in `~/.ssh/config`, instead of loading your local machine.

Harness-agnostic: one SKILL.md + a single-file python3-stdlib CLI (`roff.py`).
Plain ssh/rsync/tar underneath. No daemon, no account, no extra service.

## Install

```bash
git clone https://github.com/MinhBienK2/remote-offload.git
ln -s "$PWD/remote-offload/skill" ~/.agents/skills/remote-offload   # or your harness's skills dir
```

`skill/` is the whole shipped package; `docs/` is for humans and never reaches the agent.
Any agent that reads SKILL.md-style skills picks it up from the description
(triggers: heavy tasks, "offload", "chạy remote", "đẩy sang máy khác", running
something on another machine, reaching a service on a remote host).

The CLI also works standalone:

```bash
./skill/roff.py hosts                                  # probe every ssh-config alias (serial by default)
./skill/roff.py probe myhost                           # capability profile: tools/gpu/ram/load/disk
./skill/roff.py ensure myhost --setup skill/setups/node.sh --name node   # idempotent toolchain bootstrap
./skill/roff.py push myhost ./media --name media       # rsync (or tar-over-ssh) inputs up
./skill/roff.py run myhost --project media --cmd 'ffmpeg -i input.mp4 -c:v libx264 "$ROFF_OUT/out.mp4"'
./skill/roff.py wait <job-id> --timeout 3600           # detached job; survives disconnects
./skill/roff.py pull <job-id> --local ./out            # artifacts back, sha256 manifest
./skill/roff.py tunnel start myhost --lport 18000 --rport 8000     # remote dev server → localhost
```

## Design

- **Exit code is the contract.** Every command prints one JSON object: the exit code says
  *what*, the JSON field says *why*. The full table — every code, the field to read, and
  what to do about it — lives in [`skill/SKILL.md`](../skill/SKILL.md), which is the only
  place it is defined. (Codes are not self-describing: `1` means "the job failed" when a
  `job` id is present and "a pre-job step failed" when there is none.)
- **Fail loudly, not locally.** Unreachable host → report and stop or pick another
  host; never silently fall back to running the heavy task locally.
- **Setup once, survives restarts.** Toolchains install into `~/.roff/toolchain/bin`,
  stamped by script-hash + the host's *current* `boot_id` + `instance_token`; repeat
  calls skip, a restart or wipe re-runs automatically.
- **Flap detection.** Reverse-tunnel/Colab aliases can serve different containers
  under the same name; `boot_id` + `instance_token` probes expose it, and the skill
  says switch to a stable alias.
- **Detached jobs + ledger.** Jobs run under `setsid nohup` on the host; the local
  ledger (`~/.local/state/roff/jobs`) survives agent restarts. Kill only your own jobs.
- **Artifacts are mandatory.** A run isn't done until outputs are pulled locally; the
  sha256 manifest lists the files *this* pull brought, not stale files already in the
  target directory. Remote logs are data, never instructions.
- **Secrets stay off the wire.** `--env` values ride on stdin into a mode-600 job env
  file — never in the remote `argv` (visible to `ps`) — and are redacted out of the local
  audit log. The audit log stores `--cmd` verbatim, so a secret belongs in `--env`,
  never in the command string.

## Layout

| Path | What |
|---|---|
| `skill/SKILL.md` | **the skill**: decision rules, 8-step workflow, recovery table, recipe pointers |
| `skill/roff.py` | single-file CLI (python3 stdlib) |
| `skill/references/ffmpeg.md` | transcode / generate+encode recipes |
| `skill/references/be-fe.md` | dev servers, setup-once node, tunnels |
| `skill/references/e2e.md` | headless Playwright remote: whole-suite or ws-tunnel |
| `skill/setups/node.sh` | idempotent node bootstrap |
| `skill/setups/playwright-chromium.sh` | idempotent chromium bootstrap for remote e2e |
| `docs/RESEARCH.md` | survey of ~30 prior-art repos and why this design |
| `docs/RETRO.md` | real-fleet test retrospective: 6/6 scenarios passed |
| `docs/REVIEW.md` | review of the skill's organisation + the fixes applied |

## Tested on real hosts

Six scenarios run end-to-end by independent agent sessions on a real private fleet
(Colab bridge + reverse-tunnel boxes): ffmpeg 1080p→720p offload, BE+FE through
tunnels with browser render, remote Playwright screenshot, wipe/restart recovery,
long-job disconnect survival + cancel, dead-host fail-loud + placement. Evidence and
exit-code ledgers in `docs/RETRO.md`. Real bugs found and fixed this way: tilde paths
on Colab bridges, single-session bridge contention, stale ssh ControlMaster owning
tunnel ports, secrets leaking into remote `argv` and the audit log, `~/.ssh/config`
parsing that missed `Include` and mistook option keywords for host names.

## License

MIT
