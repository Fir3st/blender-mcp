# Local bridge log and error journal

The bridge can write two separate local files. They deliberately serve different
purposes:

| File | Created | Contents |
|---|---|---|
| `bridge.log` | At startup | Operational events: startup, Blender connections, tool starts/finishes, durations, failures, abandoned sessions, shutdown |
| `errors.jsonl` | On the first recorded failure | Sanitized failure summaries, error classes, argument shapes, frames, and execution state |

`bridge.log` is not the MCP protocol stream. MCP stdout carries JSON-RPC; stderr
goes to the process that launched the bridge. `bridge.log` is a separate private
file written by the bridge itself.

## Default locations and flags

In an editable checkout:

```text
logs/bridge.log
logs/errors.jsonl
```

For an installed package:

```text
~/.local/state/blender-mcp-local/bridge.log
~/.local/state/blender-mcp-local/errors.jsonl
```

Both are enabled by default when the bridge is launched through the CLI. The
operational log is created at startup even if no tool is called. The error journal
is created only when an event is recorded, so a clean session may have no journal
file at all.

Use:

```text
--log-file /absolute/path/bridge.log
--no-log-file
--error-log /absolute/path/errors.jsonl
--no-error-log
```

Restart the MCP process after changing flags. Both files rotate at 5 MiB with two
backups and are created with private permissions (0600). A failed write does not
replace the operation's result or prevent the bridge from starting.

## Reading the operational log

`bridge.log` is plain text. Read it with any local editor or command-line tool that
has filesystem access:

```bash
tail -n 100 logs/bridge.log
```

Typical lines include:

```text
startup version="1.0.11" pid=... host="127.0.0.1" port=9876 protocol="legacy"
blender_connect host="127.0.0.1" port=9876 protocol="legacy"
tool_started tool="get_scene_info"
tool_finished tool="get_scene_info" status="ok" duration_ms=12.4
tool_finished tool="get_scene_info" status="error" error_type="RuntimeError" ...
shutdown
```

The log records tool names and outcomes, not raw tool arguments or custom Python
code. Error text is bounded and may include object names or local paths; review the
file before sharing it.

## Reading the error journal

The journal is client-independent and newer-first. Use any of these methods:

- Ask the connected MCP client to call `get_error_log(limit=20)`.
- Inspect configuration with `get_error_log_status()`.
- If your client or local tooling has filesystem access, read the JSONL file
  directly.

Examples:

```text
get_error_log(limit=20)
get_error_log(limit=10, tool="render_still")
```

The journal is JSON Lines: one complete JSON object per line. Malformed or
incomplete records are reported by the MCP reader rather than treated as a
successful operation. Past failures from before logging was enabled cannot be
recovered. For client-to-bridge connection problems, see
[Troubleshooting](../docs/troubleshooting.md).

## What the error journal records

Each event includes time, ID, bridge version, tool, error class, fixed diagnostic
summary, source-frame metadata, argument types/counts, and an optional batch
index. It deliberately excludes raw argument values, Python code, stdout, local
variables, source-line excerpts, and arbitrary exception text. This protects the
journal from secret-bearing messages; unknown details are summarized rather than
dumped.

It may still include local paths from stack frames. Review a record before
sharing it.

## Cancellation and execution state

From 1.0.5, cancellation includes `execution_state`:

- `not_started` means no Blender call had begun.
- `in_flight` means work may continue despite the cancelled client.
- `response_received` means the callback had returned.

Queued calls are blocked after client cancellation. Batch failures preserve frame
locations. Older generic records remain readable, but omitted details cannot be
recovered retroactively.

Completed error responses also use `response_received`; a rejected RNA edit is
not still running. Transport timeouts remain `in_flight` because execution may
continue. Large execute-backend mesh/batch arguments are compressed and uploaded
in 16 KiB chunks before dispatch; a failed transfer never retries the scene edit.
