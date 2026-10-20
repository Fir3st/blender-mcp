# Local MCP error journal

The bridge logs failures by default to `logs/errors.jsonl` in this editable
project. Installed packages use `~/.local/state/blender-mcp-local/errors.jsonl`.
Use `--error-log /absolute/path/errors.jsonl` to choose a location or
`--no-error-log` to disable it. Restart the MCP process after changing flags.

Read recent errors with `get_error_log(limit=20)` and inspect configuration with
`get_error_log_status()`. Here in OpenCode the generated JSONL can also be read
directly. Ask to investigate “the latest error in the MCP log”; no client copying
is needed. Past failures before this feature was running cannot be recovered.

Each event includes time, ID, bridge version, tool, error class, fixed diagnostic
summary, source-frame metadata, argument types/counts and optional batch index.
It deliberately excludes raw argument values, Python code, stdout, local variables,
source-line excerpts and arbitrary exception text. This protects the journal from
secret-bearing messages; unknown details are summarized rather than dumped.

Files rotate at 5 MiB with two backups and are created private (0600). The reader
reports malformed/incomplete records instead of treating them as a successful
operation. Check `last_write_error` if storage is unavailable. The journal covers
failures received/executed by this bridge; client-only errors, OS crashes or a
killed process before logging are outside its visibility.

Error journals are diagnostics, not proof that a failed scene operation was rolled
back. Inspect scene state before retrying a mutation or timed-out render.

From 1.0.5, cancellation includes `execution_state`: `not_started` means no Blender
call had begun; `in_flight` means work may continue despite the cancelled client;
`response_received` means the callback had returned. Queued calls are blocked
after client cancellation. Batch failures preserve frame locations. Older generic
records remain readable, but omitted details cannot be recovered retroactively.

Completed error responses also use `response_received`; a rejected RNA edit is
not still running. Transport timeouts remain `in_flight` because execution may
continue. Large execute-backend mesh/batch arguments are compressed and uploaded
in 16 KiB chunks before dispatch; a failed transfer never retries the scene edit.
