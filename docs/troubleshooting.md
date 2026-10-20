# Troubleshooting

This guide treats Blender MCP Local as a client-independent stdio server. Where a check depends on your MCP client, use that client's own logs and MCP status UI; the bridge does not know how a particular client stores its configuration.

## Quick checks

Run these before changing the scene or reinstalling anything:

```bash
blender-mcp-local --version
```

Then launch the bridge manually in a terminal:

```bash
/absolute/path/to/project/.venv/bin/blender-mcp-local
```

A normal stdio server waits silently for JSON-RPC input. No banner is expected. Close stdin with `Ctrl+D` on macOS/Linux, or `Ctrl+Z` followed by Enter on Windows. If this command fails, the problem is between the client and the Python installation, not in Blender.

For a protocol-independent check of the package:

```bash
/absolute/path/to/project/.venv/bin/python -c "import blender_mcp_local; print(blender_mcp_local.__version__)"
```

## The client does not list the server or its tools

1. Confirm that the client is configured for a **local stdio** server, not an HTTP/SSE server.
2. Confirm that `command` points to the actual executable, not to a directory, a `.py` file, or a shell command that activates a virtual environment.
3. Use an absolute path. GUI clients, IDEs, and background agents may not inherit your shell `PATH`.
4. Check the client's MCP status/log output. A server that exits during startup usually leaves the reason there.
5. Restart the client or use its explicit MCP reconnect action. Many clients read MCP configuration only at startup.
6. If the client supports project-level configuration, confirm that the project is trusted. Pi, for example, does not load `.pi/mcp.json` until project trust is granted.
7. Look for duplicate server names, disabled entries, or an older entry that overrides the new one.

The [client setup guide](clients.md) lists typical locations and formats, but the client's own documentation is authoritative.

## The server exits immediately

A manual run that prints a traceback identifies the cause directly. Common cases:

- **Wrong Python environment:** the MCP SDK or Pydantic is missing. Install the package into the same environment as the command (`pip install -e .`).
- **Python too old:** the project requires Python 3.10 or newer.
- **Broken package installation:** recreate the virtual environment and reinstall.
- **The client is closing stdin:** some clients probe a server and close it if the handshake is slow. Check the client log and increase its MCP startup timeout if it exposes one.
- **The client does not answer MCP pings:** the bridge exits after two unanswered liveness probes. Update the client, or start the bridge with `--session-timeout 0` to disable probing. A client that keeps a dead session's pipes open is exactly what the probe is designed to clean up.

If the client shows a connection reset but the manual command works, the difference is usually the client's launch directory, environment, or command parsing.

## `Connection refused`, timeouts, or “Blender not connected”

These errors mean that the bridge started, but its TCP connection to Blender failed or timed out.

- Start the Blender-side server. With the bundled addon: `N → MCP → Start MCP Server`. With Blender Lab, start its MCP endpoint.
- Confirm that the bridge and Blender agree on host and port. The default is `127.0.0.1:9876`.
- Confirm that only one program is trying to use the same endpoint in an incompatible way. A second Blender instance is not automatically reachable on the same port.
- Check that Blender is not blocked in a modal dialog, a long render, or a modal operator. The addon services requests from Blender's main-thread timer, so a blocked UI can delay both the connection and scene commands.
- Check host firewall or endpoint-security software if you intentionally use a non-loopback host.
- If `get_blender_info` fails, the Blender endpoint is not reachable. If it succeeds but scene tools fail, inspect the specific tool error and `get_error_log`.

The TCP connection is lazy: the bridge may start successfully while Blender is closed. The failure appears on the first Blender tool call.

## Protocol mismatch or “Install Blender MCP addon 1.0.0”

The native addon requires API **1**. A very old addon, or an endpoint that does not implement the expected API, cannot be used with the current bridge.

- Replace the old 0.4.0 addon with the current 1.0.x addon.
- Restart the addon's server after replacing it.
- If you use another execute-compatible endpoint, verify that it accepts the official execute request and returns the expected result.
- Restart the bridge after updating the Python package.

`--protocol legacy|length` is only the bridge-to-Blender socket framing. It is not an MCP client transport option. The default `legacy` works with both supported endpoints; change it only for a documented endpoint requirement.

## Timeouts and long operations

`--timeout` controls the bridge's Blender socket timeout. Your MCP client has a separate tool timeout. Both must be long enough for the operation.

- A long Cycles render or simulation can exceed the default 180 seconds.
- A timeout does not prove that Blender stopped. Inspect the scene before retrying.
- The bridge never automatically retries a scene mutation or render after a failed or timed-out transfer.
- Cancelling a request after it was dispatched does not guarantee that Blender's operation stops. The journal may report `in_flight` for exactly this reason.
- Do not run a diagnostic render concurrently with the geometry changes it is intended to inspect. Socket requests are serialized, but their submission order is not a verification phase.

If renders consistently exceed the client's hard limit, reduce samples or resolution, or use a client configuration that permits a longer tool timeout.

## A bridge process survives after the client closes

Version 1.0.10 and newer send a standard MCP liveness ping after 60 seconds of inactivity. A live client answers; a client that ended but still holds the stdio pipes open does not, and the bridge exits after two unanswered probes.

If an older process is already stuck, stop it once:

```bash
pkill -f blender-mcp-local
```

On Windows, use Task Manager or your client's process list to end the old bridge process. Do not kill a process that belongs to an active session.

After updating, restart the client and the bridge. If your client does not implement server-initiated MCP pings, start the bridge with `--session-timeout 0`; in that mode you are responsible for closing old processes when the client exits.

## Error journal and diagnostics

The journal is enabled by default when the bridge is launched through the CLI:

- Editable checkout: `logs/errors.jsonl`
- Installed package: `~/.local/state/blender-mcp-local/errors.jsonl`

Use:

```text
get_error_log(limit=20)
get_error_log_status()
```

The journal contains safe summaries, error classes, argument shapes, and source-frame locations. It does not contain raw arguments, custom code, stdout, or arbitrary exception text. It may contain local paths, so review a record before sharing it.

A journal entry is not proof that a failed operation was rolled back. Inspect the scene before retrying. For the full record contract, see [logs/README.md](../logs/README.md).

If the tools themselves are unavailable, read the JSONL file directly with any local editor or command-line tool that has filesystem access. Client-side file access differs; the bridge does not require a specific client integration.

## Windows-specific checks

- Use the virtual-environment executable: `.venv\Scripts\blender-mcp-local.exe`.
- Use absolute paths and escape backslashes in JSON (`C:\\path\\...`).
- Some clients require forward slashes in JSON paths; both forms are usually accepted by the client, but the file must exist.
- If PowerShell cannot find the command, activate the environment or use the full `.exe` path.
- Check that the client is not wrapping the command in `cmd /c` with an unexpected working directory.

## macOS and Linux graphical clients

Graphical clients often launch without your shell's `PATH` or virtual-environment activation. Point directly at `.venv/bin/blender-mcp-local`. Do not rely on `source .venv/bin/activate` in a client command field unless the client explicitly documents shell execution.

Some clients sandbox local file access. The bridge itself only needs to launch the Blender endpoint and write its journal; if the client blocks the executable or its working directory, check the client's sandbox/permission settings.

## Updating the bridge or addon

A typical update is:

1. Stop the old bridge session.
2. Update the Python package.
3. Restart the bridge.
4. Refresh or reconnect the MCP session.
5. Replace and restart the Blender addon only if the addon itself changed.

Updating the Python package does not automatically update an already running bridge. Restarting Blender does not automatically restart the bridge. Both sides have independent lifecycles.

If the client still shows old tool descriptions after an update, clear/reconnect the MCP session or restart the client so it reloads the schemas.

## Determining which side failed

Use this order to avoid guessing:

1. **Client configuration:** does the client show the server as connected and list its tools?
2. **Bridge startup:** does `blender-mcp-local --version` work from the same absolute path?
3. **Blender endpoint:** does `get_blender_info` work, or does it report a connection error?
4. **Specific tool:** does `get_tool_help` show the expected schema?
5. **Scene state:** does the error journal identify validation, transport, or a Blender operation failure?
6. **Timeout or cancellation:** inspect `execution_state` before retrying a mutation or render.

If the same failure is reproducible, include the bridge version, Blender version, client name/version, the exact tool call, and the sanitized journal entry in a bug report. Do not include secrets, private file contents, or raw code.
