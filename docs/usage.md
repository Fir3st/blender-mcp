# Blender MCP Local — detailed guide (1.0.10)

A local, **tool-first MCP for Blender**: 118 tools for modeling, materials, Geometry Nodes, rigging, animation, collisions, physics, lighting, rendering, file operations, and local error diagnostics. Clients primarily compose specific operations; custom Python is a justified fallback for capabilities not covered by tools.

- Blender ↔ MCP over localhost; MCP client ↔ server over stdio.
- Custom shader and Geometry Nodes graphs, BMesh editing, UVs, and rig weights through declarative tools.
- Collision proxies, intersection checks, physics joints/baking, IK with pole targets, weight cleanup/transfer, drivers, and visual animation baking.
- Batches of up to 100 operations in one Blender request, with references to previous results.
- No external asset services, telemetry, or cloud accounts.
- Python 3.10+, MCP SDK 1.27.x/1.x, and Pydantic 2.x.
- Blender 4.2+ API target; integration-tested on **Blender 5.1.1**.

Run the commands in this guide from the project root.

## Quick start

### 1. Connect a running Blender integration

**If you already use the official Blender Lab MCP integration in Blender 5.1+, you do not need to install our addon.** Start its server at `127.0.0.1:9876` and use `--protocol legacy`. The bridge automatically recognizes the official execute-only protocol and preserves dedicated MCP tools and batches. See the [official installation instructions](https://www.blender.org/lab/mcp-server/).

With this transport, the client still calls dedicated tools such as `create_object` or `configure_ik`. The bridge translates these calls into the official `execute` wire protocol using its compressed library, loaded once; this is not a client-generated Python fallback.

#### Bundled addon — optional alternative

In Blender, open `Preferences → Add-ons → Install from Disk…` and select:

```text
dist/blender-mcp-local-addon-1.0.10.zip
```

Generate this archive first with `python tools/build_release.py` if it is not available. Enable **Blender MCP (Local)**. In the 3D viewport, open the sidebar (`N`), select the **MCP** tab, and click **Start MCP Server**. The default address is `127.0.0.1:9876`.

The standalone `addon.py` can also be used for installation or development. The ZIP contains the same addon in a standard package structure.

### 2. Install the MCP bridge

From the project root:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
blender-mcp-local --version
```

Alternatively, install a previously built source package:

```bash
pip install dist/blender_mcp_local-1.0.10.tar.gz
```

### 3. Connect an MCP client

For Claude Desktop and clients using `mcpServers`:

```json
{
  "mcpServers": {
    "blender-local": {
      "command": "/path/to/.venv/bin/blender-mcp-local",
      "args": ["--protocol", "legacy"]
    }
  }
}
```

In other MCP clients, choose a local **stdio** server and the same executable command. After connecting, start with `get_blender_info`.

Optional bridge arguments:

```text
--host 127.0.0.1
--port 9876
--timeout 180
--session-timeout 60
--protocol legacy|length
--version
--error-log /absolute/path/errors.jsonl
--no-error-log
```

The configuration variables `BLENDER_HOST`, `BLENDER_PORT`, `BLENDER_TIMEOUT`, `BLENDER_PROTOCOL` and `BLENDER_SESSION_TIMEOUT` remain supported. CLI arguments take precedence. For long renders, set appropriate timeouts in both the bridge and the MCP client.

When stdio is idle, the bridge sends a standard MCP liveness ping. A live client answers, so the session can stay idle indefinitely; a client that has ended but still holds the pipes open does not answer and the bridge exits after two probes. `--session-timeout 0` disables probing. Closing stdin or losing the client process also ends the bridge. This prevents stale old versions from remaining after an update. If processes from a pre-1.0.10 version are already stuck, stop them once with `pkill -f blender-mcp-local`.

### Upgrading from 0.4.0

1. If you use the bundled 0.4.0 addon, stop its server and replace it with version 1.0.10.
2. Update the Python bridge and restart its process.
3. Refresh the MCP connection so the client loads the new instructions and schemas.

The bundled native-RPC addon requires API 1 (version 1.0.x). The official Blender Lab extension can remain unchanged; updating/restarting the bridge is sufficient. The bridge also recognizes older execute-compatible endpoints. Python fallback now also requires `fallback_reason` and `tools_considered`.

### Node graph fixes in 1.0.9

Graph creation and replacement preserve requested node names even when Blender's default names collide. Failed rewiring restores original links and removes newly created nodes, including when input-link limits have already deleted an earlier new link. Error journals now distinguish unavailable node types, missing nodes/trees, invalid links, non-pointer RNA paths, non-mesh inputs and refused connections without recording raw argument values.

### Large requests and execution-state diagnostics in 1.0.8

Large mesh/batch arguments sent through the execute-compatible backend are compressed and uploaded in 16 KiB chunks before the scene operation runs. Failed transfers are cleaned up without automatically repeating an edit. Completed remote error responses now use `response_received`; connection timeouts remain `in_flight` because work may continue in Blender. Existing error records are preserved.

### Mesh preflight in 1.0.7

`mesh_edit` requires EXTRUDE/INSET/TRIANGULATE in the FACE domain, BEVEL/SUBDIVIDE in EDGE, and TRANSFORM in VERT; DELETE supports all domains. Domain mismatches or unknown settings are rejected while preparing an MCP batch, before its earlier steps execute. Domains are not silently inferred or rewritten.

### More precise arguments and diagnostics in 1.0.6

`set_object_origin` publishes the exact GEOMETRY/CURSOR/CENTER_OF_MASS/CENTER_OF_VOLUME modes and MEDIAN/BOUNDS center choices in its argument schema. Unsupported modes are not approximated. `get_tool_help` also accepts known client prefixes (`blender_`, `mcp__blender__`, `mcp_blender_local_`, `mcp__blender_local__`). Unknown tools are still rejected.

RNA boolean/string/enum values receive preliminary type validation; enum-flag lists are converted to actual sets. Blender still validates dynamic scalar enum entries. The journal distinguishes objects outside the view layer, unsupported origins, unknown tools, and RNA type errors. Joining still does not automatically link external or hidden objects, and validation is not bypassed. Old records without argument values cannot be retroactively supplemented.

### RNA matrices and cancelled requests in 1.0.5

`get_properties` recursively converts RNA vectors/matrices into JSON. Numeric arrays report `array_dimensions`; `set_properties` accepts correctly shaped matrices as lists of rows, uses an actual `Matrix` for assignment, and restores a deep copy of the original value on failure. Scalar drivers require `index=-1`; arrays require an explicit valid index.

Batch errors now preserve stack information for the safe error journal, and summaries distinguish additional validation reasons. A cancelled request still waiting for the Blender lock is not dispatched. If it has already been sent, Blender may continue — this is not rollback. The journal records `execution_state` (`not_started`, `in_flight`, `response_received`) for these events. Old records remain unchanged; omitted original argument values or stack data from 1.0.4 cannot be reconstructed.

### Current file and error journal in 1.0.4

`append_datablocks` can list/append data from the currently open `.blend` through a temporary copy of its **last saved version**, created in the same directory. Relative paths remain based on the original directory; the working file is not saved or overwritten. The source directory must allow creating/removing the temporary copy. Use `duplicate_object` for live unsaved objects; `link=True` back into the current file is rejected.

When launched through the CLI, the bridge automatically writes a local error journal. In this project it is `logs/errors.jsonl`; installed packages use `~/.local/state/blender-mcp-local/errors.jsonl`. Find its location with `get_error_log_status()` and read recent errors with `get_error_log(limit=20)`, for example `get_error_log(limit=10, tool="append_datablocks")`.

Each record contains a timestamp/ID, version, tool, error class, fixed diagnostic summary, stack-frame locations, argument types/counts, and an optional batch-step index. **It does not record raw argument values, custom code, stdout, local variables, or arbitrary exception text.** Details of unknown general errors are deliberately omitted. MCP validation errors, Blender/transport exceptions, and batch error results are captured without duplicate records. Client-only errors and past calls made before logging was enabled cannot be recovered.

The file has private permissions (0600) and rotates at 5 MiB with two backups. Logging failures do not replace the original error; check `last_write_error`. Optionally use `--error-log` or `--no-error-log`. See [logs/README.md](../logs/README.md) for details.

### Socket selection in 1.0.3

Duplicate display names, such as `From Min` on Map Range, are resolved using the single allowed and available socket in the node's current mode. Genuine ambiguity is still rejected; the error includes candidate indices/identifiers. `get_node_tree` also returns `enabled` and `available`. An index or unique identifier remains an explicit choice with unchanged behavior.

### Fix in 1.0.2

`clean_mesh` does not assume dictionary results from the BMesh operations `remove_doubles` and `dissolve_degenerate`, which return `None` in Blender 5.1.1. For merging, `affected` reports the actual reduction in vertices. For dissolving, it reports the sum of reductions in vertices, edges, and faces; individual counts are also returned. The original mesh is written only after all enabled operations complete. Restart the bridge after updating.

### Fixes in 1.0.1

Restart the bridge and refresh the MCP connection. The official Blender Lab extension is unchanged; scene-operation fixes are in the newly loaded command library. The optional native 1.0.0 addon remains compatible; the package contains 1.0.1 with the same RPC API 1.

Execute transport transfers large results in 16 KiB chunks with length and SHA-256 checks. Rendering runs once and returns the PNG at the original requested size. This does not reduce resolution or automatically repeat rendering. Temporary result storage is limited to four transfers / 64 MiB total; abandoned results expire.

## Tools

The server automatically sends exact argument schemas to the client. `search_tools` finds tools by task; `get_tool_help` returns complete documentation for a selected tool. Both work without a connected Blender instance.

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

### Workflow

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

### Modifiers: names, references, and actual completion

Since 1.0.1, `add_modifier.settings` uses the same RNA reference conversion and validation as `set_properties`. A Boolean can be created directly:

```json
{"object_name":"Body","type":"BOOLEAN","name":"Mouth cavity","settings":{"operation":"DIFFERENCE","solver":"EXACT","object":{"id_type":"OBJECT","name":"Cutter"}}}
```

Use the **returned `modifier` name**, not a guess or localized name. `apply_modifier` checks existence before changing selection, requires `FINISHED`, and verifies that the modifier actually disappeared from the stack. A missing name or `CANCELLED` does not return success. In a batch, use a reference such as `{"$ref":"1.modifier"}`. Failed modifier creation removes the new entry; before further settings or joining, fix creation and verify `list_modifiers`.

The `studio` preset contains AREA, AREA, and POINT lights. `size` is not a POINT property; check the actual type using `get_properties` before changing type-specific properties.

### Render preview

`render_preview(max_size)` returns an actual PNG as an MCP image and restores resolution, percentage, format, color mode, and bit depth. Large results over execute transport are transferred losslessly in chunks; the same mechanism works for batches and screenshots.

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

`execute_blender_code` is intended only for operations that cannot be expressed with available tools. Shader graphs, weights, IK, collision proxies, rigid-body baking, common drivers, UVs, and mesh editing are now covered. Examples of uncovered capabilities include specialized cloth/soft-body cache APIs or a driver outside the supported expression/variable model.

```json
{
  "code": "# Python only for the unsupported part of the task",
  "fallback_reason": "bake_physics supports rigid bodies, not cloth point-cache baking",
  "tools_considered": ["bake_physics", "add_modifier"]
}
```

The server rejects missing/empty reasons, empty lists, and unknown or ineligible names. `search_tools` and `get_tool_help` may be listed **alongside at least one specific Blender operation tool**; discovery alone is insufficient. `execute_blender_code` and `batch_tools` are ineligible entries. The server does not automatically assess the truth of the reason; the client must follow the instructions. Supported setup and subsequent verification should use dedicated tools.

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

The outputs are an addon ZIP, a Python source package, and `dist/SHA256SUMS`. The build packages only explicitly listed project files. Both archives include the project license; the source package also includes this guide. See [CHANGELOG.md](../CHANGELOG.md) for changes and compatibility.
