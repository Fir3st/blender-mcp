# Changelog

## 1.0.8 — 2026-10-20

- Compress large execute-backend mesh/batch arguments and upload them in 16 KiB chunks before dispatch, avoiding oversized single execute requests.
- Clean up failed uploads and dispatch scene operations only once; transport failures never automatically retry an edit.
- Record completed remote error responses as `response_received`, while connection timeouts remain `in_flight`.
- Add regressions for lossless large geometry transfer, failed upload/dispatch cleanup and execution-state diagnostics. Verified with 59 unit tests and 29 Blender integration tests in each native and execute-only mode.
- Native RPC API remains 1. Existing API-1 addons and the official Blender Lab extension remain compatible; update and restart the Python bridge to load these fixes.

## 1.0.7 — 2026-10-03

- Validate mesh-edit operation/domain compatibility and settings keys before Blender dispatch, including full batch preflight before earlier steps mutate the scene.
- Preserve explicit domain semantics rather than silently selecting a different mesh element type.
- Add fixed diagnostic summaries and preflight regressions for incorrect domains/settings.

## 1.0.6 — 2026-10-03

- Publish exact origin mode/center enums and resolve tool help for known MCP client name prefixes.
- Prevalidate RNA boolean/scalar versus array types and strings; convert JSON enum-flag lists to RNA sets. Keep Blender authoritative for dynamic enum values.
- Add safe summaries for view-layer join rejection, invalid origins, unknown help names and RNA type/enum errors.
- Preserve protections: no implicit scene switching, hidden-object linking, skipped settings or origin fallback guesses.

## 1.0.5 — 2026-10-03

- Recursively serialize nested RNA values, including Matrix rows/Vector elements, so `get_properties` no longer fails during JSON result transfer.
- Validate multidimensional numeric RNA shapes; preserve matrix row semantics through mathutils.Matrix and deep rollback snapshots.
- Reject component indices for scalar drivers before native creation and clarify native property/index/animatability rejection.
- Preserve remote traceback frames for failed batch steps and expand fixed safe diagnostic summaries.
- Cancel queued Blender calls before dispatch; record execution state without claiming in-flight operations were cancelled or rolled back.
- Regressions cover matrix inspection, assignment/rollback, scalar drivers, batch diagnostics and queued cancellation. Existing logs are preserved.

## 1.0.4 — 2026-10-02

- Append/list the currently open .blend through a temporary same-directory copy of its saved state, preserving relative path basis without saving/overwriting the working file.
- Resolve Blender `//` library paths and detect same-file aliases. Self-linking remains explicitly rejected; temporary copies are removed on success/failure.
- Add a private rotating local JSONL error journal enabled by the CLI, configurable with `--error-log` / `--no-error-log`.
- Capture MCP validation/unknown-tool errors, Blender/transport exceptions, cancellations and failed batch steps without duplicate events.
- Add `get_error_log` and `get_error_log_status` (118 tools total). Records include fixed summaries, error classes, frame locations and argument shapes; raw values/code/stdout/exception detail are not persisted.
- Log failure does not replace the original error. Reader rejects unexpected/malformed records and reports incomplete lines.

## 1.0.3 — 2026-10-01

- Resolve duplicate node socket names when exactly one candidate is enabled and available, including scalar/vector Map Range inputs.
- Preserve explicit indices/unique identifiers; genuinely ambiguous active inputs still fail with candidate indices and identifiers rather than silently choosing the first.
- Node-tree inspection now includes socket enabled/available flags.
- Add resolver unit tests and real Blender FLOAT/FLOAT_VECTOR graph, link, update and ambiguity-rollback regressions.

## 1.0.2 — 2026-10-01

- Fix `clean_mesh` crashing on `None` results from BMesh `remove_doubles` and `dissolve_degenerate`.
- Report merge counts from the actual vertex reduction, and degenerate cleanup counts from actual vertex/edge/face reductions rather than nonexistent operator output slots.
- Add real Blender regressions for welding a split closed volume with normals recalculation, repeated no-op cleanup and degenerate-edge dissolution.
- Command-library update only; existing API-1 native addons and the official Blender Lab extension remain compatible. Restart the bridge to load the fix.

## 1.0.1 — 2026-10-01

- Execute-compatible large results use bounded 16 KiB chunks with length and SHA-256 validation; fixes large preview/image replies on endpoints that close partial nonblocking sends.
- Scene operations run once. Failed/expired result transfer never automatically repeats a render or mutation. Result cache is bounded and released/expired.
- Preview requires a finished render and restores resolution, percentage, image format, color mode and depth.
- `apply_modifier` rejects a missing name before selection changes and requires both `FINISHED` and actual removal from the modifier stack.
- `add_modifier.settings` uses the same validated RNA/datablock-pointer conversion as `set_properties`; failed creation still removes the new modifier.
- Fallback `tools_considered` accepts `search_tools`/`get_tool_help` alongside at least one dedicated operation; discovery alone, unknown names, Python and batch wrappers remain rejected.
- Regressions cover cancelled/fake-success modifier application, pointer rollback, Unicode/partial/corrupt response transfers and actual Eevee previews at 1000/1200 px.
- Native RPC API remains 1; the official Blender Lab extension and existing native addon 1.0.0 remain compatible. Restart the bridge to load fixes.

## 1.0.0 — 2026-09-30

### Tool-first workflows

- 116 tools: the existing object, scene, material, camera, render and export tools plus declarative workflows below.
- Shader, world, Geometry Nodes and compositor graph creation, inspection, linking and input editing.
- Named node groups with typed interfaces; Geometry Nodes modifier assignment and exposed input values.
- RNA property discovery and validated editing for lights, cameras, render settings, scenes, materials, modifiers and constraints.
- Indexed mesh inspection; atomic BMesh extrude, inset, bevel, subdivision, triangulation, deletion and transforms.
- UV seams, unwrap/packing and per-face material assignment.
- Armature hierarchy creation, weight binding, bone poses/constraints, shape keys and shape-key animation.
- Rigid-body configuration, local image loading, .blend library append/link, checkpoint copies and UI viewport framing.
- Primitive-specific geometry arguments and additional ico-sphere, circle, grid and monkey primitives.
- Existing keyframe interpolation editing and Blender 5.x action-slot inspection.
- Explicit Object/Edit/Pose/Paint mode control with failed-switch state restoration.
- Box, sphere, capsule and BMesh convex collision proxies, Unreal naming, source-aligned unit-scale geometry and collider QA.
- Loose-island hulls and manual compound pieces associated with a render reference; BVH intersection, volume-overlap, containment and contact queries.
- Real rigid-body joints, collision layers/margins, solver settings, rigid-body cache bake/free and evaluated physics state.
- Rest-armature editing/mirroring, pole-target IK, editable bone constraints, weight inspection/auditing and lock-aware normalization.
- Nearest-vertex and barycentric nearest-surface deform weight transfer, preserving unrelated mask groups.
- Visual object/pose animation baking; declarative driver creation, inspection and removal with restricted arithmetic expressions.

### Transport and MCP

- Versioned native command RPC: upload/checksum/compile the bridge-owned command library once, then send operation names and JSON arguments.
- Automatic compatibility with the official Blender Lab execute-only endpoint: compressed cached runtime and short operation wrappers, preserving the concrete MCP tool API without replacing the user's Blender extension.
- Native batches execute under one scene-thread request with backward result references (`{"$ref":"0.name"}`), preventing interleaving and avoiding name-collision assumptions.
- Full batch schema preflight, first-error stop, partial-result reporting and real MCP image content.
- Socket work runs off the MCP event loop; scene requests remain serialized. Start/completion progress notifications for batches.
- Dedicated-tool instructions, tool search/help, read-only/destructive annotations, workflow prompt and offline guide/catalog resources.
- Python fallback requires a missing-capability explanation and checked dedicated tool names.
- Explicit API/version errors, finite JSON arguments, message size limits and safe missing-runtime reload. Scene operation errors are never automatically retried.
- CLI `--version`, `--host`, `--port`, `--timeout`, `--protocol`.

### Verification and compatibility

- Python 3.10+, MCP SDK 1.27.x/1.x, Pydantic 2.x; Blender 4.2+ API target.
- Verified in Blender 5.1.1 with isolated factory-startup TCP integration tests, including real Cycles rendering, GLB/FBX roundtrips, collision response and IK visual bake.
- Both null-delimited and length-prefixed TCP framing retained.
- Upgrade the bridge and restart its process. The official Blender Lab extension can remain installed; the optional custom native-RPC addon should be 1.0.0.
- `execute_blender_code` now requires `code`, `fallback_reason` and `tools_considered`.
- Batch edits are not transactions: already completed operations remain when a later step fails. Referenced result values are resolved and checked at execution time.

## 0.4.0

- Local stdio/TCP bridge with 65 Blender tools, generated command programs, mesh QA and bounded render previews.
