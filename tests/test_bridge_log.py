import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from mcp.server.fastmcp.exceptions import ToolError
from blender_mcp_local import server
from blender_mcp_local.bridge_log import BridgeLog
from blender_mcp_local.error_log import ErrorJournal


class BridgeLogFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "bridge.log"

    def test_creates_private_rotating_file(self):
        log = BridgeLog(self.path, max_bytes=220, backups=2)
        for index in range(20):
            log.info("event", index=index, tool="get_scene_info")
        self.assertTrue(self.path.exists())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        files = list(Path(self.temp.name).glob("bridge.log*"))
        self.assertLessEqual(len(files), 3)
        self.assertTrue(all(path.stat().st_mode & 0o777 == 0o600 for path in files))
        self.assertIn("event", self.path.read_text())
        log.close()

    def test_can_be_disabled(self):
        log = BridgeLog()
        log.info("event", tool="get_scene_info")
        self.assertFalse(log.status()["enabled"])
        self.assertFalse(self.path.exists())

    def test_rejects_sensitive_filenames_and_symlinks(self):
        with self.assertRaisesRegex(ValueError, "non-sensitive"):
            BridgeLog(Path(self.temp.name) / ".env.log")
        target = Path(self.temp.name) / "target.log"
        target.write_text("keep")
        alias = Path(self.temp.name) / "alias.log"
        alias.symlink_to(target)
        log = BridgeLog(alias)
        self.assertIsNotNone(log.status()["last_write_error"])
        self.assertEqual(target.read_text(), "keep")


class BridgeLogServerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "bridge.log"
        self.log = BridgeLog(self.path)
        self.journal = ErrorJournal(Path(self.temp.name) / "errors.jsonl")
        self.patch = patch.object(server, "_bridge_log", self.log)
        self.journal_patch = patch.object(server, "_error_journal", self.journal)
        self.patch.start()
        self.journal_patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(self.journal_patch.stop)

    async def test_tool_calls_and_arguments_are_logged_without_raw_values(self):
        marker = "DO_NOT_LOG_ARGUMENT"
        connection = Mock()
        connection.send_command.return_value = {"name": "Box"}
        with patch.object(server, "_get_conn", return_value=connection):
            await server.mcp.call_tool("get_scene_info", {})
            await server.mcp.call_tool("create_object", {"type": "cube", "name": marker})
        text = self.path.read_text()
        self.assertIn("tool_started", text)
        self.assertIn("tool_finished", text)
        self.assertIn('tool="get_scene_info"', text)
        self.assertIn('status="ok"', text)
        self.assertIn("duration_ms=", text)
        self.assertNotIn(marker, text)

    async def test_tool_failure_is_logged_and_journaled(self):
        marker = "DO_NOT_LOG_TOOL_ARGUMENT"
        connection = Mock()
        connection.send_command.side_effect = RuntimeError("operation failed")
        with patch.object(server, "_get_conn", return_value=connection):
            with self.assertRaises(ToolError):
                await server.mcp.call_tool("create_object", {"type": "cube", "name": marker})
        text = self.path.read_text()
        self.assertIn("status=\"error\"", text)
        self.assertIn("error_type=", text)
        self.assertIn("error_id=", text)
        self.assertNotIn(marker, text)

    async def test_disabled_log_does_not_create_a_file(self):
        self.log.close()
        self.path.unlink()
        disabled = BridgeLog()
        with patch.object(server, "_bridge_log", disabled):
            with patch.object(server, "_get_conn", return_value=Mock(send_command=Mock(return_value={}))):
                await server.mcp.call_tool("get_scene_info", {})
        self.assertFalse(self.path.exists())


class BridgeLogCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.error_path = Path(self.temp.name) / "errors.jsonl"
        self.log_path = Path(self.temp.name) / "bridge.log"
        self.original_log = server._bridge_log
        self.addCleanup(setattr, server, "_bridge_log", self.original_log)

    def main(self, run=None, arguments=None):
        with patch.object(server, "default_log_path", return_value=self.error_path), patch.object(
            server, "default_bridge_log_path", return_value=self.log_path
        ), patch.object(server.mcp, "run", side_effect=run if run is not None else None):
            server.main(arguments or [])

    def test_startup_and_shutdown_are_written(self):
        self.main()
        text = self.log_path.read_text()
        self.assertIn("startup", text)
        self.assertIn("shutdown", text)
        self.assertIn('version=', text)
        self.assertIn('host="127.0.0.1"', text)

    def test_crash_is_written_before_the_bridge_exits(self):
        with self.assertRaisesRegex(RuntimeError, "boom"):
            self.main(run=RuntimeError("boom"))
        text = self.log_path.read_text()
        self.assertIn("server_crashed", text)
        self.assertIn("shutdown", text)

    def test_no_log_file_disables_the_operational_log(self):
        self.main(arguments=["--no-log-file", "--no-error-log"])
        self.assertFalse(self.log_path.exists())
        self.assertFalse(server._bridge_log.status()["enabled"])


if __name__ == "__main__":
    unittest.main()
