# Connecting an MCP client

Blender MCP Local is a standard **local stdio** MCP server. It does not require a particular client and does not read or modify client configuration files.

Each MCP client has its own file location, key names, command syntax, trust model, and reload behavior. This page therefore starts with one client-neutral server definition and only then shows how several common clients express it.

## 1. Canonical server definition

Configure these values in your MCP client:

| Setting | Value |
|---|---|
| Transport | `stdio` |
| Command | `/absolute/path/to/project/.venv/bin/blender-mcp-local` |
| Arguments | none required |
| Working directory | optional; usually leave unset when the command is absolute |
| Environment | optional; see [bridge options](usage.md#bridge-options-and-runtime) |
| Server name | any; examples use `blender-local`. Letters, digits, `_`, and `-` are the most portable |

The bridge defaults are:

- MCP client ↔ bridge: stdio.
- Bridge ↔ Blender: `127.0.0.1:9876`.
- Blender framing: `legacy`, compatible with the bundled addon and the official Blender Lab endpoint.

There is no HTTP or SSE endpoint for the MCP client. Some clients label this transport **local** rather than **stdio**; choose the option that launches a command, not a URL.

No `--protocol` argument is required. `--protocol` controls the bridge-to-Blender socket framing, not the MCP client transport. Some older examples include `--protocol legacy`; it is unnecessary because `legacy` is already the default.

Use the executable from the virtual environment, not a bare `python`, unless you have installed the package into that interpreter and configured the client to use it explicitly. GUI clients often do not inherit your shell's `PATH`, so an absolute path is the safest choice.

On Windows, the command is normally:

```text
C:\absolute\path\to\project\.venv\Scripts\blender-mcp-local.exe
```

Use double backslashes when embedding that path in JSON.

## 2. Common `mcpServers` JSON

Many clients use an `mcpServers` object:

```json
{
  "mcpServers": {
    "blender-local": {
      "command": "/absolute/path/to/project/.venv/bin/blender-mcp-local",
      "args": []
    }
  }
}
```

This shape is a common convention, **not part of the MCP specification**. The file name and location, whether comments are allowed, environment key names, and reload behavior differ between clients. Copy the canonical definition, not another client's wrapper file.

Do not pass a shell expression such as `source .venv/bin/activate && blender-mcp-local` as the command. Use the executable itself.

## 3. Client directory

The following table is a starting point, not a compatibility promise. Client configuration formats change; use the linked official documentation for the version you have installed.

| Client | Typical place to configure | Form |
|---|---|---|
| **Pi** | `~/.pi/agent/mcp.json`, or `.pi/mcp.json` in a trusted project; `pi mcp add` also works | [`mcpServers`](https://pi.dev/docs/latest/mcp) |
| **Claude Desktop** | Settings → Developer → Edit Config (`claude_desktop_config.json`) | [`mcpServers`](https://modelcontextprotocol.io/quickstart/user) |
| **Claude Code** | `claude mcp add …`, or a project `.mcp.json` | [`mcpServers`](https://docs.claude.com/en/docs/claude-code/mcp) |
| **OpenAI Codex** | `~/.codex/config.toml` | [TOML table](https://developers.openai.com/codex/mcp/) |
| **Cursor** | Global or project `.cursor/mcp.json` | [`mcpServers`](https://cursor.com/docs/mcp) |
| **VS Code** | `.vscode/mcp.json` or the user profile | [`servers`](https://code.visualstudio.com/docs/copilot/customization/mcp-servers) |
| **Windsurf** | `~/.codeium/windsurf/mcp_config.json` | [`mcpServers`](https://docs.windsurf.com/windsurf/cascade/mcp) |
| **OpenCode** | Project or global OpenCode configuration | `mcp` section; the shape differs between OpenCode versions, so follow [its MCP documentation](https://opencode.ai/docs/mcp-servers/) |
| **Another client** | Its MCP/server settings | Add a local **stdio** server using the canonical definition above |

If your client is not listed, search its documentation for “local stdio MCP server”, not for this project's name. The server side is the same.

## 4. Examples

### Pi

Pi reads user-level servers from `~/.pi/agent/mcp.json` and, after project trust is granted, project servers from `.pi/mcp.json`. The entry uses the common `mcpServers` shape:

```json
{
  "mcpServers": {
    "blender-local": {
      "command": "/absolute/path/to/project/.venv/bin/blender-mcp-local",
      "args": []
    }
  }
}
```

You can also add it from a shell:

```bash
pi mcp add blender-local -- /absolute/path/to/project/.venv/bin/blender-mcp-local
```

Then check the registered server and start a Pi session:

```bash
pi mcp list
pi
```

Pi starts enabled servers when a session starts. If you edit `mcp.json` while Pi is running, restart Pi or reconnect the server according to its UI.

### Claude Desktop, Claude Code, Cursor, and Windsurf

These clients use the `mcpServers` form shown above, but the file location and reload procedure differ. Claude Code and some other clients also provide an `mcp add` command; use the command syntax from their documentation rather than copying a command from another client.

For Claude Desktop, merge the `blender-local` entry into the existing `mcpServers` object instead of replacing the whole file. Keep unrelated servers intact.

### OpenAI Codex

In `~/.codex/config.toml`:

```toml
[mcp_servers.blender-local]
command = "/absolute/path/to/project/.venv/bin/blender-mcp-local"
args = []
```

Codex configuration is TOML, not JSON. Use forward slashes or escaped backslashes in the path.

### VS Code

VS Code uses a top-level `servers` object and an explicit stdio type:

```json
{
  "servers": {
    "blender-local": {
      "type": "stdio",
      "command": "/absolute/path/to/project/.venv/bin/blender-mcp-local",
      "args": []
    }
  }
}
```

Some VS Code versions support `inputs` and variable prompts; those are client features, not requirements of this bridge.

### OpenCode

OpenCode does not use an `mcpServers` file. Its MCP configuration lives under an `mcp` object, and the exact shape has changed between versions. Do not copy the Pi/Claude JSON wrapper into OpenCode; use the current [OpenCode MCP documentation](https://opencode.ai/docs/mcp-servers/) and translate only the canonical command, arguments, environment, and working directory.

## 5. Paths, arguments, and environment

### Use absolute paths

GUI clients, IDEs, and background agents may launch from a different working directory and with a minimal environment. Use an absolute executable path and an absolute error-log path if you override logging.

On macOS and Linux, find the installed executable with:

```bash
command -v blender-mcp-local
```

If that prints nothing, activate the virtual environment or use its full path explicitly.

### Arguments are optional

The bridge needs no arguments for the default local endpoint. Add only what you need:

```json
{
  "command": "/absolute/path/to/project/.venv/bin/blender-mcp-local",
  "args": ["--port", "9877", "--session-timeout", "60"]
}
```

The complete list is in [Bridge options and runtime](usage.md#bridge-options-and-runtime).

Environment variables are an alternative to arguments:

```json
{
  "command": "/absolute/path/to/project/.venv/bin/blender-mcp-local",
  "args": [],
  "env": {
    "BLENDER_PORT": "9877"
  }
}
```

Key names differ by client: some use `env`, others use `environment`. Do not assume that an `env` block from one client is valid in another.

### Command versus shell string

If your client has separate `command` and `args` fields, keep the executable and its flags separate:

```json
{
  "command": "/absolute/path/to/project/.venv/bin/blender-mcp-local",
  "args": ["--port", "9877"]
}
```

If your client accepts a single command array, put the executable first:

```json
{
  "command": ["/absolute/path/to/project/.venv/bin/blender-mcp-local", "--port", "9877"]
}
```

Do not put shell operators, `source`, or `&&` into a command field unless the client explicitly documents that it runs a shell. This bridge is a long-lived stdio process; a wrapper shell can obscure its lifecycle.

## 6. Reload and verify

After saving the configuration:

1. Restart the client, or use its MCP reconnect/reload action.
2. Confirm that `blender-local` (or your chosen name) is connected.
3. Call `get_blender_info`. This checks the bridge and reports the active Blender transport.
4. Call `get_scene_info` to confirm that Blender is reachable.
5. If Blender is not running yet, `search_tools` and `get_tool_help` still work; the scene tools will report a connection error.

A client may cache the tool list for the lifetime of a session. If you update the bridge, restart the bridge process and refresh the MCP connection so the client loads the new schemas and instructions.

## 7. Common mistakes

- **Copying a whole config file from another client.** File locations and top-level keys differ. Merge the server entry instead.
- **Using a relative command.** The client may launch from a different directory. Use an absolute path.
- **Assuming the virtual environment is active.** GUI clients usually do not run your shell profile. Point directly at `.venv/bin/blender-mcp-local`.
- **Putting `--protocol` in the MCP client because a Blender addon example used it.** It is a bridge-to-Blender setting, not an MCP transport setting; the default works for both supported endpoints.
- **Treating `args` as mandatory.** An empty list is fine; omitting it may also be fine depending on the client.
- **Using comments in strict JSON.** Some clients accept JSONC, others reject comments. Match your client's format.
- **Forgetting project trust.** Pi and some other clients do not load project-level MCP configuration until the project is trusted.
- **Confusing the client's process log with the bridge error journal.** MCP stdout is protocol, stderr goes to the launching client, and the sanitized journal is a separate failure-only file. A clean run may have no journal file.
- **Expecting a config edit to affect a running session.** Most clients read server configuration at startup or reconnect.

If the server does not appear at all, see [Troubleshooting](troubleshooting.md#the-client-does-not-list-the-server-or-its-tools).
