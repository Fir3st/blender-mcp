"""Collision, physics and production rigging tools; runtime lives in Blender."""

from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from blender_mcp_local.advanced import BoneSpec, InputModel, RnaTarget


class DriverTarget(InputModel):
    id_type: Literal["OBJECT", "MATERIAL", "SCENE", "WORLD", "SHAPE_KEYS"] = "OBJECT"
    name: str
    data_path: str | None = None
    bone_name: str = ""
    transform_type: str = "LOC_X"
    transform_space: Literal["WORLD_SPACE", "TRANSFORM_SPACE", "LOCAL_SPACE"] = "WORLD_SPACE"


class DriverVariable(InputModel):
    name: str
    type: Literal["SINGLE_PROP", "TRANSFORMS", "LOC_DIFF", "ROTATION_DIFF"] = "SINGLE_PROP"
    targets: list[DriverTarget]


def register_tools(mcp: FastMCP, native_call) -> dict[str, Any]:
    functions = {}

    def serialize(value):
        if isinstance(value, BaseModel): return value.model_dump()
        if isinstance(value, list): return [serialize(item) for item in value]
        if isinstance(value, dict): return {key: serialize(item) for key, item in value.items()}
        return value

    def call(command, arguments):
        return native_call(command, {key: serialize(value) for key, value in arguments.items() if key != "call"})

    def tool(fn=None, *, read_only=False):
        def register(function):
            functions[function.__name__] = function
            return mcp.tool(annotations=ToolAnnotations(
                readOnlyHint=read_only, destructiveHint=not read_only, openWorldHint=False,
            ))(function)
        return register(fn) if fn is not None else register

    @tool
    def set_mode(object_name: str, mode: Literal["OBJECT", "EDIT", "POSE", "WEIGHT_PAINT", "VERTEX_PAINT", "SCULPT", "TEXTURE_PAINT"] = "OBJECT") -> dict[str, Any]:
        """Activate/select an object and switch Blender mode explicitly.

        Flushes an existing edit mode through Object Mode before switching.
        Restores prior selection/active object/mode if the requested switch
        fails. Use OBJECT before BMesh, rest-armature and weight cleanup tools.
        """
        return call("set_mode", locals())

    @tool
    def create_collision_proxy(object_name: str, shape: Literal["BOX", "SPHERE", "CAPSULE", "CONVEX_HULL"] = "CONVEX_HULL",
                                 engine: Literal["GENERIC", "UNREAL", "UNITY"] = "GENERIC", name: str | None = None,
                                 margin: float = 0.0, axis: Literal["X", "Y", "Z"] = "Z", segments: int = 16,
                                 evaluated: bool = True, parent: bool = False,
                                 reference_object: str | None = None) -> dict[str, Any]:
        """Build a separate watertight collision mesh from source geometry with applied scale.

        BOX/SPHERE/CAPSULE enclose source vertices; CONVEX_HULL uses a real
        BMesh hull (margin radially pads it). CAPSULE axis is source rotation's
        local X/Y/Z. Proxies use COL_Collision_[source], wire display and hidden
        rendering. Unreal names use UBX/USP/UCP/UCX_[source]_NN. Origin aligns
        with source. Regenerate after mesh edits; parent=true follows transforms.
        reference_object optionally supplies the render asset's name/origin/
        rotation/parent instead of the geometry source (manual compound pieces).
        """
        return call("create_collision_proxy", locals())

    @tool
    def create_collision_proxies(object_name: str, engine: Literal["GENERIC", "UNREAL", "UNITY"] = "GENERIC",
                                   evaluated: bool = True, margin: float = 0.0, parent: bool = False,
                                   reference_object: str | None = None) -> dict[str, Any]:
        """Create one convex hull per disconnected mesh island in a collision collection.

        This is loose-part hull generation, not automatic convex decomposition
        of a connected concave mesh. All generated pieces are removed if any
        island cannot form a 3D convex hull. Returns actual names for export.
        reference_object associates manual geometry pieces with a render asset.
        """
        return call("create_collision_proxies", locals())

    @tool(read_only=True)
    def audit_collision_proxies(object_names: list[str] | None = None, tolerance: float = 1e-5) -> dict[str, Any]:
        """Check tagged/named colliders for closed topology, convexity, positive volume,
        finite geometry, applied scale, source-origin alignment and engine naming.
        An omitted list audits active-scene proxies made by these tools.
        """
        return call("audit_collision_proxies", locals())

    @tool(read_only=True)
    def detect_collisions(object_names: list[str], evaluated: bool = True, tolerance: float = 1e-6) -> dict[str, Any]:
        """Query world-space mesh intersections/containment for 2-100 objects using BVH.

        Includes containment of closed meshes, not just intersecting triangles.
        Open meshes do not define enclosing volumes. This geometric query does not
        perform physics response; use rigid bodies and bake_physics for that.
        """
        return call("detect_collisions", locals())

    @tool
    def create_rigid_body_constraint(name: str, object_a: str, object_b: str,
                                      type: Literal["FIXED", "POINT", "HINGE", "SLIDER", "PISTON", "GENERIC", "GENERIC_SPRING", "MOTOR"] = "FIXED",
                                      location: tuple[float, float, float] | None = None,
                                      rotation: tuple[float, float, float] = (0, 0, 0),
                                      settings: dict[str, Any] | None = None) -> dict[str, Any]:
        """Create a real rigid-body joint between existing physics bodies.

        Omitted location uses their origin midpoint. Rotation defines the
        joint axes (hinge uses local Z). settings exposes limits, springs,
        motors, disable_collisions and breaking_threshold via Blender RNA.
        """
        return call("create_rigid_body_constraint", locals())

    @tool
    def configure_physics_world(substeps_per_frame: int = 10, solver_iterations: int = 10,
                                  time_scale: float = 1.0, frame_start: int = 1, frame_end: int = 250,
                                  use_split_impulse: bool = False) -> dict[str, Any]:
        """Create/configure rigid-body solver and cache range. Free an existing bake before changing it."""
        return call("configure_physics_world", locals())

    @tool
    def bake_physics(action: Literal["BAKE", "FREE"] = "BAKE") -> dict[str, Any]:
        """Bake/free only the active scene's rigid-body point cache, preserving current frame.

        Uses the range from configure_physics_world; refuses empty worlds and
        ranges over 10000 frames. Reports Blender's actual is_baked flag.
        """
        return call("bake_physics", locals())

    @tool(read_only=True)
    def get_physics_state(object_names: list[str] | None = None) -> dict[str, Any]:
        """Inspect rigid-body solver/cache and evaluated world transforms at the current frame.

        Evaluated transforms reflect real simulation, unlike original location
        properties that may stay at their starting values.
        """
        return call("get_physics_state", locals())

    @tool
    def edit_armature(armature_name: str, bones: list[BoneSpec], remove_bones: list[str] | None = None) -> dict[str, Any]:
        """Add/update full rest-bone specs (head/tail/roll/parent/deform), or explicitly remove bones.

        Validates the final hierarchy before editing. Unmentioned bones remain.
        Bone specs are complete replacements, not partial property patches.
        Shared/linked armature data and non-Object Mode input are rejected.
        """
        return call("edit_armature", locals())

    @tool
    def mirror_armature(armature_name: str, direction: Literal["NEGATIVE_X", "POSITIVE_X"] = "NEGATIVE_X") -> dict[str, Any]:
        """Symmetrize rest bones using Blender's left/right naming conventions, preserving selection/mode.

        NEGATIVE_X copies negative X to positive X; POSITIVE_X the reverse.
        Does not mirror vertex weights; use transfer/weight tools separately.
        """
        return call("mirror_armature", locals())

    @tool
    def configure_ik(armature_name: str, bone_name: str, target_name: str, pole_target_name: str,
                       chain_length: int = 2, pole_angle: float = 0.0, target_bone: str = "",
                       pole_bone: str = "", use_stretch: bool = False, name: str = "IK") -> dict[str, Any]:
        """Create/update a named IK constraint with explicit target, pole and chain length.

        Validates the ancestor chain and optional target/pole bones before
        modifying the rig. Pole angle is radians. Does not invent controllers
        or infer limb anatomy; create the desired controls with object tools.
        """
        return call("configure_ik", locals())

    @tool
    def remove_bone_constraint(armature_name: str, bone_name: str, constraint_name: str) -> dict[str, Any]:
        """Remove a named pose-bone constraint without disabling other rig systems."""
        return call("remove_bone_constraint", locals())

    @tool(read_only=True)
    def get_vertex_weights(object_name: str, armature_name: str | None = None,
                             offset: int = 0, limit: int = 100) -> dict[str, Any]:
        """Read paginated deform-bone influences and sums per vertex, including locked group flags."""
        return call("get_vertex_weights", locals())

    @tool(read_only=True)
    def audit_rig(object_name: str, armature_name: str | None = None, max_influences: int = 4,
                     tolerance: float = 1e-4, include_indices: bool = False) -> dict[str, Any]:
        """Audit skinning for unweighted vertices, invalid weights, normalization and influence limits.

        Uses deform bones only; unrelated mask groups are preserved/ignored.
        Infers an armature only when one unambiguous armature modifier exists.
        """
        return call("audit_rig", locals())

    @tool
    def normalize_weights(object_name: str, armature_name: str | None = None, max_influences: int = 4,
                             prune_threshold: float = 1e-4, vertex_indices: list[int] | None = None) -> dict[str, Any]:
        """Prune/limit/normalize deform weights, preserving locked groups and unrelated masks.

        Plans all edits first. Impossible locked-weight limits fail preflight.
        Does not invent missing influences: unweighted/unresolvable vertices
        remain unchanged and are explicitly reported. Defaults to 4 influences.
        """
        return call("normalize_weights", locals())

    @tool
    def transfer_weights(source_name: str, target_name: str, armature_name: str | None = None,
                            method: Literal["NEAREST_VERTEX", "NEAREST_SURFACE"] = "NEAREST_SURFACE",
                            max_influences: int = 4, max_distance: float | None = None) -> dict[str, Any]:
        """Transfer real deform weights by world-space nearest vertex or barycentric nearest surface.

        Uses original/rest geometry, not the currently deformed pose. Keeps
        unrelated groups, rejects locked target deform groups, and attaches
        the source armature. Distance misses/unweighted samples are reported
        without fabricating weights or erasing those target vertices.
        """
        return call("transfer_weights", locals())

    @tool
    def bake_animation(object_names: list[str], frame_start: int, frame_end: int, step: int = 1,
                           bake_types: list[Literal["OBJECT", "POSE"]] | None = None,
                           clear_constraints: bool = False) -> dict[str, Any]:
        """Bake evaluated object/bone motion to new actions with Blender visual keying.

        Use POSE for IK/constraint rigs and OBJECT for object constraints.
        clear_constraints=true removes live constraints after baking for
        playback/export; false retains them. Preserves current frame/selection.
        """
        return call("bake_animation", locals())

    @tool
    def add_driver(target: RnaTarget, data_path: str, variables: list[DriverVariable],
                       expression: str = "var", driver_type: Literal["SCRIPTED", "SUM", "AVERAGE", "MIN", "MAX"] = "SCRIPTED",
                       name: str | None = None, member: str | None = None, bone_name: str | None = None,
                       index: int = -1, replace: bool = False) -> dict[str, Any]:
        """Create a Blender driver using typed targets and restricted arithmetic expressions.

        Supports single-property, transform and distance/rotation variables.
        SCRIPTED expressions allow variables, pi, arithmetic and
        sin/cos/tan/asin/acos/atan/sqrt/abs/min/max. No Python statements,
        attributes, imports or arbitrary calls. Array properties require an
        explicit index. Existing drivers require replace=true.
        """
        return call("add_driver", locals())

    @tool(read_only=True)
    def list_drivers(target: RnaTarget, name: str | None = None, member: str | None = None,
                         bone_name: str | None = None) -> dict[str, Any]:
        """Inspect drivers scoped to the selected RNA owner, including variables and validity."""
        return call("list_drivers", locals())

    @tool
    def remove_driver(target: RnaTarget, data_path: str, name: str | None = None,
                          member: str | None = None, bone_name: str | None = None, index: int = -1) -> dict[str, Any]:
        """Remove a property driver through Blender's driver_remove API."""
        return call("remove_driver", locals())

    return functions


COMMAND_CODE = r'''
import ast
from mathutils.bvhtree import BVHTree
from mathutils.kdtree import KDTree
from mathutils.geometry import barycentric_transform

def mesh_geometry(obj, evaluated=True):
    source = obj.evaluated_get(bpy.context.evaluated_depsgraph_get()) if evaluated else obj
    mesh = source.to_mesh() if evaluated else obj.data
    try:
        vertices = [source.matrix_world @ vertex.co for vertex in mesh.vertices]
        faces = [list(face.vertices) for face in mesh.polygons]
        edges = [list(edge.vertices) for edge in mesh.edges]
        return vertices, faces, edges
    finally:
        if evaluated: source.to_mesh_clear()

def collision_basis(obj):
    location, rotation, _scale = obj.matrix_world.decompose()
    return Matrix.Translation(location) @ rotation.to_matrix().to_4x4()

def collision_name(source, shape, engine, requested=None):
    prefix = {"BOX": "UBX", "SPHERE": "USP", "CAPSULE": "UCP", "CONVEX_HULL": "UCX"}[shape] if engine == "UNREAL" else "COL"
    root = f"{prefix}_{source.name}_"
    if requested:
        if engine == "UNREAL" and not requested.startswith(root): raise ValueError(f"Unreal collider name must start with {root}")
        if bpy.data.objects.get(requested): raise ValueError("Collision object name already exists")
        return requested
    index = 0
    while bpy.data.objects.get(f"{root}{index:02d}"): index += 1
    return f"{root}{index:02d}"

def build_collision(source, points, shape, engine, margin=0, axis="Z", segments=16, name=None, parent=False):
    if not points or not math.isfinite(margin) or margin < 0 or not 8 <= segments <= 128:
        raise ValueError("Invalid collision points, margin or segments (8-128)")
    basis = collision_basis(source)
    inverse = basis.inverted()
    local = [inverse @ point for point in points]
    minimum = Vector(tuple(min(point[i] for point in local) for i in range(3)))
    maximum = Vector(tuple(max(point[i] for point in local) for i in range(3)))
    center = (minimum + maximum) / 2
    name = collision_name(source, shape, engine, name)
    parent_inverse = source.matrix_world.inverted() if parent else None
    collection_name = f"COL_Collision_{source.name}"
    existing_collection = bpy.data.collections.get(collection_name)
    if existing_collection and existing_collection.library: raise ValueError("Collision collection is linked/read-only")
    bm = bmesh.new()
    try:
        if shape == "BOX":
            half = (maximum - minimum) / 2 + Vector((margin, margin, margin))
            if min(half) <= 1e-8: raise ValueError("Box collider must have nonzero size on each axis")
            bmesh.ops.create_cube(bm, size=2)
            for vertex in bm.verts: vertex.co = center + Vector(tuple(vertex.co[i] * half[i] for i in range(3)))
        elif shape == "SPHERE":
            radius = max((point-center).length for point in local) + margin
            if radius <= 1e-8: raise ValueError("Sphere collider has zero radius")
            latitude = max(4, segments//2)
            radius /= math.cos(math.pi/segments)*math.cos(math.pi/(2*latitude))
            bmesh.ops.create_uvsphere(bm, u_segments=segments, v_segments=latitude, radius=radius)
            for vertex in bm.verts: vertex.co += center
        elif shape == "CAPSULE":
            ai = "XYZ".index(axis)
            perpendicular = [i for i in range(3) if i != ai]
            radial = [math.hypot(*(point[i]-center[i] for i in perpendicular)) for point in local]
            radius = max(radial) + margin
            if radius <= 1e-8: raise ValueError("Capsule collider has zero radial extent")
            half = max(0, max(abs(point[ai]-center[ai]) - math.sqrt(max(0, radius*radius-r*r)) for point, r in zip(local, radial)))
            hemi = max(2, segments//4)
            radius /= math.cos(math.pi/segments)*math.cos(math.pi/(4*hemi))
            coords = [[0, 0, -half-radius]]
            for side in (-1, 1):
                for j in range(hemi):
                    angle = -math.pi/2+(j+1)*math.pi/(2*hemi) if side < 0 else j*math.pi/(2*hemi)
                    z, r = side*half+radius*math.sin(angle), radius*math.cos(angle)
                    coords.extend([[r*math.cos(2*math.pi*k/segments), r*math.sin(2*math.pi*k/segments), z] for k in range(segments)])
            coords.append([0, 0, half+radius])
            verts = []
            for coordinate in coords:
                point = center.copy()
                point[perpendicular[0]] += coordinate[0]
                point[perpendicular[1]] += coordinate[1]
                point[ai] += coordinate[2]
                verts.append(bm.verts.new(point))
            rings = 2*hemi
            for k in range(segments): bm.faces.new((verts[0], verts[1+(k+1)%segments], verts[1+k]))
            for ring in range(rings-1):
                start, next_ = 1+ring*segments, 1+(ring+1)*segments
                for k in range(segments):
                    following = (k+1)%segments
                    bm.faces.new((verts[start+k], verts[start+following], verts[next_+following], verts[next_+k]))
            start = 1+(rings-1)*segments
            for k in range(segments): bm.faces.new((verts[start+k], verts[start+(k+1)%segments], verts[-1]))
            bmesh.ops.remove_doubles(bm, verts=list(bm.verts), dist=1e-8)
            bmesh.ops.dissolve_degenerate(bm, edges=list(bm.edges), dist=1e-8)
        elif shape == "CONVEX_HULL":
            for point in local: bm.verts.new(point+(point-center).normalized()*margin if margin else point)
            bmesh.ops.remove_doubles(bm, verts=list(bm.verts), dist=1e-8)
            bmesh.ops.convex_hull(bm, input=list(bm.verts), use_existing_faces=False)
            unused = [vertex for vertex in bm.verts if not vertex.link_faces]
            if unused: bmesh.ops.delete(bm, geom=unused, context="VERTS")
        else: raise ValueError("Unsupported collider shape")
        bmesh.ops.recalc_face_normals(bm, faces=list(bm.faces))
        bm.normal_update()
        volume = bm.calc_volume(signed=True)
        if abs(volume) <= 1e-12 or any(not edge.is_manifold for edge in bm.edges):
            raise ValueError("Source geometry cannot form a closed, nonzero-volume collider")
        if volume < 0: bmesh.ops.reverse_faces(bm, faces=list(bm.faces))
        mesh = bpy.data.meshes.new(name)
        try: bm.to_mesh(mesh)
        except Exception:
            bpy.data.meshes.remove(mesh)
            raise
    finally: bm.free()
    obj = bpy.data.objects.new(name, mesh)
    collection = bpy.data.collections.get(collection_name)
    if collection is None:
        collection = bpy.data.collections.new(collection_name)
        bpy.context.scene.collection.children.link(collection)
    elif not any(collection == child for child in bpy.context.scene.collection.children):
        # Existing nested scene collections are already usable.
        def in_tree(root): return root == collection or any(in_tree(child) for child in root.children)
        if not in_tree(bpy.context.scene.collection): bpy.context.scene.collection.children.link(collection)
    collection.objects.link(obj)
    obj.matrix_world = basis
    if parent:
        obj.parent = source
        obj.matrix_parent_inverse = parent_inverse
        obj.matrix_world = basis
    # Matrix decomposition can leave tiny float scale residuals on rotated proxies.
    obj.scale = (1, 1, 1)
    obj.display_type, obj.hide_render = "WIRE", True
    obj["collision_source"], obj["collision_shape"], obj["collision_engine"] = source.name, shape, engine
    return {"name": obj.name, "source": source.name, "shape": shape, "engine": engine,
            "collection": collection.name, "vertices": len(mesh.vertices), "faces": len(mesh.polygons)}

def skin_data(object_name, armature_name=None):
    obj = require_object(object_name, "MESH")
    if armature_name:
        arm = require_object(armature_name, "ARMATURE")
    else:
        arms = {mod.object for mod in obj.modifiers if mod.type == "ARMATURE" and mod.object}
        if len(arms) != 1: raise ValueError("Provide armature_name unless exactly one armature modifier identifies it")
        arm = arms.pop()
    deform = {bone.name for bone in arm.data.bones if bone.use_deform}
    groups = {group.index: group for group in obj.vertex_groups if group.name in deform}
    rows = [{weight.group: weight.weight for weight in vertex.groups if weight.group in groups and weight.weight != 0}
            for vertex in obj.data.vertices]
    return obj, arm, groups, rows

def weight_audit(obj, groups, rows, max_influences=4, tolerance=1e-4, include_indices=False):
    if not 1 <= max_influences <= 32 or not 0 < tolerance < 1: raise ValueError("Invalid influence limit/tolerance")
    issues = {"unweighted": [], "unnormalized": [], "over_limit": [], "invalid_weights": []}
    for index, row in enumerate(rows):
        total = sum(row.values())
        if total <= tolerance: issues["unweighted"].append(index)
        elif abs(total-1) > tolerance: issues["unnormalized"].append(index)
        if len(row) > max_influences: issues["over_limit"].append(index)
        if any(not math.isfinite(value) or not 0 <= value <= 1 for value in row.values()): issues["invalid_weights"].append(index)
    result = {"object": obj.name, "ok": not any(issues.values()), "vertices": len(rows),
              "deform_groups": [group.name for group in groups.values()], "issues": {name: len(indices) for name, indices in issues.items()},
              "max_influences_found": max((len(row) for row in rows), default=0)}
    if include_indices: result["indices"] = {name: indices[:100] for name, indices in issues.items()}
    return result

def apply_skin_rows(obj, groups, plans):
    # Leave locked groups and unrelated masks untouched.
    unlocked = [index for index, group in groups.items() if not group.lock_weight]
    for vertex, row in plans:
        for index in unlocked: groups[index].remove([vertex])
        for index, value in row.items():
            if not groups[index].lock_weight and value > 0: groups[index].add([vertex], value, "REPLACE")

def armature_editable(name):
    obj = require_object(name, "ARMATURE")
    if obj.mode != "OBJECT" or obj.data.users > 1 or obj.library or obj.data.library:
        raise ValueError("Armature must be local, unshared and in Object Mode")
    return obj

def driver_id(spec):
    tables = {"OBJECT": bpy.data.objects, "MATERIAL": bpy.data.materials, "SCENE": bpy.data.scenes,
              "WORLD": bpy.data.worlds, "SHAPE_KEYS": bpy.data.shape_keys}
    item = tables[spec["id_type"]].get(spec["name"])
    if item is None: raise ValueError(f"Driver target not found: {spec['name']}")
    return item

def expression_valid(expression, variables):
    if len(expression) > 1000: raise ValueError("Driver expression is too long")
    tree = ast.parse(expression, mode="eval")
    funcs = {"sin", "cos", "tan", "asin", "acos", "atan", "sqrt", "abs", "min", "max"}
    allowed = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant, ast.Name, ast.Load, ast.Call,
               ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.Mod, ast.UAdd, ast.USub)
    nodes = list(ast.walk(tree))
    if len(nodes) > 128: raise ValueError("Driver expression is too complex")
    for node in nodes:
        if not isinstance(node, allowed): raise ValueError("Only restricted arithmetic driver expressions are supported")
        if isinstance(node, ast.Name) and node.id not in set(variables)|funcs|{"pi"}: raise ValueError(f"Unknown driver name: {node.id}")
        if isinstance(node, ast.Call) and (not isinstance(node.func, ast.Name) or node.func.id not in funcs or node.keywords): raise ValueError("Unsupported driver function")
        if isinstance(node, ast.Constant) and (not isinstance(node.value, (int, float)) or not math.isfinite(node.value)): raise ValueError("Driver constants must be finite numbers")

def driver_summary(curve):
    driver = curve.driver
    return {"data_path": curve.data_path, "index": curve.array_index, "type": driver.type,
            "expression": driver.expression, "valid": driver.is_valid, "simple_expression": driver.is_simple_expression,
            "variables": [{"name": var.name, "type": var.type, "targets": [
                {"id_type": target.id_type, "name": target.id.name if target.id else None,
                 "data_path": target.data_path, "bone_name": target.bone_target,
                 "transform_type": target.transform_type, "transform_space": target.transform_space}
                for target in var.targets]} for var in driver.variables]}

def write_driver(driver, type_, expression, planned):
    for var in list(driver.variables): driver.variables.remove(var)
    driver.type = type_
    driver.use_self = False
    for spec, targets in planned:
        var = driver.variables.new(); var.name, var.type = spec["name"], spec["type"]
        for destination, (item, target) in zip(var.targets, targets):
            if spec["type"] == "SINGLE_PROP": destination.id_type = "KEY" if target["id_type"] == "SHAPE_KEYS" else target["id_type"]
            destination.id = item
            if spec["type"] == "SINGLE_PROP": destination.data_path = target["data_path"]
            else:
                destination.bone_target = target.get("bone_name", "")
                destination.transform_type = target.get("transform_type", "LOC_X")
                destination.transform_space = target.get("transform_space", "WORLD_SPACE")
    if type_ == "SCRIPTED": driver.expression = expression

def connected_islands(count, edges):
    neighbors = [set() for _ in range(count)]
    for a, b in edges: neighbors[a].add(b); neighbors[b].add(a)
    remaining, islands = set(range(count)), []
    while remaining:
        stack, indices = [remaining.pop()], []
        while stack:
            index = stack.pop(); indices.append(index)
            unseen = neighbors[index] & remaining
            remaining.difference_update(unseen); stack.extend(unseen)
        islands.append(indices)
    return islands

def dispatch_production(cmd, params):
    if cmd == "set_mode":
        obj = require_object(params["object_name"])
        if bpy.context.view_layer.objects.get(obj.name) is None: raise ValueError("Object is not in active view layer")
        previous, active = list(bpy.context.selected_objects), bpy.context.view_layer.objects.active
        old_mode = bpy.context.object.mode if bpy.context.object else "OBJECT"
        try:
            if old_mode != "OBJECT": bpy.ops.object.mode_set(mode="OBJECT")
            bpy.ops.object.select_all(action="DESELECT")
            obj.select_set(True); bpy.context.view_layer.objects.active = obj
            result = bpy.ops.object.mode_set(mode=params.get("mode", "OBJECT"))
            if "FINISHED" not in result: raise RuntimeError("Blender mode switch failed")
        except Exception:
            if bpy.context.object and bpy.context.object.mode != "OBJECT": bpy.ops.object.mode_set(mode="OBJECT")
            bpy.ops.object.select_all(action="DESELECT")
            for selected in previous: selected.select_set(True)
            bpy.context.view_layer.objects.active = active
            if active and old_mode != "OBJECT": bpy.ops.object.mode_set(mode=old_mode)
            raise
        return {"active": obj.name, "mode": obj.mode, "context_mode": bpy.context.mode}
    if cmd in {"create_collision_proxy", "create_collision_proxies"}:
        obj = require_object(params["object_name"], "MESH")
        reference = require_object(params["reference_object"], "MESH") if params.get("reference_object") else obj
        points, _faces, edges = mesh_geometry(obj, params.get("evaluated", True))
        if cmd == "create_collision_proxy":
            result = build_collision(reference, points, params.get("shape", "CONVEX_HULL"), params.get("engine", "GENERIC"),
                params.get("margin", 0), params.get("axis", "Z"), params.get("segments", 16), params.get("name"), params.get("parent", False))
            result["geometry_source"] = obj.name
            return result
        islands = connected_islands(len(points), edges)
        existing_collection = bpy.data.collections.get(f"COL_Collision_{reference.name}")
        results = []
        try:
            for island in islands:
                results.append(build_collision(reference, [points[i] for i in island], "CONVEX_HULL", params.get("engine", "GENERIC"),
                    params.get("margin", 0), parent=params.get("parent", False)))
        except Exception:
            for result in results:
                proxy = bpy.data.objects[result["name"]]
                mesh = proxy.data
                bpy.data.objects.remove(proxy, do_unlink=True)
                bpy.data.meshes.remove(mesh)
            collection = bpy.data.collections.get(f"COL_Collision_{reference.name}")
            if existing_collection is None and collection is not None and not collection.objects and not collection.children:
                bpy.data.collections.remove(collection)
            raise
        return {"source": reference.name, "geometry_source": obj.name, "proxies": results, "components": len(islands)}
    if cmd == "audit_collision_proxies":
        bpy.context.view_layer.update()
        tolerance = params.get("tolerance", 1e-5)
        if not 0 < tolerance < 1: raise ValueError("Invalid collision audit tolerance")
        objects = [require_object(name, "MESH") for name in params["object_names"]] if params.get("object_names") is not None else [obj for obj in bpy.context.scene.objects if obj.type == "MESH" and obj.get("collision_shape")]
        results = []
        for obj in objects:
            bm = bmesh.new()
            try:
                bm.from_mesh(obj.data); bm.normal_update()
                points = [vertex.co for vertex in bm.verts]
                nonconvex = any(any((point-face.verts[0].co).dot(face.normal) > tolerance for point in points) for face in bm.faces)
                issues = []
                if not bm.faces or any(not edge.is_manifold for edge in bm.edges): issues.append("non_watertight")
                if bm.calc_volume(signed=True) <= tolerance**3: issues.append("nonpositive_volume")
                if nonconvex: issues.append("nonconvex")
                if any(not all(math.isfinite(v) for v in point) for point in points): issues.append("nonfinite_geometry")
                if any(abs(component-1) > tolerance for component in obj.scale): issues.append("unapplied_scale")
                source = bpy.data.objects.get(obj.get("collision_source", ""))
                if source is None: issues.append("missing_source")
                elif (source.matrix_world.translation-obj.matrix_world.translation).length > tolerance: issues.append("origin_mismatch")
                if obj.get("collision_engine") == "UNREAL" and source:
                    prefix = {"BOX": "UBX", "SPHERE": "USP", "CAPSULE": "UCP", "CONVEX_HULL": "UCX"}.get(obj.get("collision_shape"))
                    if not prefix or not obj.name.startswith(f"{prefix}_{source.name}_"): issues.append("engine_name_mismatch")
                results.append({"name": obj.name, "ok": not issues, "issues": issues, "vertices": len(bm.verts),
                                "faces": len(bm.faces), "volume": bm.calc_volume(signed=True)})
            finally: bm.free()
        return {"ok": all(result["ok"] for result in results), "objects": results}
    if cmd == "detect_collisions":
        names = params["object_names"]
        tolerance = params.get("tolerance", 1e-6)
        if not 2 <= len(names) <= 100 or len(set(names)) != len(names) or not 0 < tolerance < 1: raise ValueError("Provide 2-100 unique objects and a positive tolerance below 1")
        meshes = []
        for name in names:
            obj = require_object(name, "MESH")
            vertices, faces, edges = mesh_geometry(obj, params.get("evaluated", True))
            if not vertices or not faces: raise ValueError("Collision query requires mesh faces")
            counts = {}
            for face in faces:
                for a, b in zip(face, face[1:]+face[:1]):
                    edge = tuple(sorted((a, b))); counts[edge] = counts.get(edge, 0)+1
            closed = bool(counts) and all(count == 2 for count in counts.values())
            minimum = tuple(min(v[i] for v in vertices) for i in range(3))
            maximum = tuple(max(v[i] for v in vertices) for i in range(3))
            islands = connected_islands(len(vertices), edges)
            meshes.append((name, vertices, BVHTree.FromPolygons(vertices, faces, epsilon=tolerance), closed, minimum, maximum, islands))
        def inside(point, tree):
            nearest = tree.find_nearest(point)
            if nearest[0] is not None and nearest[3] <= tolerance: return 1  # surface contact
            direction = Vector((1, 0.37139, 0.52917)).normalized()
            origin, hits = point.copy(), 0
            for _ in range(10000):
                location, _normal, _index, _distance = tree.ray_cast(origin, direction)
                if location is None: return 2 if hits % 2 else 0  # strict inside vs outside
                hits += 1; origin = location + direction*(tolerance*4)
            raise ValueError("Containment ray exceeded intersection limit")
        def volume_relation(source, target):
            if not target[3]: return False, False, False
            contained, interior, contact = False, False, False
            for island in source[6]:
                points = [source[1][index] for index in island]
                fits_bounds = all(min(point[i] for point in points) >= target[4][i]-tolerance and max(point[i] for point in points) <= target[5][i]+tolerance for i in range(3))
                states = [inside(point, target[2]) for point in points] if fits_bounds else [inside(points[0], target[2])]
                contained |= fits_bounds and all(state > 0 for state in states)
                interior |= any(state == 2 for state in states)
                contact |= any(state == 1 for state in states)
                center = sum(points, Vector()) / len(points)
                # Centroids distinguish overlapping coplanar boxes; a concave
                # mesh's center is only a sample if it is inside its own volume.
                if source[3] and inside(center, source[2]) == 2:
                    state = inside(center, target[2])
                    interior |= state == 2
                    contact |= state == 1
            return contained, interior, contact
        pairs = []
        for index, a in enumerate(meshes):
            for b in meshes[index+1:]:
                if any(a[5][i]+tolerance < b[4][i] or b[5][i]+tolerance < a[4][i] for i in range(3)): continue
                overlaps = a[2].overlap(b[2])
                if overlaps:
                    kind = "SURFACE_INTERSECTION"
                else:
                    ra, rb = volume_relation(a, b), volume_relation(b, a)
                    kind = "CONTAINMENT" if ra[0] or rb[0] else "VOLUME_OVERLAP" if ra[1] or rb[1] else "TOUCHING" if ra[2] or rb[2] else None
                if kind: pairs.append({"a": a[0], "b": b[0], "kind": kind, "overlap_pairs": len(overlaps)})
        return {"pairs": pairs, "tested_objects": len(meshes), "surface_only_objects": [mesh[0] for mesh in meshes if not mesh[3]]}
    if cmd == "create_rigid_body_constraint":
        world = bpy.context.scene.rigidbody_world
        if world and world.point_cache.is_baked: raise ValueError("Free the rigid-body bake before adding constraints")
        a, b = require_object(params["object_a"], "MESH"), require_object(params["object_b"], "MESH")
        if a == b or a.rigid_body is None or b.rigid_body is None: raise ValueError("Constraint requires two distinct rigid bodies")
        obj = bpy.data.objects.new(params["name"], None)
        bpy.context.collection.objects.link(obj)
        obj.empty_display_type = "ARROWS"
        obj.location = params.get("location") or (a.matrix_world.translation+b.matrix_world.translation)/2
        obj.rotation_euler = params.get("rotation", [0, 0, 0])
        try:
            with active_objects([obj]): bpy.ops.rigidbody.constraint_add(type=params.get("type", "FIXED"))
            rna_set(obj.rigid_body_constraint, {**(params.get("settings") or {}), "object1": a, "object2": b})
        except Exception:
            bpy.data.objects.remove(obj, do_unlink=True)
            raise
        return {"name": obj.name, "type": obj.rigid_body_constraint.type, "object_a": a.name, "object_b": b.name}
    if cmd == "configure_physics_world":
        scene = bpy.context.scene
        start, end = params.get("frame_start", 1), params.get("frame_end", 250)
        if start < 1 or end < start or end-start > 9999: raise ValueError("Physics cache range must contain 1-10000 positive frames")
        if scene.rigidbody_world is None: bpy.ops.rigidbody.world_add()
        world = scene.rigidbody_world
        if world.point_cache.is_baked: raise ValueError("Free the rigid-body bake before changing solver/cache settings")
        rna_set(world, {"substeps_per_frame": params.get("substeps_per_frame", 10), "solver_iterations": params.get("solver_iterations", 10),
                        "time_scale": params.get("time_scale", 1), "use_split_impulse": params.get("use_split_impulse", False),
                        "point_cache.frame_start": start, "point_cache.frame_end": end})
        return {"substeps_per_frame": world.substeps_per_frame, "solver_iterations": world.solver_iterations, "frame_start": start, "frame_end": end}
    if cmd == "bake_physics":
        scene, world = bpy.context.scene, bpy.context.scene.rigidbody_world
        if world is None: raise ValueError("No rigid-body world")
        cache = world.point_cache
        frame, subframe = scene.frame_current, scene.frame_subframe
        if params.get("action", "BAKE") == "BAKE":
            if world.collection is None or not any(obj.rigid_body for obj in world.collection.all_objects): raise ValueError("No rigid bodies to bake")
            if cache.frame_end-cache.frame_start > 9999: raise ValueError("Physics bake exceeds 10000 frames")
        try:
            with bpy.context.temp_override(scene=scene, point_cache=cache):
                if params.get("action", "BAKE") == "FREE": bpy.ops.ptcache.free_bake()
                else:
                    scene.frame_set(cache.frame_start)
                    bpy.ops.ptcache.bake(bake=True)
            expected = params.get("action", "BAKE") == "BAKE"
            if cache.is_baked != expected: raise RuntimeError("Blender did not complete the requested cache operation")
        finally: scene.frame_set(frame, subframe=subframe)
        return {"is_baked": cache.is_baked, "frame_start": cache.frame_start, "frame_end": cache.frame_end}
    if cmd == "get_physics_state":
        world = bpy.context.scene.rigidbody_world
        names = params.get("object_names")
        objects = [require_object(name) for name in names] if names is not None else [obj for obj in bpy.context.scene.objects if obj.rigid_body]
        if len(objects) > 1000: raise ValueError("Inspect at most 1000 physics objects at once")
        bpy.context.view_layer.update()
        depsgraph = bpy.context.evaluated_depsgraph_get()
        bodies = []
        for obj in objects:
            if obj.rigid_body is None: raise ValueError(f"No rigid body: {obj.name}")
            evaluated = obj.evaluated_get(depsgraph)
            bodies.append({"name": obj.name, "type": obj.rigid_body.type, "collision_shape": obj.rigid_body.collision_shape,
                "collision_collections": [i for i, enabled in enumerate(obj.rigid_body.collision_collections) if enabled],
                "location": list(evaluated.matrix_world.translation), "matrix_world": [list(row) for row in evaluated.matrix_world]})
        return {"frame": bpy.context.scene.frame_current, "is_baked": world.point_cache.is_baked if world else False, "objects": bodies}
    if cmd == "edit_armature":
        obj = armature_editable(params["armature_name"])
        specs, removed = params.get("bones", []), params.get("remove_bones") or []
        names = [spec["name"] for spec in specs]
        if len(set(names)) != len(names) or len(set(removed)) != len(removed) or set(names)&set(removed): raise ValueError("Duplicate/conflicting bone edits")
        current = {bone.name: {"name": bone.name, "head": list(bone.head_local), "tail": list(bone.tail_local), "parent": bone.parent.name if bone.parent else None}
                   for bone in obj.data.bones}
        if any(name not in current for name in removed): raise ValueError("Removed bone does not exist")
        final = {name: spec for name, spec in current.items() if name not in removed}
        final.update({spec["name"]: spec for spec in specs})
        for spec in final.values():
            if not all(math.isfinite(value) for value in (*spec["head"], *spec["tail"], spec.get("roll", 0))) or (Vector(spec["tail"])-Vector(spec["head"])).length < 1e-6: raise ValueError("Invalid rest-bone geometry")
            seen, parent = {spec["name"]}, spec.get("parent")
            while parent:
                if parent not in final or parent in seen: raise ValueError("Invalid final bone hierarchy")
                seen.add(parent); parent = final[parent].get("parent")
            connected = spec.get("connected", obj.data.bones[spec["name"]].use_connect if spec["name"] in obj.data.bones else False)
            if connected:
                if not spec.get("parent"): raise ValueError("Connected bone requires a parent")
                if (Vector(spec["tail"])-Vector(final[spec["parent"]]["tail"])).length < 1e-6: raise ValueError("Connected edit would collapse a bone")
        with active_objects([obj]):
            bpy.ops.object.mode_set(mode="EDIT")
            bones = obj.data.edit_bones
            for name in removed: bones.remove(bones[name])
            for spec in specs:
                bone = bones.get(spec["name"]) or bones.new(spec["name"])
                bone.use_connect = False
                bone.parent = None
                bone.head, bone.tail, bone.roll = spec["head"], spec["tail"], spec.get("roll", 0)
                bone.use_deform = spec.get("deform", True)
            for spec in specs:
                bone = bones[spec["name"]]
                bone.parent = bones.get(spec.get("parent") or "")
                bone.use_connect = spec.get("connected", False)
        return {"armature": obj.name, "bones": [bone.name for bone in obj.data.bones], "removed": removed}
    if cmd == "mirror_armature":
        obj = armature_editable(params["armature_name"])
        with active_objects([obj]):
            bpy.ops.object.mode_set(mode="EDIT")
            bpy.ops.armature.select_all(action="SELECT")
            bpy.ops.armature.symmetrize(direction=params.get("direction", "NEGATIVE_X"))
        return {"armature": obj.name, "bones": [bone.name for bone in obj.data.bones]}
    if cmd in {"configure_ik", "remove_bone_constraint"}:
        obj = require_object(params["armature_name"], "ARMATURE")
        bone = obj.pose.bones.get(params["bone_name"])
        if bone is None: raise ValueError("Pose bone not found")
        if cmd == "remove_bone_constraint":
            constraint = bone.constraints.get(params["constraint_name"])
            if constraint is None: raise ValueError("Bone constraint not found")
            bone.constraints.remove(constraint)
            return {"removed": params["constraint_name"], "armature": obj.name, "bone": bone.name}
        target, pole = require_object(params["target_name"]), require_object(params["pole_target_name"])
        chain, ancestor = 0, bone
        while ancestor: chain += 1; ancestor = ancestor.parent
        length = params.get("chain_length", 2)
        if not 1 <= length <= chain: raise ValueError("IK chain length exceeds available ancestors")
        for target_obj, subtarget in ((target, params.get("target_bone", "")), (pole, params.get("pole_bone", ""))):
            if subtarget and (target_obj.type != "ARMATURE" or target_obj.data.bones.get(subtarget) is None): raise ValueError("IK target bone not found")
        name = params.get("name", "IK")
        constraint = bone.constraints.get(name)
        if constraint and constraint.type != "IK": raise ValueError("Named constraint is not IK")
        created = constraint is None
        if created: constraint = bone.constraints.new("IK"); constraint.name = name
        try:
            rna_set(constraint, {"target": target, "pole_target": pole, "subtarget": params.get("target_bone", ""),
                "pole_subtarget": params.get("pole_bone", ""), "chain_count": length, "pole_angle": params.get("pole_angle", 0),
                "use_stretch": params.get("use_stretch", False)})
        except Exception:
            if created: bone.constraints.remove(constraint)
            raise
        bpy.context.view_layer.update()
        return {"armature": obj.name, "bone": bone.name, "constraint": constraint.name, "target": target.name, "pole": pole.name, "chain_length": length}
    if cmd in {"get_vertex_weights", "audit_rig", "normalize_weights"}:
        obj, arm, groups, rows = skin_data(params["object_name"], params.get("armature_name"))
        if cmd == "get_vertex_weights":
            offset, limit = params.get("offset", 0), params.get("limit", 100)
            if offset < 0 or not 1 <= limit <= 1000: raise ValueError("Invalid weights page")
            return {"object": obj.name, "armature": arm.name, "total": len(rows), "vertices": [
                {"index": index, "sum": sum(rows[index].values()), "weights": [{"bone": groups[key].name, "weight": value, "locked": groups[key].lock_weight} for key, value in rows[index].items()]}
                for index in range(offset, min(len(rows), offset+limit))]}
        if cmd == "audit_rig": return weight_audit(obj, groups, rows, params.get("max_influences", 4), params.get("tolerance", 1e-4), params.get("include_indices", False))
        require_mesh(obj.name)
        maximum, threshold = params.get("max_influences", 4), params.get("prune_threshold", 1e-4)
        if not 1 <= maximum <= 32 or not 0 <= threshold < 1: raise ValueError("Invalid weight cleanup settings")
        indices = params.get("vertex_indices")
        indices = list(range(len(rows))) if indices is None else indices
        if len(set(indices)) != len(indices) or any(i < 0 or i >= len(rows) for i in indices): raise ValueError("Invalid vertex indices")
        plans, unresolved = [], []
        for index in indices:
            row = rows[index]
            if any(not math.isfinite(value) or not 0 <= value <= 1 for value in row.values()): raise ValueError("Invalid source weights")
            locked = {key: value for key, value in row.items() if groups[key].lock_weight}
            total = sum(locked.values())
            if len(locked) > maximum or total > 1+1e-6: raise ValueError("Locked weights cannot satisfy requested influence/normalization limits")
            choices = sorted([(key, value) for key, value in row.items() if not groups[key].lock_weight and value >= threshold], key=lambda pair: (-pair[1], groups[pair[0]].name))[:maximum-len(locked)]
            remaining = max(0, 1-total)
            if remaining > 1e-6 and not choices: unresolved.append(index); continue
            subtotal = sum(value for _, value in choices)
            new = {**locked, **{key: value*remaining/subtotal for key, value in choices if subtotal > 0 and remaining > 0}}
            plans.append((index, new))
        apply_skin_rows(obj, groups, plans)
        _, _, groups, rows = skin_data(obj.name, arm.name)
        return {"changed_vertices": len(plans), "unresolved_count": len(unresolved), "unresolved_indices": unresolved[:100], "audit": weight_audit(obj, groups, rows, maximum)}
    if cmd == "transfer_weights":
        bpy.context.view_layer.update()
        source, arm, source_groups, rows = skin_data(params["source_name"], params.get("armature_name"))
        target = require_mesh(params["target_name"])
        if source == target: raise ValueError("Weight source and target must differ")
        maximum, distance = params.get("max_influences", 4), params.get("max_distance")
        if not 1 <= maximum <= 32 or (distance is not None and (not math.isfinite(distance) or distance <= 0)): raise ValueError("Invalid transfer settings")
        deform = {bone.name for bone in arm.data.bones if bone.use_deform}
        if any(group.lock_weight and group.name in deform for group in target.vertex_groups): raise ValueError("Target deform groups are locked")
        if any(mod.type == "ARMATURE" and mod.object not in {None, arm} for mod in target.modifiers): raise ValueError("Target is bound to another armature")
        coordinates = [source.matrix_world @ vertex.co for vertex in source.data.vertices]
        if not coordinates: raise ValueError("Weight source has no vertices")
        method = params.get("method", "NEAREST_SURFACE")
        if method == "NEAREST_VERTEX":
            tree = KDTree(len(coordinates))
            for i, coordinate in enumerate(coordinates): tree.insert(coordinate, i)
            tree.balance()
        else:
            source.data.calc_loop_triangles()
            triangles = [list(triangle.vertices) for triangle in source.data.loop_triangles if triangle.area > 1e-12]
            if not triangles: raise ValueError("Surface weight transfer requires nondegenerate faces")
            tree = BVHTree.FromPolygons(coordinates, triangles, all_triangles=True)
        plans, misses = [], []
        for vertex in target.data.vertices:
            point = target.matrix_world @ vertex.co
            blended = {}
            if method == "NEAREST_VERTEX":
                _co, index, separation = tree.find(point)
                blended = dict(rows[index])
            else:
                co, _normal, triangle_index, separation = tree.find_nearest(point)
                if co is None: misses.append(vertex.index); continue
                triangle = triangles[triangle_index]
                a, b, c = [coordinates[index] for index in triangle]
                factors = barycentric_transform(co, a, b, c, Vector((1, 0, 0)), Vector((0, 1, 0)), Vector((0, 0, 1)))
                for index, factor in zip(triangle, factors):
                    for key, value in rows[index].items(): blended[key] = blended.get(key, 0)+value*max(0, factor)
            if distance is not None and separation > distance: misses.append(vertex.index); continue
            best = sorted(blended.items(), key=lambda item: (-item[1], source_groups[item[0]].name))[:maximum]
            total = sum(value for _, value in best)
            if total <= 1e-8: misses.append(vertex.index); continue
            plans.append((vertex.index, {source_groups[key].name: value/total for key, value in best if value > 0}))
        groups = {}
        for bone in {bone for _, row in plans for bone in row}:
            group = target.vertex_groups.get(bone) or target.vertex_groups.new(name=bone)
            groups[bone] = group
        target_groups = {group.index: group for group in target.vertex_groups if group.name in deform}
        apply_skin_rows(target, target_groups, [(index, {groups[bone].index: value for bone, value in row.items()}) for index, row in plans])
        mod = next((mod for mod in target.modifiers if mod.type == "ARMATURE"), None) or target.modifiers.new("Armature", "ARMATURE")
        mod.object = arm
        return {"source": source.name, "target": target.name, "armature": arm.name, "transferred_vertices": len(plans), "missed_vertices": len(misses), "missed_indices": misses[:100]}
    if cmd == "bake_animation":
        objects = [require_object(name) for name in params["object_names"]]
        start, end, step = params["frame_start"], params["frame_end"], params.get("step", 1)
        types = set(params.get("bake_types") or ["OBJECT", "POSE"])
        if not objects or start < 0 or end < start or not 1 <= step <= 120 or (end-start)//step > 9999: raise ValueError("Invalid animation bake range")
        if not types <= {"OBJECT", "POSE"} or (types == {"POSE"} and any(obj.type != "ARMATURE" for obj in objects)): raise ValueError("POSE baking requires armatures")
        scene, frame, subframe = bpy.context.scene, bpy.context.scene.frame_current, bpy.context.scene.frame_subframe
        try:
            with active_objects(objects):
                result = bpy.ops.nla.bake(frame_start=start, frame_end=end, step=step, only_selected=False,
                    visual_keying=True, clear_constraints=params.get("clear_constraints", False),
                    clear_parents=False, use_current_action=False, bake_types=types)
                if "FINISHED" not in result: raise RuntimeError("Blender animation bake did not finish")
        finally: scene.frame_set(frame, subframe=subframe)
        return {"objects": [{"name": obj.name, "action": obj.animation_data.action.name if obj.animation_data and obj.animation_data.action else None} for obj in objects],
                "frame_start": start, "frame_end": end, "step": step, "cleared_constraints": params.get("clear_constraints", False)}
    if cmd in {"add_driver", "list_drivers", "remove_driver"}:
        owner = rna_target(params)
        id_data = owner.id_data
        prefix = "" if isinstance(owner, bpy.types.ID) else owner.path_from_id()
        curves = list(id_data.animation_data.drivers) if id_data.animation_data else []
        if cmd == "list_drivers":
            return {"drivers": [driver_summary(curve) for curve in curves if not prefix or curve.data_path.startswith(prefix+".") or curve.data_path.startswith(prefix+"[")]}
        path, index = params["data_path"], params.get("index", -1)
        if "__" in path: raise ValueError("Private driver property paths are not allowed")
        value = owner.path_resolve(path)
        full_path = (prefix+("" if path.startswith("[") else ".") if prefix else "")+path
        if cmd == "remove_driver": return {"removed": owner.driver_remove(path, index), "data_path": full_path}
        if hasattr(value, "__len__") and not isinstance(value, str) and not 0 <= index < len(value): raise ValueError("Array driver requires an explicit valid index")
        if isinstance(value, (int, float, bool, str)) and index != -1:
            raise ValueError("Scalar driver property does not accept an array index; use index=-1")
        specs = params["variables"]
        names = [spec["name"] for spec in specs]
        if len(set(names)) != len(names) or any(not name.isidentifier() or not name[0].isalpha() for name in names): raise ValueError("Invalid driver variable names")
        if set(names) & {"pi", "sin", "cos", "tan", "asin", "acos", "atan", "sqrt", "abs", "min", "max"}: raise ValueError("Driver variable shadows a reserved function/constant")
        if params.get("driver_type", "SCRIPTED") == "SCRIPTED": expression_valid(params.get("expression", "var"), names)
        planned = []
        for spec in specs:
            expected = 2 if spec["type"] in {"LOC_DIFF", "ROTATION_DIFF"} else 1
            if len(spec["targets"]) != expected: raise ValueError("Wrong number of driver targets")
            targets = []
            for target in spec["targets"]:
                item = driver_id(target)
                if spec["type"] == "SINGLE_PROP":
                    if not target.get("data_path") or "__" in target["data_path"]: raise ValueError("Provide a public single-property driver path")
                    source_value = item.path_resolve(target["data_path"])
                    if not isinstance(source_value, (int, float, bool)): raise ValueError("Single-property driver targets must resolve to a numeric scalar; index arrays explicitly")
                else:
                    if not isinstance(item, bpy.types.Object): raise ValueError("Transform driver targets must be objects")
                    if target.get("bone_name") and (item.type != "ARMATURE" or item.pose.bones.get(target["bone_name"]) is None): raise ValueError("Driver bone target not found")
                    choices = {entry.identifier for entry in bpy.types.DriverTarget.bl_rna.properties["transform_type"].enum_items}
                    if target.get("transform_type", "LOC_X") not in choices: raise ValueError("Unsupported driver transform type")
                targets.append((item, target))
            planned.append((spec, targets))
        existing = next((curve for curve in curves if curve.data_path == full_path and curve.array_index == max(0, index)), None)
        if existing and not params.get("replace", False): raise ValueError("Driver already exists; use replace=true")
        try:
            curve = owner.driver_add(path, index)
        except (TypeError, ValueError) as error:
            raise ValueError("Blender rejected driver creation for this property/index; verify animatability and RNA path") from error
        if isinstance(curve, list): raise ValueError("Expected one driver curve")
        old = driver_summary(curve)
        old_plan = [(spec, [(target.id, data) for target, data in zip(var.targets, spec["targets"])]) for var, spec in zip(curve.driver.variables, old["variables"])]
        old_self = curve.driver.use_self
        try:
            driver = curve.driver
            write_driver(driver, params.get("driver_type", "SCRIPTED"), params.get("expression", "var"), planned)
            id_data.update_tag(); bpy.context.view_layer.update()
        except Exception:
            if existing is None: owner.driver_remove(path, index)
            else:
                write_driver(curve.driver, old["type"], old["expression"], old_plan)
                curve.driver.use_self = old_self
            raise
        return driver_summary(curve)
    raise ValueError(f"Unknown Blender command: {cmd}")
'''
