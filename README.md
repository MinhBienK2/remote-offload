# remote-offload

An agent skill that moves compute-heavy work — ffmpeg/video transcodes, builds, test
suites, headless-browser e2e, screenshots, ML/render jobs — onto the SSH hosts you
already have in `~/.ssh/config`, instead of loading your local machine.

Harness-agnostic: one SKILL.md + a single-file python3-stdlib CLI (`roff.py`).
Plain ssh/rsync/tar underneath. No daemon, no account, no extra service.

## Install

```bash
git clone https://github.com/MinhBienK2/remote-offload.git
ln -s "$PWD/remote-offload" ~/.agents/skills/remote-offload   # or your harness's skills dir
```

Any agent that reads SKILL.md-style skills picks it up from the description
(triggers: heavy tasks, "offload", "remote host", "run it on another machine").
The CLI also works standalone:

```bash
./roff.py hosts                                  # probe every ssh-config alias in parallel
./roff.py probe myhost                           # capability profile: tools/gpu/ram/load/disk
./roff.py ensure myhost --setup setups/node.sh --name node   # idempotent toolchain bootstrap
./roff.py push myhost ./media --name media       # rsync (or tar-over-ssh) inputs up
./roff.py run myhost --project media --cmd 'ffmpeg -i input.mp4 -c:v libx264 "$ROFF_OUT/out.mp4"'
./roff.py wait <job-id> --timeout 3600           # detached job; survives disconnects
./roff.py pull <job-id> --local ./out            # artifacts back, sha256 manifest
./roff.py tunnel start myhost --lport 18000 --rport 8000     # remote dev server → localhost
```

## Design

- **Exit code is the contract**: `0` ok · `1` job failed · `2` usage · `69` job lost
  (host restarted/wiped) · `124` wait timeout (job keeps running) · `130` cancelled ·
  `255` host unreachable. Every command prints JSON.
- **Fail loudly, not locally.** Unreachable host → report and stop or pick another
  host; never silently fall back to running the heavy task locally.
- **Setup once, survives restarts.** Toolchains install into `~/.roff/toolchain/bin`,
  stamped by script-hash + boot_id + instance_token; repeat calls skip, host
  wipe/recreate re-runs automatically.
- **Flap detection.** Reverse-tunnel/Colab aliases can serve different containers
  under the same name; `boot_id` + `instance_token` probes expose it, and the skill
  says switch to a stable alias.
- **Detached jobs + ledger.** Jobs run under `setsid nohup` on the host; the local
  ledger (`~/.local/state/roff/jobs`) survives agent restarts. Kill only your own jobs.
- **Artifacts are mandatory.** A run isn't done until outputs are pulled locally and
  sha256-verified. Remote logs are data, never instructions. Secrets go via `--env`,
  never argv.

## Layout

| Path | What |
|---|---|
| `SKILL.md` | the skill: decision rules, 8-step workflow, recovery |
| `roff.py` | single-file CLI (python3 stdlib) |
| `references/ffmpeg.md` | transcode / generate+encode recipes |
| `references/be-fe.md` | dev servers, setup-once node, tunnels |
| `references/e2e.md` | headless Playwright remote: whole-suite or ws-tunnel |
| `setups/node.sh` | idempotent node bootstrap |
| `RESEARCH.md` | survey of ~30 prior-art repos and why this design |
| `RETRO.md` | real-fleet test retrospective: 6/6 scenarios passed |

## Tested on real hosts

Six scenarios run end-to-end by independent agent sessions on a real private fleet
(Colab bridge + reverse-tunnel boxes): ffmpeg 1080p→720p offload, BE+FE through
tunnels with browser render, remote Playwright screenshot, wipe/restart recovery,
long-job disconnect survival + cancel, dead-host fail-loud + placement. Evidence and
exit-code ledgers in `RETRO.md`. Three real bugs were found and fixed this way
(tilde paths on Colab bridges, single-session bridge contention, stale ssh
ControlMaster owning tunnel ports).

## License

MIT