import base64
import ast
import asyncio
import json
import struct
import sys
import threading
import time
import types
import unittest
from unittest.mock import Mock, patch

from mcp.server.fastmcp.exceptions import ToolError
from mcp.shared.memory import create_connected_server_and_client_session

from blender_mcp_local import server
from blender_mcp_local.server import BlenderConnection, _build_command_code


class FakeSocket:
    def __init__(self, incoming: bytes, chunk_size: int = 3) -> None:
        self.incoming = bytearray(incoming)
        self.chunk_size = chunk_size
        self.sent = bytearray()
        self.closed = False

    def recv(self, size: int) -> bytes:
        size = min(size, self.chunk_size, len(self.incoming))
        data = bytes(self.incoming[:size])
        del self.incoming[:size]
        return data

    def sendall(self, data: bytes) -> None:
        self.sent.extend(data)

    def close(self) -> None:
        self.closed = True


class BlenderConnectionTests(unittest.TestCase):
    def test_execute_code_uses_length_prefixed_json(self) -> None:
        response_data = json.dumps({"status": "success", "result": {"value": 42}}).encode()
        fake_socket = FakeSocket(struct.pack(">I", len(response_data)) + response_data)
        connection = BlenderConnection("127.0.0.1", 9876, protocol="length")
        connection.sock = fake_socket  # type: ignore[assignment]

        result = connection.execute_code("result = {'value': 42}")

        self.assertEqual(result, {"value": 42})
        request_size = struct.unpack(">I", fake_socket.sent[:4])[0]
        request = json.loads(fake_socket.sent[4:4 + request_size])
        self.assertEqual(request["type"], "execute")
        self.assertEqual(request["params"]["code"], "result = {'value': 42}")
        self.assertTrue(request["params"]["strict_json"])
        self.assertTrue(fake_socket.closed)

    def test_execute_code_supports_legacy_null_delimiter(self) -> None:
        response = json.dumps({"status": "success", "result": "ok"}).encode() + b"\0"
        fake_socket = FakeSocket(response)
        connection = BlenderConnection("127.0.0.1", 9876, protocol="legacy")
        connection.sock = fake_socket  # type: ignore[assignment]

        self.assertEqual(connection.execute_code("result = 'ok'"), "ok")
        self.assertEqual(fake_socket.sent[-1:], b"\0")
        request = json.loads(fake_socket.sent[:-1])
        self.assertEqual(request["type"], "execute")
        self.assertEqual(request["code"], "result = 'ok'")
        self.assertNotIn("params", request)

    def test_generated_blender_program_compiles(self) -> None:
        commands = (
            "get_blender_info",
            "audit_mesh",
            "clean_mesh",
            "create_mesh",
            "create_curve",
            "create_text_object",
            "set_parent",
            "apply_transform",
            "convert_object_to_mesh",
            "join_mesh_objects",
            "set_object_origin",
            "remove_modifier",
            "list_constraints",
            "add_constraint",
            "remove_constraint",
            "list_collections",
            "set_frame",
            "insert_keyframe",
            "delete_keyframe",
            "list_animation_data",
            "set_transform_keyframes",
            "render_preview",
            "render_animation",
            "import_asset",
            "export_scene",
            "save_blend_file",
        )
        for command in commands:
            with self.subTest(command=command):
                code = _build_command_code(command, {})
                compile(code, "<generated>", "exec")
                self.assertIn(f'cmd == "{command}"', code)

    def test_calls_are_serialized(self) -> None:
        class RecordingConnection:
            def __init__(self) -> None:
                self.guard = threading.Lock()
                self.active = 0
                self.max_active = 0

            def send_command(self, cmd: str, _params: object) -> str:
                with self.guard:
                    self.active += 1
                    self.max_active = max(self.max_active, self.active)
                time.sleep(0.02)
                with self.guard:
                    self.active -= 1
                return cmd

        connection = RecordingConnection()
        original = server._conn
        server._conn = connection  # type: ignore[assignment]
        results = []
        try:
            threads = [threading.Thread(target=lambda: results.append(server._call("test"))) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        finally:
            server._conn = original

        self.assertEqual(results, ["test", "test"])
        self.assertEqual(connection.max_active, 1)

    def test_native_rpc_uploads_library_once_then_sends_only_arguments(self) -> None:
        connection = BlenderConnection("127.0.0.1", 9876)
        with patch.object(connection, "_request", side_effect=[
            {"status": "success", "result": {"api_version": 1}},
            {"status": "success", "result": {}},
            {"status": "success", "result": {"name": "Cube"}},
            {"status": "success", "result": {"name": "Cube"}},
        ]) as request:
            connection.send_command("create_object", {"name": "Cube"})
            connection.send_command("get_object_info", {"name": "Cube"})
        commands = [call.args[0] for call in request.call_args_list]
        self.assertEqual([cmd["type"] for cmd in commands], ["bridge_info", "load_command_library", "command", "command"])
        self.assertNotIn("code", commands[-1]["params"])
        self.assertLess(len(json.dumps(commands[-1])), 300)
        compile(commands[1]["params"]["source"], "<library>", "exec")

    def test_missing_library_reloads_before_one_safe_retry(self) -> None:
        connection = BlenderConnection("127.0.0.1", 9876)
        connection.library_ready = True
        with patch.object(connection, "_request", side_effect=[
            {"status": "error", "code": "LIBRARY_NOT_LOADED"},
            {"status": "success", "result": {"api_version": 1}},
            {"status": "success", "result": {}},
            {"status": "success", "result": {"created": True}},
        ]) as request:
            self.assertEqual(connection.send_command("create_object"), {"created": True})
        self.assertEqual(request.call_count, 4)

    def test_blender_operation_errors_are_not_retried(self) -> None:
        connection = BlenderConnection("127.0.0.1", 9876)
        connection.library_ready = True
        with patch.object(connection, "_request", return_value={"status": "error", "message": "Bad geometry"}) as request:
            with self.assertRaisesRegex(RuntimeError, "Bad geometry"):
                connection.send_command("create_mesh")
        request.assert_called_once()

    def test_nonfinite_arguments_are_rejected_before_connecting(self) -> None:
        connection = BlenderConnection("127.0.0.1", 9876)
        with patch.object(connection, "connect") as connect:
            with self.assertRaises(ValueError):
                connection._request({"type": "command", "params": {"value": float("nan")}})
        connect.assert_not_called()

    def test_official_execute_protocol_is_detected_and_cached(self) -> None:
        connection = BlenderConnection("127.0.0.1", 9876)
        _, library_id = server._command_library()
        with patch.object(connection, "_request", side_effect=[
            {"status": "error", "message": "Unknown request type: 'bridge_info'"},
            {"status": "ok", "result": {"library_id": library_id}},
            {"status": "ok", "result": {"name": "Cube"}},
            {"status": "ok", "result": {"name": "Cube"}},
        ]) as request:
            connection.send_command("create_object", {"name": "Cube"})
            connection.send_command("get_object_info", {"name": "Cube"})
        commands = [call.args[0] for call in request.call_args_list]
        self.assertEqual(connection.backend, "execute")
        self.assertEqual([cmd["type"] for cmd in commands], ["bridge_info", "execute", "execute", "execute"])
        self.assertIn("driver_namespace", commands[1]["code"])
        self.assertLess(len(commands[-1]["code"]), 1000)

    def test_official_runtime_loss_is_reloaded_without_repeating_scene_errors(self) -> None:
        connection = BlenderConnection("127.0.0.1", 9876)
        connection.backend, connection.library_ready = "execute", True
        _, library_id = server._command_library()
        with patch.object(connection, "execute_code", side_effect=[
            {"_mcp_runtime_missing": True}, {"library_id": library_id}, {"name": "Cube"},
        ]) as execute:
            self.assertEqual(connection.send_command("create_object"), {"name": "Cube"})
        self.assertEqual(execute.call_count, 3)
        with patch.object(connection, "execute_code", side_effect=RuntimeError("Bad geometry")) as execute:
            with self.assertRaisesRegex(RuntimeError, "Bad geometry"):
                connection.send_command("create_mesh")
        execute.assert_called_once()

    def test_large_execute_results_cross_a_small_response_endpoint_losslessly(self) -> None:
        result = {"format": "png", "data": base64.b64encode(bytes(range(256)) * 1024).decode(),
                  "label": "Žralok 🦈" * 2000}
        runtime = {"dispatch": Mock(return_value=result)}
        exec(server._BATCH_RUNTIME, runtime)
        runtime["_library_id"] = server._command_library()[1]
        bpy = types.ModuleType("bpy")
        bpy.app = types.SimpleNamespace(driver_namespace={"blender_mcp_local_command_runtime": runtime})
        connection = BlenderConnection("127.0.0.1", 9876)
        connection.backend, connection.library_ready = "execute", True
        sizes = []

        def request(message):
            namespace = {}
            exec(message["code"], namespace)
            response = {"status": "ok", "result": namespace["result"]}
            size = len(json.dumps(response).encode())
            sizes.append(size)
            if size > 32768:
                raise RuntimeError("Blender connection error: Blender closed the connection")
            return response

        with patch.dict(sys.modules, {"bpy": bpy}), patch.object(connection, "_request", side_effect=request):
            self.assertEqual(connection.send_command("render_preview", {"max_size": 1200}), result)
        runtime["dispatch"].assert_called_once()
        self.assertGreater(len(sizes), 2)
        self.assertLessEqual(max(sizes), 32768)
        self.assertFalse(runtime["_result_chunks"])

    def test_large_execute_arguments_are_compressed_without_changing_geometry(self) -> None:
        arguments = {"name": "Síť", "vertices": [[i / 16, j / 16, (i * j) / 256]
                     for i in range(60) for j in range(64)],
                     "faces": [[i, i + 1, i + 65, i + 64] for i in range(3746)]}
        operations = [{"command": "create_mesh", "arguments": arguments}]
        for type_, params, dispatch_name, expected in (
            ("command", operations[0], "dispatch", ("create_mesh", arguments)),
            ("batch", {"operations": operations}, "dispatch_batch", (operations,)),
        ):
            with self.subTest(type=type_):
                runtime = {}
                exec(server._BATCH_RUNTIME, runtime)
                runtime[dispatch_name] = Mock(return_value={"created": True})
                runtime["_library_id"] = server._command_library()[1]
                bpy = types.ModuleType("bpy")
                bpy.app = types.SimpleNamespace(driver_namespace={"blender_mcp_local_command_runtime": runtime})
                connection = BlenderConnection("127.0.0.1", 9876)
                sizes = []

                def request(message):
                    sizes.append(len(json.dumps(message).encode()))
                    if sizes[-1] > 32768:
                        raise RuntimeError("Test endpoint request budget exceeded")
                    namespace = {}
                    exec(message["code"], namespace)
                    return {"status": "ok", "result": namespace["result"]}

                with patch.dict(sys.modules, {"bpy": bpy}), patch.object(connection, "_request", side_effect=request):
                    self.assertEqual(connection._execute_library_request(type_, params), {"created": True})
                runtime[dispatch_name].assert_called_once_with(*expected)
                self.assertLessEqual(max(sizes), 32768)
                self.assertNotIn("_pending_request", runtime)

    def test_failed_argument_upload_or_dispatch_is_cleaned_up_without_retry(self) -> None:
        params = {"command": "create_mesh", "arguments": {
            "name": "Large", "vertices": [[i, i + 1, i + 2] for i in range(4000)]}}
        for failure in ("upload", "dispatch"):
            with self.subTest(failure=failure):
                runtime = {}
                exec(server._BATCH_RUNTIME, runtime)
                runtime["dispatch"] = Mock(side_effect=RuntimeError("Scene operation failed"))
                runtime["_library_id"] = server._command_library()[1]
                bpy = types.ModuleType("bpy")
                bpy.app = types.SimpleNamespace(driver_namespace={"blender_mcp_local_command_runtime": runtime})
                connection = BlenderConnection("127.0.0.1", 9876)
                uploads = []

                def request(message):
                    namespace = {}
                    exec(message["code"], namespace)
                    result = namespace["result"]
                    if "uploaded" in result:
                        uploads.append(result["uploaded"])
                        if failure == "upload" and len(uploads) == 2:
                            raise RuntimeError("Connection timed out after upload")
                    return {"status": "ok", "result": result}

                with patch.dict(sys.modules, {"bpy": bpy}), patch.object(connection, "_request", side_effect=request):
                    with self.assertRaises(RuntimeError):
                        connection._execute_library_request("command", params)
                self.assertGreaterEqual(len(uploads), 2)
                self.assertEqual(runtime["dispatch"].call_count, 0 if failure == "upload" else 1)
                self.assertNotIn("_pending_request", runtime)

    def test_failed_result_transfer_never_repeats_the_scene_operation(self) -> None:
        runtime = {"dispatch": Mock(return_value={"data": "x" * 50000})}
        exec(server._BATCH_RUNTIME, runtime)
        runtime["_library_id"] = server._command_library()[1]
        runtime["read_result_chunk"] = Mock(side_effect=RuntimeError("lost chunk"))
        bpy = types.ModuleType("bpy")
        bpy.app = types.SimpleNamespace(driver_namespace={"blender_mcp_local_command_runtime": runtime})
        connection = BlenderConnection("127.0.0.1", 9876)
        connection.backend, connection.library_ready = "execute", True

        def request(message):
            namespace = {}
            try:
                exec(message["code"], namespace)
                return {"status": "ok", "result": namespace["result"]}
            except RuntimeError as error:
                return {"status": "error", "message": str(error)}

        with patch.dict(sys.modules, {"bpy": bpy}), patch.object(connection, "_request", side_effect=request):
            with self.assertRaisesRegex(RuntimeError, "lost chunk"):
                connection.send_command("create_mesh")
        runtime["dispatch"].assert_called_once()
        self.assertFalse(runtime["_result_chunks"])

    def test_chunk_cache_is_bounded_and_expired_results_are_rejected(self) -> None:
        runtime = {}
        exec(server._BATCH_RUNTIME, runtime)
        identifiers = [runtime["transport_result"]({"data": "x" * 20000})["_mcp_chunked_result"]["id"]
                       for _ in range(5)]
        self.assertEqual(len(runtime["_result_chunks"]), 4)
        with self.assertRaisesRegex(RuntimeError, "evicted"):
            runtime["read_result_chunk"](identifiers[0], 0)
        runtime["_result_chunks"][identifiers[-1]][1] -= 181
        with self.assertRaisesRegex(RuntimeError, "expired"):
            runtime["read_result_chunk"](identifiers[-1], 0)

    def test_chunk_size_and_checksum_are_verified(self) -> None:
        payload = json.dumps({"data": "x" * 20000}).encode()
        descriptor = {"_mcp_chunked_result": {"id": "a" * 32, "size": len(payload),
                                            "sha256": server.hashlib.sha256(payload).hexdigest()}}
        connection = BlenderConnection("127.0.0.1", 9876)
        for corrupt in ("short", "checksum"):
            with self.subTest(corrupt=corrupt):
                blocks = [payload[offset:offset + server.EXECUTE_CHUNK_BYTES]
                          for offset in range(0, len(payload), server.EXECUTE_CHUNK_BYTES)]
                blocks[0] = blocks[0][1:] if corrupt == "short" else b"!" + blocks[0][1:]
                replies = [{"data": base64.b64encode(block).decode()} for block in blocks] + [{}]
                with patch.object(connection, "execute_code", side_effect=replies):
                    with self.assertRaisesRegex(RuntimeError, "Incomplete|checksum"):
                        connection._read_execute_result(descriptor)

    def test_unicode_escaping_is_counted_in_response_size(self) -> None:
        runtime = {}
        exec(server._BATCH_RUNTIME, runtime)
        result = {"label": "🦈" * 4000}
        descriptor = runtime["transport_result"](result)["_mcp_chunked_result"]
        self.assertGreater(descriptor["size"], 32768)

    def test_invalid_chunk_descriptor_is_rejected_before_io(self) -> None:
        connection = BlenderConnection("127.0.0.1", 9876)
        with patch.object(connection, "execute_code") as execute:
            with self.assertRaisesRegex(RuntimeError, "descriptor"):
                connection._read_execute_result({"_mcp_chunked_result": {"id": "bad", "size": 100}})
        execute.assert_not_called()

class SocketResolutionTests(unittest.TestCase):
    def setUp(self):
        function = next(node for node in ast.parse(server.COMMAND_CODE).body
                        if isinstance(node, ast.FunctionDef) and node.name == "socket_at")
        namespace = {}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "<socket-resolver>", "exec"), namespace)
        self.resolve = namespace["socket_at"]

    def socket(self, name, identifier, enabled=True, unavailable=False):
        return types.SimpleNamespace(name=name, identifier=identifier, enabled=enabled, is_unavailable=unavailable)

    def test_duplicate_names_use_the_one_available_socket(self):
        scalar = self.socket("From Min", "From Min", enabled=False)
        vector = self.socket("From Min", "From Min_001")
        self.assertIs(self.resolve([scalar, vector], "From Min"), vector)
        scalar.enabled, vector.enabled = True, False
        self.assertIs(self.resolve([scalar, vector], "From Min"), scalar)

    def test_two_available_sockets_remain_ambiguous(self):
        sockets = [self.socket("Value", "Value"), self.socket("Value", "Value_001")]
        with self.assertRaisesRegex(ValueError, "ambiguous.*candidates"):
            self.resolve(sockets, "Value")

    def test_explicit_indices_and_unique_identifiers_are_preserved(self):
        sockets = [self.socket("Value", "Value"), self.socket("Value", "Value_001", enabled=False)]
        self.assertIs(self.resolve(sockets, "1"), sockets[1])
        self.assertIs(self.resolve(sockets, "Value_001"), sockets[1])
        with self.assertRaisesRegex(ValueError, "out of range"):
            self.resolve(sockets, 2)

    def test_unavailable_socket_is_not_an_automatic_candidate(self):
        sockets = [self.socket("Value", "Value", unavailable=True), self.socket("Value", "Value_001")]
        self.assertIs(self.resolve(sockets, "Value"), sockets[1])


class ModifierApplyTests(unittest.TestCase):
    def run_apply(self, name, outcome, remove=False):
        modifiers = {"Subsurf": object()}
        obj = types.SimpleNamespace(name="Fin", mode="OBJECT", modifiers=modifiers, select_set=Mock())
        operator = Mock(return_value=outcome)
        if remove:
            operator.side_effect = lambda **_kwargs: (modifiers.pop("Subsurf"), outcome)[1]
        object_ops = types.SimpleNamespace(modifier_apply=operator, select_all=Mock(), mode_set=Mock())
        bpy = types.ModuleType("bpy")
        bpy.data = types.SimpleNamespace(objects={"Fin": obj})
        bpy.context = types.SimpleNamespace(object=obj, view_layer=types.SimpleNamespace(objects=types.SimpleNamespace(active=obj)))
        bpy.ops = types.SimpleNamespace(object=object_ops)
        mathutils = types.ModuleType("mathutils")
        mathutils.Matrix = mathutils.Vector = object
        namespace = {}
        with patch.dict(sys.modules, {"bpy": bpy, "bmesh": types.ModuleType("bmesh"), "mathutils": mathutils}):
            exec(_build_command_code("apply_modifier", {"object_name": "Fin", "modifier_name": name}), namespace)
        return namespace["result"], modifiers

    def test_missing_name_does_not_report_application(self):
        with self.assertRaisesRegex(KeyError, "Modifier not found"):
            self.run_apply("Subdivision", {"CANCELLED"})

    def test_cancelled_operator_does_not_report_application(self):
        with self.assertRaisesRegex(RuntimeError, "was not applied"):
            self.run_apply("Subsurf", {"CANCELLED"})

    def test_finished_operator_must_really_remove_modifier(self):
        with self.assertRaisesRegex(RuntimeError, "was not applied"):
            self.run_apply("Subsurf", {"FINISHED"})

    def test_actual_application_reports_the_actual_name(self):
        result, modifiers = self.run_apply("Subsurf", {"FINISHED"}, remove=True)
        self.assertEqual(result, {"object": "Fin", "applied": "Subsurf"})
        self.assertFalse(modifiers)


class ToolPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_client_receives_policy_and_fallback_schema(self) -> None:
        async with create_connected_server_and_client_session(server.mcp) as client:
            initialized = await client.initialize()
            self.assertEqual(initialized.instructions, server.TOOL_USAGE_INSTRUCTIONS)
            self.assertEqual(initialized.serverInfo.version, server.__version__)
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            self.assertIn("batch_tools", tools)
            self.assertIn("create_mesh", tools)
            self.assertTrue(tools["get_object_info"].annotations.readOnlyHint)
            self.assertEqual(
                set(tools["execute_blender_code"].inputSchema["required"]),
                {"code", "fallback_reason", "tools_considered"},
            )
            with patch.object(server, "_call") as call:
                result = await client.call_tool("execute_blender_code", {"code": "print('hello')"})
            self.assertTrue(result.isError)
            call.assert_not_called()
            self.assertEqual(len(tools), 118)
            self.assertEqual(len((await client.list_resources()).resources), 2)
            self.assertEqual((await client.list_prompts()).prompts[0].name, "blender_workflow")

    async def test_python_fallback_requires_reviewed_dedicated_tools(self) -> None:
        invalid = (
            {"fallback_reason": "  ", "tools_considered": ["create_material"]},
            {"fallback_reason": "Custom shader nodes", "tools_considered": []},
            {"fallback_reason": "Custom shader nodes", "tools_considered": ["unknown"]},
            {"fallback_reason": "Custom shader nodes", "tools_considered": ["execute_blender_code"]},
            {"fallback_reason": "Custom shader nodes", "tools_considered": ["batch_tools"]},
            {"fallback_reason": "Custom shader nodes", "tools_considered": ["search_tools", "get_tool_help"]},
        )
        for arguments in invalid:
            with self.subTest(arguments=arguments), patch.object(server, "_call") as call:
                with self.assertRaises(ToolError):
                    await server.mcp.call_tool("execute_blender_code", {"code": "print('hello')", **arguments})
                call.assert_not_called()

    async def test_python_fallback_remains_available_for_missing_capabilities(self) -> None:
        with patch.object(server, "_call", return_value={"output": "nodes updated\n"}) as call:
            result = await server.mcp.call_tool("execute_blender_code", {
                "code": "print('nodes updated')",
                "fallback_reason": "bake_physics supports rigid bodies, not cloth point-cache baking",
                "tools_considered": ["bake_physics", "add_modifier", "search_tools", "get_tool_help"],
            })
        call.assert_called_once_with("execute_code", {"code": "print('nodes updated')"})
        content = result[0] if isinstance(result, tuple) else result
        self.assertEqual(content[0].text, "nodes updated\n")

    async def test_batch_calls_existing_tools_with_their_normal_arguments(self) -> None:
        connection = Mock()
        connection.send_batch.return_value = {"completed": 2, "total": 2, "failed": False, "results": [
            {"index": 0, "tool": "create_object", "result": {"name": "Box.001"}},
            {"index": 1, "tool": "add_modifier", "result": {"modifier": "Bevel"}},
        ]}
        with patch.object(server, "_get_conn", return_value=connection):
            result = await server.batch_tools([
                server.BlenderToolCall(tool="create_object", arguments={"name": "Box", "type": "cube"}),
                server.BlenderToolCall(tool="add_modifier", arguments={
                    "object_name": {"$ref": "0.name"}, "type": "BEVEL", "settings": {"width": 0.1},
                }),
            ])
        connection.send_batch.assert_called_once_with([
            {"command": "create_object", "arguments": {"type": "cube", "light_type": "POINT", "name": "Box"}},
            {"command": "add_modifier", "arguments": {"object_name": {"$ref": "0.name"}, "type": "BEVEL", "settings": {"width": 0.1}}},
        ])
        self.assertFalse(result.isError)
        summary = json.loads(result.content[0].text)
        self.assertEqual(summary["completed"], 2)
        self.assertEqual(summary["results"][0]["result"], {"name": "Box.001"})

    async def test_batch_validates_all_operations_before_editing_blender(self) -> None:
        invalid = (
            {"tool": "unknown"},
            {"tool": "execute_blender_code", "arguments": {"code": "print('hello')"}},
            {"tool": "batch_tools", "arguments": {"operations": []}},
            {"tool": "modify_object", "arguments": {}},
            {"tool": "modify_object", "arguments": {"name": "Box", "location": "bad"}},
            {"tool": "create_object", "arguments": {"unsupported": True}},
            {"tool": "create_object", "arguments": {}, "unexpected": True},
            {"tool": "modify_object", "arguments": {"name": {"$ref": "1.name"}}},
            {"tool": "modify_object", "arguments": {"name": {"$ref": "-1.name"}}},
        )
        for operation in invalid:
            with self.subTest(operation=operation), patch.object(server, "_get_conn") as call:
                with self.assertRaises(ToolError):
                    await server.mcp.call_tool("batch_tools", {"operations": [
                        {"tool": "create_object", "arguments": {"name": "Box"}}, operation,
                    ]})
                call.assert_not_called()

    async def test_batch_stops_at_first_blender_error_and_reports_completed_edits(self) -> None:
        connection = Mock()
        connection.send_batch.return_value = {"completed": 1, "total": 3, "failed": True, "results": [
            {"index": 0, "tool": "create_object", "result": {"name": "Box"}},
            {"index": 1, "tool": "modify_object", "error": "Object not found"},
        ]}
        with patch.object(server, "_get_conn", return_value=connection):
            result = await server.batch_tools([
                server.BlenderToolCall(tool="create_object", arguments={"name": "Box"}),
                server.BlenderToolCall(tool="modify_object", arguments={"name": "Missing"}),
                server.BlenderToolCall(tool="delete_object", arguments={"name": "Box"}),
            ])
        self.assertTrue(result.isError)
        connection.send_batch.assert_called_once()
        summary = json.loads(result.content[0].text)
        self.assertEqual(summary["completed"], 1)
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["results"][1]["index"], 1)
        self.assertIn("Object not found", summary["results"][1]["error"])

    async def test_batch_preserves_mcp_image_content(self) -> None:
        data = base64.b64encode(b"png-bytes").decode()
        connection = Mock()
        connection.send_batch.return_value = {"completed": 1, "total": 1, "failed": False, "results": [
            {"index": 0, "tool": "render_preview", "result": {"data": data}},
        ]}
        async with create_connected_server_and_client_session(server.mcp) as client:
            with patch.object(server, "_get_conn", return_value=connection):
                result = await client.call_tool("batch_tools", {"operations": [
                    {"tool": "render_preview", "arguments": {"max_size": 256}},
                ]})
        self.assertFalse(result.isError)
        self.assertEqual(result.content[1].type, "image")
        self.assertEqual(result.content[1].data, data)
        self.assertEqual(result.content[1].mimeType, "image/png")
        summary = json.loads(result.content[0].text)
        self.assertEqual(summary["results"][0]["result"]["image_content_index"], 1)

    async def test_batch_size_limits_do_not_execute_anything(self) -> None:
        for count in (0, 101):
            with self.subTest(count=count), patch.object(server, "_call") as call:
                with self.assertRaises(ValueError):
                    await server.batch_tools([server.BlenderToolCall(tool="get_scene_info")] * count)
                call.assert_not_called()

    async def test_advanced_tool_batch_prepares_serializable_commands(self) -> None:
        connection = Mock()
        connection.send_batch.return_value = {"completed": 1, "total": 1, "failed": False, "results": []}
        with patch.object(server, "_get_conn", return_value=connection):
            await server.batch_tools([server.BlenderToolCall(tool="set_properties", arguments={
                "target": "OBJECT_DATA", "name": "Lamp", "properties": {"energy": 500},
            })])
        commands = connection.send_batch.call_args.args[0]
        json.dumps(commands)
        self.assertEqual(commands[0]["arguments"]["properties"], {"energy": 500})

    async def test_search_and_help_work_without_blender(self) -> None:
        with patch.object(server, "_get_conn") as connection:
            result = await server.search_tools("shader node")
            self.assertIn("edit_node_tree", {tool["name"] for tool in result["tools"]})
            help_ = await server.get_tool_help("mesh_edit")
            self.assertIn("operation", help_["input_schema"]["properties"])
            connection.assert_not_called()

    async def test_help_accepts_known_host_prefixes_and_origin_schema_is_explicit(self):
        for prefix in ("blender_", "mcp__blender__", "mcp_blender_local_", "mcp__blender_local__"):
            result = await server.get_tool_help(prefix + "set_object_origin")
            self.assertEqual(result["name"], "set_object_origin")
            self.assertEqual(result["input_schema"]["properties"]["center"]["enum"], ["MEDIAN", "BOUNDS"])
        with self.assertRaises(ValueError):
            await server.get_tool_help("unknown_tool")

    async def test_mesh_domain_and_settings_fail_full_preflight_without_dispatch(self):
        for arguments in (
            {"object_name": "Mesh", "operation": "TRANSFORM", "settings": {"scale": [1, 1, 1]}},
            {"object_name": "Mesh", "operation": "BEVEL", "domain": "FACE"},
            {"object_name": "Mesh", "operation": "INSET", "domain": "VERT"},
            {"object_name": "Mesh", "operation": "SUBDIVIDE", "domain": "EDGE", "settings": {"thickness": 1}},
        ):
            with self.subTest(arguments=arguments), patch.object(server, "_get_conn") as connection:
                with self.assertRaises(ToolError):
                    await server.mcp.call_tool("batch_tools", {"operations": [
                        {"tool": "create_object", "arguments": {"name": "Mesh"}},
                        {"tool": "mesh_edit", "arguments": arguments},
                    ]})
                connection.assert_not_called()

    async def test_production_schemas_and_model_batch_serialization(self) -> None:
        tools = {tool.name: tool for tool in await server.mcp.list_tools()}
        self.assertTrue(tools["detect_collisions"].annotations.readOnlyHint)
        self.assertIn("pole_target_name", tools["configure_ik"].inputSchema["required"])
        connection = Mock()
        connection.send_batch.return_value = {"completed": 1, "total": 1, "failed": False, "results": []}
        with patch.object(server, "_get_conn", return_value=connection):
            await server.batch_tools([server.BlenderToolCall(tool="add_driver", arguments={
                "target": "OBJECT", "name": "Driven", "data_path": "location", "index": 1,
                "variables": [{"name": "var", "targets": [{"name": "Control", "data_path": "location[0]"}]}],
                "expression": "var * 2",
            })])
        commands = connection.send_batch.call_args.args[0]
        json.dumps(commands)
        self.assertEqual(commands[0]["arguments"]["variables"][0]["targets"][0]["name"], "Control")

    async def test_socket_work_does_not_block_mcp_tool_discovery(self) -> None:
        started, release = threading.Event(), threading.Event()

        def slow_command(_command, _arguments):
            started.set()
            release.wait(timeout=2)
            return {"name": "Cube"}

        connection = Mock()
        connection.send_command.side_effect = slow_command
        with patch.object(server, "_get_conn", return_value=connection):
            pending = asyncio.create_task(server.mcp.call_tool("get_object_info", {"name": "Cube"}))
            try:
                await asyncio.wait_for(asyncio.to_thread(started.wait), timeout=0.5)
                await asyncio.wait_for(server.mcp.call_tool("search_tools", {"query": "UV"}), timeout=0.5)
                self.assertFalse(pending.done())
            finally:
                release.set()
                await pending


if __name__ == "__main__":
    unittest.main()
