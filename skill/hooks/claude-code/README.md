# Claude Code hook — `PreToolUse` on `Bash`

The hook stops heavy commands before they start on the user's machine and
points the agent at `roff do`. It is what makes the skill fire in projects and
harnesses the agent does not remember.

## Install

```bash
python3 <skill-dir>/hooks/claude-code/install.py            # install
python3 <skill-dir>/hooks/claude-code/install.py --dry-run  # show what it would do
python3 <skill-dir>/hooks/claude-code/install.py --uninstall
```

`install.py` merges into `~/.claude/settings.json` (keeps a `.bak`), never
overwrites other hooks, and is idempotent. The entry it writes is equivalent
to:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          { "type": "command", "command": "python3 <skill-dir>/hooks/claude-code/pretooluse.py" }
        ]
      }
    ]
  }
}
```

### Nó cài luôn lệnh `roff`

Cùng lúc đó, `install.py` đặt một script `roff` vào `~/.local/bin` (đổi được
bằng `--bin-dir`, tắt được bằng `--no-bin`). Không có lệnh này, agent đọc
SKILL.md rồi chạy `roff do …` sẽ nhận *command not found* — và phản ứng tự
nhiên của một agent là chạy lệnh đó ở máy local, tức đúng cái điều skill sinh
ra để tránh.

Script là wrapper (không phải symlink: `__file__` của script chạy qua symlink là
đường dẫn symlink, không phải thư mục thật của skill):

```sh
#!/bin/sh
exec /usr/bin/python3 "<skill-dir>/roff.py" "$@"
```

`install.py` in ra `on_path: true|false`; nếu `false`, thêm thư mục đó vào
`PATH`.

## How it answers — and why

**Choice: exit code 2 + the reason on stderr.** This is the stable contract
across Claude Code versions: exit 2 blocks the tool call and the agent reads
stderr as the reason it was blocked.

| Situation | Exit | stdout | stderr |
|---|---|---|---|
| not a Bash tool / malformed input | 0 | — | — |
| light command | 0 | — | — |
| interactive command (server, dev server) | 0 | — | a hint (`roff do` + `roff tunnel`) |
| heavy command | **2** | — | the reason + `roff do --cmd '…'` |

The alternative, newer, form is a JSON object on stdout with
`hookSpecificOutput.permissionDecision = "deny"`. The hook supports it behind
an opt-in, because it is not available in every version:

```bash
ROFF_HOOK_OUTPUT=json python3 <skill-dir>/hooks/claude-code/pretooluse.py
```

```json
{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
 "permissionDecisionReason": "…"}}
```

With that mode the hook exits 0 and the decision travels in the JSON.

## The hook never rewrites the command

A rewrite changes what the command means — wrong flags, lost environment,
silently different scope. The hook only says "run this over there"; the agent
decides how. The blocked command is echoed verbatim in the message so the
agent can pass it straight to `roff do --cmd`.

## What counts as heavy

| Group | Commands |
|---|---|
| media | `ffmpeg` (with an output), `ffprobe` on a file ≥ 50 MB, `blender`, `magick`/`convert` on images, `sox` |
| build | `cargo build/test`, `go build/test`, `mvn`, `gradle`, `make` (but not `make -n`), `cmake --build`, `npm/pnpm/yarn/bun run build`, `npm test`, `npm ci`, `npm install`, `tsc` (whole project), `next build`, `vite build`, `webpack`, `pip install` (requirements file or many packages), `docker build`, `dotnet build/publish/restore/msbuild`, `swift build`, `bazel build`, `ninja`, `meson compile`, `sbt compile/package/assembly`, `mix compile`, `deno compile`, `python -m build` |
| test | `pytest`, `jest`, `vitest`, `playwright test`, `cypress run`, `tox`, `nox`, `rspec`, `python -m unittest`, `dotnet test`, `swift test`, `bazel test`, `meson test`, `sbt test`, `mix test`, `deno test`, `deno task test`, `phpunit`, `php artisan test` |
| ml | `python train*.py`, `accelerate launch`, `torchrun`, `python -m torch.distributed.run` |

Flags that only print information or run a dry pass never count: `--help`,
`-h`, `--version`, `-v`, `-V`, `--dry-run`, `-n`, `--just-print`, `--recon`.
A test run limited to **one file or one filter** is not heavy either
(`pytest tests/test_a.py`, `phpunit --filter FooTest`, `sbt 'testOnly …'`).

Dev servers are only **suggested**, never blocked: `npm run dev`, `vite`,
`dotnet watch`, `dotnet run`, `mix phx.server`, `php artisan serve`,
`swift run`, `bazel run`, `sbt` (the REPL).

Wrappers are stripped before matching, so `npx vitest run`, `pnpm exec vitest
run`, `yarn dlx playwright test`, `uv run pytest`, `poetry run pytest`,
`bunx …` and `python -m …` all match the command they wrap.

A command made of several parts (`cd x && npm test`) is judged by its heaviest
part. The split respects quotes, so `bash -lc 'cd x && npm test'` stays one
part — and a shell wrapper is judged by what is **inside** it:

| Command | Verdict |
|---|---|
| `bash -lc "npm test"` | blocked |
| `sh -c "pytest"` | blocked |
| `bash -lc "cd x && npm test"` | blocked |
| `bash -lc "ls -la"` | allowed |

## Other agent runtimes

The hook reads the same payload shape Codex CLI uses
(`tool_name` + `tool_input.command`), and Codex's `PreToolUse` hook contract
is the same JSON / exit-2 one. The tool name is matched loosely — `Bash`,
`shell`, `local_shell`, `exec_command` are all treated as a shell — and a
command sent as an argv array is joined before classification.

Add a runtime's own tool name with `ROFF_HOOK_TOOLS=name1,name2`.

Codex ignores `permissionDecision: "allow"` and `"ask"`, so keep the default
(exit 2 + stderr) there rather than the JSON mode.

## What is not blocked

- **Reading**: `ls`, `cat`, `grep`, `rg`, `git status/log/diff`, `find`, and the
  rest of the read-only set.
- **One specific target**: `pytest tests/test_a.py::test_b`, `vitest run
  src/a.test.ts`, `jest src/a.test.ts`, `rspec spec/models/user_spec.rb`,
  `tsc src/a.ts`, `make -n build`. These are scoped runs, not suite runs.
- **Interactive commands**: `npm run dev`, `npm start`, `vite`, `next dev`,
  `python -m http.server`, `jupyter`. These need a live process — offloading
  them without a tunnel breaks them. The hook suggests `roff do` +
  `roff tunnel` instead of blocking.

## Escape hatch

`ROFF_LOCAL=1` anywhere in the command lets it through:

```bash
ROFF_LOCAL=1 npm test
```

`ROFF_HOOK_OFF=1` turns the hook off **globally** for the session it is set in —
every command passes, silently. Use it when you want to decide yourself, or to
measure what the skill does *without* the hook:

```bash
ROFF_HOOK_OFF=1 claude      # hook im lặng cả phiên
```

## Configuration

`~/.config/roff/config.ini` (optional — without it the defaults apply):

```ini
[heavy]
add = gradle          # block these too
add = mytool --all
allow = npm test      # never block these
allow = make -n
```

`allow` wins over `add` wins over the defaults. A broken config never blocks
anything: the hook falls back to the built-in list.

Point the hook at another config with `ROFF_CONFIG=/path/to/config.ini`.

## Design notes

- The hook imports **no** `roffkit` code — it runs before the package needs to
  exist, and it must not fail because of an unrelated import.
- Standard library only, Python 3.10+.
- It never writes to stdout except in the opt-in JSON mode, and it never
  rewrites the agent's command.
- The heavy list is a **starting point**, not a policy: the machine's own
  `~/.config/roff/config.ini` is the place to tune it.
