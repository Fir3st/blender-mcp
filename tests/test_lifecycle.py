"""Process-lifecycle regressions for a stdio client that keeps pipes open."""

import json
import os
from pathlib import Path
import select
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
INITIALIZE = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
               "clientInfo": {"name": "lifecycle-test", "version": "1"}},
}
INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}


class StdioLifecycleTests(unittest.TestCase):
    def start(self, session_timeout="0.5"):
        environment = dict(os.environ, BLENDER_SESSION_TIMEOUT=session_timeout,
                           BLENDER_SESSION_PROBE_TIMEOUT="0.3", BLENDER_SESSION_PROBE_RETRY="0.2")
        return subprocess.Popen(
            [sys.executable, "-m", "blender_mcp_local.server", "--session-timeout", session_timeout,
             "--no-error-log"],
            cwd=ROOT, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1,
        )

    def read_message(self, process, timeout=5):
        ready, _, _ = select.select([process.stdout], [], [], timeout)
        if not ready:
            return None
        line = process.stdout.readline()
        return json.loads(line) if line else None

    def initialize(self, process):
        process.stdin.write(json.dumps(INITIALIZE) + "\n")
        process.stdin.flush()
        message = self.read_message(process)
        self.assertIsNotNone(message)
        self.assertEqual(message["id"], 1)
        self.assertIn("result", message)
        process.stdin.write(json.dumps(INITIALIZED) + "\n")
        process.stdin.flush()

    def stop(self, process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        for stream in (process.stdin, process.stdout, process.stderr):
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    def test_silent_client_is_detected_and_does_not_leave_a_bridge_running(self):
        process = self.start()
        try:
            self.initialize(process)
            # Keep stdin open and ignore ping requests, like a client session
            # that ended while its process still owns the pipe.
            self.assertEqual(process.wait(timeout=8), 0)
            self.assertIn("stopped responding", process.stderr.read())
        finally:
            self.stop(process)

    def test_idle_client_that_answers_pings_stays_alive(self):
        process = self.start()
        try:
            self.initialize(process)
            pings = 0
            while pings < 2:
                message = self.read_message(process)
                self.assertIsNotNone(message, "bridge exited or stopped probing an idle client")
                if message.get("method") == "ping":
                    process.stdin.write(json.dumps(
                        {"jsonrpc": "2.0", "id": message["id"], "result": {}}) + "\n")
                    process.stdin.flush()
                    pings += 1
            self.assertIsNone(process.poll())
            process.stdin.close()
            self.assertEqual(process.wait(timeout=5), 0)
        finally:
            self.stop(process)

    def test_closing_stdin_terminates_the_bridge(self):
        process = self.start(session_timeout="0")
        try:
            self.initialize(process)
            process.stdin.close()
            self.assertEqual(process.wait(timeout=5), 0)
        finally:
            self.stop(process)


if __name__ == "__main__":
    unittest.main()
