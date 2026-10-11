# Blender MCP Local

A local **Model Context Protocol (MCP)** server connecting AI clients to Blender. It provides 118 dedicated tools for creating and inspecting scenes, rather than requiring generated Python code for every operation.

```text
MCP client ← stdio → Python bridge ← localhost TCP → Blender
```

No cloud accounts, external asset services, or telemetry. Supports both the bundled addon and the execute-compatible endpoint of the official Blender Lab integration.

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
- Blender: **4.2+** API target; integration-tested on **5.1.1** according to the existing project documentation.
- An MCP client supporting local stdio servers.

## Quick start

### 1. Install the bridge

From the root of the downloaded or cloned project:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
blender-mcp-local --version
```

On Windows, use `.venv\Scripts\Activate.ps1` in PowerShell instead of `source`.

### 2. Connect Blender

Choose one option:

- **Bundled addon:** install `addon.py` through `Preferences → Add-ons → Install from Disk…`, enable **Blender MCP (Local)**, and click **Start MCP Server** in the `N → MCP` panel.
- **Blender Lab integration:** follow the [official instructions](https://www.blender.org/lab/mcp-server/) and start the endpoint at `127.0.0.1:9876`.

Build the addon ZIP with `python tools/build_release.py`. The `dist/` directory is generated and is not included in the source repository.

### 3. Configure your MCP client

For clients using `mcpServers`:

```json
{
  "mcpServers": {
    "blender-local": {
      "command": "/absolute/path/to/project/.venv/bin/blender-mcp-local",
      "args": ["--protocol", "legacy"]
    }
  }
}
```

Replace the path with the actual executable location. On Windows, use `.venv\Scripts\blender-mcp-local.exe` and escape backslashes in JSON. For other clients, configure an equivalent local stdio server.

After connecting, call `get_scene_info` and `get_blender_info`. Discover tools with `search_tools` and `get_tool_help`; these also work without a running Blender instance.

## Security and privacy

**Use only with a trusted MCP client on your own computer.** The endpoint can modify scenes, access files, and execute Python in Blender. It is not a security sandbox; do not expose the TCP port to the internet or a public network. Requiring a Python fallback explanation does not provide security isolation.

Save your scene or a checkpoint before destructive operations. Cancelling an already dispatched request does not guarantee that the Blender operation stops.

The CLI writes a local error journal by default. It does not record raw argument values or custom code, but may include paths from stack frames. Review logs before sharing them. Disable logging with `--no-error-log`; see the [error journal documentation](logs/README.md).

## Documentation

- [Detailed guide, tool catalog, and examples](docs/usage.md)
- [Version history and compatibility](CHANGELOG.md)
- [Local error journal](logs/README.md)

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
docs/usage.md            Detailed user documentation
```

## License

This project is licensed under the [Apache License, Version 2.0](LICENSE).

- Use, modification, redistribution, commercial use, resale, and integration into paid products or services are permitted under the license terms.
- Redistribution requires a copy of the license, prominent notices in modified files, and preservation of applicable notices as specified by the license.
- The project attribution notice is provided in [NOTICE](NOTICE); preserve applicable notices when redistributing as required by Section 4 of the license.
- Apache 2.0 does not require prior permission for commercial use or a visible project credit in the product interface.

The full license text governs. Dependencies and any separately licensed material are subject to their own terms.
