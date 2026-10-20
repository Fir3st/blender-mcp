# Local MCP error journal

The bridge logs failures by default to `logs/errors.jsonl` in this editable
project. Installed packages use `~/.local/state/blender-mcp-local/errors.jsonl`.
Use `--error-log /absolute/path/errors.jsonl` to choose a location or
`--no-error-log` to disable it. Restart the MCP process after changing flags.

## Reading the journal

The journal is a client-independent diagnostic file. Use any of these methods:

- Ask the connected MCP client to call `get_error_log(limit=20)`.
- Inspect configuration with `get_error_log_status()`.
- If your client or local tooling has filesystem access, read the JSONL file
  directly. This does not depend on a particular MCP client.

Examples:

```text
get_error_log(limit=20)
get_error_log(limit=10, tool="render_still")
```

If you read the file from a shell, it is JSON Lines: one complete JSON object
per line. Malformed or incomplete records are reported by the MCP reader rather
than treated as a successful operation.

Past failures from before logging was enabled cannot be recovered. For
client-to-bridge connection problems, see
[Troubleshooting](../docs/troubleshooting.md).

## What is recorded

Each event includes time, ID, bridge version, tool, error class, fixed diagnostic
summary, source-frame metadata, argument types/counts, and an optional batch
index. It deliberately excludes raw argument values, Python code, stdout, local
variables, source-line excerpts, and arbitrary exception text. This protects the
journal from secret-bearing messages; unknown details are summarized rather than
dumped.

It may still include local paths from stack frames. Review a record before
sharing it.

## Retention and visibility

Files rotate at 5 MiB with two backups and are created private (0600). The reader
reports malformed/incomplete records instead of treating them as a successful
operation. Check `last_write_error` if storage is unavailable. The journal covers
failures received or executed by this bridge; client-only errors, OS crashes, or
a killed process before logging are outside its visibility.

Error journals are diagnostics, not proof that a failed scene operation was rolled
back. Inspect scene state before retrying a mutation or timed-out render.

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
