"""Run: python tests/run_blender_integration.py --blender /path/to/blender."""

import argparse
import asyncio
import base64
import json
import hashlib
from pathlib import Path
import queue
import struct
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from mcp.shared.memory import create_connected_server_and_client_session
from blender_mcp_local import server
from blender_mcp_local.error_log import ErrorJournal


class BlenderIntegrationTests(unittest.TestCase):
    connection = None
    output = None

    def cmd(self, command, **arguments):
        return self.connection.send_command(command, arguments)

    def test_geometry_uv_and_atomic_mesh_failure(self):
        name = self.cmd("create_object", name="MeshTest", type="cube")["name"]
        before = self.cmd("get_object_info", name=name)["mesh"]
        with self.assertRaises(RuntimeError):
            self.cmd("mesh_edit", object_name=name, operation="INSET", domain="FACE", indices=[999], settings={})
        self.assertEqual(self.cmd("get_object_info", name=name)["mesh"], before)
        result = self.cmd("mesh_edit", object_name=name, operation="INSET", domain="FACE", indices=[0], settings={"thickness": 0.1})
        self.assertGreater(result["faces"], before["polygons"])
        self.cmd("mark_mesh_seams", object_name=name, edge_indices=[0, 1], clear_existing=True)
        selection = self.cmd("get_selection")
        uv = self.cmd("uv_unwrap", object_name=name, method="SMART_PROJECT", margin=0.02, angle_limit=1.15)
        self.assertTrue(uv["uv_layers"])
        self.assertEqual(self.cmd("get_selection"), selection)
        converted = self.cmd("convert_object_to_mesh", object_name=name, new_name="MeshCopy", apply_modifiers=True, keep_original=True)
        self.assertGreater(converted["counts"]["vertices"], 0)
        self.assertEqual(self.cmd("audit_mesh", object_names=[name])["object_count"], 1)
        page = self.cmd("get_mesh_data", object_name=name, domain="FACE", offset=0, limit=2)
        self.assertEqual(len(page["items"]), 2)
        self.assertEqual(page["next_offset"], 2)
        self.cmd("create_material", name="FaceMaterial", color=[0.8, 0.2, 0.1, 1])
        assigned = self.cmd("assign_face_material", object_name=name, material_name="FaceMaterial", face_indices=[0])
        self.assertEqual(self.cmd("get_mesh_data", object_name=name, domain="FACE", offset=0, limit=1)["items"][0]["material_index"], assigned["slot"])

    def test_all_bmesh_edit_operations(self):
        cases = [
            ("EXTRUDE", "FACE", [0], {"offset": [0, 0, 0.5]}),
            ("BEVEL", "EDGE", None, {"offset": 0.1, "segments": 2}),
            ("SUBDIVIDE", "EDGE", None, {"cuts": 1}),
            ("TRIANGULATE", "FACE", None, {}),
            ("TRANSFORM", "VERT", None, {"translation": [1, 0, 0], "rotation": [0, 0, 0.2], "scale": [1, 2, 1]}),
            ("DELETE", "FACE", [0], {}),
        ]
        for operation, domain, indices, settings in cases:
            with self.subTest(operation=operation):
                name = self.cmd("create_object", name=f"Edit{operation}", type="cube")["name"]
                result = self.cmd("mesh_edit", object_name=name, operation=operation, domain=domain, indices=indices, settings=settings)
                self.assertGreater(result["vertices"], 0)
                self.assertEqual(result["operation"], operation)

    def test_clean_mesh_welds_duplicates_and_recalculates_normals(self):
        name = self.cmd("create_mesh", name="Zabry | uzavreny objem 3 P", vertices=[
            [-1, -1, -1], [-1, -1, 1], [-1, 1, -1], [-1, 1, 1],
            [1, -1, -1], [1, -1, 1], [1, 1, -1], [1, 1, 1],
            [-0.999995, -1, -1],
        ], faces=[[8, 4, 5, 1], [0, 2, 6, 4], [0, 1, 3, 2],
                  [2, 3, 7, 6], [4, 6, 7, 5], [1, 5, 7, 3]])["name"]
        result = self.cmd("clean_mesh", object_name=name, merge_distance=0.00001, recalculate_normals=True)
        self.assertEqual(result["before"]["counts"]["vertices"], 9)
        self.assertEqual(result["after"]["counts"]["vertices"], 8)
        self.assertEqual(result["operations"][0]["affected"], 1)
        self.assertEqual(result["operations"][1]["operation"], "recalculate_normals")
        self.assertEqual(result["operations"][1]["affected"], 6)
        self.assertTrue(result["after"]["ok"], result)
        again = self.cmd("clean_mesh", object_name=name, merge_distance=0.00001, recalculate_normals=True)
        self.assertEqual(again["operations"][0]["affected"], 0)
        self.assertTrue(again["after"]["ok"])

    def test_clean_mesh_dissolves_degenerate_edges(self):
        name = self.cmd("create_mesh", name="DegenerateCleanup", vertices=[[0, 0, 0], [0, 0, 0], [1, 0, 0]],
                        edges=[[0, 1], [1, 2]], faces=[])["name"]
        result = self.cmd("clean_mesh", object_name=name, degenerate_distance=0.00001)
        self.assertEqual(result["before"]["issues"]["degenerate_edges"], 1)
        self.assertEqual(result["after"]["issues"]["degenerate_edges"], 0)
        self.assertGreater(result["operations"][0]["affected"], 0)

    def test_collision_proxy_shapes_and_export(self):
        source = self.cmd("create_object", name="SM_CollisionAsset", type="cube", scale=[-1.5, 2, 0.5], rotation=[0.1, 0.2, 0.3])["name"]
        proxies = []
        for shape, prefix in (("BOX", "UBX"), ("SPHERE", "USP"), ("CAPSULE", "UCP"), ("CONVEX_HULL", "UCX")):
            with self.subTest(shape=shape):
                proxy = self.cmd("create_collision_proxy", object_name=source, shape=shape, engine="UNREAL", margin=0.02, parent=True)
                self.assertTrue(proxy["name"].startswith(f"{prefix}_{source}_"))
                self.assertEqual(self.cmd("get_object_info", name=proxy["name"])["scale"], [1, 1, 1])
                audit = self.cmd("audit_collision_proxies", object_names=[proxy["name"]])
                self.assertTrue(audit["ok"], audit)
                proxies.append(proxy["name"])
        self.cmd("modify_object", name=source, location=[10, 0, 0])
        self.assertTrue(self.cmd("audit_collision_proxies", object_names=proxies)["ok"])
        piece = self.cmd("create_object", name="ManualColliderPiece", type="cube", location=[10, 0, 0], scale=[0.2, 0.3, 0.4])["name"]
        compound = self.cmd("create_collision_proxy", object_name=piece, reference_object=source, shape="CONVEX_HULL", engine="UNREAL")
        self.assertEqual(compound["source"], source)
        self.assertTrue(self.cmd("audit_collision_proxies", object_names=[compound["name"]])["ok"])
        proxies.append(compound["name"])
        self.cmd("export_scene", path=str(self.output / "collision.fbx"), object_names=[source, *proxies])
        self.assertGreater((self.output / "collision.fbx").stat().st_size, 100)
        imported = self.cmd("import_asset", path=str(self.output / "collision.fbx"))["objects"]
        for prefix in ("UBX", "USP", "UCP", "UCX"):
            self.assertTrue(any(name.startswith(f"{prefix}_{source}_") for name in imported))

    def test_collision_islands_and_containment(self):
        a = self.cmd("create_object", name="CollisionIslandA", type="cube", location=[20, 0, 0])["name"]
        b = self.cmd("create_object", name="CollisionIslandB", type="cube", location=[24, 0, 0])["name"]
        source = self.cmd("join_mesh_objects", object_names=[a, b], active_name=a, new_name="SM_Islands")["object"]
        result = self.cmd("create_collision_proxies", object_name=source, engine="UNREAL")
        self.assertEqual(result["components"], 2)
        self.assertTrue(self.cmd("audit_collision_proxies", object_names=[proxy["name"] for proxy in result["proxies"]])["ok"])
        outside = self.cmd("create_object", name="Outside", type="cube", location=[40, 0, 0])["name"]
        overlapping = self.cmd("create_object", name="Overlap", type="cube", location=[20.5, 0, 0])["name"]
        inner = self.cmd("create_object", name="Inner", type="cube", location=[24, 0, 0], scale=[0.2, 0.2, 0.2])["name"]
        pairs = self.cmd("detect_collisions", object_names=[source, outside, overlapping, inner])["pairs"]
        self.assertTrue(any(pair["kind"] in {"SURFACE_INTERSECTION", "VOLUME_OVERLAP"} and {source, overlapping} == {pair["a"], pair["b"]} for pair in pairs), pairs)
        self.assertTrue(any(pair["kind"] == "CONTAINMENT" and inner in {pair["a"], pair["b"]} for pair in pairs))
        self.assertFalse(any(outside in {pair["a"], pair["b"]} for pair in pairs))
        flat = self.cmd("create_object", name="FlatHull", type="plane")["name"]
        before = self.cmd("find_objects", pattern="UCX_FlatHull_*")["total"]
        with self.assertRaises(RuntimeError): self.cmd("create_collision_proxy", object_name=flat, shape="CONVEX_HULL", engine="UNREAL")
        self.assertEqual(self.cmd("find_objects", pattern="UCX_FlatHull_*")["total"], before)

    def test_actual_collision_physics_bake_and_constraints(self):
        floor = self.cmd("create_object", name="PhysicsFloor", type="cube", location=[60, 0, -0.5], scale=[3, 3, 0.5])["name"]
        body = self.cmd("create_object", name="FallingBody", type="cube", location=[60, 0, 5], scale=[0.5, 0.5, 0.5])["name"]
        for name in (floor, body): self.cmd("apply_transform", object_name=name, scale=True)
        self.cmd("configure_rigid_body", object_names=[floor], type="PASSIVE", mass=1, friction=0.5, restitution=0,
                 collision_shape="BOX", collision_margin=0.01, collision_collections=[0])
        self.cmd("configure_rigid_body", object_names=[body], type="ACTIVE", mass=1, friction=0.5, restitution=0,
                 collision_shape="BOX", collision_margin=0.01, collision_collections=[0])
        self.cmd("configure_physics_world", frame_start=1, frame_end=45, substeps_per_frame=10, solver_iterations=20)
        baked = self.cmd("bake_physics", action="BAKE")
        self.assertTrue(baked["is_baked"])
        self.cmd("set_frame", frame=45)
        state = self.cmd("get_physics_state", object_names=[body])
        height = state["objects"][0]["location"][2]
        self.assertGreater(height, 0.35)
        self.assertLess(height, 1.0)
        self.assertEqual(state["objects"][0]["collision_collections"], [0])
        with self.assertRaises(RuntimeError): self.cmd("configure_physics_world", frame_start=1, frame_end=50)
        self.assertFalse(self.cmd("bake_physics", action="FREE")["is_baked"])
        self.cmd("configure_rigid_body", object_names=[body], type="ACTIVE", mass=1, friction=0.5, restitution=0,
                 collision_shape="BOX", collision_margin=0.01, collision_collections=[1])
        self.cmd("bake_physics", action="BAKE")
        self.cmd("set_frame", frame=45)
        self.assertLess(self.cmd("get_physics_state", object_names=[body])["objects"][0]["location"][2], 0)
        self.cmd("bake_physics", action="FREE")
        joint = self.cmd("create_rigid_body_constraint", name="HingeJoint", object_a=floor, object_b=body, type="HINGE",
                         settings={"use_limit_ang_z": True, "limit_ang_z_lower": -0.5, "limit_ang_z_upper": 0.5})
        properties = self.cmd("get_properties", target="RIGID_BODY_CONSTRAINT", name=joint["name"], paths=["type", "object1", "object2"])
        self.assertEqual(properties["properties"]["type"]["value"], "HINGE")
        self.cmd("delete_object", name=joint["name"])
        self.cmd("delete_object", name=body)
        self.cmd("delete_object", name=floor)
        self.cmd("set_frame", frame=1)

    def test_ik_edit_mirror_and_visual_animation_bake(self):
        arm = self.cmd("create_armature", name="AdvancedRig", bones=[
            {"name": "Upper.L", "head": [0, 0, 0], "tail": [0, 0.2, 1], "roll": 0.1},
            {"name": "Lower.L", "head": [0, 0.2, 1], "tail": [0, 0, 2], "parent": "Upper.L", "connected": True},
        ])["name"]
        self.cmd("edit_armature", armature_name=arm, bones=[{"name": "Control", "head": [0, 0, 0], "tail": [0, 1, 0], "deform": False}])
        before = self.cmd("get_armature", armature_name=arm)
        with self.assertRaises(RuntimeError):
            self.cmd("edit_armature", armature_name=arm, bones=[{"name": "Upper.L", "head": [0, 0, 0], "tail": [0, 0.2, 1], "parent": "Lower.L"}])
        self.assertEqual(self.cmd("get_armature", armature_name=arm), before)
        target = self.cmd("create_object", name="IKTarget", type="empty", location=[0, 0.5, 1.5])["name"]
        pole = self.cmd("create_object", name="IKPole", type="empty", location=[0, -2, 1])["name"]
        self.cmd("configure_ik", armature_name=arm, bone_name="Lower.L", target_name=target, pole_target_name=pole, chain_length=2)
        rig = self.cmd("get_armature", armature_name=arm)
        tail = next(bone["pose_tail"] for bone in rig["bones"] if bone["name"] == "Lower.L")
        self.assertLess(sum((a-b)**2 for a, b in zip(tail, [0, 0.5, 1.5])), 0.01)
        self.cmd("set_transform_keyframes", object_name=target, frames=[1, 5], locations=[[0, 0.5, 1.5], [0.3, 0.5, 1.3]], interpolation="LINEAR")
        self.cmd("set_frame", frame=5)
        end_tail = next(bone["pose_tail"] for bone in self.cmd("get_armature", armature_name=arm)["bones"] if bone["name"] == "Lower.L")
        self.cmd("bake_animation", object_names=[arm], frame_start=1, frame_end=5, bake_types=["POSE"], clear_constraints=True)
        self.cmd("set_frame", frame=5)
        baked_rig = self.cmd("get_armature", armature_name=arm)
        baked_tail = next(bone["pose_tail"] for bone in baked_rig["bones"] if bone["name"] == "Lower.L")
        self.assertLess(sum((a-b)**2 for a, b in zip(baked_tail, end_tail)), 1e-6)
        self.assertTrue(self.cmd("list_animation_data", object_name=arm)["fcurves"])
        self.assertFalse(any(bone["constraints"] for bone in baked_rig["bones"]))
        mirror = self.cmd("create_armature", name="MirrorRig", bones=[{"name": "Arm.L", "head": [-1, 0, 0], "tail": [-2, 0, 0]}])["name"]
        self.assertIn("Arm.R", self.cmd("mirror_armature", armature_name=mirror, direction="NEGATIVE_X")["bones"])
        self.cmd("add_bone_constraint", armature_name=mirror, bone_name="Arm.R", type="LIMIT_ROTATION")
        self.cmd("remove_bone_constraint", armature_name=mirror, bone_name="Arm.R", constraint_name="Limit Rotation")
        self.cmd("set_frame", frame=1)

    def test_weight_audit_normalization_and_barycentric_transfer(self):
        arm = self.cmd("create_armature", name="WeightRig", bones=[
            {"name": f"Bone{i}", "head": [i, 0, 0], "tail": [i, 0, 1]} for i in range(5)
        ])["name"]
        source = self.cmd("create_mesh", name="WeightsSource", vertices=[[0, 0, 0], [1, 0, 0], [0, 1, 0]], faces=[[0, 1, 2]])["name"]
        self.cmd("bind_armature", object_name=source, armature_name=arm, weights=[
            {"bone": f"Bone{i}", "vertices": [0, 1, 2], "weight": 0.3} for i in range(5)
        ])
        audit = self.cmd("audit_rig", object_name=source, include_indices=True)
        self.assertEqual(audit["issues"]["over_limit"], 3)
        cleaned = self.cmd("normalize_weights", object_name=source, max_influences=4)
        self.assertTrue(cleaned["audit"]["ok"], cleaned)
        self.assertAlmostEqual(self.cmd("get_vertex_weights", object_name=source)["vertices"][0]["sum"], 1)
        self.cmd("set_properties", target="VERTEX_GROUP", name=source, member="Bone0", properties={"lock_weight": True})
        # Existing locked values are preserved while other groups are normalized.
        self.cmd("bind_armature", object_name=source, armature_name=arm,
                 weights=[{"bone": "Bone1", "vertices": [0, 1, 2], "weight": 0.8}])
        self.assertTrue(self.cmd("normalize_weights", object_name=source)["audit"]["ok"])
        first = self.cmd("get_vertex_weights", object_name=source)["vertices"][0]["weights"]
        self.assertAlmostEqual(next(weight["weight"] for weight in first if weight["bone"] == "Bone0"), 0.25)
        self.cmd("set_properties", target="VERTEX_GROUP", name=source, member="Bone0", properties={"lock_weight": False})
        # A varying gradient verifies surface interpolation rather than copying a nearest vertex.
        self.cmd("bind_armature", object_name=source, armature_name=arm, weights=[
            {"bone": f"Bone{i}", "vertices": [0, 1, 2], "weight": 0} for i in range(5)
        ])
        self.cmd("bind_armature", object_name=source, armature_name=arm, weights=[
            {"bone": "Bone0", "vertices": [0], "weight": 1},
            {"bone": "Bone1", "vertices": [1], "weight": 1},
            {"bone": "Bone2", "vertices": [2], "weight": 1},
        ])
        target = self.cmd("create_mesh", name="WeightsTarget", vertices=[[0.25, 0.25, 0], [0.5, 0.25, 0], [0.25, 0.5, 0]], faces=[[0, 1, 2]])["name"]
        transferred = self.cmd("transfer_weights", source_name=source, target_name=target, method="NEAREST_SURFACE")
        self.assertEqual(transferred["transferred_vertices"], 3)
        weights = {entry["bone"]: entry["weight"] for entry in self.cmd("get_vertex_weights", object_name=target)["vertices"][0]["weights"]}
        self.assertAlmostEqual(weights["Bone0"], 0.5)
        self.assertAlmostEqual(weights["Bone1"], 0.25)
        self.assertAlmostEqual(weights["Bone2"], 0.25)
        self.assertTrue(self.cmd("audit_rig", object_name=target)["ok"])
        self.assertEqual(self.cmd("transfer_weights", source_name=source, target_name=target, method="NEAREST_VERTEX")["transferred_vertices"], 3)

    def test_declarative_drivers_and_expression_rejection(self):
        control = self.cmd("create_object", name="DriverControl", type="empty", location=[2, 0, 0])["name"]
        driven = self.cmd("create_object", name="DrivenObject", type="empty")["name"]
        variables = [{"name": "var", "type": "SINGLE_PROP", "targets": [{"id_type": "OBJECT", "name": control, "data_path": "location[0]"}]}]
        driver = self.cmd("add_driver", target="OBJECT", name=driven, data_path="location", index=1, variables=variables, expression="var * 2")
        self.assertTrue(driver["simple_expression"])
        self.assertAlmostEqual(self.cmd("get_object_info", name=driven)["location"][1], 4)
        before = self.cmd("list_drivers", target="OBJECT", name=driven)
        with self.assertRaises(RuntimeError):
            self.cmd("add_driver", target="OBJECT", name=driven, data_path="location", index=1, variables=variables,
                     expression="__import__('os').getcwd()", replace=True)
        self.assertEqual(self.cmd("list_drivers", target="OBJECT", name=driven), before)
        self.assertTrue(self.cmd("remove_driver", target="OBJECT", name=driven, data_path="location", index=1)["removed"])
        self.assertFalse(self.cmd("list_drivers", target="OBJECT", name=driven)["drivers"])
        transforms = [{"name": "var", "type": "TRANSFORMS", "targets": [{"id_type": "OBJECT", "name": control, "transform_type": "LOC_X"}]}]
        self.cmd("add_driver", target="OBJECT", name=driven, data_path="location", index=1, variables=transforms, expression="var * 3")
        self.assertAlmostEqual(self.cmd("get_object_info", name=driven)["location"][1], 6)
        other = self.cmd("create_object", name="OtherDriverControl", type="empty", location=[5, 0, 0], rotation=[0, 0, 1.57079632679])["name"]
        targets = [{"id_type": "OBJECT", "name": name} for name in (control, other)]
        self.cmd("add_driver", target="OBJECT", name=driven, data_path="location", index=2,
                 variables=[{"name": "var", "type": "LOC_DIFF", "targets": targets}], expression="var")
        self.assertAlmostEqual(self.cmd("get_object_info", name=driven)["location"][2], 3)
        self.cmd("add_driver", target="OBJECT", name=driven, data_path="location", index=2,
                 variables=[{"name": "var", "type": "ROTATION_DIFF", "targets": targets}], expression="var", replace=True)
        self.assertAlmostEqual(self.cmd("get_object_info", name=driven)["location"][2], 1.57079632679, places=5)

    def test_shader_graph_and_failed_additions_preserve_graph(self):
        self.cmd("create_material", name="NodeMaterial", color=[0.2, 0.4, 0.8, 1])
        graph = self.cmd("edit_node_tree", target="MATERIAL", name="NodeMaterial", clear=True, remove_nodes=[], nodes=[
            {"name": "BSDF", "type": "ShaderNodeBsdfPrincipled", "inputs": {"Roughness": 0.4}},
            {"name": "Noise", "type": "ShaderNodeTexNoise", "inputs": {"Scale": 5}},
            {"name": "Output", "type": "ShaderNodeOutputMaterial"},
        ], links=[
            {"from_node": "Noise", "from_socket": "Fac", "to_node": "BSDF", "to_socket": "Roughness"},
            {"from_node": "BSDF", "from_socket": "BSDF", "to_node": "Output", "to_socket": "Surface"},
        ])
        self.assertEqual(len(graph["links"]), 2)
        with self.assertRaises(RuntimeError):
            self.cmd("edit_node_tree", target="MATERIAL", name="NodeMaterial", clear=True, remove_nodes=[], nodes=[
                {"name": "Bad", "type": "ShaderNodeTexNoise", "inputs": {"Missing Socket": 1}},
            ], links=[])
        self.assertEqual(self.cmd("get_node_tree", target="MATERIAL", name="NodeMaterial"), graph)
        updated = self.cmd("set_node_inputs", target="MATERIAL", name="NodeMaterial", node_name="Noise", inputs={"Scale": 3})
        noise = next(node for node in updated["nodes"] if node["name"] == "Noise")
        self.assertEqual(next(socket["default"] for socket in noise["inputs"] if socket["name"] == "Scale"), 3)

    def test_rna_matrices_serialize_set_and_restore_after_failed_assignment(self):
        name = self.cmd("create_object", name="MatrixInspection", type="cube", location=[1, 2, 3])["name"]
        paths = ["matrix_world", "matrix_local", "matrix_basis", "location", "rotation_euler"]
        before = self.cmd("get_properties", target="OBJECT", name=name, paths=paths)
        matrix = before["properties"]["matrix_world"]["value"]
        self.assertEqual(len(matrix), 4)
        self.assertTrue(all(len(row) == 4 and all(isinstance(v, (int, float)) for v in row) for row in matrix))
        self.assertEqual(before["properties"]["matrix_world"]["array_dimensions"], [4, 4])
        edited = [row[:] for row in matrix]
        edited[0][3] = 7
        self.cmd("set_properties", target="OBJECT", name=name, properties={"matrix_world": edited})
        self.assertAlmostEqual(self.cmd("get_object_info", name=name)["location"][0], 7)
        accepted = self.cmd("get_properties", target="OBJECT", name=name, paths=paths)
        with self.assertRaises(RuntimeError):
            self.cmd("set_properties", target="OBJECT", name=name, properties={"matrix_world": matrix, "rotation_mode": "INVALID"})
        self.assertEqual(self.cmd("get_properties", target="OBJECT", name=name, paths=paths), accepted)
        with self.assertRaisesRegex(RuntimeError, "Wrong array shape"):
            self.cmd("set_properties", target="OBJECT", name=name, properties={"matrix_world": [[1, 0]]})
        self.assertEqual(self.cmd("get_properties", target="OBJECT", name=name, paths=paths), accepted)

    def test_constraint_typed_settings_and_enum_flags(self):
        arm = self.cmd("create_armature", name="TypedConstraintRig", bones=[{"name": "Root", "head": [0, 0, 0], "tail": [0, 0, 1]}])["name"]
        before = self.cmd("get_armature", armature_name=arm)
        with self.assertRaisesRegex(RuntimeError, "RNA boolean requires"):
            self.cmd("add_bone_constraint", armature_name=arm, bone_name="Root", type="LIMIT_ROTATION", settings={"use_limit_x": "true"})
        self.assertEqual(self.cmd("get_armature", armature_name=arm), before)
        self.cmd("add_bone_constraint", armature_name=arm, bone_name="Root", type="LIMIT_ROTATION", settings={"use_limit_x": True, "min_x": -0.2})
        material = self.cmd("create_material", name="FlagMaterial")["material"]
        self.cmd("set_properties", target="MATERIAL", name=material, properties={"pass_index": 3})
        mesh = self.cmd("create_object", name="FlagBoolean", type="cube")["name"]
        modifier = self.cmd("add_modifier", object_name=mesh, type="BOOLEAN")["modifier"]
        self.cmd("get_properties", target="MODIFIER", name=mesh, member=modifier, paths=["debug_options"])
        self.cmd("set_properties", target="MODIFIER", name=mesh, member=modifier, properties={"debug_options": []})
        self.assertEqual(self.cmd("get_properties", target="MODIFIER", name=mesh, member=modifier, paths=["debug_options"])["properties"]["debug_options"]["value"], [])

    def test_join_excluded_objects_and_invalid_origin_do_not_change_scene(self):
        a = self.cmd("create_object", name="JoinIncluded", type="cube")["name"]
        b = self.cmd("create_object", name="JoinExcluded", type="cube")["name"]
        collection = self.cmd("create_collection", name="ExcludedForJoin")["collection"]
        self.cmd("move_to_collection", object_name=b, collection_name=collection)
        self.cmd("set_properties", target="OBJECT", name=b, properties={"hide_viewport": True})
        # Hidden selection is not forced by the join helper; a separate scene is
        # a definitive non-view-layer fixture without manipulating user state.
        original = self.cmd("get_scene_info")["scene"]
        isolated = self.cmd("create_scene", name="JoinOtherScene", make_active=True)["scene"]
        try:
            with self.assertRaisesRegex(RuntimeError, "active view layer"):
                self.cmd("join_mesh_objects", object_names=[a, b], active_name=a)
        finally:
            self.cmd("set_active_scene", name=original)
            self.cmd("delete_scene", name=isolated)
        before = self.cmd("get_object_info", name=a)
        with self.assertRaisesRegex(RuntimeError, "Unsupported origin mode"):
            self.cmd("set_object_origin", object_name=a, mode="BOTTOM", center="MEDIAN")
        self.assertEqual(self.cmd("get_object_info", name=a), before)

    def test_scalar_drivers_reject_component_indices_without_creating_curves(self):
        control = self.cmd("create_object", name="ScalarDriverControl", type="empty")["name"]
        light = self.cmd("create_object", name="ScalarDriverLight", type="light", light_type="AREA")["name"]
        variable = [{"name": "var", "type": "SINGLE_PROP", "targets": [{"id_type": "OBJECT", "name": control, "data_path": "location[0]"}]}]
        with self.assertRaisesRegex(RuntimeError, "Scalar driver property"):
            self.cmd("add_driver", target="OBJECT_DATA", name=light, data_path="energy", index=0, variables=variable, expression="var")
        self.assertFalse(self.cmd("list_drivers", target="OBJECT_DATA", name=light)["drivers"])
        driver = self.cmd("add_driver", target="OBJECT_DATA", name=light, data_path="energy", index=-1, variables=variable, expression="var")
        self.assertEqual(driver["data_path"], "energy")

    def test_geometry_nodes_declarative_graph(self):
        name = self.cmd("create_object", name="GeoObject", type="cube")["name"]
        group = self.cmd("create_node_group", name="GeometryTest", tree_type="GeometryNodeTree", sockets=[
            {"name": "Geometry", "direction": "INPUT", "socket_type": "NodeSocketGeometry"},
            {"name": "Geometry", "direction": "OUTPUT", "socket_type": "NodeSocketGeometry"},
            {"name": "Amount", "direction": "INPUT", "socket_type": "NodeSocketFloat", "default": 1},
        ])
        self.cmd("edit_node_tree", target="NODE_GROUP", name=group["name"], nodes=[
            {"name": "In", "type": "NodeGroupInput"}, {"name": "Out", "type": "NodeGroupOutput"},
            {"name": "Move", "type": "GeometryNodeTransform", "inputs": {"Translation": [0, 0, 1]}},
        ], links=[
            {"from_node": "In", "from_socket": "Geometry", "to_node": "Move", "to_socket": "Geometry"},
            {"from_node": "Move", "from_socket": "Geometry", "to_node": "Out", "to_socket": "Geometry"},
        ])
        attached = self.cmd("assign_geometry_nodes", object_name=name, node_group=group["name"], inputs={"Amount": 2}, modifier_name="GeometryNodes")
        self.assertTrue(attached["inputs"])
        converted = self.cmd("convert_object_to_mesh", object_name=name, keep_original=True, apply_modifiers=True)
        self.assertEqual(converted["counts"]["vertices"], 8)

    def test_map_range_names_resolve_the_active_data_type_and_real_ambiguity_fails(self):
        self.cmd("create_material", name="MapRangeSockets")
        for data_type, value in (("FLOAT", 2.5), ("FLOAT_VECTOR", [2.5, 3.5, 4.5])):
            with self.subTest(data_type=data_type):
                graph = self.cmd("edit_node_tree", target="MATERIAL", name="MapRangeSockets", clear=True, nodes=[
                    {"name": "Range", "type": "ShaderNodeMapRange", "properties": {"data_type": data_type},
                     "inputs": {"From Min": value}},
                    {"name": "Value", "type": "ShaderNodeValue"},
                ], links=[{"from_node": "Value", "from_socket": "Value", "to_node": "Range", "to_socket": "From Max"}])
                node = next(node for node in graph["nodes"] if node["name"] == "Range")
                active = [socket for socket in node["inputs"] if socket["name"] == "From Min" and socket["enabled"] and socket["available"]]
                self.assertEqual(len(active), 1)
                self.assertEqual(active[0]["default"], value)
                updated = self.cmd("set_node_inputs", target="MATERIAL", name="MapRangeSockets", node_name="Range", inputs={"From Min": value})
                self.assertEqual(updated, graph)
        before = self.cmd("get_node_tree", target="MATERIAL", name="MapRangeSockets")
        with self.assertRaisesRegex(RuntimeError, "ambiguous"):
            self.cmd("edit_node_tree", target="MATERIAL", name="MapRangeSockets", clear=True,
                     nodes=[{"name": "Math", "type": "ShaderNodeMath", "inputs": {"Value": 0.5}}], links=[])
        self.assertEqual(self.cmd("get_node_tree", target="MATERIAL", name="MapRangeSockets"), before)

    def test_compositor_graph_on_blender_five(self):
        group = self.cmd("create_node_group", name="CompositorTest", tree_type="CompositorNodeTree", sockets=[
            {"name": "Image", "direction": "OUTPUT", "socket_type": "NodeSocketColor"},
        ])
        scene_properties = self.cmd("get_properties", target="SCENE")
        if "compositing_node_group" in scene_properties["properties"]:
            self.cmd("set_properties", target="SCENE", properties={"compositing_node_group": {"id_type": "NODE_GROUP", "name": group["name"]}})
            graph = self.cmd("edit_node_tree", target="COMPOSITOR", nodes=[
                {"name": "Color", "type": "CompositorNodeRGB"}, {"name": "Output", "type": "NodeGroupOutput"},
            ], links=[{"from_node": "Color", "from_socket": 0, "to_node": "Output", "to_socket": "Image"}])
            self.assertEqual(len(graph["links"]), 1)
            self.cmd("set_properties", target="SCENE", properties={"compositing_node_group": None})
        else:
            graph = self.cmd("edit_node_tree", target="COMPOSITOR", nodes=[
                {"name": "Color", "type": "CompositorNodeRGB"}, {"name": "Output", "type": "CompositorNodeComposite"},
            ], links=[{"from_node": "Color", "from_socket": 0, "to_node": "Output", "to_socket": "Image"}])
            self.assertEqual(len(graph["links"]), 1)
            self.cmd("set_properties", target="SCENE", properties={"use_nodes": False})

    def test_rig_weights_pose_shape_keys_and_animation_slots(self):
        mesh = self.cmd("create_mesh", name="RigMesh", vertices=[[0, 0, 0], [1, 0, 0], [0, 1, 0]], faces=[[0, 1, 2]])["name"]
        arm = self.cmd("create_armature", name="Rig", bones=[
            {"name": "Root", "head": [0, 0, 0], "tail": [0, 0, 1]},
            {"name": "Tip", "head": [0, 0, 1], "tail": [0, 0, 2], "parent": "Root", "connected": True},
        ])["name"]
        self.assertEqual(len(self.cmd("get_armature", armature_name=arm)["bones"]), 2)
        binding = self.cmd("bind_armature", object_name=mesh, armature_name=arm, automatic_weights=False,
            weights=[{"bone": "Root", "vertices": [0, 1], "weight": 1}, {"bone": "Tip", "vertices": [2], "weight": 1}])
        self.assertEqual(set(binding["groups"]), {"Root", "Tip"})
        self.cmd("pose_bones", armature_name=arm, poses=[{"name": "Root", "location": [0.2, 0, 0]}], frame=1)
        self.assertTrue(self.cmd("list_animation_data", object_name=arm)["fcurves"])
        self.assertAlmostEqual(self.cmd("get_mesh_data", object_name=mesh, evaluated=True, offset=0, limit=1)["items"][0]["coordinate"][0], 0.2, places=5)
        self.cmd("add_bone_constraint", armature_name=arm, bone_name="Tip", type="LIMIT_ROTATION",
                 settings={"use_limit_x": True, "min_x": -0.5, "max_x": 0.5})
        tip = next(bone for bone in self.cmd("get_armature", armature_name=arm)["bones"] if bone["name"] == "Tip")
        self.assertEqual(tip["constraints"][0]["type"], "LIMIT_ROTATION")
        shape = self.cmd("set_shape_key", object_name=mesh, name="Smile", coordinates=[[0, 0, 0], [1, 0, 0.2], [0, 1, 0.2]], value=0.5)
        self.assertEqual(shape["value"], 0.5)
        self.cmd("set_shape_key_keyframes", object_name=mesh, shape_key="Smile", frames=[1, 10], values=[0, 1], interpolation="LINEAR")
        self.cmd("set_transform_keyframes", object_name=mesh, frames=[1, 10], locations=[[0, 0, 0], [1, 0, 0]], interpolation="LINEAR")
        self.assertEqual(len(self.cmd("list_animation_data", object_name=mesh)["fcurves"]), 3)
        self.assertEqual(self.cmd("set_keyframe_interpolation", object_name=mesh, interpolation="LINEAR")["updated_keys"], 6)
        self.cmd("set_frame", frame=10)
        self.assertAlmostEqual(self.cmd("get_object_info", name=mesh)["location"][0], 1)
        self.cmd("set_frame", frame=1)

    def test_explicit_mode_control_and_failed_switch_restoration(self):
        mesh = self.cmd("create_object", name="ModeMesh", type="cube")["name"]
        self.assertEqual(self.cmd("set_mode", object_name=mesh, mode="EDIT")["mode"], "EDIT")
        before = self.cmd("get_selection")
        with self.assertRaises(RuntimeError): self.cmd("set_mode", object_name=mesh, mode="POSE")
        self.assertEqual(self.cmd("get_selection"), before)
        self.assertEqual(self.cmd("set_mode", object_name=mesh, mode="OBJECT")["mode"], "OBJECT")

    def test_modifier_names_pointer_settings_and_failed_creation(self):
        mesh = self.cmd("create_object", name="Oprava | břišní L", type="cube")["name"]
        modifier = self.cmd("add_modifier", object_name=mesh, type="SUBSURF", settings={"levels": 2})["modifier"]
        selection = self.cmd("get_selection")
        with self.assertRaisesRegex(RuntimeError, "Modifier not found"):
            self.cmd("apply_modifier", object_name=mesh, modifier_name="Subdivision")
        self.assertEqual(self.cmd("get_selection"), selection)
        self.assertEqual(self.cmd("list_modifiers", object_name=mesh)["modifiers"][0]["name"], modifier)
        result = self.cmd("apply_modifier", object_name=mesh, modifier_name=modifier)
        self.assertEqual(result["applied"], modifier)
        self.assertFalse(self.cmd("list_modifiers", object_name=mesh)["modifiers"])
        self.assertGreater(self.cmd("get_object_info", name=mesh)["mesh"]["vertices"], 8)

        target = self.cmd("create_object", name="PointerTarget", type="cube")["name"]
        cutter = self.cmd("create_object", name="Řez | tlama", type="cube", location=[0.75, 0, 0], scale=[0.4, 0.4, 0.4])["name"]
        boolean = self.cmd("add_modifier", object_name=target, type="BOOLEAN", name="Skutečná dutina", settings={
            "operation": "DIFFERENCE", "solver": "EXACT", "object": {"id_type": "OBJECT", "name": cutter},
        })["modifier"]
        value = self.cmd("get_properties", target="MODIFIER", name=target, member=boolean, paths=["object"])
        self.assertEqual(value["properties"]["object"]["value"]["name"], cutter)
        self.cmd("apply_modifier", object_name=target, modifier_name=boolean)
        self.assertGreater(self.cmd("get_object_info", name=target)["mesh"]["vertices"], 8)
        before = self.cmd("list_modifiers", object_name=target)
        for settings in ({"object": {"id_type": "OBJECT", "name": "MissingCutter"}}, {"unknown_setting": 1}):
            with self.subTest(settings=settings), self.assertRaises(RuntimeError):
                self.cmd("add_modifier", object_name=target, type="BOOLEAN", settings=settings)
            self.assertEqual(self.cmd("list_modifiers", object_name=target), before)
        with self.assertRaisesRegex(RuntimeError, "RNA target not found"):
            self.cmd("set_properties", target="MODIFIER", name=target, member="MissingModifier",
                     properties={"object": {"id_type": "OBJECT", "name": cutter}})

    def test_large_eevee_previews_restore_settings_and_return_mcp_images(self):
        original_scene = self.cmd("get_scene_info")["scene"]
        scene = self.cmd("create_scene", name="PreviewTransferTest", make_active=True)["scene"]
        try:
            mesh = self.cmd("create_object", name="PreviewNoiseCube", type="cube")["name"]
            camera = self.cmd("create_object", name="Kamera | profil - test", type="camera")["name"]
            self.cmd("frame_camera_to_object", camera_name=camera, object_name=mesh, direction=[0, -1, 0])
            self.cmd("zoom_camera", camera_name=camera, type="ORTHO", ortho_scale=2.6)
            self.cmd("set_active_camera", name=camera)
            material = self.cmd("create_material", name="PreviewNoise")["material"]
            self.cmd("edit_node_tree", target="MATERIAL", name=material, clear=True, nodes=[
                {"name": "Noise", "type": "ShaderNodeTexNoise", "inputs": {"Scale": 300, "Detail": 0}},
                {"name": "Emission", "type": "ShaderNodeEmission", "inputs": {"Strength": 1}},
                {"name": "Output", "type": "ShaderNodeOutputMaterial"},
            ], links=[
                {"from_node": "Noise", "from_socket": "Color", "to_node": "Emission", "to_socket": "Color"},
                {"from_node": "Emission", "from_socket": "Emission", "to_node": "Output", "to_socket": "Surface"},
            ])
            self.cmd("assign_material", object_name=mesh, material_name=material)
            self.cmd("set_render_settings", engine="BLENDER_EEVEE", resolution=[1500, 800])
            self.cmd("set_properties", target="RENDER", properties={"image_settings.file_format": "JPEG"})
            paths = ["resolution_x", "resolution_y", "resolution_percentage", "filepath",
                     "image_settings.file_format", "image_settings.color_mode", "image_settings.color_depth"]
            before = self.cmd("get_properties", target="RENDER", paths=paths)
            preview = self.cmd("render_preview", max_size=1000)
            data = base64.b64decode(preview["data"])
            self.assertGreater(len(data), 32768)
            self.assertEqual(struct.unpack(">II", data[16:24]), (1000, 533))
            self.assertEqual(self.cmd("get_properties", target="RENDER", paths=paths), before)

            async def render_batch():
                with patch.object(server, "_get_conn", return_value=self.connection):
                    async with create_connected_server_and_client_session(server.mcp) as client:
                        return await client.call_tool("batch_tools", {"operations": [
                            {"tool": "render_preview", "arguments": {"max_size": 1200}},
                            {"tool": "get_scene_info", "arguments": {}},
                        ]})
            result = asyncio.run(render_batch())
            self.assertFalse(result.isError, result)
            self.assertEqual(result.content[1].type, "image")
            data = base64.b64decode(result.content[1].data)
            self.assertGreater(len(data), 32768)
            self.assertEqual(struct.unpack(">II", data[16:24]), (1200, 640))
            self.assertEqual(self.cmd("get_properties", target="RENDER", paths=paths), before)
        finally:
            self.cmd("set_active_scene", name=original_scene)
            self.cmd("delete_scene", name=scene)

    def test_properties_physics_and_real_render(self):
        name = self.cmd("create_object", name="RenderCube", type="cube")["name"]
        camera = self.cmd("create_object", name="TestCamera", type="camera", location=[4, -6, 4])["name"]
        self.cmd("frame_camera_to_object", camera_name=camera, object_name=name)
        light = self.cmd("create_object", name="TestLight", type="light", light_type="AREA", location=[2, -3, 4])["name"]
        self.cmd("set_properties", target="OBJECT_DATA", name=light, properties={"energy": 800, "size": 4})
        before = self.cmd("get_properties", target="OBJECT_DATA", name=light, paths=["energy", "type"])
        with self.assertRaises(RuntimeError):
            self.cmd("set_properties", target="OBJECT_DATA", name=light, properties={"energy": 500, "type": "INVALID"})
        self.assertEqual(self.cmd("get_properties", target="OBJECT_DATA", name=light, paths=["energy", "type"]), before)
        point = self.cmd("create_object", name="PointValidation", type="light", light_type="POINT")["name"]
        point_before = self.cmd("get_properties", target="OBJECT_DATA", name=point, paths=["energy"])
        with self.assertRaisesRegex(RuntimeError, "Unknown RNA property: size"):
            self.cmd("set_properties", target="OBJECT_DATA", name=point, properties={"energy": 1400, "size": 3})
        self.assertEqual(self.cmd("get_properties", target="OBJECT_DATA", name=point, paths=["energy"]), point_before)
        self.cmd("configure_rigid_body", object_names=[name], type="PASSIVE", mass=1, friction=0.5, restitution=0, collision_shape="BOX")
        self.cmd("set_render_settings", engine="CYCLES", resolution=[64, 64])
        self.cmd("set_properties", target="SCENE", properties={"cycles.samples": 4, "cycles.use_denoising": False})
        result = self.cmd("render_preview", max_size=64)
        self.assertTrue(base64.b64decode(result["data"]).startswith(b"\x89PNG\r\n\x1a\n"))
        image_path = self.output / "preview.png"
        self.cmd("render_still", path=str(image_path))
        self.assertTrue(image_path.exists())
        loaded = self.cmd("load_image", path=str(image_path), name="TestRenderImage")
        self.assertEqual(loaded["size"], [64, 64])
        with self.assertRaisesRegex(RuntimeError, "viewport"):
            self.cmd("frame_viewport", object_names=[name], shading="SOLID")

    def test_checkpoint_library_and_gltf_roundtrip(self):
        name = self.cmd("create_object", name="AssetCube", type="cube")["name"]
        path = self.output / "checkpoint.blend"
        original = self.cmd("get_blender_info")["file_path"]
        self.cmd("save_checkpoint", path=str(path))
        self.assertEqual(self.cmd("get_blender_info")["file_path"], original)
        available = self.cmd("append_datablocks", path=str(path), kind="objects", names=None, link=False)
        self.assertIn(name, available["available"])
        appended = self.cmd("append_datablocks", path=str(path), kind="objects", names=[name], link=False)
        self.assertNotEqual(appended["names"][0], name)
        glb = self.output / "asset.glb"
        self.cmd("export_scene", path=str(glb), object_names=[name])
        self.assertGreater(glb.stat().st_size, 100)
        self.assertTrue(self.cmd("import_asset", path=str(glb))["objects"])

    def test_append_current_file_reads_saved_state_and_cleans_snapshot(self):
        source = self.cmd("create_object", name="SavedLibrarySource", type="cube", location=[1, 2, 3])["name"]
        path = self.output / "current-library.blend"
        self.cmd("save_blend_file", path=str(path))
        original_digest = hashlib.sha256(path.read_bytes()).hexdigest()
        self.cmd("modify_object", name=source, location=[99, 0, 0])
        unsaved = self.cmd("create_object", name="UnsavedOnly", type="cube")["name"]
        available = self.cmd("append_datablocks", path="//current-library.blend", kind="objects", names=None)
        self.assertTrue(available["source_is_current_file"])
        self.assertIn(source, available["available"])
        self.assertNotIn(unsaved, available["available"])
        appended = self.cmd("append_datablocks", path=str(path), kind="objects", names=[source], link=False)
        self.assertTrue(appended["source_is_current_file"])
        self.assertNotEqual(appended["names"][0], source)
        self.assertEqual(self.cmd("get_object_info", name=appended["names"][0])["location"], [1, 2, 3])
        self.assertEqual(self.cmd("get_object_info", name=source)["location"], [99, 0, 0])
        self.assertEqual(self.cmd("get_blender_info")["file_path"], str(path))
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), original_digest)
        with self.assertRaisesRegex(RuntimeError, "Cannot link"):
            self.cmd("append_datablocks", path=str(path), kind="objects", names=[source], link=True)
        with self.assertRaisesRegex(RuntimeError, "missing or duplicated"):
            self.cmd("append_datablocks", path=str(path), kind="objects", names=["MissingInSavedFile"], link=False)
        self.assertFalse(list(self.output.glob(".blender-mcp-append-*")))

    def test_real_mcp_batch_with_result_references_and_error_stop(self):
        async def run():
            with patch.object(server, "_get_conn", return_value=self.connection):
                async with create_connected_server_and_client_session(server.mcp) as client:
                    group = await client.call_tool("create_node_group", {
                        "name": "MCPStructuredGroup", "tree_type": "ShaderNodeTree",
                        "sockets": [{"name": "Value", "socket_type": "NodeSocketFloat", "default": 0.5}],
                    })
                    self.assertFalse(group.isError)
                    self.assertEqual(group.structuredContent["sockets"][0]["name"], "Value")
                    result = await client.call_tool("batch_tools", {"operations": [
                        {"tool": "create_object", "arguments": {"name": "BatchCube"}},
                        {"tool": "modify_object", "arguments": {"name": {"$ref": "0.name"}, "location": [2, 3, 4]}},
                        {"tool": "set_properties", "arguments": {"target": "OBJECT", "name": {"$ref": "0.name"}, "properties": {"hide_render": True}}},
                        {"tool": "modify_object", "arguments": {"name": "DoesNotExist"}},
                        {"tool": "delete_object", "arguments": {"name": {"$ref": "0.name"}}},
                    ]})
                    self.assertTrue(result.isError)
                    summary = json.loads(result.content[0].text)
                    self.assertEqual(summary["completed"], 3)
                    return summary["results"][0]["result"]["name"]
        name = asyncio.run(run())
        self.assertEqual(self.cmd("get_object_info", name=name)["location"], [2, 3, 4])

    def test_legacy_and_length_native_rpc_and_info(self):
        for protocol in ("legacy", "length"):
            connection = server.BlenderConnection("127.0.0.1", self.connection.port, protocol=protocol)
            info = connection.send_command("get_blender_info", {"bridge_version": "1.0.0", "protocol": protocol})
            self.assertEqual(info["capabilities"]["addon_version"], server.__version__)
            self.assertTrue(info["background"])
            self.assertFalse(info["capabilities"]["has_viewport"])

    def test_real_blender_error_is_available_in_local_journal(self):
        async def run():
            journal = ErrorJournal(self.output / "errors.jsonl")
            with patch.object(server, "_get_conn", return_value=self.connection), patch.object(server, "_error_journal", journal):
                async with create_connected_server_and_client_session(server.mcp) as client:
                    failed = await client.call_tool("apply_modifier", {"object_name": "MissingForJournal", "modifier_name": "Subsurf"})
                    self.assertTrue(failed.isError)
                    log = await client.call_tool("get_error_log", {"limit": 10, "tool": "apply_modifier"})
                    self.assertFalse(log.isError)
                    events = log.structuredContent["events"]
                    self.assertEqual(len(events), 1)
                    self.assertEqual(events[0]["error_type"], "KeyError")
                    self.assertTrue(any(frame["file"] == "<blender-mcp-command-library>" for frame in events[0]["frames"]))
        asyncio.run(run())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--blender", required=True)
    parser.add_argument("--temp-root")
    parser.add_argument("--execute-only", action="store_true", help="Emulate Blender Lab's execute-only wire protocol")
    args = parser.parse_args()
    host = Path(__file__).with_name("blender_host.py")
    with tempfile.TemporaryDirectory(prefix="blender-mcp-test-", dir=args.temp_root) as directory:
        command = [args.blender, "--background", "--factory-startup", "--disable-autoexec", "--python", str(host)]
        if args.execute_only: command += ["--", "--execute-only"]
        process = subprocess.Popen(command,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        lines, readiness = [], queue.Queue()

        def collect():
            for line in process.stdout:
                lines.append(line)
                if line.startswith("MCP_TEST_PORT="): readiness.put(int(line.split("=")[1]))
        thread = threading.Thread(target=collect, daemon=True)
        thread.start()
        try:
            port = readiness.get(timeout=30)
            connection = server.BlenderConnection("127.0.0.1", port)
            BlenderIntegrationTests.connection = connection
            BlenderIntegrationTests.output = Path(directory)
            result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(BlenderIntegrationTests))
            connection._result(connection._request({"type": "test_shutdown"}))
            process.wait(timeout=30)
            if not result.wasSuccessful() or process.returncode != 0:
                print("".join(lines))
                raise SystemExit(1)
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
            thread.join(timeout=5)


if __name__ == "__main__":
    main()
