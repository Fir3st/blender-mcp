# Blender MCP Local — detailed guide

A local, **tool-first MCP server for Blender**: 118 tools for modeling, materials, Geometry Nodes, rigging, animation, collisions, physics, lighting, rendering, file operations, and local error diagnostics. Clients primarily compose specific operations; custom Python is a justified fallback for capabilities not covered by tools.

The server is **client-independent**. It exposes standard MCP over local stdio, and any client that can launch a local stdio server can use it. Client-specific configuration lives in [Connecting an MCP client](clients.md); this guide focuses on the bridge, Blender, and the tools.

## Contents

- [How it works](#how-it-works)
- [Requirements and compatibility](#requirements-and-compatibility)
- [Install the bridge](#install-the-bridge)
- [Start the Blender endpoint](#start-the-blender-endpoint)
- [Connect an MCP client](#connect-an-mcp-client)
- [Verify the connection](#verify-the-connection)
- [Bridge options and runtime](#bridge-options-and-runtime)
- [Upgrading](#upgrading)
- [Tool catalog](#tool-catalog)
- [Workflow](#workflow)
- [Batches and actual object names](#batches-and-actual-object-names)
- [Lights, cameras, and render settings without Python](#lights-cameras-and-render-settings-without-python)
- [Modifiers: names, references, and actual completion](#modifiers-names-references-and-actual-completion)
- [Shader, Geometry Nodes, and compositor](#shader-geometry-nodes-and-compositor)
- [Modeling, UVs, and characters](#modeling-uvs-and-characters)
- [Collisions and collision proxies](#collisions-and-collision-proxies)
- [Physics and baking](#physics-and-baking)
- [Advanced rigging, weights, and drivers](#advanced-rigging-weights-and-drivers)
- [Python fallback](#python-fallback)
- [Security, limits, and cancellation](#security-limits-and-cancellation)
- [Troubleshooting](#troubleshooting)
- [Architecture](#architecture)
- [Tests and releases](#tests-and-releases)

## How it works

```text
MCP client ← stdio → Python bridge ← localhost TCP → Blender endpoint
                                              ↓
                                   main-thread command runtime
```

- The MCP client starts the bridge and communicates with it over standard input/output.
- The bridge owns the 118 tool schemas, validates arguments, serializes Blender access, and translates tool calls into the endpoint's operation runtime.
- The Blender endpoint executes scene work on Blender's main thread.
- The default endpoint is `127.0.0.1:9876`; the bridge can use the bundled addon or the official Blender Lab execute-compatible endpoint.
- No external asset service, cloud account, or telemetry is involved.

Custom Python is available through `execute_blender_code` only when dedicated tools cannot express the operation. The bridge does not generate Python for supported tool calls, and the fallback is not a substitute for the tool API.

## Requirements and compatibility

| Component | Requirement |
|---|---|
| Python | 3.10 or newer |
| MCP Python SDK | 1.27+ (`<2`) |
| Pydantic | 2.x |
| Blender | 4.2+ API target; integration-tested on 5.1.1 |
| MCP client | Any client that supports local stdio servers and tools |
| Blender endpoint | Bundled addon, or the official Blender Lab MCP endpoint |

The optional native-RPC addon requires API **1** (the 1.0.x addon line). The official Blender Lab extension can remain unchanged; the bridge detects its execute-only protocol automatically. Both transports use the same dedicated MCP tools and batch behavior.

## Install the bridge

From the project root:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
blender-mcp-local --version
```

On Windows PowerShell, activate with `.venv\Scripts\Activate.ps1`.

Alternatively, build and install a generated source package:

```bash
python tools/build_release.py
ls dist
pip install dist/blender_mcp_local-1.0.11.tar.gz   # replace with the generated version
```

`dist/` is generated locally and is not part of the source repository. Build it with `python tools/build_release.py`.

## Start the Blender endpoint

Choose one endpoint. The bridge defaults to `127.0.0.1:9876`.

### Option A: bundled addon

Build the addon archive if needed:

```bash
python tools/build_release.py
```

In Blender, open `Preferences → Add-ons → Install from Disk…` and select the generated `dist/blender-mcp-local-addon-*.zip`. Enable **Blender MCP (Local)**. In the 3D viewport, open the sidebar (`N`), select the **MCP** tab, and click **Start MCP Server**.

The addon's socket server runs inside Blender and stops when Blender exits or when you stop the server in the panel. The addon auto-detects the bridge's socket framing, so no framing option is required.

### Option B: official Blender Lab endpoint

Follow the [official Blender Lab MCP instructions](https://www.blender.org/lab/mcp-server/) and start its endpoint at `127.0.0.1:9876`. No custom addon is needed. The bridge recognizes the execute-only endpoint and loads its compressed command runtime automatically.

The client still calls dedicated tools such as `create_object` or `configure_ik`; this translation is not client-generated Python.

## Connect an MCP client

The bridge is a standard local stdio server. Use this canonical definition in your client:

| Setting | Value |
|---|---|
| Transport | `stdio` |
| Command | `/absolute/path/to/project/.venv/bin/blender-mcp-local` |
| Arguments | none required |
| Server name | any; examples use `blender-local` |

No arguments are needed for the default host, port, and framing. Some clients prefix displayed tool names with the server name; `get_tool_help` accepts the common prefixed forms. If your client has separate `command` and `args` fields, keep the executable and flags separate. If it accepts a command array, put the executable first.

Do not copy another client's wrapper file into a different client. Client configuration locations and formats differ; see [Connecting an MCP client](clients.md) for the client directory, selected examples, Windows path rules, and reload behavior.

There is no HTTP or SSE endpoint for the MCP client. The bridge-to-Blender TCP connection is internal and controlled by `--host`, `--port`, and `--protocol`.

## Verify the connection

After the client starts the server:

1. Call `get_blender_info` to inspect the bridge, the active Blender transport, and capabilities.
2. Call `get_scene_info` to confirm that Blender is reachable.
3. Use `search_tools` and `get_tool_help` to discover a tool and its exact schema.

`search_tools` and `get_tool_help` work without a running Blender instance. Scene tools report a connection error when the Blender endpoint is unavailable.

## Bridge options and runtime

The bridge is launched by the MCP client, not by a separate daemon. It has no configuration file of its own: options are passed in the client's server entry as command-line arguments or environment variables. Its command-line options control the Python process:

| Option | Default | Environment | Purpose |
|---|---:|---|---|
| `--host` | `127.0.0.1` | `BLENDER_HOST` | Blender endpoint host |
| `--port` | `9876` | `BLENDER_PORT` | Blender endpoint port |
| `--timeout` | `180` | `BLENDER_TIMEOUT` | Socket timeout in seconds |
| `--session-timeout` | `60` | `BLENDER_SESSION_TIMEOUT` | Idle seconds before an MCP liveness ping; `0` disables probing |
| `--protocol` | `legacy` | `BLENDER_PROTOCOL` | Bridge-to-Blender framing: `legacy` or `length` |
| `--log-file` | automatic | `BLENDER_LOG_FILE` | Write the operational bridge log to a specific `.log` path |
| `--no-log-file` | off | — | Disable the operational bridge log |
| `--error-log` | automatic | — | Write the sanitized JSONL error journal to a specific path |
| `--no-error-log` | off | — | Disable the persistent error journal |
| `--version` | — | — | Print the bridge version and exit |

CLI arguments take precedence over environment variables. For long renders, increase `--timeout` and the client's own tool timeout; they are separate limits.

### MCP lifecycle and stale processes

The MCP client ↔ bridge transport is stdio. Closing stdin, losing the client process, or an MCP disconnect ends the session. When stdio is idle, the bridge sends a standard MCP liveness ping after `--session-timeout` seconds:

- A live client answers the ping, so a session can remain idle indefinitely.
- A client that has ended but still holds the stdio pipes open does not answer; after two unanswered probes the bridge closes the Blender socket and exits.
- `--session-timeout 0` disables probing for clients that do not implement server-initiated pings.

This prevents an old bridge from surviving a client restart or update. If a process from a pre-1.0.10 version is already stuck, stop it once with:

```bash
pkill -f blender-mcp-local
```

Do not use that command while an active session is using the bridge.

### Logging model, bridge log, and error journal

The bridge has several distinct diagnostic channels:

- **MCP stdio is the protocol channel.** stdout carries JSON-RPC and must not be treated as a log file.
- **stderr goes to the process that launched the bridge.** If a client starts the bridge as its child, its own process log may capture stderr, startup warnings, and a crash traceback.
- **`bridge.log` is the bridge's operational log.** It is created at startup and records startup metadata, Blender connection attempts/results, tool starts and finishes with durations and status, failures, abandoned sessions, and shutdown.
- **`errors.jsonl` is the sanitized error journal.** It records failures only and is created on the first recorded failure.

Default locations:

| File | Editable checkout | Installed package |
|---|---|---|
| Operational log | `logs/bridge.log` | `~/.local/state/blender-mcp-local/bridge.log` |
| Error journal | `logs/errors.jsonl` | `~/.local/state/blender-mcp-local/errors.jsonl` |

Both files rotate at 5 MiB with two backups and are created with private permissions (0600). Logging failures do not replace the operation's result or prevent the bridge from starting.

`bridge.log` never records raw tool arguments, custom Python code, or MCP stdio payloads. It does record tool names, durations, status, error classes, bounded error text, and local connection metadata. The error journal deliberately excludes arbitrary exception text and raw values; use it for a smaller, sanitized failure record.

Use `get_error_log(limit=20)` and `get_error_log_status()` for the error journal. The operational log is a regular text file; read it with any local tool that has filesystem access. Use `--log-file /absolute/path/bridge.log` or `--no-log-file` to change the operational log, and `--error-log` or `--no-error-log` for the journal. See [logs/README.md](../logs/README.md) for the full contract.

### Why `--protocol` is not an MCP client setting

`--protocol legacy|length` selects the framing used on the local TCP socket between the bridge and Blender. It does not configure the MCP client and does not change the stdio transport. The default `legacy` works with both supported Blender endpoints. Change it only when you have a specific endpoint requirement.

## Upgrading

1. Stop the old bridge process or let its client session end.
2. Update the Python package and restart the bridge.
3. Refresh or reconnect the MCP session so the client loads the new instructions and schemas.
4. If you replaced the bundled addon, restart its server in Blender.

Existing API-1 addons remain compatible with the 1.0.x bridge. The official Blender Lab extension normally needs no update; restarting the bridge is enough to load new scene-operation code.

If you are upgrading from the old 0.4.0 addon, replace it with the current 1.0.x addon before using the current bridge. The current bridge also recognizes older execute-compatible endpoints.

Detailed per-release changes, compatibility notes, and verification counts are in [CHANGELOG.md](../CHANGELOG.md).

## Tool catalog

The server sends exact argument schemas to the client. `search_tools` finds tools by task; `get_tool_help` returns complete documentation for a selected tool. If a client displays tools with a server-name prefix, `get_tool_help` also accepts the known prefixes `blender_`, `mcp__blender__`, `mcp_blender_local_`, and `mcp__blender_local__`.

| Area | Tools |
|---|---|
| Inspection and discovery | `get_blender_info`, `get_scene_info`, `get_object_info`, `find_objects`, `get_selection`, `search_tools`, `get_tool_help` |
| Local error journal | `get_error_log`, `get_error_log_status` |
| Objects and geometry | `create_object`, `create_mesh`, `create_curve`, `create_text_object`, `modify_object`, `duplicate_object`, `rename_object`, `delete_object`, `delete_objects_by_pattern`, `clear_scene`, `select_objects`, `set_mode`, `set_parent`, `apply_transform`, `shade_smooth` |
| Mesh and UV | `get_mesh_data`, `mesh_edit`, `mark_mesh_seams`, `uv_unwrap`, `audit_mesh`, `clean_mesh`, `convert_object_to_mesh`, `join_mesh_objects`, `set_object_origin` |
| Scenes and collections | `list_scenes`, `create_scene`, `set_active_scene`, `delete_scene`, `list_collections`, `create_collection`, `move_to_collection` |
| Materials | `list_materials`, `list_object_materials`, `create_material`, `update_material`, `set_material`, `assign_material`, `assign_face_material`, `set_image_texture`, `load_image`, `set_world_color` |
| Blender properties | `get_properties`, `set_properties` |
| Node graphs | `create_node_group`, `get_node_tree`, `edit_node_tree`, `set_node_inputs`, `assign_geometry_nodes` |
| Modifiers and constraints | `list_modifiers`, `add_modifier`, `remove_modifier`, `apply_modifier`, `list_constraints`, `add_constraint`, `remove_constraint` |
| Cameras and lights | `list_cameras`, `set_active_camera`, `aim_camera_at`, `zoom_camera`, `frame_camera_to_object`, `create_light_setup`, `frame_viewport` |
| Rigging and deformation | `create_armature`, `edit_armature`, `mirror_armature`, `get_armature`, `bind_armature`, `pose_bones`, `configure_ik`, `add_bone_constraint`, `remove_bone_constraint`, `set_shape_key`, `set_shape_key_keyframes` |
| Skinning QA | `get_vertex_weights`, `audit_rig`, `normalize_weights`, `transfer_weights` |
| Animation and drivers | `set_frame`, `insert_keyframe`, `delete_keyframe`, `set_transform_keyframes`, `set_keyframe_interpolation`, `list_animation_data`, `bake_animation`, `add_driver`, `list_drivers`, `remove_driver` |
| Collisions | `create_collision_proxy`, `create_collision_proxies`, `audit_collision_proxies`, `detect_collisions` |
| Physics | `configure_rigid_body`, `create_rigid_body_constraint`, `configure_physics_world`, `bake_physics`, `get_physics_state` |
| Rendering and previews | `set_render_settings`, `set_output_path`, `render_still`, `render_preview`, `render_animation`, `get_viewport_screenshot` |
| Files and assets | `import_asset`, `export_scene`, `append_datablocks`, `save_blend_file`, `save_checkpoint` |
| Batches and fallback | `batch_tools`, `execute_blender_code` |

## Workflow

1. Inspect scene state and Blender capabilities: `get_scene_info`, `get_blender_info`.
2. Select tools: `search_tools`, `get_tool_help`; use `get_properties` for settings.
3. Execute individual operations or prepare a batch.
4. Verify the result: `get_object_info`, `get_mesh_data`, `get_node_tree`, `audit_mesh`, a screenshot, or a render.
5. Save the working file or a separate checkpoint.

The server also provides MCP resources `blender://guide`, `blender://capabilities`, and the prompt `blender_workflow(task)`.

## Batches and actual object names

`batch_tools` uses the same schemas and Blender operations as individual tools. The entire batch is sent in one request, and its steps are not interleaved with other commands.

```json
{
  "operations": [
    {"tool": "create_object", "arguments": {"type": "cube", "name": "Box"}},
    {"tool": "add_modifier", "arguments": {
      "object_name": {"$ref": "0.name"},
      "type": "BEVEL",
      "settings": {"width": 0.1, "segments": 3}
    }},
    {"tool": "set_material", "arguments": {
      "object_name": {"$ref": "0.name"},
      "color": [0.2, 0.4, 0.8, 1.0]
    }}
  ]
}
```

`{"$ref":"0.name"}` refers to the `name` field in the first step's result, so it also works when the actual name is `Box.001`. References can be used for top-level name/string arguments and inside free-form `settings`/values. They do not replace numeric or structured arguments required by a Pydantic model. References must point to an earlier step; nested paths and array indices use dots, for example `0.created.0`.

All tool names and argument schemas are validated before execution. Object existence, RNA settings, and actual reference values are checked when their respective steps execute. The batch stops at the first error and returns `isError`, the failing index, and completed results. **Completed changes remain in the scene.** Nested batches and Python execution are not allowed. Previews and screenshots are returned as actual MCP image content.

## Lights, cameras, and render settings without Python

`get_properties` returns a property's value, type, range, and enum choices. `OBJECT_DATA` uses the object name; for a modifier/constraint, also specify `member`.

```json
{"target":"OBJECT_DATA","name":"KeyLight","properties":{"energy":1200,"size":4,"color":[1.0,0.8,0.6]}}
```

```json
{"target":"SCENE","properties":{"cycles.samples":128,"cycles.use_denoising":true}}
```

```json
{"target":"OBJECT_DATA","name":"Camera","properties":{"dof.use_dof":true,"dof.aperture_fstop":2.8}}
```

Specify datablock pointers, such as a modifier target or a camera focus object, as `{"id_type":"OBJECT","name":"Subject"}`. `set_properties` accepts only RNA properties and rolls back changes if an assignment fails. It does not accept Python expressions or methods.

## Modifiers: names, references, and actual completion

`add_modifier.settings` uses the same RNA reference conversion and validation as `set_properties`. A Boolean can be created directly:

```json
{"object_name":"Body","type":"BOOLEAN","name":"Mouth cavity","settings":{"operation":"DIFFERENCE","solver":"EXACT","object":{"id_type":"OBJECT","name":"Cutter"}}}
```

Use the **returned `modifier` name**, not a guess or localized name. `apply_modifier` checks existence before changing selection, requires `FINISHED`, and verifies that the modifier actually disappeared from the stack. A missing name or `CANCELLED` does not return success. In a batch, use a reference such as `{"$ref":"1.modifier"}`. Failed modifier creation removes the new entry; before further settings or joining, fix creation and verify `list_modifiers`.

The `studio` preset contains AREA, AREA, and POINT lights. `size` is not a POINT property; check the actual type using `get_properties` before changing type-specific properties.

### Render preview

`render_preview(max_size)` returns an actual PNG as an MCP image and restores resolution, percentage, format, color mode, and bit depth. Large results over the execute transport are transferred losslessly in chunks; the same mechanism works for batches and screenshots.

Do not schedule a diagnostic render concurrently with the geometry changes it is meant to inspect. Socket operations are serialized, but the order of concurrently submitted requests does not replace a completed phase. After a lost connection, inspect state first; a transfer error does not automatically start a new render or repeat a scene operation.

## Shader, Geometry Nodes, and compositor

`edit_node_tree` accepts a list of new node specifications and links:

```json
{
  "target": "MATERIAL", "name": "MyMaterial", "clear": true,
  "nodes": [
    {"name":"BSDF","type":"ShaderNodeBsdfPrincipled","inputs":{"Roughness":0.4}},
    {"name":"Noise","type":"ShaderNodeTexNoise","inputs":{"Scale":5}},
    {"name":"Output","type":"ShaderNodeOutputMaterial"}
  ],
  "links": [
    {"from_node":"Noise","from_socket":"Fac","to_node":"BSDF","to_socket":"Roughness"},
    {"from_node":"BSDF","from_socket":"BSDF","to_node":"Output","to_socket":"Surface"}
  ]
}
```

First create the material with `create_material`. `get_node_tree` provides socket names, identifiers, indices, availability, and defaults. If a duplicate name has multiple available sockets, use an index or unique identifier; if only one is available, the bridge selects it according to the node mode. `set_node_inputs` changes existing inputs; `edit_node_tree` adds new nodes or explicitly replaces the graph.

For Geometry Nodes, use `create_node_group` with `GeometryNodeTree` and its INPUT/OUTPUT interface, then `assign_geometry_nodes`. The same editing tools support `NODE_GROUP`, `WORLD`, and `COMPOSITOR`. Blender 5.x compositor uses `scene.compositing_node_group`; older APIs use the scene node tree.

## Modeling, UVs, and characters

- `create_object.geometry` passes parameters to the actual primitive operator: segments, radius, depth, torus radii, and so on.
- `get_mesh_data` returns indices and geometry in bounded pages. Evaluated data is intended for inspecting modifier results, not editing original mesh indices.
- `mesh_edit` performs a BMesh operation on temporary data and writes the mesh only after success. It rejects shared/linked data and meshes with shape keys; the operation requires Object Mode.
- `mark_mesh_seams` and `uv_unwrap` cover seams, smart project, angle-based/conformal unwrap, and packing.
- `create_armature` validates the bone hierarchy; `bind_armature` supports explicit and automatic weights; `pose_bones` can insert keyframes directly.
- `add_bone_constraint` covers pose-bone IK and other Blender constraints; `set_shape_key_keyframes` animates shape keys.
- `set_mode` switches between Object/Edit/Pose/Weight Paint modes; use Object Mode before BMesh, rest-bone editing, and weight cleanup.
- Animation uses actual F-curves, including Blender 5.x's new action-slot API.

`audit_mesh` provides topology QA; `clean_mesh` performs only explicitly enabled repairs. Destructive scene tools protect objects shared with other scenes by default. `force_shared=true` is an explicit global intervention.

## Collisions and collision proxies

`create_collision_proxy` creates a separate closed mesh of type `BOX`, `SPHERE`, `CAPSULE`, or an actual `CONVEX_HULL`. It prepares geometry at scene scale, preserves the source origin, and creates a unit-scale object in `COL_Collision_[source]`. `parent=true` attaches its motion to the source object; source geometry edits require proxy regeneration.

```json
{
  "object_name": "SM_Character", "shape": "CAPSULE", "axis": "Z",
  "engine": "UNREAL", "segments": 16, "margin": 0.01, "parent": true
}
```

Unreal names use `UBX_`, `USP_`, `UCP_`, or `UCX_` + the exact source name + an index. Sphere/capsule proxies use conservative dimensions accounting for polygon approximation. Hull `margin` means radial padding before hull calculation. Unity/generic receives an exportable mesh; the package does not automatically configure collider components in the engine.

`create_collision_proxies` creates a convex hull for each disconnected mesh component. For manually designed compound parts, use `reference_object`: geometry comes from `object_name`, while naming and origin come from the selected render asset. This workflow does not claim to provide automatic VHACD decomposition of arbitrary connected concave meshes.

Before exporting, use `audit_collision_proxies`: it checks closure, convexity, orientation/volume, scale, origin, and naming. Export the render mesh together with the returned proxy names.

`detect_collisions` checks 2–100 meshes in world space using BVH and containment tests. It returns `SURFACE_INTERSECTION`, `VOLUME_OVERLAP`, `CONTAINMENT`, or `TOUCHING`, including disconnected components and coplanar boxes. An open mesh does not define an interior volume. Geometric detection does not simulate physical response; Blender's rigid-body solver does that.

## Physics and baking

1. Prepare simple geometry and apply scale.
2. Configure `configure_rigid_body`: ACTIVE/PASSIVE, collision shape, mass/friction/restitution, margin, `kinematic`, and collision layers 0–19. Interacting bodies must share a layer.
3. If needed, create `create_rigid_body_constraint`: FIXED, POINT, HINGE, SLIDER, PISTON, GENERIC, GENERIC_SPRING, or MOTOR. Set limits, springs, and motors through `settings` or RNA tools.
4. Set substeps, solver iterations, and frame range with `configure_physics_world`.
5. Run `bake_physics(action="BAKE")`; use `FREE` before making changes.
6. Inspect results at selected frames with `get_physics_state`.

`get_physics_state` returns **evaluated world transforms** from the actual simulation. The original object location may remain at its starting value. Baking operates only on the active scene's rigid-body point cache, preserves the current frame, and has a 10,000-frame limit. RNA tools also support `RIGID_BODY`, `RIGID_BODY_CONSTRAINT`, and `RIGID_BODY_WORLD`.

## Advanced rigging, weights, and drivers

- `edit_armature` adds or updates rest bones, including roll; it validates the resulting hierarchy before editing. `mirror_armature` uses Blender symmetrize and its left/right naming conventions.
- `configure_ik` requires a target object and pole target; it supports bone targets, chain length, pole angle, and stretch. It updates an existing constraint by name.
- `get_properties`/`set_properties` support `POSE_BONE` (armature `name`, bone `member`), `BONE_CONSTRAINT` (`name`, `bone_name`, constraint `member`), and `VERTEX_GROUP` (mesh `name`, group `member`, for example `lock_weight`).
- `audit_rig` checks uninfluenced vertices, weight sums, invalid values, and influence limits. The default is **4 deform influences per vertex**.
- `normalize_weights` performs prune/limit/normalize while respecting locked groups and preserving unrelated mask groups. It does not invent weights for uninfluenced vertices; it returns an unresolved count and an audit.
- `transfer_weights` offers nearest-vertex and actual barycentric nearest-surface interpolation in rest/world space. Distance misses are explicitly reported and retain their original weights.
- `bake_animation` converts evaluated object/bone motion into new actions. For exporting rig motion from IK, you can explicitly choose `clear_constraints=true`.
- `add_driver` uses declarative SINGLE_PROP, TRANSFORMS, LOC_DIFF, and ROTATION_DIFF variables. Expressions are restricted to arithmetic and listed mathematical functions; this is not general Python execution.

Driver example: the second coordinate of `Driven` is twice the first coordinate of `Control`:

```json
{
  "target": "OBJECT", "name": "Driven", "data_path": "location", "index": 1,
  "expression": "var * 2",
  "variables": [{"name":"var","type":"SINGLE_PROP","targets":[
    {"id_type":"OBJECT","name":"Control","data_path":"location[0]"}
  ]}]
}
```

Inspect drivers with `list_drivers` and remove them with `remove_driver`. After weight cleanup, check deformation in extreme poses; a static skinning audit alone does not evaluate anatomical bending quality.

## Python fallback

`execute_blender_code` is intended only for operations that cannot be expressed with available tools. Shader graphs, weights, IK, collision proxies, rigid-body baking, common drivers, UVs, and mesh editing are covered. Examples of uncovered capabilities include specialized cloth/soft-body cache APIs or a driver outside the supported expression/variable model.

```json
{
  "code": "# Python only for the unsupported part of the task",
  "fallback_reason": "bake_physics supports rigid bodies, not cloth point-cache baking",
  "tools_considered": ["bake_physics", "add_modifier"]
}
```

The server rejects missing/empty reasons, empty lists, and unknown or ineligible names. `search_tools` and `get_tool_help` may be listed **alongside at least one specific Blender operation tool**; discovery alone is insufficient. `execute_blender_code` and `batch_tools` are ineligible entries. The server does not automatically assess the truth of the reason; the client must follow the instructions. Supported setup and subsequent verification should use dedicated tools.

## Security, limits, and cancellation

**Use only with a trusted MCP client on your own computer.** The bridge can modify scenes, access files, and execute Python in Blender. It is not a security sandbox.

- Do not expose the Blender TCP port to the internet or an untrusted network.
- Save the scene or a checkpoint before destructive operations.
- A client permission prompt is not a guarantee that an in-flight Blender operation can be stopped.
- Cancelling an already dispatched request does not guarantee that the scene operation stops.
- A failed or timed-out transfer does not automatically retry a mutation or render. Inspect scene state before retrying.
- Batches are limited to 100 operations and are not transactions. Completed steps remain after a later failure.
- Socket requests are serialized; concurrent submission order does not replace a completed verification phase.
- The maximum Blender message size is 64 MiB. Execute-transport result storage is bounded to four transfers / 64 MiB total, and abandoned results expire.
- The error journal stores sanitized summaries, not raw code or arguments, but may contain local paths. Review it before sharing.

Execution-state values in the journal distinguish what happened:

- `not_started`: no Blender call had begun.
- `in_flight`: the request may still be running in Blender.
- `response_received`: Blender returned a response, including an error response.

A rejected RNA edit is not still running; a transport timeout can be `in_flight`.

## Troubleshooting

Start with:

- **No tools in the client:** verify the absolute command path and restart/reconnect the MCP session.
- **`Connection refused`:** start the Blender endpoint and confirm `127.0.0.1:9876`.
- **Bridge exits immediately:** run `blender-mcp-local --version` manually and check the client's MCP log.
- **Timeout during a long render:** raise both `--timeout` and the client's tool timeout.
- **Old process survives:** update to 1.0.10+ and read [MCP lifecycle and stale processes](#mcp-lifecycle-and-stale-processes).
- **Scene errors:** inspect `get_error_log(limit=20)` and the [local error journal](../logs/README.md).

The full checklist, including Windows paths, project trust, protocol confusion, and safe log inspection, is in [Troubleshooting](troubleshooting.md).

## Architecture

```text
MCP client ← stdio → Python bridge ← local TCP JSON → Blender addon
                                                 ↓
                                      main-thread command runtime
```

- `addon.py`: nonblocking socket server and versioned RPC; Blender operations run on the main thread.
- `src/blender_mcp_local/server.py`: MCP schemas, client policy, transport, batch preflight, and original operations.
- `src/blender_mcp_local/advanced.py`: declarative workflow tools and their Blender runtime.
- `src/blender_mcp_local/production.py`: collisions, physics, skinning QA, IK, drivers, and animation baking.

The bridge uploads its implementation library once. The bundled addon verifies its SHA-256 and compiles it; the official execute-compatible endpoint loads the compressed library into `bpy.app.driver_namespace`. Subsequent calls carry only an operation and arguments, with a short bridge-owned wrapper for the official transport. This internal implementation is separate from client-generated Python fallback. A missing-library error allows safe runtime recovery; failed scene operations are not automatically repeated. `get_blender_info` returns the active `transport` and corresponding capabilities.

Socket I/O runs outside the MCP event loop. The bridge serializes access to Blender; a native batch executes as one Blender main-thread request. Both framing modes are supported, with a 64 MiB message limit. Render previews restore temporarily changed render parameters. Background Blender uses camera rendering; a visible viewport requires the UI.

## Tests and releases

Fast automated tests without Blender:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Actual integration tests in a separate Blender process:

```bash
.venv/bin/python tests/run_blender_integration.py --blender /path/to/blender
```

The same scenarios over an emulated official execute-only protocol:

```bash
.venv/bin/python tests/run_blender_integration.py --blender /path/to/blender --execute-only
```

The runner uses factory startup, disabled autoexec, its own local port, and a temporary directory. It verifies actual TCP transport, node graphs, BMesh, UVs, mesh deformation, IK baking, drivers, normalization and barycentric weight transfer, collision proxy/FBX roundtrips, intersections/containment, and actual rigid-body collision with baked cache and collision layers. It includes a Cycles render, Eevee previews at 1000/1200 px from a 1500×800 scene, restoration of render parameters, modifier-name/pointer validation, a checkpoint, and a GLB roundtrip. The execute-only test endpoint deliberately closes responses above 32 KiB so large images must use chunked transfer. `--temp-root` selects the directory for test outputs.

Build local release archives using only the Python standard library:

```bash
.venv/bin/python tools/build_release.py
```

The outputs are an addon ZIP, a Python source package, and `dist/SHA256SUMS`. The build packages only explicitly listed project files. Both archives include the project license; the source package also includes this guide, the client setup guide, the troubleshooting guide, and the error-journal documentation. See [CHANGELOG.md](../CHANGELOG.md) for changes and compatibility.
