# Codex CLI hook — `PreToolUse`

Same job as the Claude Code hook, for Codex CLI: block heavy commands before
they start on the user's machine and point the agent at `roff do`.

## Install

```bash
python3 <skill-dir>/hooks/codex/install.py            # install
python3 <skill-dir>/hooks/codex/install.py --dry-run  # show what it would do
python3 <skill-dir>/hooks/codex/install.py --uninstall
```

It merges into `~/.codex/hooks.json` (keeps a `.bak`), never overwrites other
hooks, and is idempotent. The entry it writes is:

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

**The same script as the Claude Code hook.** Codex CLI's hook contract is the
same: the payload on stdin carries `tool_name` and `tool_input.command`, and a
hook that exits 2 with the reason on stderr blocks the call. The tool name is
matched loosely (`Bash`, `shell`, `local_shell`, `exec_command`), and a command
sent as an argv array is joined before it is classified.

## Why not the JSON decision mode

`ROFF_HOOK_OUTPUT=json` makes the hook answer with
`hookSpecificOutput.permissionDecision`. Claude Code accepts `deny` there;
**Codex rejects `allow` and `ask`** and only honours `deny`/`block`. The
installer therefore keeps the default mode (exit 2 + stderr), which both
runtimes understand.

## Not verified end to end yet

The Codex backend on this machine currently answers `503
d1_migration_incomplete`, so the whole Codex path could not be exercised. The
protocol was read off the Codex binary (`codex` 0.142.4), and the hook is
**fail-open**: an unexpected payload shape lets the command through rather than
breaking the session.
