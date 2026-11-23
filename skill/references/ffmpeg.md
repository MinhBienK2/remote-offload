# ffmpeg on the fleet

Two branches. Pick by **file size and measured duration**, not by wish:

| Source | Branch |
|---|---|
| under ~100 MB, encode under ~2 min | run it locally (`ROFF_LOCAL=1 ffmpeg …`) — offloading costs more than it saves |
| 100 MB – 1 GB | [one file, one host](#branch-1-one-file-one-host) |
| over ~1 GB and encode over ~10 min | [split → encode in parallel → concat](#branch-2-split--encode-in-parallel--concat) |

## The rule that decides everything

Bandwidth is the bottleneck, not CPU. Measured local → Muse: **0.5–1 MB/s**
(5.5 MB/s at best, unstable). A 2 GB file costs ~35 minutes to upload and ~35
minutes to download, while the encode itself may take 10.

So the budget for one file is **two crossings**: local → host, and host → local.
Anything that crosses a third time (one copy per host, one copy per shard) is
how an offload turns into a 40-minute wait.

**Put the data on the host once, then work there.** If the input is produced by
an earlier job, pass it as `--in job:<id>` — nothing crosses the wire at all.

## Branch 1 — one file, one host

```bash
roff do --cmd '
  ffmpeg -hide_banner -loglevel error -i "$ROFF_IN" \
    -c:v libx264 -preset medium -crf 23 -c:a aac \
    "$ROFF_OUT/out.mp4"
  ffprobe -v error -show_entries format=duration,size -of json "$ROFF_OUT/out.mp4" \
    > "$ROFF_OUT/probe.json"
' --in ./movie.mp4 --out ./out --need ffmpeg
```

Then:

```bash
roff wait <job-id>          # 124 means "still running", not failure
roff result <job-id>        # exit 0 + sha256 manifest of what landed in ./out
```

Done when: exit 0, `out/out.mp4` exists locally, and `probe.json` shows a
duration within one frame of the source. `ffmpeg` without `-hide_banner
-loglevel error` floods the job log — always set both.

Choose the codec from the host, not from habit: `probe.gpu` is `null` on this
fleet, so `h264_nvenc` is out — use `libx264`.

## Branch 2 — split → encode in parallel → concat

Three jobs. Each one has a single job; do not fuse them.

**Step 1 — split (one host, copy codec, no re-encode).** Splitting with
`-c copy` costs seconds even for a huge file:

```bash
A=$(roff do --cmd '
  mkdir -p "$ROFF_OUT/parts"
  ffmpeg -hide_banner -loglevel error -i "$ROFF_IN" \
    -f segment -segment_time 60 -c copy "$ROFF_OUT/parts/seg-%03d.mkv"
' --in ./movie.mp4 --out ./parts --need ffmpeg | jq -r .job)
roff wait "$A"
```

**Step 2 — encode every segment, one host each.** `--shards N` hands each host
one segment; the segment arrives as `$ROFF_IN`, its index as `$ROFF_SHARD`:

```bash
B=$(roff do --in "job:$A" --shards 4 --out ./encoded --need ffmpeg --cmd '
  ffmpeg -hide_banner -loglevel error -i "$ROFF_IN" \
    -c:v libx264 -preset medium -crf 23 -c:a aac \
    "$ROFF_OUT/part-$ROFF_SHARD.mp4"
' | jq -r .job)
roff wait "$B"
```

**Step 3 — concat (one host, no re-encode):**

```bash
C=$(roff do --in "job:$B" --out ./out --need ffmpeg --cmd '
  ls "$ROFF_IN"*/part-*.mp4 | sort > "$ROFF_OUT/list.txt"
  ffmpeg -hide_banner -loglevel error -f concat -safe 0 -i "$ROFF_OUT/list.txt" \
    -c copy "$ROFF_OUT/out.mp4"
' | jq -r .job)
roff wait "$C"
```

Done when: `out/out.mp4` plays, `ffprobe` shows the same duration as the source
(±1 frame is acceptable — segment boundaries land on keyframes), and the frame
count matches the source (±1).

Two things break this recipe, and both are silent:

- **Different encoder settings per segment** → the concat glitches. Use one
  setting string for every shard; copy it verbatim.
- **The parts were re-encoded with different CRF** → visible quality jumps at
  the seams.

Concat with `-c copy` needs the parts to share codec parameters. If a segment
was produced by a different encoder, re-encode that segment before the concat.

## Audio only

Same shape, one extra flag: `-vn` drops video, `-c:a libmp3lame -b:a 192k` for
mp3, `-c:a flac` for lossless. Batch audio is a `--each` job, not a shard job —
see `fleet.md`.

```bash
roff do --cmd '
  ffmpeg -hide_banner -loglevel error -i "$ROFF_IN" -vn \
    -c:a flac "$ROFF_OUT/out.flac"
' --in ./song.wav --out ./out --need ffmpeg
```

## 2-pass encode

Two-pass needs the **same file twice** — pass 1 writes the statistics file,
pass 2 uses it. Keep both in one job so the file never crosses the wire twice:

```bash
roff do --cmd '
  ffmpeg -hide_banner -loglevel error -i "$ROFF_IN" \
    -c:v libx264 -b:v 2000k -pass 1 -passlogfile "$ROFF_OUT/log" -f null /dev/null
  ffmpeg -hide_banner -loglevel error -i "$ROFF_IN" \
    -c:v libx264 -b:v 2000k -pass 2 -passlogfile "$ROFF_OUT/log" \
    "$ROFF_OUT/out.mp4"
' --in ./movie.mp4 --out ./out --need ffmpeg
```

Two-pass buys ~10 % over a good CRF. Use it when the target bitrate is fixed
(compatibility with an existing pipeline), not for speed.

## When not to use this recipe

- The output is needed **right now** and the encode takes under a minute —
  upload latency dominates.
- The file is already on a host: `--in job:<id>` or the project's own host
  instead of re-pushing it.
- The user wants to watch the encode progress in their own player — offload
  the render, keep the preview local.
