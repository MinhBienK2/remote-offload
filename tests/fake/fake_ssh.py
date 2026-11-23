"""Bộ giả lập ssh/rsync cho test — không cần mạng thật.

Cách dùng: đặt thư mục chứa script `ssh` và `rsync` giả vào PATH, trỏ
`ROFF_FAKE_SSH` tới file JSON mô tả hành vi mong muốn.

File mô tả (JSON):
{
  "hosts": {
    "muse-a": {
      "boot_id": "aaaa-1111",
      "instance_token": "tok-a",
      "home": "/root",
      "cores": 4,
      "mem_available_mb": 700,
      "reachable": true,
      "ssh_error": "",              # nếu có -> ssh fail với text này
      "stdout": "",                 # stdout cho mọi lệnh
      "responses": [                # khớp theo thứ tự
         {"match": "cat /proc/sys/kernel/random/boot_id", "stdout": "aaaa-1111\n"},
         {"match": "ROFF_BOOT_ID", "stdout": "ROFF_BOOT_ID=aaaa-1111\n"}
      ]
    }
  }
}

Script `ssh` giả nhận đúng kiểu argv như ssh thật:
    ssh [options...] alias command...
Nó đọc alias (token không bắt đầu bằng '-'), rồi:
 - nếu alias không có trong file -> exit 255, stderr "ssh: Could not resolve hostname <alias>"
 - nếu host.ssh_error -> exit 255, stderr host.ssh_error
 - nếu có "responses": lần lượt so khớp "match" (substring) với command,
   in stdout của response khớp đầu tiên; nếu không có match nào khớp -> in host.stdout
 - exit code trong response ("exit"), default 0
"""
import json
import os
import sys


def main() -> int:
    argv = sys.argv[1:]
    alias = None
    cmd_parts = []
    i = 0
    # Bỏ qua options (bắt đầu bằng '-') và giá trị của option có tham số.
    opts_with_value = {
        "-o", "-p", "-i", "-l", "-F", "-E", "-J", "-b", "-c", "-D", "-e", "-I",
        "-L", "-R", "-W", "-w", "-m", "-Q", "-S", "-t",
    }
    while i < len(argv):
        a = argv[i]
        if a in opts_with_value:
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        alias = a
        cmd_parts = argv[i + 1:]
        break

    if alias is None:
        sys.stderr.write("ssh: no host specified\n")
        return 255

    spec_path = os.environ.get("ROFF_FAKE_SSH")
    if not spec_path or not os.path.exists(spec_path):
        sys.stderr.write(f"ssh: Could not resolve hostname {alias}: Name or service not known\n")
        return 255

    with open(spec_path) as fh:
        spec = json.load(fh)

    hosts = spec.get("hosts", {})
    host = hosts.get(alias)
    if host is None:
        sys.stderr.write(
            f"ssh: Could not resolve hostname {alias}: Name or service not known\n")
        return 255

    if host.get("ssh_error"):
        sys.stderr.write(host["ssh_error"] + "\n")
        return 255

    command = " ".join(cmd_parts)
    # Ghi lại lệnh đã nhận (tuỳ chọn) — test CLI cần xác nhận "mkdir -p ..."
    # có thực sự được gửi tới host hay không.
    log_path = spec.get("log")
    if log_path:
        try:
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(f"{alias}\t{command}\n")
        except OSError:
            pass
    for resp in host.get("responses", []):
        if resp.get("match", "") in command:
            out = resp.get("stdout", "")
            err = resp.get("stderr", "")
            if out:
                sys.stdout.write(out)
            if err:
                sys.stderr.write(err)
            return int(resp.get("exit", 0))

    out = host.get("stdout", "")
    if out:
        sys.stdout.write(out)
    return int(host.get("exit", 0))


if __name__ == "__main__":
    sys.exit(main())
