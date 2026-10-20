import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from mcp.server.fastmcp.exceptions import ToolError
from mcp.shared.memory import create_connected_server_and_client_session
from blender_mcp_local import server
from blender_mcp_local.error_log import ErrorJournal


class ErrorLogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "errors.jsonl"
        self.journal = ErrorJournal(self.path)
        self.patch = patch.object(server, "_error_journal", self.journal)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    async def test_blender_failure_is_logged_once_and_can_be_read_over_mcp(self):
        connection = Mock()
        connection.send_command.side_effect = RuntimeError("ValueError: Cannot load from the current blend file.")
        with patch.object(server, "_get_conn", return_value=connection):
            async with create_connected_server_and_client_session(server.mcp) as client:
                result = await client.call_tool("append_datablocks", {"path": "saved.blend", "names": ["Source"]})
                self.assertTrue(result.isError)
                log = await client.call_tool("get_error_log", {"limit": 10})
                self.assertFalse(log.isError)
                events = log.structuredContent["events"]
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["tool"], "append_datablocks")
                self.assertEqual(events[0]["error_type"], "ValueError")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    async def test_schema_failure_and_unknown_tool_are_logged(self):
        async with create_connected_server_and_client_session(server.mcp) as client:
            for tool, arguments in (("modify_object", {}), ("not_a_tool", {})):
                result = await client.call_tool(tool, arguments)
                self.assertTrue(result.isError)
        self.assertEqual(len(self.journal.read()["events"]), 2)

    async def test_remote_operation_error_is_a_received_response_not_in_flight(self):
        for backend in ("native", "execute"):
            with self.subTest(backend=backend):
                connection = server.BlenderConnection("127.0.0.1", 9876)
                connection.library_ready = True
                connection.backend = backend
                with patch.object(server, "_get_conn", return_value=connection), patch.object(
                    connection, "_request", return_value={"status": "error", "message": "ValueError: Unknown RNA property: unavailable"}
                ):
                    with self.assertRaises(ToolError):
                        await server.mcp.call_tool("get_properties", {"target": "SCENE", "paths": ["unavailable"]})
                event = self.journal.read()["events"][-1]
                self.assertEqual(event["execution_state"], "response_received")
                self.assertEqual(event["error_type"], "ValueError")

    async def test_connection_timeout_remains_in_flight_and_is_not_retried(self):
        connection = server.BlenderConnection("127.0.0.1", 9876)
        connection.library_ready = True
        with patch.object(server, "_get_conn", return_value=connection), patch.object(
            connection, "_request", side_effect=RuntimeError("Blender connection error: timed out")
        ) as request:
            with self.assertRaises(ToolError):
                await server.mcp.call_tool("get_scene_info", {})
        request.assert_called_once()
        self.assertEqual(self.journal.read()["events"][-1]["execution_state"], "in_flight")

    async def test_batch_step_failure_has_its_tool_index_and_no_duplicate_event(self):
        connection = Mock()
        connection.send_batch.return_value = {"completed": 0, "total": 1, "failed": True, "results": [
            {"index": 0, "tool": "modify_object", "error": "ValueError: Object not found: DEMO_VALUE"},
        ]}
        with patch.object(server, "_get_conn", return_value=connection):
            async with create_connected_server_and_client_session(server.mcp) as client:
                result = await client.call_tool("batch_tools", {"operations": [
                    {"tool": "modify_object", "arguments": {"name": "Missing"}},
                ]})
                self.assertTrue(result.isError)
        events = self.journal.read()["events"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["tool"], "modify_object")
        self.assertEqual(events[0]["batch_index"], 0)

    async def test_raw_values_code_source_lines_and_exception_details_are_not_written(self):
        marker = "DO_NOT_LOG_VALUE"
        error = RuntimeError('Traceback (most recent call last):\n  File "<string>", line 8, in <module>\n    value = "' + marker + '"\nValueError: ' + marker)
        self.journal.record(tool="append_datablocks", error=error, arguments={"path": marker, "names": [marker]},
                            argument_keys=["path", "names"], version=server.__version__)
        self.journal.record(tool="execute_blender_code", error=error, arguments={"code": marker},
                            argument_keys=["code"], version=server.__version__)
        text = self.path.read_text()
        self.assertNotIn(marker, text)
        events = self.journal.read()["events"]
        self.assertEqual(events[-1]["frames"][-1], {"file": "<string>", "line": 8, "function": "<module>"})

    async def test_rotation_disabled_logging_and_storage_failure(self):
        small = ErrorJournal(self.path, max_bytes=1500)
        for _ in range(10):
            small.record(tool="get_scene_info", error=ValueError("failure"), version=server.__version__)
        self.assertLessEqual(len(list(Path(self.temp.name).glob("*.jsonl"))), 3)
        self.assertTrue(small.read()["events"])
        self.assertIsNone(ErrorJournal().record(tool="get_scene_info", error=ValueError()))
        blocked = Path(self.temp.name) / "not_a_directory"
        blocked.write_text("fixture")
        broken = ErrorJournal(blocked / "errors.jsonl")
        self.assertIsNone(broken.record(tool="get_scene_info", error=ValueError()))
        self.assertIsNotNone(broken.status()["last_write_error"])

    async def test_reader_drops_unexpected_and_partial_records(self):
        self.journal.record(tool="get_scene_info", error=ValueError(), version=server.__version__)
        row = json.loads(self.path.read_text())
        row["unexpected"] = "DO_NOT_RETURN_VALUE"
        with self.path.open("a") as output:
            output.write(json.dumps(row) + "\n{partial\n")
        log = self.journal.read()
        self.assertEqual(len(log["events"]), 1)
        self.assertEqual(log["invalid_lines"], 2)
        self.assertNotIn("DO_NOT_RETURN_VALUE", json.dumps(log))

    async def test_successful_tools_do_not_create_an_error_file(self):
        with patch.object(server, "_get_conn", return_value=Mock(send_command=Mock(return_value={"scene": "Test"}))):
            await server.mcp.call_tool("get_scene_info", {})
        self.assertFalse(self.path.exists())

    async def test_cli_enables_logging_by_default_and_can_disable_it(self):
        with patch.object(server, "default_log_path", return_value=self.path), patch.object(server.mcp, "run"):
            server.main([])
            self.assertTrue(server._error_journal.status()["enabled"])
            self.assertEqual(server._error_journal.status()["path"], str(self.path))
            server.main(["--no-error-log"])
            self.assertFalse(server._error_journal.status()["enabled"])

    async def test_event_filter_and_symlink_guard(self):
        identifier = self.journal.record(tool="append_datablocks", error=ValueError(), version=server.__version__)
        self.journal.record(tool="get_scene_info", error=ValueError(), version=server.__version__)
        self.assertEqual(len(self.journal.read(event_id=identifier)["events"]), 1)
        self.assertEqual(len(self.journal.read(tool="append_datablocks")["events"]), 1)
        alias = Path(self.temp.name) / "alias.jsonl"
        alias.symlink_to(self.path)
        linked = ErrorJournal(alias)
        self.assertIsNone(linked.record(tool="get_scene_info", error=ValueError()))
        with self.assertRaisesRegex(ValueError, "symlink"):
            linked.read()

    async def test_cancelled_request_waiting_for_blender_is_not_dispatched(self):
        connection = Mock(send_command=Mock(return_value={"scene": "Test"}))
        server._conn_lock.acquire()
        try:
            with patch.object(server, "_get_conn", return_value=connection):
                pending = asyncio.create_task(server.mcp.call_tool("get_scene_info", {}))
                await asyncio.sleep(0.05)
                pending.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await pending
                server._conn_lock.release()
                await asyncio.sleep(0.05)
                connection.send_command.assert_not_called()
        finally:
            if server._conn_lock.locked(): server._conn_lock.release()
        events = self.journal.read()["events"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["execution_state"], "not_started")

    async def test_batch_failure_retains_safe_remote_frames(self):
        runtime = {"dispatch": Mock(side_effect=ValueError("Unknown RNA property: DO_NOT_LOG_VALUE"))}
        exec(server._BATCH_RUNTIME, runtime)
        summary = runtime["dispatch_batch"]([{"command": "add_modifier", "arguments": {}}])
        self.assertIn("Traceback", summary["results"][0]["error"])
        self.journal.record(tool="add_modifier", error=RuntimeError(summary["results"][0]["error"]), version=server.__version__)
        text = self.path.read_text()
        self.assertNotIn("DO_NOT_LOG_VALUE", text)
        self.assertTrue(self.journal.read()["events"][0]["frames"])

    async def test_node_connection_and_mesh_errors_have_safe_specific_summaries(self):
        cases = [
            ("Connection refused: DO_NOT_LOG_VALUE", "The Blender endpoint refused the connection; start its MCP server and verify the host/port."),
            ("Node type DO_NOT_LOG_VALUE undefined", "The node type is unavailable or unsupported by this Blender version or node-tree type."),
            ("Node owner not found: DO_NOT_LOG_VALUE", "The material/world owning the requested node tree was not found."),
            ("Node tree not found; DO_NOT_LOG_VALUE", "The requested node tree is absent or disabled; create or enable it first."),
            ("Node name already exists; DO_NOT_LOG_VALUE", "A requested node name already exists; use set_node_inputs or explicitly replace it."),
            ("A node to remove was not found: DO_NOT_LOG_VALUE", "A node selected for removal was not found; inspect the actual node names."),
            ("Link references a missing or removed node: DO_NOT_LOG_VALUE", "A node link references a missing or removed node."),
            ("Incompatible node socket types: DO_NOT_LOG_VALUE", "The requested node link connects incompatible socket types."),
            ("Not an RNA pointer property: DO_NOT_LOG_VALUE", "A nested RNA path traverses a non-pointer or unknown property."),
            ("Object 'DO_NOT_LOG_VALUE' is not a mesh", "Mesh inspection/editing was requested for a non-mesh object."),
        ]
        for message, expected in cases:
            with self.subTest(message=message):
                event_id = self.journal.record(tool="edit_node_tree", error=RuntimeError(message), version=server.__version__)
                self.assertEqual(self.journal.read(event_id=event_id)["events"][0]["summary"], expected)
        self.assertNotIn("DO_NOT_LOG_VALUE", self.path.read_text())


if __name__ == "__main__":
    unittest.main()
