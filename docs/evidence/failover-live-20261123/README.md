# S20 — FAILOVER THẬT trên fleet Muse (đo tự động, 23/11/2026)

Job này **không** phải kịch bản cố ý: nó là job giữ slot của kịch bản
S41. Trong lúc chạy, máy `muse-pmb333` chết (reverse tunnel rớt) ⇒
`roff` tự phát hiện và **chuyển job sang máy khác**.

## Vì sao đây là bằng chứng cho S20

S20 (trước đây: “đo thủ công”) là *1 máy chết giữa lô ⇒ job phải được
chuyển sang máy khác*. Ở đây điều đó xảy ra **tự nhiên** và được ghi lại
đầy đủ trong ledger:

* **attempt 0** — chạy trên `muse-pmb333`
* `boot_id` / `instance_token` của máy **cũ**
* 6 sự kiện `disconnected` (tunnel rớt, `127.0.0.1:2222 Connection refused`)
* sự kiện `failover` với `reason: host_restarted` ⇒ chuyển sang `muse-bienpm333`
* **attempt 1** — chạy lại trên `muse-bienpm333` với `boot_id`/`instance_token` mới
* job **không** bị mất: `state: running`, tiến trình mới `pid: 4764`

## Dữ liệu thô

```json
[
  {
    "attempt": 0,
    "host": "muse-pmb333",
    "pid": 9881,
    "boot_id": "fe60056a-99c3-4834-867a-9ec0182029bd",
    "instance_token": "d69166cbba4406c71bff8ddd5dafe84d",
    "started": 1795422796.8905697,
    "job_dir": "/home/hatch/.roff/jobs/261123-153312-4c46",
    "workdir": "/home/hatch/.roff/projects/remote-offload",
    "failover": false
  },
  {
    "attempt": 1,
    "host": "muse-bienpm333",
    "pid": 4764,
    "boot_id": "9fd59921-72f6-41aa-8bba-271131b44aff",
    "instance_token": "5fbfada57c1b3eb6f482669c07d5cc2e",
    "started": 1795422984.1805193,
    "job_dir": "/home/hatch/.roff/jobs/261123-153312-4c46",
    "workdir": "/home/hatch/.roff/projects/remote-offload",
    "failover": true
  }
]
```

```json
[
  {
    "event": "disconnected",
    "ts": 1795422909.484457,
    "host": "muse-pmb333",
    "detail": "mux_client_request_session: read from master failed: Broken pipe\r\nssh: connect to host 127.0.0.1 port 2222: Connection refused",
    "error": "ssh_unreachable"
  },
  {
    "event": "disconnected",
    "ts": 1795422914.5187612,
    "host": "muse-pmb333",
    "detail": "ssh: connect to host 127.0.0.1 port 2222: Connection refused",
    "error": "ssh_unreachable"
  },
  {
    "event": "disconnected",
    "ts": 1795422919.5521612,
    "host": "muse-pmb333",
    "detail": "ssh: connect to host 127.0.0.1 port 2222: Connection refused",
    "error": "ssh_unreachable"
  },
  {
    "event": "disconnected",
    "ts": 1795422924.5868156,
    "host": "muse-pmb333",
    "detail": "ssh: connect to host 127.0.0.1 port 2222: Connection refused",
    "error": "ssh_unreachable"
  },
  {
    "event": "disconnected",
    "ts": 1795422929.6262689,
    "host": "muse-pmb333",
    "detail": "ssh: connect to host 127.0.0.1 port 2222: Connection refused",
    "error": "ssh_unreachable"
  },
  {
    "event": "disconnected",
    "ts": 1795422934.6613526,
    "host": "muse-pmb333",
    "detail": "ssh: connect to host 127.0.0.1 port 2222: Connection refused",
    "error": "ssh_unreachable"
  },
  {
    "event": "failover",
    "ts": 1795422984.1963165,
    "reason": "host_restarted",
    "host": "muse-bienpm333",
    "previous": "muse-pmb333",
    "reasons": [
      "project 'remote-offload' đã có sẵn trên host (+40)",
      "toolchain đã cài sẵn (bin, node) (+10)",
      "còn 3.4/4 core rảnh (×3 → +10.3)",
      "RAM khả dụng 789 MB (+7.9)",
      "layout bền vững (/home/hatch/.roff) (+15)",
      "máy restart 1 lần trong 24h (-20)"
    ]
  }
]
```
