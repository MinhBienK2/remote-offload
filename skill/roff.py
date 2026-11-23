#!/usr/bin/env python3
"""Launcher của roff — điểm vào duy nhất cho người dùng và cho watcher.

```bash
python3 <skill-dir>/roff.py do --cmd 'npm test' --in ./src --out ./out
```

Đặt thư mục chứa file này vào ``sys.path`` rồi gọi ``roffkit.cli.main()``.
Watcher chạy nền gọi lại đúng file này (``__watch <job_id>``), nên launcher
phải nằm cạnh package ``roffkit/``.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from roffkit.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
