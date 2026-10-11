"""Clean, isolated Blender host for run_blender_integration.py (no user file)."""

import importlib.util
import json
from pathlib import Path
import sys
import time

import bpy


root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("blender_mcp_integration_addon", root / "addon.py")
addon = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = addon
spec.loader.exec_module(addon)
addon.register()
server = addon.BlenderMCPServer("127.0.0.1", 0)
server.start()
running = True


def shutdown(_params):
    global running
    running = False
    return {"stopped": True}


addon.HANDLERS["test_shutdown"] = shutdown
if "--execute-only" in sys.argv:
    def official_execute(params):
        namespace = {"result": {}}
        exec(compile(params["code"], "<execute-wire>", "exec"), namespace)
        result = namespace["result"]
        if not isinstance(result, dict): raise ValueError("Official result must be a dict")
        return result
    addon.HANDLERS = {"execute": official_execute, "test_shutdown": shutdown}
    buffered_send = server._send

    def small_response_send(response):
        # Reproduce an execute endpoint that closes rather than flushing a large
        # nonblocking send. The bridge must transfer data without rerendering.
        if len(json.dumps(response).encode()) > 32768:
            server._drop_client()
            return
        buffered_send(response)

    server._send = small_response_send
print(f"MCP_TEST_PORT={server.listen_sock.getsockname()[1]}", flush=True)
deadline = time.monotonic() + 300
try:
    while running and time.monotonic() < deadline:
        server._tick()
        time.sleep(0.01)
finally:
    server._flush_send()
    server.stop()
