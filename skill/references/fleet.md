# Using the whole fleet

Two ways to spread work, and they are not interchangeable:

| Goal | Option |
|---|---|
| many independent items (a folder of videos, a matrix of cases) | `--each '<glob>'` |
| one big item (a 2-hour render) | `--shards N` — split into N parts (see `ffmpeg.md`) |

## `--each` — one job per item, spread over the fleet

```bash
roff do --each './vids/*.mp4' --out ./out --need ffmpeg --cmd '
  ffmpeg -hide_banner -loglevel error -i "$ROFF_ITEM" \
    -c:v libx264 -preset medium -crf 23 -c:a aac \
    "$ROFF_OUT/$(basename "$ROFF_ITEM" .mp4).mp4"
'
```

`roff do` creates **one job per item** and spreads them over the fleet: item 1 goes to
the first host, item 2 to the second, and so on. Each job gets its own working
directory (`…/projects/<project>/part-N`) so two items never touch the same files.

When there are **fewer usable hosts than items**, the extra items pile onto the
least loaded host and wait their turn. `roff do` never dies just because the
fleet is smaller than the batch.

Inside the command:

- `$ROFF_ITEM` — the current item (full path on the host)
- `$ROFF_ITEMS` — how many items the batch holds
- `$ROFF_OUT` — where the artifacts go; the run collects them into one local
  directory with a sha256 manifest.

Name the output from `$ROFF_ITEM` so two items never collide.

`roff do` returns one JSON object with `jobs` (all the job ids) and `count`. `job` is
only the first one. Wait for each id, or for the first one and let the rest report
themselves.

**Slots.** Each host runs `slots` jobs at once (`slots = 1` by default in the
config). Four hosts = four concurrent jobs; a fifth item **waits in a FIFO
queue** — `roff do` still returns exit 0 with `state: "queued"`, and the
watcher starts it as soon as a slot frees up. `high` priority can jump the
queue. A job never runs locally as a fallback when the fleet is busy; and
"the fleet is busy" is **not** exit 10 — exit 10 means no host is usable at
all.

## The input crosses the wire once per host

The input directory is copied to a **shared store on the host** (`~/.roff/inputs/…`),
not into every job's working directory. Two items that land on the same host share
one copy.

That matters because the link to the hosts is slow (0.5–1 MB/s): pushing a 2 GB
dataset to four hosts costs four transfers, and pushing it once per *item* would
cost ten.

The shared input is **read-only**. A job that tries to modify it fails loudly
instead of corrupting a sibling's data. Copy it into `$ROFF_WORK` first if the
command has to change it.

## Bandwidth is the limit, not CPU

Measured on this fleet: local → Muse is **0.5–1 MB/s**, unstable (5.5 MB/s at
best). Host ↔ host does not work at all — the bridge times out on the SSH
banner.

So:

- **Data first, compute second.** Get the input onto a host with one push, then
  run the batch there. Uploading the same 2 GB file to four hosts costs 2 hours
  of transfer and buys nothing.
- Prefer `--in job:<id>` when a previous job produced the data — it stays on
  the host that made it and never crosses the wire.
- Expect a batch of N items to cost N × size in transfer **per host**, not per
  item. At 1 MB/s, 20 videos of 500 MB each spread over 4 hosts is still ~40
  minutes of transfer. Shrink the data (pre-encode, or work from a
  proxy/scaled copy) or accept that the fleet is not the bottleneck.
- Never use `--each` to ship a large dataset to many hosts; use `--shards` so the
  data crosses the wire once.

## Measuring whether the fleet helped

Two numbers, and they differ:

- **Compute speedup** — encode time on 1 host ÷ encode time on N hosts.
  Target: **≥ 0.6 × N** (transfer time excluded).
- **End-to-end speedup** — wall time of the whole batch on 1 host ÷ wall time
  on N hosts, transfer included. Target: **≥ 1.3× at N=2, ≥ 1.8× at N=4**.

The end-to-end number is the honest one, and on this fleet it is much smaller
than the compute number because the transfer dominates.

Measure both:

```bash
# baseline: one host
roff do --host <host> --each './vids/*.mp4' --out ./out1 --cmd '…'   # time it
# fleet
roff do --each './vids/*.mp4' --out ./out2 --cmd '…'                  # time it
```

If the end-to-end gain is under the target, the batch is transfer-bound: keep
the data on the host, shrink it, or run the batch on one host.

## Choosing the fleet size

- 4 hosts, `slots = 1` each → 4 concurrent jobs. Raising `slots` on a host
  with ~450–900 MB free RAM buys OOM kills, not speed.
- Hosts are not idle: they run the user's other services. `roff hosts` shows
  the profile (load, free RAM, disk); the scheduler prefers the roomiest one
  and explains its choice in `host_reasons`.
- One host can die mid-batch. The jobs it was running are restarted on another
  host; jobs that already finished are not redone.

## When not to use the fleet

- The data is bigger than the transfer can afford — offload the compute, not
  the bytes.
- Fewer than ~6 independent items.
- The work needs state shared between items (one process, one memory) — that
  is a single job on one host.
- The user wants the result now more than they want the throughput.
