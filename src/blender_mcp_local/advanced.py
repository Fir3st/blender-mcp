"""Declarative Blender workflows. COMMAND_CODE is loaded once by the addon."""

from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class NodeSpec(InputModel):
    name: str
    type: str = Field(description="Blender node bl_idname, e.g. ShaderNodeTexNoise")
    location: tuple[float, float] = (0, 0)
    properties: dict[str, Any] = Field(default_factory=dict)
    inputs: dict[str, Any] = Field(default_factory=dict, description="Socket name or zero-based index as string -> default value")


class NodeLink(InputModel):
    from_node: str
    from_socket: str | int
    to_node: str
    to_socket: str | int


class InterfaceSocket(InputModel):
    name: str
    direction: Literal["INPUT", "OUTPUT"] = "INPUT"
    socket_type: str = "NodeSocketFloat"
    default: Any = None


class BoneSpec(InputModel):
    name: str
    head: tuple[float, float, float]
    tail: tuple[float, float, float]
    parent: str | None = None
    connected: bool = False
    deform: bool = True
    roll: float = 0.0


class VertexWeight(InputModel):
    bone: str
    vertices: list[int]
    weight: float = Field(ge=0, le=1)


class BonePose(InputModel):
    name: str
    location: tuple[float, float, float] | None = None
    rotation: tuple[float, float, float] | None = Field(default=None, description="XYZ Euler radians")
    scale: tuple[float, float, float] | None = None


RnaTarget = Literal["OBJECT", "OBJECT_DATA", "MATERIAL", "WORLD", "SCENE", "RENDER", "MODIFIER", "CONSTRAINT",
                    "POSE_BONE", "BONE_CONSTRAINT", "RIGID_BODY", "RIGID_BODY_CONSTRAINT", "RIGID_BODY_WORLD", "VERTEX_GROUP"]
TreeTarget = Literal["MATERIAL", "WORLD", "NODE_GROUP", "COMPOSITOR"]


def register_tools(mcp: FastMCP, call) -> dict[str, Any]:
    """Keep MCP schemas independent of bpy, which only exists in Blender."""
    native_call = call

    def call(command, arguments):
        return native_call(command, {key: value for key, value in arguments.items() if key != "call"})

    functions = {}

    def tool(fn=None, *, read_only=False):
        decorator = mcp.tool(annotations=ToolAnnotations(
            readOnlyHint=read_only, destructiveHint=not read_only, openWorldHint=False,
        ))
        def register(function):
            functions[function.__name__] = function
            return decorator(function)
        return register(fn) if fn is not None else register

    @tool(read_only=True)
    def find_objects(pattern: str = "*", object_type: str | None = None, collection: str | None = None,
                     limit: int = 100, offset: int = 0) -> dict[str, Any]:
        """Find objects in the active scene by glob/type/collection, with bounded pagination."""
        return call("find_objects", locals())

    @tool(read_only=True)
    def get_selection() -> dict[str, Any]:
        """Inspect active object, selected objects and current mode without changing selection."""
        return call("get_selection", {})

    @tool(read_only=True)
    def get_mesh_data(object_name: str, domain: Literal["VERT", "EDGE", "FACE"] = "VERT",
                        offset: int = 0, limit: int = 1000, evaluated: bool = False) -> dict[str, Any]:
        """Read paginated mesh coordinates/connectivity/normals/material indices or seam flags.

        Use returned indices with mesh_edit, mark_mesh_seams and face material
        assignment. evaluated=true inspects modifier output; those indices
        must not be used to edit the original topology.
        """
        return call("get_mesh_data", locals())

    @tool
    def assign_face_material(object_name: str, material_name: str, face_indices: list[int]) -> dict[str, Any]:
        """Assign an existing material to explicit mesh faces; creates/reuses its material slot."""
        return call("assign_face_material", locals())

    @tool(read_only=True)
    def get_properties(target: RnaTarget, name: str | None = None, member: str | None = None,
                       paths: list[str] | None = None, bone_name: str | None = None) -> dict[str, Any]:
        """Inspect Blender RNA values, types, ranges and enum options before editing.

        OBJECT_DATA uses the object's name (camera/light/mesh data). MODIFIER and
        CONSTRAINT use object name + member name. SCENE/RENDER use active scene.
        paths can contain nested properties, e.g. cycles.samples or dof.aperture_fstop.
        POSE_BONE uses armature name + member bone. BONE_CONSTRAINT uses armature
        name + bone_name + member constraint. RIGID_BODY targets use object name;
        RIGID_BODY_WORLD uses the active scene's world.
        VERTEX_GROUP uses object name + member group (e.g. lock_weight).
        No Python expressions, methods, preferences or arbitrary bpy paths.
        """
        return call("get_properties", locals())

    @tool
    def set_properties(target: RnaTarget, properties: dict[str, Any], name: str | None = None,
                       member: str | None = None, bone_name: str | None = None) -> dict[str, Any]:
        """Set Blender RNA properties with validation and rollback on assignment errors.

        Use for light energy/color/shape, camera DOF/clipping, Cycles samples,
        units, color management, and updating modifier/constraint settings.
        Inspect get_properties first. Datablock pointers use
        {"id_type":"OBJECT","name":"Target"}; supported id_type values:
        OBJECT, MATERIAL, IMAGE, TEXTURE, NODE_GROUP, WORLD. No code or eval.
        """
        return call("set_properties", locals())

    @tool
    def load_image(path: str, name: str | None = None) -> dict[str, Any]:
        """Load a local image datablock for shader nodes or textures; reuses an already loaded image."""
        return call("load_image", locals())

    @tool
    def create_node_group(name: str, tree_type: Literal["GeometryNodeTree", "ShaderNodeTree", "CompositorNodeTree"] = "GeometryNodeTree",
                          sockets: list[InterfaceSocket] | None = None) -> dict[str, Any]:
        """Create a named Geometry/Shader/Compositor node group with typed interface sockets.

        Returns actual group name and socket identifiers. Geometry modifiers
        generally require INPUT and OUTPUT NodeSocketGeometry sockets.
        """
        return call("create_node_group", {"name": name, "tree_type": tree_type,
                    "sockets": [socket.model_dump() for socket in sockets or []]})

    @tool(read_only=True)
    def get_node_tree(target: TreeTarget, name: str | None = None) -> dict[str, Any]:
        """Inspect nodes, properties, input/output socket identifiers, defaults and links.

        Supports material/world shader graphs, Geometry Nodes groups and the
        active scene compositor, including Blender 5.x compositing node groups.
        Use socket indices when names are duplicated.
        """
        return call("get_node_tree", locals())

    @tool
    def edit_node_tree(target: TreeTarget, nodes: list[NodeSpec], links: list[NodeLink],
                       name: str | None = None, remove_nodes: list[str] | None = None,
                       clear: bool = False) -> dict[str, Any]:
        """Build shader, Geometry Nodes or compositor graphs declaratively, without Python.

        nodes describe new named nodes, RNA properties and input defaults.
        links use named nodes and socket names or indices. Existing nodes can
        be linked without recreating them. New nodes/links are validated before
        removing existing nodes; failed additions are cleaned up. clear=true
        replaces the graph. Node properties support datablock pointer values.
        """
        return call("edit_node_tree", {"target": target, "name": name, "clear": clear,
                    "nodes": [node.model_dump() for node in nodes],
                    "links": [link.model_dump() for link in links], "remove_nodes": remove_nodes or []})

    @tool
    def set_node_inputs(target: TreeTarget, node_name: str, inputs: dict[str, Any],
                        name: str | None = None) -> dict[str, Any]:
        """Update existing node input defaults by socket name/index, with rollback on error."""
        return call("set_node_inputs", locals())

    @tool
    def assign_geometry_nodes(object_name: str, node_group: str, inputs: dict[str, Any] | None = None,
                              modifier_name: str = "GeometryNodes") -> dict[str, Any]:
        """Attach a Geometry Nodes group and set exposed inputs by name or identifier.

        Object/material/image inputs accept datablock pointer values. Returns
        modifier name and interface identifiers. Updates the named modifier if
        it already exists and is a NODES modifier.
        """
        return call("assign_geometry_nodes", locals())

    @tool
    def mesh_edit(object_name: str, operation: Literal["EXTRUDE", "INSET", "BEVEL", "SUBDIVIDE", "TRIANGULATE", "DELETE", "TRANSFORM"],
                  domain: Literal["VERT", "EDGE", "FACE"] = "FACE", indices: list[int] | None = None,
                  settings: dict[str, Any] | None = None) -> dict[str, Any]:
        """Edit mesh data using BMesh in Object Mode; indices=None selects all in domain.

        EXTRUDE/FACE: offset XYZ; INSET/FACE: thickness/depth;
        BEVEL/EDGE: offset/segments/profile; SUBDIVIDE/EDGE: cuts/smooth;
        TRIANGULATE/FACE; DELETE: any domain; TRANSFORM/VERT: translation,
        rotation XYZ radians, scale XYZ. Edits run on a temporary BMesh and
        commit only on success. Shared meshes and shape-key topology edits are
        rejected. Returned indices refer to the resulting mesh, not old data.
        Operation/domain mismatches and unknown settings fail before Blender
        dispatch (including full batch preflight). Do not rely on FACE default
        for TRANSFORM, BEVEL or SUBDIVIDE.
        """
        expected = {"EXTRUDE": "FACE", "INSET": "FACE", "BEVEL": "EDGE", "SUBDIVIDE": "EDGE",
                    "TRIANGULATE": "FACE", "TRANSFORM": "VERT"}.get(operation)
        if expected and domain != expected:
            raise ValueError(f"{operation} requires {expected} domain; received {domain}")
        allowed = {"EXTRUDE": {"offset"}, "INSET": {"thickness", "depth"}, "BEVEL": {"offset", "segments", "profile"},
                   "SUBDIVIDE": {"cuts", "smooth"}, "TRIANGULATE": set(), "DELETE": set(),
                   "TRANSFORM": {"translation", "rotation", "scale"}}[operation]
        if set(settings or {}) - allowed:
            raise ValueError(f"Unknown {operation} settings; inspect mesh_edit tool help")
        return call("mesh_edit", {"object_name": object_name, "operation": operation, "domain": domain,
                                  "indices": indices, "settings": settings})

    @tool
    def uv_unwrap(object_name: str, method: Literal["SMART_PROJECT", "ANGLE_BASED", "CONFORMAL"] = "SMART_PROJECT",
                  margin: float = 0.02, angle_limit: float = 1.151917) -> dict[str, Any]:
        """Unwrap all mesh faces and pack UV islands; restores active object, selection and mode.

        SMART_PROJECT is seam-independent; ANGLE_BASED/CONFORMAL use existing
        mesh seams. margin is UV-space fraction, angle_limit is radians.
        """
        return call("uv_unwrap", locals())

    @tool
    def mark_mesh_seams(object_name: str, edge_indices: list[int], clear_existing: bool = False) -> dict[str, Any]:
        """Mark explicit mesh edge indices as UV seams before angle-based/conformal unwrap."""
        return call("mark_mesh_seams", locals())

    @tool
    def create_armature(name: str, bones: list[BoneSpec]) -> dict[str, Any]:
        """Create editable armature bones with head/tail, hierarchy and connected/deform flags.

        Validates unique names, nonzero lengths, parents and hierarchy cycles
        before creating data. Returns the actual object name and bone names.
        """
        return call("create_armature", {"name": name, "bones": [bone.model_dump() for bone in bones]})

    @tool(read_only=True)
    def get_armature(armature_name: str) -> dict[str, Any]:
        """Inspect rest bones, parent hierarchy, deform flags and pose transforms."""
        return call("get_armature", locals())

    @tool
    def add_bone_constraint(armature_name: str, bone_name: str, type: str,
                              target_name: str | None = None, subtarget: str | None = None,
                              settings: dict[str, Any] | None = None) -> dict[str, Any]:
        """Add a real pose-bone constraint, including IK, COPY_TRANSFORMS and rotation limits.

        target_name references an object; subtarget names its bone. settings
        accepts Blender constraint RNA properties (e.g. chain_count for IK).
        Removes the newly created constraint if any setting is invalid.
        """
        return call("add_bone_constraint", locals())

    @tool
    def bind_armature(object_name: str, armature_name: str, weights: list[VertexWeight] | None = None,
                      automatic_weights: bool = False) -> dict[str, Any]:
        """Bind a mesh to an armature with automatic weights or explicit bone vertex weights.

        Explicit weights replace the listed vertices' weights in the named
        bone groups. Automatic weights use Blender's bone-heat operator and
        require suitable connected geometry. Choose one weighting mode.
        """
        return call("bind_armature", {"object_name": object_name, "armature_name": armature_name,
                    "weights": [weight.model_dump() for weight in weights or []], "automatic_weights": automatic_weights})

    @tool
    def pose_bones(armature_name: str, poses: list[BonePose], frame: float | None = None) -> dict[str, Any]:
        """Set bone-local pose transforms; optionally keyframe only supplied channels.

        Rotations use XYZ Euler radians; corresponding bones are switched to
        XYZ rotation mode. Use object constraints for mechanical/IK control.
        """
        return call("pose_bones", {"armature_name": armature_name, "frame": frame,
                    "poses": [pose.model_dump() for pose in poses]})

    @tool
    def set_shape_key_keyframes(object_name: str, shape_key: str, frames: list[float],
                                 values: list[float], interpolation: Literal["CONSTANT", "LINEAR", "BEZIER"] = "BEZIER") -> dict[str, Any]:
        """Animate a named shape key over multiple frames without generating Python."""
        return call("set_shape_key_keyframes", locals())

    @tool
    def set_shape_key(object_name: str, name: str, coordinates: list[tuple[float, float, float]],
                       value: float = 0.0) -> dict[str, Any]:
        """Create/update a relative shape key from absolute local vertex coordinates.

        Coordinates must match mesh vertex count. Creates Basis if missing;
        preserves topology. Use set_shape_key_keyframes to animate its value.
        """
        return call("set_shape_key", locals())

    @tool
    def configure_rigid_body(object_names: list[str], type: Literal["ACTIVE", "PASSIVE"] = "ACTIVE",
                              mass: float = 1.0, friction: float = 0.5, restitution: float = 0.0,
                              collision_shape: str = "CONVEX_HULL", kinematic: bool | None = None,
                              collision_margin: float | None = None,
                              collision_collections: list[int] | None = None) -> dict[str, Any]:
        """Add/update real rigid-body physics, margins, animated bodies and collision layers.

        collision_collections contains enabled zero-based layer indices 0-19;
        bodies need a shared collision layer to interact. Omitted optional
        settings preserve existing values. Use simplified collision geometry
        and applied scale for stable simulations.
        """
        return call("configure_rigid_body", locals())

    @tool
    def append_datablocks(path: str, kind: Literal["objects", "collections", "materials", "node_groups"] = "objects",
                           names: list[str] | None = None, link: bool = False) -> dict[str, Any]:
        """Append/link named assets from a local .blend library; names=None lists available names only.

        Appended objects/collections are linked into the active scene. Returns
        actual datablock names, including Blender collision suffixes.
        Appending/listing the current file uses a temporary copy of its last
        saved state; unsaved edits are not included. Linking into itself is rejected.
        """
        return call("append_datablocks", locals())

    @tool
    def save_checkpoint(path: str) -> dict[str, Any]:
        """Save a .blend copy checkpoint without changing the working file's path."""
        return call("save_checkpoint", locals())

    @tool
    def frame_viewport(object_names: list[str] | None = None,
                        shading: Literal["WIREFRAME", "SOLID", "MATERIAL", "RENDERED"] = "SOLID") -> dict[str, Any]:
        """Frame named/selected objects in a real 3D viewport and set its shading mode.

        Requires Blender UI; background mode returns an explicit error. Use
        frame_camera_to_object and render_preview for headless workflows.
        """
        return call("frame_viewport", locals())

    return functions


COMMAND_CODE = r'''
import bpy
import bmesh
import math
import os
import shutil
import tempfile
import fnmatch
from contextlib import contextmanager
from mathutils import Vector, Euler, Matrix

def require_object(name, type_=None):
    obj = bpy.data.objects.get(name)
    if obj is None or (type_ and obj.type != type_):
        raise ValueError(f"Object not found or wrong type: {name} ({type_ or 'any'})")
    return obj

def require_mesh(name):
    obj = require_object(name, "MESH")
    if obj.data.users > 1 or obj.library or obj.data.library:
        raise ValueError("Mesh is shared or linked; make an independent duplicate first")
    if obj.mode != "OBJECT":
        raise ValueError("Mesh operation requires Object Mode")
    return obj

def simple_object(obj):
    return {"name": obj.name, "type": obj.type, "location": list(obj.location),
            "rotation": list(obj.rotation_euler), "scale": list(obj.scale)}

def json_value(value):
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, bpy.types.ID):
        return {"name": value.name, "type": value.bl_rna.identifier}
    if isinstance(value, set):
        return sorted(value)
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    try:
        return [json_value(item) for item in value]
    except TypeError:
        return str(value)

def pointer_value(value):
    if isinstance(value, dict) and set(value) == {"id_type", "name"}:
        tables = {"OBJECT": bpy.data.objects, "MATERIAL": bpy.data.materials,
                  "IMAGE": bpy.data.images, "TEXTURE": bpy.data.textures,
                  "NODE_GROUP": bpy.data.node_groups, "WORLD": bpy.data.worlds}
        table = tables.get(value["id_type"])
        item = table.get(value["name"]) if table is not None else None
        if item is None:
            raise ValueError(f"Datablock pointer not found: {value}")
        return item
    return value

def property_at(root, path):
    parts = path.split(".")
    if any(not part.isidentifier() or part.startswith("_") or part in {"bl_rna", "rna_type"} for part in parts):
        raise ValueError(f"Expected an RNA property path: {path}")
    owner = root
    for part in parts[:-1]:
        prop = owner.bl_rna.properties.get(part)
        if prop is None or prop.type != "POINTER":
            raise ValueError(f"Not an RNA pointer property: {part}")
        owner = getattr(owner, part)
        if owner is None:
            raise ValueError(f"Unset pointer in property path: {path}")
    prop = owner.bl_rna.properties.get(parts[-1])
    if prop is None:
        raise ValueError(f"Unknown RNA property: {path}")
    return owner, parts[-1], prop

def rna_set(root, properties):
    changes = []
    for path, value in properties.items():
        owner, key, prop = property_at(root, path)
        if prop.is_readonly or prop.type == "COLLECTION":
            raise ValueError(f"Property is not writable: {path}")
        value = pointer_value(value)
        if prop.type == "BOOLEAN":
            if prop.is_array:
                if not isinstance(value, (list, tuple)) or len(value) != prop.array_length or any(type(item) is not bool for item in value):
                    raise ValueError(f"RNA boolean array requires true/false components of the expected length: {path}")
            elif type(value) is not bool:
                raise ValueError(f"RNA boolean requires true/false: {path}")
        if prop.type == "STRING" and not isinstance(value, str):
            raise ValueError(f"RNA string requires a string value: {path}")
        if prop.type == "ENUM":
            choices = {item.identifier for item in prop.enum_items}
            if prop.is_enum_flag:
                if not isinstance(value, (list, tuple, set)) or not all(isinstance(item, str) and item in choices for item in value):
                    raise ValueError(f"Invalid RNA enum flag value for {path}")
                value = set(value)
            elif not isinstance(value, str):
                raise ValueError(f"Invalid RNA enum value for {path}")
        if prop.type in {"INT", "FLOAT"}:
            values = [value]
            if prop.is_array:
                dimensions = [size for size in prop.array_dimensions if size]
                if not dimensions: dimensions = [prop.array_length]
                values = [value]
                for size in dimensions:
                    expanded = []
                    for row in values:
                        if not hasattr(row, "__len__") or isinstance(row, (str, bytes)) or len(row) != size:
                            raise ValueError(f"Wrong array shape for {path}; expected {dimensions}")
                        expanded.extend(row)
                    values = expanded
            if not all(isinstance(v, (int, float)) and math.isfinite(v) and prop.hard_min <= v <= prop.hard_max for v in values):
                raise ValueError(f"Out of range or nonfinite value for {path}")
        old = getattr(owner, key)
        if isinstance(old, Matrix):
            value = Matrix(value)
            old = old.copy()
        elif getattr(prop, "is_array", False):
            old = json_value(old)
        changes.append((owner, key, value, old))
    applied = []
    try:
        for owner, key, value, old in changes:
            setattr(owner, key, value)
            applied.append((owner, key, old))
    except Exception:
        for owner, key, old in reversed(applied):
            setattr(owner, key, old)
        raise
    return {path: json_value(getattr(property_at(root, path)[0], property_at(root, path)[1])) for path in properties}

def rna_target(params):
    target, name = params["target"], params.get("name")
    if target in {"OBJECT", "OBJECT_DATA", "MODIFIER", "CONSTRAINT", "POSE_BONE", "BONE_CONSTRAINT", "RIGID_BODY", "RIGID_BODY_CONSTRAINT", "VERTEX_GROUP"}:
        obj = require_object(name)
        if target == "OBJECT": return obj
        if target == "OBJECT_DATA":
            if obj.data is None: raise ValueError("Object has no data")
            return obj.data
        if target == "VERTEX_GROUP":
            group = obj.vertex_groups.get(params.get("member") or "")
            if group is None: raise ValueError("Vertex group not found")
            return group
        if target in {"RIGID_BODY", "RIGID_BODY_CONSTRAINT"}:
            item = obj.rigid_body if target == "RIGID_BODY" else obj.rigid_body_constraint
            if item is None: raise ValueError("Object has no requested physics data")
            return item
        if target in {"POSE_BONE", "BONE_CONSTRAINT"}:
            if obj.type != "ARMATURE": raise ValueError("Expected an armature")
            bone = obj.pose.bones.get(params.get("bone_name") if target == "BONE_CONSTRAINT" else params.get("member"))
            if bone is None: raise ValueError("Pose bone not found")
            if target == "POSE_BONE": return bone
            item = bone.constraints.get(params.get("member") or "")
            if item is None: raise ValueError("Bone constraint not found")
            return item
        table = obj.modifiers if target == "MODIFIER" else obj.constraints
        item = table.get(params.get("member") or "")
    elif target == "SCENE": return bpy.context.scene
    elif target == "RENDER": return bpy.context.scene.render
    elif target == "RIGID_BODY_WORLD":
        if bpy.context.scene.rigidbody_world is None: raise ValueError("No rigid-body world")
        return bpy.context.scene.rigidbody_world
    elif target == "MATERIAL": item = bpy.data.materials.get(name or "")
    elif target == "WORLD": item = bpy.data.worlds.get(name) if name else bpy.context.scene.world
    else: raise ValueError(f"Unknown RNA target: {target}")
    if item is None: raise ValueError(f"RNA target not found: {target}/{name}/{params.get('member')}")
    return item

def node_tree(target, name=None, create=False):
    if target == "NODE_GROUP":
        tree = bpy.data.node_groups.get(name or "")
    elif target in {"MATERIAL", "WORLD"}:
        table = bpy.data.materials if target == "MATERIAL" else bpy.data.worlds
        owner = table.get(name) if name else (bpy.context.scene.world if target == "WORLD" else None)
        if owner is None: raise ValueError(f"Node owner not found: {target}/{name}")
        if create: owner.use_nodes = True
        tree = owner.node_tree
    elif target == "COMPOSITOR":
        scene = bpy.context.scene
        if hasattr(scene, "compositing_node_group"):
            tree = scene.compositing_node_group
            if tree is None and create:
                tree = bpy.data.node_groups.new("Compositor", "CompositorNodeTree")
                scene.compositing_node_group = tree
        else:
            if create: scene.use_nodes = True
            tree = scene.node_tree
    else: raise ValueError(f"Unknown node target: {target}")
    if tree is None: raise ValueError("Node tree not found; create/enable it first")
    return tree

def socket_at(sockets, key):
    if isinstance(key, int) or str(key).isdigit():
        index = int(key)
        if not 0 <= index < len(sockets): raise ValueError(f"Socket index out of range: {index}")
        return sockets[index]
    matches = [socket for socket in sockets if socket.name == key or socket.identifier == key]
    if len(matches) == 1:
        return matches[0]
    available = [socket for socket in matches if socket.enabled and not getattr(socket, "is_unavailable", False)]
    if len(available) == 1:
        return available[0]
    candidates = [(index, socket.identifier, socket.enabled and not getattr(socket, "is_unavailable", False))
                  for index, socket in enumerate(sockets) if socket in matches]
    raise ValueError(f"Socket name is missing or ambiguous: {key}; use an index or unique identifier; candidates: {candidates}")

def socket_defaults(node, values):
    changes = []
    for key, value in values.items():
        socket = socket_at(node.inputs, key)
        if not hasattr(socket, "default_value"): raise ValueError(f"Socket has no default: {key}")
        old = socket.default_value
        old = tuple(old) if hasattr(old, "__len__") and not isinstance(old, str) else old
        changes.append((socket, pointer_value(value), old))
    done = []
    try:
        for socket, value, old in changes:
            socket.default_value = value
            done.append((socket, old))
    except Exception:
        for socket, old in reversed(done): socket.default_value = old
        raise

def tree_summary(tree):
    def sockets(items):
        return [{"index": i, "name": s.name, "identifier": s.identifier, "type": s.bl_idname,
                 "enabled": bool(s.enabled), "available": not getattr(s, "is_unavailable", False),
                 "default": json_value(s.default_value) if hasattr(s, "default_value") else None}
                for i, s in enumerate(items)]
    return {"name": tree.name, "type": tree.bl_idname,
            "nodes": [{"name": n.name, "type": n.bl_idname, "location": list(n.location),
                       "inputs": sockets(n.inputs), "outputs": sockets(n.outputs)} for n in tree.nodes],
            "links": [{"from_node": l.from_node.name, "from_socket": l.from_socket.identifier,
                       "to_node": l.to_node.name, "to_socket": l.to_socket.identifier} for l in tree.links]}

@contextmanager
def active_objects(objects, active=None):
    previous = [obj.name for obj in bpy.context.selected_objects]
    previous_active = bpy.context.view_layer.objects.active
    previous_mode = bpy.context.object.mode if bpy.context.object else "OBJECT"
    for obj in objects:
        if bpy.context.view_layer.objects.get(obj.name) is None:
            raise ValueError(f"Object is not in active view layer: {obj.name}")
    try:
        if previous_mode != "OBJECT": bpy.ops.object.mode_set(mode="OBJECT")
        bpy.ops.object.select_all(action="DESELECT")
        for obj in objects: obj.select_set(True)
        bpy.context.view_layer.objects.active = active or (objects[-1] if objects else None)
        yield
    finally:
        if bpy.context.object and bpy.context.object.mode != "OBJECT": bpy.ops.object.mode_set(mode="OBJECT")
        bpy.ops.object.select_all(action="DESELECT")
        for name in previous:
            obj = bpy.context.view_layer.objects.get(name)
            if obj: obj.select_set(True)
        if previous_active and bpy.context.view_layer.objects.get(previous_active.name):
            bpy.context.view_layer.objects.active = previous_active
            if previous_mode != "OBJECT": bpy.ops.object.mode_set(mode=previous_mode)

def dispatch_advanced(cmd, params):
    if cmd == "find_objects":
        limit, offset = params.get("limit", 100), params.get("offset", 0)
        if not 1 <= limit <= 1000 or offset < 0: raise ValueError("limit must be 1-1000 and offset nonnegative")
        collection = bpy.data.collections.get(params["collection"]) if params.get("collection") else None
        if params.get("collection") and collection is None: raise ValueError("Collection not found")
        objects = sorted([o for o in bpy.context.scene.objects
            if fnmatch.fnmatchcase(o.name, params.get("pattern", "*"))
            and (not params.get("object_type") or o.type == params["object_type"].upper())
            and (collection is None or o.name in collection.all_objects)], key=lambda o: o.name)
        return {"total": len(objects), "offset": offset, "objects": [simple_object(o) for o in objects[offset:offset+limit]],
                "next_offset": offset+limit if offset+limit < len(objects) else None}
    if cmd == "get_selection":
        obj = bpy.context.view_layer.objects.active
        return {"active": obj.name if obj else None, "selected": [o.name for o in bpy.context.selected_objects],
                "mode": bpy.context.mode}
    if cmd == "get_mesh_data":
        obj = require_object(params["object_name"], "MESH")
        offset, limit = params.get("offset", 0), params.get("limit", 1000)
        if offset < 0 or not 1 <= limit <= 10000: raise ValueError("offset must be nonnegative; limit 1-10000")
        evaluated = params.get("evaluated", False)
        source = obj.evaluated_get(bpy.context.evaluated_depsgraph_get()) if evaluated else obj
        mesh = source.to_mesh() if evaluated else obj.data
        try:
            domain = params.get("domain", "VERT")
            table = {"VERT": mesh.vertices, "EDGE": mesh.edges, "FACE": mesh.polygons}[domain]
            items = []
            for item in list(table)[offset:offset+limit]:
                entry = {"index": item.index}
                if domain == "VERT": entry.update(coordinate=list(item.co), normal=list(item.normal))
                if domain == "EDGE": entry.update(vertices=list(item.vertices), seam=item.use_seam)
                if domain == "FACE": entry.update(vertices=list(item.vertices), normal=list(item.normal), material_index=item.material_index)
                items.append(entry)
            return {"object": obj.name, "domain": domain, "evaluated": evaluated, "total": len(table), "items": items,
                    "next_offset": offset+limit if offset+limit < len(table) else None}
        finally:
            if evaluated: source.to_mesh_clear()
    if cmd == "assign_face_material":
        obj = require_mesh(params["object_name"])
        material = bpy.data.materials.get(params["material_name"])
        indices = params["face_indices"]
        if material is None or any(i < 0 or i >= len(obj.data.polygons) for i in indices): raise ValueError("Material or face indices are invalid")
        slot = next((index for index, item in enumerate(obj.data.materials) if item == material), None)
        if slot is None:
            slot = len(obj.data.materials)
            obj.data.materials.append(material)
        for i in indices: obj.data.polygons[i].material_index = slot
        return {"object": obj.name, "material": material.name, "slot": slot, "faces": len(indices)}
    if cmd in {"get_properties", "set_properties"}:
        root = rna_target(params)
        if cmd == "set_properties":
            world = bpy.context.scene.rigidbody_world
            physics_edit = params["target"].startswith("RIGID_BODY") or any(path.startswith("rigidbody_world.") for path in params["properties"])
            if physics_edit and world and world.point_cache.is_baked:
                raise ValueError("Free the rigid-body bake before editing physics settings")
            return {"properties": rna_set(root, params["properties"])}
        result = {}
        paths = params.get("paths") or [p.identifier for p in root.bl_rna.properties if p.type != "COLLECTION" and p.identifier not in {"rna_type"}]
        for path in paths:
            owner, key, prop = property_at(root, path)
            entry = {"type": prop.type, "read_only": prop.is_readonly, "description": prop.description,
                     "value": json_value(getattr(owner, key))}
            if prop.type in {"INT", "FLOAT"}: entry.update(min=prop.hard_min, max=prop.hard_max, array_length=prop.array_length,
                                                          array_dimensions=[size for size in prop.array_dimensions if size])
            if prop.type == "ENUM": entry["enum"] = [item.identifier for item in prop.enum_items]
            result[path] = entry
        return {"target": root.bl_rna.identifier, "properties": result}
    if cmd == "load_image":
        path = os.path.abspath(params["path"])
        if not os.path.isfile(path): raise FileNotFoundError(path)
        image = bpy.data.images.load(path, check_existing=True)
        if params.get("name"): image.name = params["name"]
        return {"image": image.name, "size": list(image.size), "path": image.filepath}
    if cmd == "create_node_group":
        sockets = params.get("sockets") or []
        pairs = [(s["direction"], s["name"]) for s in sockets]
        if len(set(pairs)) != len(pairs): raise ValueError("Interface socket names must be unique within direction")
        group = bpy.data.node_groups.new(params["name"], params["tree_type"])
        try:
            for spec in sockets:
                socket = group.interface.new_socket(name=spec["name"], in_out=spec["direction"], socket_type=spec["socket_type"])
                if spec.get("default") is not None: socket.default_value = pointer_value(spec["default"])
        except Exception:
            bpy.data.node_groups.remove(group)
            raise
        return {"name": group.name, "sockets": [{"name": s.name, "direction": s.in_out, "identifier": s.identifier}
            for s in group.interface.items_tree if s.item_type == "SOCKET"]}
    if cmd in {"get_node_tree", "edit_node_tree", "set_node_inputs"}:
        tree = node_tree(params["target"], params.get("name"), create=cmd == "edit_node_tree")
        if cmd == "get_node_tree": return tree_summary(tree)
        if cmd == "set_node_inputs":
            node = tree.nodes.get(params["node_name"])
            if node is None: raise ValueError("Node not found")
            socket_defaults(node, params["inputs"])
            return tree_summary(tree)
        specs = params["nodes"]
        names = [spec["name"] for spec in specs]
        if len(set(names)) != len(names): raise ValueError("Node names must be unique")
        old_nodes = list(tree.nodes)
        removed = old_nodes if params.get("clear") else [tree.nodes.get(name) for name in params.get("remove_nodes", [])]
        if any(node is None for node in removed): raise ValueError("A node to remove was not found")
        removed_names = {node.name for node in removed}
        if any(tree.nodes.get(name) and name not in removed_names for name in names): raise ValueError("Node name already exists; use set_node_inputs for existing nodes")
        lookup = {node.name: node for node in old_nodes if node.name not in removed_names}
        created = []
        original_links = [(link.from_socket, link.to_socket, link.is_muted) for link in tree.links]
        links_changed = False
        try:
            for spec in specs:
                node = tree.nodes.new(spec["type"])
                created.append(node)
                node.location = spec.get("location", [0, 0])
                rna_set(node, spec.get("properties") or {})
                socket_defaults(node, spec.get("inputs") or {})
                lookup[spec["name"]] = node
            pending_links = []
            for spec in params["links"]:
                source, dest = lookup.get(spec["from_node"]), lookup.get(spec["to_node"])
                if source is None or dest is None: raise ValueError("Link references a missing or removed node")
                output, input_ = socket_at(source.outputs, spec["from_socket"]), socket_at(dest.inputs, spec["to_socket"])
                if not output.enabled or not input_.enabled: raise ValueError("Link references a disabled socket")
                pending_links.append((output, input_))
            for output, input_ in pending_links:
                links_changed = True
                link = tree.links.new(output, input_)
                if not link.is_valid: raise ValueError("Incompatible node socket types")
        except Exception:
            # Link limits may already have deleted an earlier new link. Never
            # dereference those stale RNA handles while rolling back the graph.
            if links_changed:
                for link in list(tree.links): tree.links.remove(link)
            for node in created: tree.nodes.remove(node)
            if links_changed:
                for output, input_, muted in original_links:
                    link = tree.links.new(output, input_)
                    link.is_muted = muted
            raise
        # Free every final name before assigning any of them. Blender's default
        # names can otherwise collide even within this newly created node set.
        temporary_index = 0
        reserved_names = set(names)
        for node in created:
            temporary = f"__MCP_PENDING_{temporary_index}"
            while temporary in reserved_names or tree.nodes.get(temporary) is not None:
                temporary_index += 1
                temporary = f"__MCP_PENDING_{temporary_index}"
            node.name = temporary
        for node in removed: tree.nodes.remove(node)
        for spec, node in zip(specs, created): node.name = spec["name"]
        return tree_summary(tree)
    if cmd == "assign_geometry_nodes":
        obj = require_object(params["object_name"])
        group = bpy.data.node_groups.get(params["node_group"])
        if group is None or group.bl_idname != "GeometryNodeTree": raise ValueError("Geometry node group not found")
        socket_map = {}
        for socket in group.interface.items_tree:
            if socket.item_type == "SOCKET" and socket.in_out == "INPUT":
                socket_map[socket.identifier] = socket
                socket_map[socket.name] = socket
        values = []
        for key, value in (params.get("inputs") or {}).items():
            socket = socket_map.get(key)
            if socket is None or socket.socket_type == "NodeSocketGeometry": raise ValueError(f"Unknown editable group input: {key}")
            values.append((socket.identifier, pointer_value(value)))
        mod = obj.modifiers.get(params.get("modifier_name", "GeometryNodes"))
        if mod and mod.type != "NODES": raise ValueError("Named modifier is not a Geometry Nodes modifier")
        created = mod is None
        if created: mod = obj.modifiers.new(params.get("modifier_name", "GeometryNodes"), "NODES")
        previous_group = mod.node_group
        old = {}
        try:
            mod.node_group = group
            for key, value in values:
                old[key] = mod.get(key)
                mod[key] = value
            obj.update_tag()
        except Exception:
            if created: obj.modifiers.remove(mod)
            else:
                for key, value in old.items():
                    if value is None: del mod[key]
                    else: mod[key] = value
                mod.node_group = previous_group
            raise
        return {"object": obj.name, "modifier": mod.name, "node_group": group.name, "inputs": [key for key, _ in values]}
    if cmd == "mesh_edit":
        obj = require_mesh(params["object_name"])
        if obj.data.shape_keys: raise ValueError("Use shape-key tools instead of editing topology with shape keys")
        bm = bmesh.new()
        try:
            bm.from_mesh(obj.data)
            domain, op = params.get("domain", "FACE"), params["operation"]
            table = {"VERT": bm.verts, "EDGE": bm.edges, "FACE": bm.faces}[domain]
            table.ensure_lookup_table()
            indices = params.get("indices")
            if indices is not None and (len(set(indices)) != len(indices) or any(i < 0 or i >= len(table) for i in indices)): raise ValueError("Invalid or duplicate mesh indices")
            selected = list(table) if indices is None else [table[i] for i in indices]
            settings = params.get("settings") or {}
            allowed = {"EXTRUDE": {"offset"}, "INSET": {"thickness", "depth"}, "BEVEL": {"offset", "segments", "profile"},
                       "SUBDIVIDE": {"cuts", "smooth"}, "TRIANGULATE": set(), "DELETE": set(),
                       "TRANSFORM": {"translation", "rotation", "scale"}}[op]
            if set(settings) - allowed: raise ValueError(f"Unknown {op} settings: {set(settings) - allowed}")
            expected = {"EXTRUDE": "FACE", "INSET": "FACE", "BEVEL": "EDGE", "SUBDIVIDE": "EDGE", "TRIANGULATE": "FACE", "TRANSFORM": "VERT"}.get(op)
            if expected and expected != domain: raise ValueError(f"{op} requires {expected} domain")
            if op == "EXTRUDE":
                result = bmesh.ops.extrude_face_region(bm, geom=selected)
                verts = [v for v in result["geom"] if isinstance(v, bmesh.types.BMVert)]
                bmesh.ops.translate(bm, verts=verts, vec=Vector(settings.get("offset", [0, 0, 1])))
            elif op == "INSET": bmesh.ops.inset_region(bm, faces=selected, thickness=float(settings.get("thickness", 0.1)), depth=float(settings.get("depth", 0)))
            elif op == "BEVEL": bmesh.ops.bevel(bm, geom=selected, offset=float(settings.get("offset", 0.1)), segments=int(settings.get("segments", 1)), profile=float(settings.get("profile", 0.5)), affect="EDGES")
            elif op == "SUBDIVIDE": bmesh.ops.subdivide_edges(bm, edges=selected, cuts=int(settings.get("cuts", 1)), smooth=float(settings.get("smooth", 0)), use_grid_fill=True)
            elif op == "TRIANGULATE": bmesh.ops.triangulate(bm, faces=selected)
            elif op == "DELETE": bmesh.ops.delete(bm, geom=selected, context={"VERT": "VERTS", "EDGE": "EDGES", "FACE": "FACES"}[domain])
            elif op == "TRANSFORM":
                matrix = Matrix.Translation(Vector(settings.get("translation", [0, 0, 0]))) @ Euler(settings.get("rotation", [0, 0, 0])).to_matrix().to_4x4() @ Matrix.Diagonal(Vector((*settings.get("scale", [1, 1, 1]), 1)))
                bmesh.ops.transform(bm, verts=selected, matrix=matrix)
            if any(not all(math.isfinite(v) for v in vert.co) for vert in bm.verts): raise ValueError("Operation produced nonfinite geometry")
            bm.normal_update()
            bm.to_mesh(obj.data)
            obj.data.update()
        finally: bm.free()
        return {"object": obj.name, "operation": op, "vertices": len(obj.data.vertices), "edges": len(obj.data.edges), "faces": len(obj.data.polygons)}
    if cmd == "mark_mesh_seams":
        obj = require_mesh(params["object_name"])
        indices = params["edge_indices"]
        if any(i < 0 or i >= len(obj.data.edges) for i in indices): raise ValueError("Edge index out of range")
        if params.get("clear_existing"):
            for edge in obj.data.edges: edge.use_seam = False
        for i in indices: obj.data.edges[i].use_seam = True
        return {"object": obj.name, "seam_edges": sum(edge.use_seam for edge in obj.data.edges)}
    if cmd == "uv_unwrap":
        obj = require_mesh(params["object_name"])
        margin = float(params.get("margin", 0.02))
        angle = float(params.get("angle_limit", 1.151917))
        if not 0 <= margin <= 1 or not 0 <= angle <= math.pi: raise ValueError("Invalid UV margin or angle")
        with active_objects([obj]):
            bpy.ops.object.mode_set(mode="EDIT")
            bpy.ops.mesh.select_all(action="SELECT")
            if params.get("method", "SMART_PROJECT") == "SMART_PROJECT": bpy.ops.uv.smart_project(angle_limit=angle, island_margin=margin)
            else: bpy.ops.uv.unwrap(method=params["method"], margin=margin)
            bpy.ops.uv.select_all(action="SELECT")
            bpy.ops.uv.pack_islands(margin=margin)
        return {"object": obj.name, "uv_layers": [layer.name for layer in obj.data.uv_layers], "loops": len(obj.data.loops)}
    if cmd == "create_armature":
        specs = params["bones"]
        names = [s["name"] for s in specs]
        if not names or len(set(names)) != len(names): raise ValueError("Provide unique bone names")
        parents = {s["name"]: s.get("parent") for s in specs}
        for spec in specs:
            if not all(math.isfinite(v) for v in (*spec["head"], *spec["tail"])) or (Vector(spec["tail"]) - Vector(spec["head"])).length < 1e-6: raise ValueError("Bone has invalid head/tail")
            visited, parent = {spec["name"]}, spec.get("parent")
            while parent:
                if parent not in parents or parent in visited: raise ValueError("Invalid bone parent or hierarchy cycle")
                visited.add(parent)
                parent = parents[parent]
        arm = bpy.data.armatures.new(params["name"])
        obj = bpy.data.objects.new(params["name"], arm)
        bpy.context.collection.objects.link(obj)
        try:
            with active_objects([obj]):
                bpy.ops.object.mode_set(mode="EDIT")
                bones = {}
                for spec in specs:
                    bone = arm.edit_bones.new(spec["name"])
                    bone.head, bone.tail = spec["head"], spec["tail"]
                    bone.roll = spec.get("roll", 0)
                    bone.use_deform = spec.get("deform", True)
                    bones[spec["name"]] = bone
                for spec in specs:
                    bone = bones[spec["name"]]
                    if spec.get("parent"):
                        bone.parent = bones[spec["parent"]]
                        bone.use_connect = spec.get("connected", False)
        except Exception:
            bpy.data.objects.remove(obj, do_unlink=True)
            bpy.data.armatures.remove(arm)
            raise
        return {"name": obj.name, "bones": [bone.name for bone in arm.bones]}
    if cmd == "get_armature":
        obj = require_object(params["armature_name"], "ARMATURE")
        bpy.context.view_layer.update()
        return {"name": obj.name, "bones": [{"name": b.name, "parent": b.parent.name if b.parent else None,
            "head": list(b.head_local), "tail": list(b.tail_local), "deform": b.use_deform,
            "location": list(obj.pose.bones[b.name].location), "rotation_mode": obj.pose.bones[b.name].rotation_mode,
            "scale": list(obj.pose.bones[b.name].scale),
            "pose_head": list(obj.pose.bones[b.name].head), "pose_tail": list(obj.pose.bones[b.name].tail),
            "pose_matrix": [list(row) for row in obj.pose.bones[b.name].matrix],
            "constraints": [{"name": c.name, "type": c.type} for c in obj.pose.bones[b.name].constraints]} for b in obj.data.bones]}
    if cmd == "add_bone_constraint":
        obj = require_object(params["armature_name"], "ARMATURE")
        bone = obj.pose.bones.get(params["bone_name"])
        if bone is None: raise ValueError("Pose bone not found")
        properties = dict(params.get("settings") or {})
        if params.get("target_name") is not None: properties["target"] = {"id_type": "OBJECT", "name": params["target_name"]}
        if params.get("subtarget") is not None: properties["subtarget"] = params["subtarget"]
        constraint = bone.constraints.new(params["type"].upper())
        try: rna_set(constraint, properties)
        except Exception:
            bone.constraints.remove(constraint)
            raise
        return {"armature": obj.name, "bone": bone.name, "constraint": constraint.name, "type": constraint.type}
    if cmd == "bind_armature":
        obj = require_mesh(params["object_name"])
        arm = require_object(params["armature_name"], "ARMATURE")
        weights = params.get("weights") or []
        if params.get("automatic_weights") and weights: raise ValueError("Choose automatic or explicit weights")
        for spec in weights:
            if arm.data.bones.get(spec["bone"]) is None or not 0 <= spec["weight"] <= 1 or any(i < 0 or i >= len(obj.data.vertices) for i in spec["vertices"]): raise ValueError("Invalid bone weights")
            group = obj.vertex_groups.get(spec["bone"])
            if group and group.lock_weight: raise ValueError(f"Weight group is locked: {group.name}")
        if params.get("automatic_weights") and any(group.lock_weight and group.name in arm.data.bones for group in obj.vertex_groups):
            raise ValueError("Unlock deform groups before regenerating automatic weights")
        if params.get("automatic_weights"):
            with active_objects([obj, arm], arm): bpy.ops.object.parent_set(type="ARMATURE_AUTO")
        else:
            mod = next((m for m in obj.modifiers if m.type == "ARMATURE" and m.object == arm), None)
            if mod is None: mod = obj.modifiers.new("Armature", "ARMATURE")
            mod.object = arm
            for spec in weights:
                group = obj.vertex_groups.get(spec["bone"]) or obj.vertex_groups.new(name=spec["bone"])
                group.add(spec["vertices"], spec["weight"], "REPLACE")
        return {"object": obj.name, "armature": arm.name, "groups": [g.name for g in obj.vertex_groups]}
    if cmd == "pose_bones":
        obj = require_object(params["armature_name"], "ARMATURE")
        plans = []
        for spec in params["poses"]:
            bone = obj.pose.bones.get(spec["name"])
            if bone is None: raise ValueError(f"Pose bone not found: {spec['name']}")
            channels = {key: spec.get(key) for key in ("location", "rotation", "scale") if spec.get(key) is not None}
            if any(not all(math.isfinite(v) for v in value) for value in channels.values()): raise ValueError("Nonfinite pose values")
            plans.append((bone, channels))
        for bone, channels in plans:
            for key, value in channels.items():
                path = "rotation_euler" if key == "rotation" else key
                if key == "rotation": bone.rotation_mode = "XYZ"
                setattr(bone, path, value)
                if params.get("frame") is not None: bone.keyframe_insert(data_path=path, frame=params["frame"])
        bpy.context.view_layer.update()
        return {"armature": obj.name, "bones": [b.name for b, _ in plans], "frame": params.get("frame")}
    if cmd == "set_shape_key":
        obj = require_mesh(params["object_name"])
        coordinates = params["coordinates"]
        if len(coordinates) != len(obj.data.vertices) or any(len(co) != 3 or not all(math.isfinite(v) for v in co) for co in coordinates): raise ValueError("Shape coordinates must be finite XYZ triples matching vertex count")
        if not 0 <= params.get("value", 0) <= 1: raise ValueError("Shape key value must be 0-1")
        if params["name"] == "Basis": raise ValueError("Use a non-Basis shape key name")
        if obj.data.shape_keys is None: obj.shape_key_add(name="Basis", from_mix=False)
        key = obj.data.shape_keys.key_blocks.get(params["name"]) or obj.shape_key_add(name=params["name"], from_mix=False)
        for vertex, co in zip(key.data, coordinates): vertex.co = co
        key.value = params.get("value", 0)
        return {"object": obj.name, "shape_key": key.name, "value": key.value}
    if cmd == "set_shape_key_keyframes":
        obj = require_object(params["object_name"], "MESH")
        keys = obj.data.shape_keys
        key = keys.key_blocks.get(params["shape_key"]) if keys else None
        frames, values = params["frames"], params["values"]
        if key is None: raise ValueError("Shape key not found")
        if not frames or len(frames) != len(values) or len(frames) > 10000: raise ValueError("frames/values lengths must match, 1-10000 entries")
        if not all(math.isfinite(frame) for frame in frames) or not all(math.isfinite(value) and key.slider_min <= value <= key.slider_max for value in values): raise ValueError("Nonfinite frames or out-of-range shape values")
        previous, old_interpolation = key.value, bpy.context.preferences.edit.keyframe_new_interpolation_type
        try:
            bpy.context.preferences.edit.keyframe_new_interpolation_type = params["interpolation"]
            for frame, value in zip(frames, values):
                key.value = value
                key.keyframe_insert(data_path="value", frame=frame)
        finally:
            key.value = previous
            bpy.context.preferences.edit.keyframe_new_interpolation_type = old_interpolation
        return {"object": obj.name, "shape_key": key.name, "frames": frames, "interpolation": params["interpolation"]}
    if cmd == "configure_rigid_body":
        world = bpy.context.scene.rigidbody_world
        if world and world.point_cache.is_baked: raise ValueError("Free the rigid-body bake before editing bodies")
        objects = [require_mesh(name) for name in params["object_names"]]
        if not objects: raise ValueError("Provide at least one object")
        if params["mass"] <= 0 or not 0 <= params["friction"] <= 1 or not 0 <= params["restitution"] <= 1: raise ValueError("Invalid rigid body physical values")
        shape_options = {item.identifier for item in bpy.types.RigidBodyObject.bl_rna.properties["collision_shape"].enum_items}
        if params["collision_shape"] not in shape_options: raise ValueError("Unsupported collision shape")
        additional = {}
        if params.get("kinematic") is not None: additional["kinematic"] = params["kinematic"]
        if params.get("collision_margin") is not None:
            margin = params["collision_margin"]
            if not math.isfinite(margin) or margin < 0: raise ValueError("Invalid collision margin")
            additional.update(use_margin=True, collision_margin=margin)
        layers = params.get("collision_collections")
        if layers is not None:
            if not layers or any(not 0 <= layer < 20 for layer in layers): raise ValueError("Enable at least one collision layer in 0-19")
            additional["collision_collections"] = [index in layers for index in range(20)]
        for obj in objects:
            if obj.rigid_body is None:
                with active_objects([obj]): bpy.ops.rigidbody.object_add()
            rna_set(obj.rigid_body, {**{key: params[key] for key in ("type", "mass", "friction", "restitution", "collision_shape")}, **additional})
        return {"objects": [o.name for o in objects], "type": params["type"]}
    if cmd == "append_datablocks":
        path, kind = os.path.abspath(bpy.path.abspath(params["path"])), params["kind"]
        if not os.path.isfile(path) or not path.lower().endswith(".blend"): raise ValueError("Expected an existing .blend library")
        current = bool(bpy.data.filepath and os.path.isfile(bpy.data.filepath) and os.path.samefile(path, bpy.data.filepath))
        names, link = params.get("names"), params.get("link", False)
        if current and link and names is not None:
            raise ValueError("Cannot link from the current blend file into itself; use append (link=False) or a separate library")
        snapshot = None
        try:
            library_path = path
            if current:
                # Same directory preserves // paths in the saved library.
                fd, snapshot = tempfile.mkstemp(prefix=".blender-mcp-append-", suffix=".blend", dir=os.path.dirname(bpy.data.filepath))
                os.close(fd)
                shutil.copyfile(path, snapshot)
                library_path = snapshot
            with bpy.data.libraries.load(library_path, link=link) as (source, dest):
                available = list(getattr(source, kind))
                if names is None: return {"kind": kind, "available": available, "source_is_current_file": current}
                if not names or len(set(names)) != len(names) or any(name not in available for name in names): raise ValueError("Library datablock names are missing or duplicated")
                setattr(dest, kind, names)
        finally:
            if snapshot is not None:
                os.unlink(snapshot)
        loaded = getattr(dest, kind)
        if kind == "objects":
            for obj in loaded: bpy.context.collection.objects.link(obj)
        if kind == "collections":
            for collection in loaded: bpy.context.scene.collection.children.link(collection)
        return {"kind": kind, "names": [item.name for item in loaded], "linked": link, "source_is_current_file": current}
    if cmd == "save_checkpoint":
        path = os.path.abspath(params["path"])
        if not path.lower().endswith(".blend"): raise ValueError("Checkpoint path must end in .blend")
        bpy.ops.wm.save_as_mainfile(filepath=path, copy=True)
        return {"path": path, "working_file": bpy.data.filepath or None}
    if cmd == "frame_viewport":
        if bpy.app.background:
            raise RuntimeError("No visible 3D viewport in background mode; use camera framing and render_preview")
        objects = [require_object(name) for name in params["object_names"]] if params.get("object_names") else list(bpy.context.selected_objects)
        if not objects: raise ValueError("No objects to frame")
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == "VIEW_3D":
                    region = next((r for r in area.regions if r.type == "WINDOW"), None)
                    if region:
                        with active_objects(objects), bpy.context.temp_override(window=window, area=area, region=region):
                            area.spaces.active.shading.type = params["shading"]
                            bpy.ops.view3d.view_selected(use_all_regions=False)
                        return {"framed": [o.name for o in objects], "shading": params["shading"]}
        raise RuntimeError("No 3D viewport; use camera framing and render_preview in background mode")
    return dispatch_production(cmd, params)
'''
