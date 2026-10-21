# Blender MCP Local

A local, **client-independent Model Context Protocol (MCP)** server for Blender. It exposes 118 dedicated tools for creating and inspecting scenes, instead of requiring generated Python for every operation.

Any MCP client that can launch a local **stdio** server can use it. The bridge does not depend on a particular client, does not read client configuration files, and does not assume a specific config format.

```text
MCP client ← stdio → Python bridge ← localhost TCP → Blender
```

No cloud accounts, external asset services, or telemetry. Works with the bundled Blender addon and with the execute-compatible endpoint of the official Blender Lab integration.

## Features

- Objects, custom meshes, curves, materials, modifiers, and UVs.
- Shader, Geometry Nodes, and compositor graphs; RNA property inspection and editing.
- Armatures, IK, weights, shape keys, animation, and drivers.
- Collision proxies, geometry audits, and rigid-body physics.
- Cameras, lighting, rendering, screenshots, import, export, and checkpoints.
- Batched operations with references to previous results, tool discovery, and local error diagnostics.

Custom Python is available as a fallback with a required explanation of the missing capability. Batches are not transactions: completed changes remain in the scene if a later operation fails.

## Requirements

- Python **3.10+**.
- Blender: **4.2+** API target; integration-tested on **5.1.1**.
- An MCP client that supports local **stdio** servers and tools.
- Either the bundled Blender addon or the official Blender Lab MCP endpoint.

## Quick start

### 1. Install the bridge

From the root of the downloaded or cloned project:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
blender-mcp-local --version
```

On Windows, activate the environment with `.venv\Scripts\Activate.ps1` in PowerShell.

### 2. Start a Blender endpoint

Choose one option:

- **Bundled addon:** build/install `addon.py` or the addon ZIP through `Preferences → Add-ons → Install from Disk…`, enable **Blender MCP (Local)**, and click **Start MCP Server** in the `N → MCP` panel.
- **Blender Lab integration:** follow the [official instructions](https://www.blender.org/lab/mcp-server/) and start the endpoint at `127.0.0.1:9876`.

The bridge recognizes the official execute-only endpoint automatically. Its default settings are shown in the next step.

### 3. Register the bridge with your MCP client

Use this canonical definition and translate it into your client's own configuration format:

| Setting | Value |
|---|---|
| Transport | `stdio` |
| Command | `/absolute/path/to/project/.venv/bin/blender-mcp-local` |
| Arguments | none required |
| Working directory | optional; not needed when the command is absolute |
| Server name | any; examples use `blender-local` |

The defaults connect to `127.0.0.1:9876` and use `legacy` bridge-to-Blender framing, which is compatible with both supported endpoints. Add flags only when overriding the host, port, timeout, logging, or framing.

Client configuration syntax is owned by the client. Some use an `mcpServers` JSON object; others use a command array, TOML, or a different key layout. See [Connecting an MCP client](docs/clients.md) for the client directory, copyable examples, Windows path rules, reload behavior, and the common mistakes to avoid.

On Windows, the executable is `.venv\Scripts\blender-mcp-local.exe`; escape backslashes when writing JSON.

### 4. Verify the connection

After the client starts the server, call `get_blender_info` and `get_scene_info`. Discover tools with `search_tools` and `get_tool_help`; discovery also works without a running Blender instance.

## How it works

The Python bridge is the only process that talks to the MCP client. It owns the tool schemas, validates arguments, serializes Blender access, and translates dedicated tool calls into the addon's operation runtime. The Blender endpoint performs the actual scene work on Blender's main thread.

- `addon.py` is optional; the official Blender Lab endpoint can replace it.
- The MCP transport is always local stdio.
- The bridge-to-Blender transport is localhost TCP; `--protocol` controls that framing, not the MCP client connection.
- Scene operations run once. A failed or timed-out transfer never automatically repeats a mutation or render.

## Security and privacy

**Use only with a trusted MCP client on your own computer.** The endpoint can modify scenes, access files, and execute Python in Blender. It is not a security sandbox; do not expose the TCP port to the internet or a public network. Requiring a Python fallback explanation does not provide security isolation.

Save your scene or a checkpoint before destructive operations. Cancelling an already dispatched request does not guarantee that the Blender operation stops.

The CLI records failures in a local error journal by default. This is not a general runtime log; it is created on the first recorded failure and does not capture stdout/stderr from the client process. It does not record raw argument values or custom code, but may include paths from stack frames. Review logs before sharing them. Disable logging with `--no-error-log`; see the [error journal documentation](logs/README.md).

## Documentation

- [Connecting an MCP client](docs/clients.md) — client-neutral setup, per-client entry points, reloading, and path rules
- [Detailed guide and tool catalog](docs/usage.md) — installation, Blender endpoint, runtime options, workflows, and architecture
- [Troubleshooting](docs/troubleshooting.md) — missing tools, connection failures, timeouts, stale processes, and logs
- [Local error journal](logs/README.md)
- [Version history and compatibility](CHANGELOG.md)

## Development and testing

```bash
python -m unittest discover -s tests -v
python tools/build_release.py
```

Unit tests do not require a running Blender instance. Integration tests launch a separate Blender process; see the [detailed guide](docs/usage.md#tests-and-releases).

```text
addon.py                 Optional Blender addon and local socket server
src/blender_mcp_local/   MCP bridge, tools, and command runtime
tests/                   Unit and Blender integration tests
tools/build_release.py   Addon ZIP and source archive builder
docs/                    Client setup, usage guide, troubleshooting
```

## License

This project is licensed under the [Apache License, Version 2.0](LICENSE).

- Use, modification, redistribution, commercial use, resale, and integration into paid products or services are permitted under the license terms.
- Redistribution requires a copy of the license, prominent notices in modified files, and preservation of applicable notices as specified by the license.
- The project attribution notice is provided in [NOTICE](NOTICE); preserve applicable notices when redistributing as required by Section 4 of the license.
- Apache 2.0 does not require prior permission for commercial use or a visible project credit in the product interface.

The full license text governs. Dependencies and any separately licensed material are subject to their own terms.
