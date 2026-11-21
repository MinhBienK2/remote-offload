# ffmpeg / media offload

Probe first: `roff probe <host>` — `tools.ffmpeg` must be present (Colab/muse images usually ship it). If missing, write an idempotent setup (static ffmpeg build into `~/.roff/toolchain/bin`) and `roff ensure`.

## Transcode a local file

```bash
roff push <host> ./media --name media            # uploads inputs (excludes nothing media-specific)
roff run <host> --project media --cmd '
  ffmpeg -hide_banner -loglevel error -i input.mp4 \
    -c:v libx264 -preset medium -crf 23 -c:a aac \
    "$ROFF_OUT/output.mp4"
  ffprobe -v error -show_entries format=duration,size -of json "$ROFF_OUT/output.mp4" > "$ROFF_OUT/probe.json"
'
roff wait <id> --timeout 3600                    # long renders: big timeout, 124 = still running
roff pull <id> --local ./out                     # sha256 manifest; verify before declaring done
```

## Generate + encode (no local input)

```bash
roff run <host> --cmd '
  ffmpeg -hide_banner -loglevel error -f lavfi -i testsrc2=duration=120:size=1920x1080:rate=30 \
    -c:v libx264 -preset fast "$ROFF_OUT/demo.mp4"
'
```

## Rules

- Always `-hide_banner -loglevel error` (or `warning`) — ffmpeg's default banner floods the job log.
- Write every output under `"$ROFF_OUT"`, then `roff pull`. Never leave results only on the host.
- GPU encoding (`h264_nvenc`) only when `probe.gpu` is non-null AND `ffmpeg -encoders | grep nvenc` succeeds on that host; otherwise CPU libx264.
- Many files: one job looping over inputs beats one job per file (ssh round-trips dominate).
- Host restarted mid-render → `wait` returns 69: re-probe, re-push inputs, resubmit. Partial outputs on the host are gone.