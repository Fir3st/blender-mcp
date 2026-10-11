import importlib.util
import hashlib
import json
from pathlib import Path
import struct
import sys
import types
import unittest
from unittest.mock import Mock


def load_addon_module():
    bpy = types.ModuleType("bpy")
    bpy.types = types.SimpleNamespace(Operator=object, Panel=object, PropertyGroup=object)
    bpy.props = types.SimpleNamespace(
        StringProperty=lambda **_kwargs: None,
        IntProperty=lambda **_kwargs: None,
        BoolProperty=lambda **_kwargs: None,
    )
    mathutils = types.ModuleType("mathutils")
    mathutils.Vector = object
    previous_bpy = sys.modules.get("bpy")
    previous_mathutils = sys.modules.get("mathutils")
    sys.modules["bpy"] = bpy
    sys.modules["mathutils"] = mathutils
    try:
        spec = importlib.util.spec_from_file_location("blender_mcp_test_addon", Path(__file__).parents[1] / "addon.py")
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module
    finally:
        if previous_bpy is None:
            sys.modules.pop("bpy", None)
        else:
            sys.modules["bpy"] = previous_bpy
        if previous_mathutils is None:
            sys.modules.pop("mathutils", None)
        else:
            sys.modules["mathutils"] = previous_mathutils


class PartialSendSocket:
    def __init__(self) -> None:
        self.output = bytearray()
        self.calls = 0

    def send(self, data: bytes) -> int:
        self.calls += 1
        if self.calls % 2 == 0:
            raise BlockingIOError
        size = min(7, len(data))
        self.output.extend(data[:size])
        return size

    def close(self) -> None:
        pass


class AddonTransportTests(unittest.TestCase):
    def test_nonblocking_partial_response_is_buffered(self) -> None:
        addon = load_addon_module()
        server = addon.BlenderMCPServer("127.0.0.1", 9876)
        socket = PartialSendSocket()
        server.client = socket
        server.client_protocol = "length"
        response = {"status": "success", "result": {"data": "x" * 100}}

        server._send(response)
        while server.send_buf:
            server._flush_send()

        size = struct.unpack(">I", socket.output[:4])[0]
        self.assertEqual(json.loads(socket.output[4:4 + size]), response)
        self.assertIs(server.client, socket)
        self.assertGreater(socket.calls, 2)

    def test_library_checksum_and_dispatch(self) -> None:
        addon = load_addon_module()
        server = addon.BlenderMCPServer("127.0.0.1", 9876)
        source = "def dispatch(command, arguments):\n    return {'command': command, 'arguments': arguments}\n"
        library_id = hashlib.sha256(source.encode()).hexdigest()
        result = server._dispatch(json.dumps({"type": "load_command_library", "params": {
            "source": source, "library_id": library_id,
        }}).encode())
        self.assertEqual(result["status"], "success")
        result = server._dispatch(json.dumps({"type": "command", "params": {
            "library_id": library_id, "command": "get_scene_info", "arguments": {},
        }}).encode())
        self.assertEqual(result["result"]["command"], "get_scene_info")
        invalid = server._dispatch(json.dumps({"type": "load_command_library", "params": {
            "source": source, "library_id": "wrong",
        }}).encode())
        self.assertEqual(invalid["status"], "error")
        self.assertEqual(addon._library_id, library_id)

    def test_missing_library_has_explicit_nonmutating_error(self) -> None:
        addon = load_addon_module()
        server = addon.BlenderMCPServer("127.0.0.1", 9876)
        result = server._dispatch(json.dumps({"type": "command", "params": {
            "library_id": "missing", "command": "create_object", "arguments": {},
        }}).encode())
        self.assertEqual(result["code"], "LIBRARY_NOT_LOADED")

    def test_batch_resolves_actual_names_and_stops_at_first_error(self) -> None:
        addon = load_addon_module()
        addon._library_id = "test"
        addon._command_library = Mock(side_effect=[{"name": "Box.001"}, {"name": "Box.001"}, ValueError("bad")])
        result = addon.cmd_batch({"library_id": "test", "operations": [
            {"command": "create_object", "arguments": {"name": "Box"}},
            {"command": "modify_object", "arguments": {"name": {"$ref": "0.name"}}},
            {"command": "modify_object", "arguments": {"name": "Missing"}},
            {"command": "delete_object", "arguments": {"name": "Box.001"}},
        ]})
        self.assertTrue(result["failed"])
        self.assertEqual(result["completed"], 2)
        self.assertEqual(addon._command_library.call_args_list[1].args, ("modify_object", {"name": "Box.001"}))
        self.assertEqual(addon._command_library.call_count, 3)

    def test_both_framing_protocols_dispatch_real_messages(self) -> None:
        for protocol in ("legacy", "length"):
            with self.subTest(protocol=protocol):
                addon = load_addon_module()
                server = addon.BlenderMCPServer("127.0.0.1", 9876)
                socket = PartialSendSocket()
                server.client = socket
                data = json.dumps({"type": "bridge_info"}).encode()
                server.recv_buf.extend(data + b"\0" if protocol == "legacy" else struct.pack(">I", len(data)) + data)
                server._process_messages()
                while server.send_buf:
                    server._flush_send()
                output = socket.output[:-1] if protocol == "legacy" else socket.output[4:]
                self.assertEqual(json.loads(output)["result"]["api_version"], 1)

    def test_invalid_json_root_is_an_error_response(self) -> None:
        addon = load_addon_module()
        server = addon.BlenderMCPServer("127.0.0.1", 9876)
        self.assertEqual(server._dispatch(b"[]")["status"], "error")


if __name__ == "__main__":
    unittest.main()
