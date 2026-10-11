"""Local-only, tool-first MCP bridge to Blender, with a cached command runtime."""

from __future__ import annotations

import base64
import asyncio
import argparse
import hashlib
import inspect
import json
import os
import socket
import struct
import threading
import textwrap
import zlib
from contextlib import asynccontextmanager
from contextvars import ContextVar
from functools import wraps
from typing import Any, Literal

from mcp.server.fastmcp import Context, FastMCP, Image
from mcp.server.fastmcp.tools import Tool
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from blender_mcp_local import __version__
from blender_mcp_local.advanced import COMMAND_CODE, register_tools
from blender_mcp_local.production import COMMAND_CODE as PRODUCTION_CODE, register_tools as register_production_tools
from blender_mcp_local.error_log import ErrorJournal, default_log_path


HOST = os.environ.get("BLENDER_HOST", "127.0.0.1")
PORT = int(os.environ.get("BLENDER_PORT", "9876"))
TIMEOUT = float(os.environ.get("BLENDER_TIMEOUT", "180"))
PROTOCOL = os.environ.get("BLENDER_PROTOCOL", "legacy").lower()
EXECUTE_CHUNK_BYTES = 16 * 1024
MAX_RESULT_BYTES = 64 * 1024 * 1024
_error_context: ContextVar[dict[str, Any] | None] = ContextVar("error_context", default=None)
_error_journal = ErrorJournal()


class BlenderMCP(FastMCP):
    """Keep socket work off the MCP event loop and expose useful tool hints."""

    async def call_tool(self, name, arguments):
        tool = self._tool_manager.get_tool(name)
        context = {"logged": False, "cancelled": False, "execution_state": "not_started", "tool": name if tool else "unknown_tool",
                   "argument_keys": list(tool.parameters.get("properties", {})) if tool else []}
        marker = _error_context.set(context)
        try:
            result = await super().call_tool(name, arguments)
            if isinstance(result, CallToolResult) and result.isError and not context["logged"]:
                _record_error(context["tool"], RuntimeError("Tool returned an error result"), arguments, phase="returned_error")
            return result
        except asyncio.CancelledError as error:
            context["cancelled"] = True
            if not context["logged"]:
                _record_error(context["tool"], error, arguments, phase="cancelled")
            raise
        except Exception as error:
            if not context["logged"]:
                _record_error(context["tool"], error, arguments, phase="tool")
            raise
        finally:
            _error_context.reset(marker)

    def add_tool(self, fn, *args, **kwargs):
        if kwargs.get("annotations") is None:
            name = kwargs.get("name") or fn.__name__
            read_only = name.startswith(("get_", "list_", "find_", "search_", "audit_"))
            kwargs["annotations"] = ToolAnnotations(
                readOnlyHint=read_only,
                destructiveHint=not read_only,
                openWorldHint=False,
            )
        if not inspect.iscoroutinefunction(fn):
            original = fn

            @wraps(original)
            async def threaded(*arguments, **keywords):
                return await asyncio.to_thread(original, *arguments, **keywords)

            fn = threaded
        return super().add_tool(fn, *args, **kwargs)


class BlenderConnection:
    def __init__(self, host: str, port: int, protocol: str = PROTOCOL) -> None:
        if protocol not in {"legacy", "length"}:
            raise ValueError("protocol must be 'legacy' or 'length'")
        self.host = host
        self.port = port
        self.protocol = protocol
        self.sock: socket.socket | None = None
        self.library_ready = False
        self.backend = "native"

    def connect(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(TIMEOUT)
        s.connect((self.host, self.port))
        self.sock = s

    def disconnect(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None

    def _ensure(self) -> socket.socket:
        if self.sock is None:
            self.connect()
        assert self.sock is not None
        return self.sock

    def _recv_exact(self, size: int) -> bytes:
        sock = self._ensure()
        buf = bytearray()
        while len(buf) < size:
            chunk = sock.recv(size - len(buf))
            if not chunk:
                raise ConnectionError("Blender closed the connection")
            buf += chunk
        return bytes(buf)

    def _recv_response(self) -> dict[str, Any]:
        if self.protocol == "length":
            (size,) = struct.unpack(">I", self._recv_exact(4))
            if size > 64 * 1024 * 1024:
                raise ConnectionError(f"Blender response is too large: {size} bytes")
            data = self._recv_exact(size)
        else:
            sock = self._ensure()
            buf = bytearray()
            while b"\0" not in buf:
                chunk = sock.recv(65536)
                if not chunk:
                    raise ConnectionError("Blender closed the connection")
                buf.extend(chunk)
                if len(buf) > 64 * 1024 * 1024:
                    raise ConnectionError("Blender response is too large")
            data = bytes(buf[:buf.index(0)])
        response = json.loads(data.decode("utf-8"))
        if not isinstance(response, dict):
            raise ConnectionError("Blender returned a non-object response")
        return response

    def _request(self, command: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(command, allow_nan=False).encode("utf-8")
        if len(data) > 64 * 1024 * 1024:
            raise ValueError("Blender request exceeds the 64 MiB message limit")
        payload = struct.pack(">I", len(data)) + data if self.protocol == "length" else data + b"\0"
        try:
            sock = self._ensure()
            sock.sendall(payload)
            response = self._recv_response()
        except (OSError, ConnectionError) as e:
            self.disconnect()
            raise RuntimeError(f"Blender connection error: {e}") from e
        finally:
            self.disconnect()

        return response

    @staticmethod
    def _result(response: dict[str, Any]) -> Any:
        if response.get("status") not in ("ok", "success"):
            raise RuntimeError(response.get("message", "Unknown Blender error"))
        return response.get("result", {})

    def execute_code(self, code: str, *, strict_json: bool = True) -> Any:
        command = {"type": "execute", "params": {"code": code, "strict_json": strict_json}}
        if self.protocol == "legacy":
            command = {"type": "execute", "code": code, "strict_json": strict_json}
        return self._result(self._request(command))

    def _load_library(self) -> None:
        if self.backend == "execute":
            self._load_execute_library()
            return
        response = self._request({"type": "bridge_info"})
        message = response.get("message", "").lower()
        if response.get("status") == "error" and "bridge_info" in message and any(
            text in message for text in ("unknown command", "unknown request type")
        ):
            # Blender Lab's official extension accepts only the execute wire
            # command. Keep our tool API and trusted runtime on top of it.
            self.backend = "execute"
            self._load_execute_library()
            return
        info = self._result(response)
        if info.get("api_version") != 1:
            raise RuntimeError("Install Blender MCP addon 1.0.0 before using this bridge")
        source, library_id = _command_library()
        if info.get("library_id") != library_id:
            self._result(self._request({"type": "load_command_library", "params": {
                "source": source, "library_id": library_id,
            }}))
        self.library_ready = True

    def _load_execute_library(self) -> None:
        source, library_id = _command_library()
        # Blender Lab receives 4096 bytes per timer tick. Compress the trusted
        # library so initial upload fits well below its client timeout budget.
        payload = json.dumps({"source_b64": base64.b64encode(zlib.compress(source.encode())).decode(), "library_id": library_id}, allow_nan=False)
        code = f"""
import bpy, json, base64, zlib
payload = json.loads({payload!r})
key = 'blender_mcp_local_command_runtime'
runtime = bpy.app.driver_namespace.get(key)
if not isinstance(runtime, dict) or runtime.get('_library_id') != payload['library_id']:
    runtime = {{'__name__': '__blender_mcp_commands__'}}
    source = zlib.decompress(base64.b64decode(payload['source_b64'])).decode()
    exec(compile(source, '<blender-mcp-command-library>', 'exec'), runtime)
    runtime['_library_id'] = payload['library_id']
    bpy.app.driver_namespace[key] = runtime
result = {{'api_version': 1, 'library_id': runtime['_library_id']}}
"""
        result = self.execute_code(code)
        if not isinstance(result, dict) or result.get("library_id") != library_id:
            raise RuntimeError("Execute-compatible Blender endpoint did not initialize the command runtime")
        self.library_ready = True

    def _execute_library_request(self, type_: str, params: dict[str, Any]) -> Any:
        if type_ not in {"command", "batch"}:
            raise ValueError("Unsupported command-runtime request")
        _, library_id = _command_library()
        payload = json.dumps(params, allow_nan=False)
        invocation = "runtime['dispatch'](params['command'], params['arguments'])" if type_ == "command" else "runtime['dispatch_batch'](params['operations'])"
        code = f"""
import bpy, json
params = json.loads({payload!r})
runtime = bpy.app.driver_namespace.get('blender_mcp_local_command_runtime')
if not isinstance(runtime, dict) or runtime.get('_library_id') != {library_id!r}:
    result = {{'_mcp_runtime_missing': True}}
else:
    result = runtime['transport_result']({invocation})
"""
        return self._read_execute_result(self.execute_code(code))

    def _read_execute_result(self, result: Any) -> Any:
        if not isinstance(result, dict) or set(result) != {"_mcp_chunked_result"}:
            return result
        descriptor = result["_mcp_chunked_result"]
        if not isinstance(descriptor, dict):
            raise RuntimeError("Invalid chunked Blender result descriptor")
        identifier, size, digest = (descriptor.get(key) for key in ("id", "size", "sha256"))
        if (not isinstance(identifier, str) or len(identifier) != 32
                or any(char not in "0123456789abcdef" for char in identifier)
                or type(size) is not int or not 0 < size <= MAX_RESULT_BYTES
                or not isinstance(digest, str) or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)):
            raise RuntimeError("Invalid chunked Blender result descriptor")
        data = bytearray()
        try:
            for offset in range(0, size, EXECUTE_CHUNK_BYTES):
                code = f"""
import bpy
runtime = bpy.app.driver_namespace.get('blender_mcp_local_command_runtime')
if not isinstance(runtime, dict) or 'read_result_chunk' not in runtime:
    raise RuntimeError('Blender result runtime disappeared; the scene operation was not retried')
result = runtime['read_result_chunk']({identifier!r}, {offset})
"""
                chunk = self.execute_code(code)
                if not isinstance(chunk, dict) or not isinstance(chunk.get("data"), str):
                    raise RuntimeError("Invalid Blender result chunk")
                block = base64.b64decode(chunk["data"], validate=True)
                if len(block) != min(EXECUTE_CHUNK_BYTES, size - offset):
                    raise RuntimeError("Incomplete Blender result chunk")
                data.extend(block)
            if hashlib.sha256(data).hexdigest() != digest:
                raise RuntimeError("Blender result checksum mismatch")
            return json.loads(data.decode("utf-8"))
        finally:
            # Never rerun a scene operation after a failed transfer. Cleanup is
            # best effort; the bounded runtime cache expires abandoned data.
            try:
                self.execute_code(f"""
import bpy
runtime = bpy.app.driver_namespace.get('blender_mcp_local_command_runtime')
if isinstance(runtime, dict) and 'release_result' in runtime:
    runtime['release_result']({identifier!r})
result = {{}}
""")
            except RuntimeError:
                pass

    def _library_request(self, type_: str, params: dict[str, Any]) -> Any:
        if not self.library_ready:
            self._load_library()
        _, library_id = _command_library()
        if self.backend == "execute":
            result = self._execute_library_request(type_, params)
            if result == {"_mcp_runtime_missing": True}:
                self.library_ready = False
                self._load_library()
                result = self._execute_library_request(type_, params)
                if result == {"_mcp_runtime_missing": True}:
                    raise RuntimeError("Blender command runtime disappeared after reload")
            return result
        response = self._request({"type": type_, "params": {**params, "library_id": library_id}})
        # This error is returned before any scene operation: safe to reload once.
        if response.get("code") == "LIBRARY_NOT_LOADED":
            self.library_ready = False
            self._load_library()
            response = self._request({"type": type_, "params": {**params, "library_id": library_id}})
        return self._result(response)

    def send_command(self, cmd_type: str, params: dict[str, Any] | None = None) -> Any:
        result = self._library_request("command", {"command": cmd_type, "arguments": params or {}})
        if cmd_type == "get_blender_info":
            result["capabilities"].update(transport=self.backend, native_rpc=self.backend == "native", native_batch=self.backend == "native", batch_single_request=True)
        return result

    def send_batch(self, operations: list[dict[str, Any]]) -> Any:
        return self._library_request("batch", {"operations": operations})


_conn: BlenderConnection | None = None
_conn_lock = threading.Lock()
_prepared_commands: ContextVar[list[dict[str, Any]] | None] = ContextVar("prepared_commands", default=None)
_library_cache: tuple[str, str] | None = None


_BATCH_RUNTIME = '''
import json
import base64
import hashlib
import time
import uuid
import traceback

_result_chunks = {}
_chunk_bytes = 16 * 1024
_max_result_bytes = 64 * 1024 * 1024

def transport_result(result):
    data = json.dumps(result, ensure_ascii=True, allow_nan=False, separators=(',', ':')).encode('utf-8')
    if len(data) <= _chunk_bytes:
        return result
    if len(data) > _max_result_bytes:
        raise ValueError('Blender result exceeds the 64 MiB transport limit')
    now = time.monotonic()
    for identifier, entry in list(_result_chunks.items()):
        if now - entry[1] > 180:
            del _result_chunks[identifier]
    while _result_chunks and (len(_result_chunks) >= 4 or
            sum(len(entry[0]) for entry in _result_chunks.values()) + len(data) > _max_result_bytes):
        del _result_chunks[min(_result_chunks, key=lambda key: _result_chunks[key][1])]
    identifier = uuid.uuid4().hex
    _result_chunks[identifier] = [data, now]
    return {'_mcp_chunked_result': {'id': identifier, 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}}

def read_result_chunk(identifier, offset):
    entry = _result_chunks.get(identifier)
    if entry is None or time.monotonic() - entry[1] > 180:
        _result_chunks.pop(identifier, None)
        raise RuntimeError('Blender result expired or was evicted; the scene operation was not retried')
    data = entry[0]
    if type(offset) is not int or not 0 <= offset < len(data) or offset % _chunk_bytes:
        raise ValueError('Invalid Blender result chunk offset')
    entry[1] = time.monotonic()
    return {'data': base64.b64encode(data[offset:offset + _chunk_bytes]).decode('ascii')}

def release_result(identifier):
    _result_chunks.pop(identifier, None)

def resolve_batch_refs(value, results):
    if isinstance(value, dict):
        if set(value) == {'$ref'}:
            parts = str(value['$ref']).split('.')
            index = int(parts[0])
            if not 0 <= index < len(results) or 'error' in results[index]:
                raise ValueError('Unavailable batch result: ' + str(value['$ref']))
            result = results[index]['result']
            for part in parts[1:]:
                result = result[int(part)] if isinstance(result, list) else result[part]
            return result
        return {key: resolve_batch_refs(item, results) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_batch_refs(item, results) for item in value]
    return value

def dispatch_batch(operations):
    if not 1 <= len(operations) <= 100:
        raise ValueError('A batch must contain 1-100 operations')
    if any(op.get('command') in {'execute', 'execute_code', 'batch', 'load_command_library'} for op in operations):
        raise ValueError('Python execution and nested batches are not allowed')
    results = []
    for index, operation in enumerate(operations):
        try:
            arguments = resolve_batch_refs(operation.get('arguments') or {}, results)
            result = dispatch(operation['command'], arguments)
            json.dumps(result, allow_nan=False)
            results.append({'index': index, 'tool': operation['command'], 'result': result})
        except Exception as error:
            results.append({'index': index, 'tool': operation['command'], 'error': f'{type(error).__name__}: {error}\\n{traceback.format_exc()}'})
            return {'completed': index, 'total': len(operations), 'results': results, 'failed': True}
    return {'completed': len(results), 'total': len(operations), 'results': results, 'failed': False}
'''


def _get_conn() -> BlenderConnection:
    global _conn
    if _conn is None:
        _conn = BlenderConnection(HOST, PORT, protocol=PROTOCOL)
    return _conn


@asynccontextmanager
async def _lifespan(_server: FastMCP):
    try:
        yield {}
    finally:
        global _conn
        if _conn is not None:
            _conn.disconnect()
            _conn = None


TOOL_USAGE_INSTRUCTIONS = """Drive Blender with the dedicated MCP tools first.
You MUST use a dedicated tool, or a sequence of dedicated tools, whenever they
can perform the requested operation. Do not replace supported operations with
Python just to reduce tool calls, write loops, or build an entire scene at once.
Use batch_tools for repeated operations or multi-step work in one MCP call.

Start by inspecting get_scene_info and, when needed, get_blender_info. Reuse
actual names returned by tools. Verify edits with get_object_info, audit_mesh,
get_viewport_screenshot or render_preview as appropriate.

Tool routing:
- Primitives, cameras and lights: create_object; transforms: modify_object.
- Custom geometry: create_mesh (vertices/edges/faces), create_curve (paths,
  cables and pipes with bevel), create_text_object; repetition: duplicate_object
  or add_modifier(type='ARRAY', settings=...). A custom shape alone does not
  require Python when its vertex and face data can be passed to create_mesh.
- Materials: create_material, update_material, assign_material, set_material,
  set_image_texture and set_world_color.
- Modifiers and constraints: add_modifier/add_constraint with settings;
  apply_modifier, remove_modifier, remove_constraint and the list tools.
- Organization: collection/scene tools, set_parent, select_objects.
- Animation: set_transform_keyframes for multiple frames, insert_keyframe,
  delete_keyframe, set_frame and list_animation_data.
- Cameras/lighting: aim_camera_at, frame_camera_to_object, zoom_camera,
  set_active_camera and create_light_setup.
- Cleanup: audit_mesh, clean_mesh, convert_object_to_mesh, join_mesh_objects,
  apply_transform, set_object_origin and shade_smooth.
- Output: render tools, set_render_settings, import_asset, export_scene,
  save_blend_file and set_output_path.

Use get_properties/set_properties for RNA settings (light energy, camera DOF,
Cycles settings, modifier updates). Use node-tree tools for shader, compositor
and Geometry Nodes graphs; mesh_edit/uv_unwrap for mesh/UV operations; armature,
weight, pose and shape-key tools for character work. Use configure_ik with a
pole target, audit_rig/normalize_weights/transfer_weights for clean skinning,
and bake_animation for visual rig animation. Use collision proxy creation,
audit_collision_proxies and detect_collisions for collision geometry; use
create_rigid_body_constraint/configure_physics_world/bake_physics for simulation.
Use add_driver/list_drivers/remove_driver for declarative driver setup.
Search tools with
search_tools/get_tool_help before assuming a capability is missing.

execute_blender_code is ONLY a last-resort fallback for a specific operation
that the dedicated tools cannot express. Before calling it,
check the relevant tools and their arguments. Supply fallback_reason explaining
the exact missing capability and tools_considered naming the tools you checked.
Keep the snippet limited to the unsupported part; perform supported setup,
transforms, materials, rendering and inspection with dedicated tools. A tool
error is not automatically a missing capability: correct its arguments or
scene prerequisites first. Never use Python to bypass a tool's safeguards.
"""

mcp = BlenderMCP("blender-mcp-local", instructions=TOOL_USAGE_INSTRUCTIONS, lifespan=_lifespan)
# FastMCP otherwise reports the SDK package's version in the MCP handshake.
mcp._mcp_server.version = __version__


def _call(cmd: str, params: dict[str, Any] | None = None) -> Any:
    prepared = _prepared_commands.get()
    if prepared is not None:
        prepared.append({"command": cmd, "arguments": params or {}})
        return {"data": ""}  # image wrappers can also prepare their command
    try:
        return _dispatch_to_blender(lambda: _get_conn().send_command(cmd, params))
    except Exception as error:
        if not _error_context.get() or not _error_context.get()["logged"]:
            _record_error(cmd, error, params, phase="blender_call")
        raise


def _dispatch_to_blender(callback):
    context = _error_context.get()
    with _conn_lock:
        if context is not None and context["cancelled"]:
            raise RuntimeError("Request cancelled before Blender dispatch; no scene operation started")
        if context is not None:
            context["execution_state"] = "in_flight"
        result = callback()
        if context is not None:
            context["execution_state"] = "response_received"
        return result


def _record_error(tool_name, error, arguments=None, *, phase="tool", batch_index=None):
    context = _error_context.get()
    if tool_name == "execute_code":
        tool_name = "execute_blender_code"
    function = globals().get("_native_functions", {}).get(tool_name)
    keys = list(inspect.signature(function).parameters) if function is not None else []
    if context and context["tool"] == tool_name:
        keys = context["argument_keys"]
    elif function is None and tool_name not in {"batch_tools", "execute_blender_code", "server"}:
        tool_name = "unknown_tool"
    try:
        event_id = _error_journal.record(tool=tool_name, error=error, arguments=arguments, argument_keys=keys,
                                         phase=phase, version=__version__, batch_index=batch_index,
                                         execution_state=context.get("execution_state") if context else None)
    except Exception:
        _error_journal.last_write_error = "DiagnosticError"
        event_id = None
    if context is not None:
        context["logged"] = True
    return event_id


def _command_library() -> tuple[str, str]:
    global _library_cache
    if _library_cache is None:
        program = _build_command_code("__dispatch__", {})
        program = program.replace("params = json.loads('{}')", "params = arguments", 1)
        program = program.replace("cmd = '__dispatch__'", "cmd = command", 1)
        source = COMMAND_CODE + PRODUCTION_CODE + "\ndef dispatch(command, arguments):\n" + textwrap.indent(program, "    ") + "\n    return result\n" + _BATCH_RUNTIME
        _library_cache = (source, hashlib.sha256(source.encode()).hexdigest())
    return _library_cache


def _build_command_code(cmd: str, params: dict[str, Any]) -> str:
    params_json = json.dumps(params, allow_nan=False)
    return f"""
import base64
import io
import json
import math
import os
import tempfile
from contextlib import redirect_stdout

import bmesh
import bpy
from mathutils import Matrix, Vector

params = json.loads({params_json!r})

def object_summary(obj):
    return {{
        "name": obj.name,
        "type": obj.type,
        "location": list(obj.location),
        "rotation_euler": list(obj.rotation_euler),
        "scale": list(obj.scale),
        "visible": not obj.hide_viewport,
    }}

def principled_bsdf(material):
    if not material.node_tree:
        raise RuntimeError(f"Material '{{material.name}}' has no node tree")
    node = material.node_tree.nodes.get("Principled BSDF")
    if node is None:
        node = next((item for item in material.node_tree.nodes if item.type == "BSDF_PRINCIPLED"), None)
    if node is None:
        raise RuntimeError(f"Material '{{material.name}}' has no Principled BSDF node")
    return node

def collection_in_tree(root, target):
    return root == target or any(collection_in_tree(child, target) for child in root.children)

def operator_available(operator):
    try:
        operator.get_rna_type()
        return True
    except (AttributeError, RuntimeError):
        return False

def action_fcurves(obj):
    animation = obj.animation_data
    action = animation.action if animation else None
    if action is None:
        return []
    if hasattr(action, "fcurves"):
        return list(action.fcurves)
    slot = getattr(animation, "action_slot", None)
    return [curve for layer in action.layers for strip in layer.strips
            for bag in getattr(strip, "channelbags", [])
            if slot is None or bag.slot_handle == slot.handle
            for curve in bag.fcurves]

def mesh_audit(mesh, obj=None, include_indices=False, max_indices=100):
    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()
        bm.verts.index_update()
        bm.edges.index_update()
        bm.faces.index_update()
        issue_indices = {{
            "loose_vertices": [vert.index for vert in bm.verts if not vert.link_edges],
            "loose_edges": [edge.index for edge in bm.edges if not edge.link_faces],
            "boundary_edges": [edge.index for edge in bm.edges if len(edge.link_faces) == 1],
            "non_manifold_edges": [edge.index for edge in bm.edges if not edge.is_manifold],
            "degenerate_edges": [edge.index for edge in bm.edges if edge.calc_length() <= 1e-12],
            "zero_area_faces": [face.index for face in bm.faces if face.calc_area() <= 1e-12],
            "ngons": [face.index for face in bm.faces if len(face.verts) > 4],
            "non_finite_vertices": [
                vert.index for vert in bm.verts
                if not all(math.isfinite(component) for component in vert.co)
            ],
            "invalid_material_faces": [
                poly.index for poly in mesh.polygons
                if poly.material_index >= len(mesh.materials) and poly.material_index != 0
            ],
        }}
        issues = {{name: len(indices) for name, indices in issue_indices.items()}}
        negative_scale = bool(obj and obj.matrix_world.to_3x3().determinant() < 0)
        issues["negative_scale"] = int(negative_scale)
        result = {{
            "ok": not any(issues.values()),
            "counts": {{
                "vertices": len(mesh.vertices),
                "edges": len(mesh.edges),
                "polygons": len(mesh.polygons),
                "triangles": sum(1 for face in bm.faces if len(face.verts) == 3),
                "uv_layers": len(mesh.uv_layers),
                "materials": len(mesh.materials),
                "mesh_users": mesh.users,
            }},
            "issues": issues,
            "negative_scale": negative_scale,
        }}
        if include_indices:
            result["indices"] = {{name: indices[:max_indices] for name, indices in issue_indices.items()}}
            result["truncated"] = any(len(indices) > max_indices for indices in issue_indices.values())
        return result
    finally:
        bm.free()

cmd = {cmd!r}

if cmd == "get_scene_info":
    scene = bpy.context.scene
    objects = [{{"name": o.name, "type": o.type, "location": list(o.location)}} for o in scene.objects]
    result = {{
        "scene": scene.name,
        "frame_current": scene.frame_current,
        "frame_start": scene.frame_start,
        "frame_end": scene.frame_end,
        "object_count": len(objects),
        "objects": objects,
    }}
elif cmd == "get_blender_info":
    render_engine_property = bpy.context.scene.render.bl_rna.properties.get("engine")
    has_viewport = not bpy.app.background and any(
        area.type == "VIEW_3D"
        for window in bpy.context.window_manager.windows
        for area in window.screen.areas
    )
    result = {{
        "version": bpy.app.version_string,
        "version_tuple": list(bpy.app.version),
        "file_path": bpy.data.filepath or None,
        "file_saved": bool(bpy.data.filepath),
        "is_dirty": bpy.data.is_dirty,
        "background": bpy.app.background,
        "active_scene": bpy.context.scene.name,
        "render_engine": bpy.context.scene.render.engine,
        "capabilities": {{
            "bridge_version": params.get("bridge_version"),
            "addon_version": bpy.app.driver_namespace.get("blender_mcp_local_version"),
            "protocol": params.get("protocol"),
            "native_rpc": True,
            "native_batch": True,
            "batch_result_references": True,
            "max_message_bytes": 64 * 1024 * 1024,
            "has_viewport": has_viewport,
            "render_engines": [item.identifier for item in render_engine_property.enum_items] if render_engine_property else [],
            "import_export": {{
                "gltf": operator_available(bpy.ops.import_scene.gltf) and operator_available(bpy.ops.export_scene.gltf),
                "fbx": operator_available(bpy.ops.import_scene.fbx) and operator_available(bpy.ops.export_scene.fbx),
                "obj": operator_available(bpy.ops.wm.obj_import) or operator_available(bpy.ops.import_scene.obj),
                "stl": operator_available(bpy.ops.wm.stl_import) or operator_available(bpy.ops.import_mesh.stl),
            }},
        }},
    }}
elif cmd == "get_object_info":
    obj = bpy.data.objects.get(params["name"])
    if obj is None:
        raise KeyError(f"Object not found: {{params['name']}}")
    result = object_summary(obj)
    result["materials"] = [m.name for m in obj.material_slots if m.material]
    if obj.type == "MESH" and obj.data:
        result["mesh"] = {{
            "vertices": len(obj.data.vertices),
            "edges": len(obj.data.edges),
            "polygons": len(obj.data.polygons),
        }}
    result["bounding_box_world"] = [list(obj.matrix_world @ Vector(c)) for c in obj.bound_box]
elif cmd == "audit_mesh":
    include_indices = bool(params.get("include_indices", False))
    max_indices = int(params.get("max_indices", 100))
    if not 1 <= max_indices <= 10000:
        raise ValueError("max_indices must be between 1 and 10000")
    names = params.get("object_names")
    if names is None:
        objects = [obj for obj in bpy.context.scene.objects if obj.type == "MESH"]
    else:
        objects = []
        for name in names:
            obj = bpy.data.objects.get(name)
            if obj is None:
                raise KeyError(f"Object not found: {{name}}")
            if obj.type != "MESH":
                raise TypeError(f"Object '{{name}}' is not a mesh")
            objects.append(obj)
    result = {{
        "objects": [dict({{"name": obj.name}}, **mesh_audit(obj.data, obj, include_indices, max_indices)) for obj in objects],
        "object_count": len(objects),
    }}
elif cmd == "clean_mesh":
    obj = bpy.data.objects.get(params["object_name"])
    if obj is None:
        raise KeyError(f"Object not found: {{params['object_name']}}")
    if obj.type != "MESH":
        raise TypeError(f"Object '{{obj.name}}' is not a mesh")
    if obj.mode != "OBJECT":
        raise ValueError("clean_mesh requires the object to be in Object Mode")
    if obj.data.users > 1 and not params.get("allow_shared_data", False):
        raise ValueError(f"Mesh '{{obj.data.name}}' has {{obj.data.users}} users")
    requested = [
        params.get("merge_distance") is not None,
        bool(params.get("remove_loose", False)),
        params.get("degenerate_distance") is not None,
        bool(params.get("recalculate_normals", False)),
    ]
    if not any(requested):
        raise ValueError("At least one cleanup operation must be enabled")
    merge_distance = params.get("merge_distance")
    degenerate_distance = params.get("degenerate_distance")
    if merge_distance is not None and float(merge_distance) < 0:
        raise ValueError("merge_distance cannot be negative")
    if degenerate_distance is not None and float(degenerate_distance) < 0:
        raise ValueError("degenerate_distance cannot be negative")
    before = mesh_audit(obj.data, obj)
    bm = bmesh.new()
    operations = []
    try:
        bm.from_mesh(obj.data)
        if merge_distance is not None:
            vertex_count = len(bm.verts)
            bmesh.ops.remove_doubles(bm, verts=list(bm.verts), dist=float(merge_distance))
            operations.append({{"operation": "merge_by_distance", "affected": vertex_count - len(bm.verts)}})
        if degenerate_distance is not None:
            counts = (len(bm.verts), len(bm.edges), len(bm.faces))
            bmesh.ops.dissolve_degenerate(bm, dist=float(degenerate_distance), edges=list(bm.edges))
            removed = [max(0, before_count - after_count) for before_count, after_count in
                       zip(counts, (len(bm.verts), len(bm.edges), len(bm.faces)))]
            operations.append({{"operation": "dissolve_degenerate", "affected": sum(removed),
                                "removed_vertices": removed[0], "removed_edges": removed[1], "removed_faces": removed[2]}})
        if params.get("remove_loose", False):
            loose_edges = [edge for edge in bm.edges if not edge.link_faces]
            if loose_edges:
                bmesh.ops.delete(bm, geom=loose_edges, context="EDGES")
            loose_verts = [vert for vert in bm.verts if not vert.link_edges]
            if loose_verts:
                bmesh.ops.delete(bm, geom=loose_verts, context="VERTS")
            operations.append({{"operation": "remove_loose", "affected": len(loose_edges) + len(loose_verts)}})
        if params.get("recalculate_normals", False):
            bmesh.ops.recalc_face_normals(bm, faces=list(bm.faces))
            operations.append({{"operation": "recalculate_normals", "affected": len(bm.faces)}})
        bm.to_mesh(obj.data)
        obj.data.update()
    finally:
        bm.free()
    result = {{
        "object": obj.name,
        "mesh": obj.data.name,
        "operations": operations,
        "before": before,
        "after": mesh_audit(obj.data, obj),
    }}
elif cmd == "create_object":
    type_ = (params.get("type") or "cube").lower()
    location = tuple(params.get("location") or (0, 0, 0))
    rotation = tuple(params.get("rotation") or (0, 0, 0))
    scale = tuple(params.get("scale") or (1, 1, 1))
    primitives = {{
        "cube": bpy.ops.mesh.primitive_cube_add, "sphere": bpy.ops.mesh.primitive_uv_sphere_add,
        "ico_sphere": bpy.ops.mesh.primitive_ico_sphere_add, "cylinder": bpy.ops.mesh.primitive_cylinder_add,
        "plane": bpy.ops.mesh.primitive_plane_add, "cone": bpy.ops.mesh.primitive_cone_add,
        "torus": bpy.ops.mesh.primitive_torus_add, "circle": bpy.ops.mesh.primitive_circle_add,
        "grid": bpy.ops.mesh.primitive_grid_add, "monkey": bpy.ops.mesh.primitive_monkey_add,
    }}
    geometry = params.get("geometry") or {{}}
    if type_ in primitives:
        if set(geometry) & {{"location", "rotation", "scale", "enter_editmode"}}:
            raise ValueError("Use top-level transform parameters; primitives are created in Object Mode")
        primitives[type_](location=location, rotation=rotation, **geometry)
    elif type_ == "empty":
        if geometry: raise ValueError("geometry is supported only for mesh primitives")
        bpy.ops.object.empty_add(location=location, rotation=rotation, scale=scale)
    elif type_ == "camera":
        if geometry: raise ValueError("geometry is supported only for mesh primitives")
        bpy.ops.object.camera_add(location=location, rotation=rotation)
    elif type_ == "light":
        if geometry: raise ValueError("geometry is supported only for mesh primitives")
        bpy.ops.object.light_add(type=(params.get("light_type") or "POINT").upper(), location=location, rotation=rotation)
    else:
        raise ValueError(f"Unknown object type: {{type_}}")
    obj = bpy.context.active_object
    if params.get("name"):
        obj.name = params["name"]
    obj.scale = scale
    result = object_summary(obj)
elif cmd == "create_mesh":
    name = params.get("name") or "Mesh"
    vertices, edges, faces = params.get("vertices", []), params.get("edges", []), params.get("faces", [])
    if any(len(vertex) != 3 or not all(math.isfinite(v) for v in vertex) for vertex in vertices):
        raise ValueError("Mesh vertices must be finite XYZ triples")
    if any(len(edge) != 2 or len(set(edge)) != 2 for edge in edges) or any(len(face) < 3 or len(set(face)) != len(face) for face in faces):
        raise ValueError("Mesh edges/faces have invalid or repeated indices")
    if any(index < 0 or index >= len(vertices) for element in [*edges, *faces] for index in element):
        raise ValueError("Mesh index out of range")
    collection_name = params.get("collection_name")
    collection = bpy.data.collections.get(collection_name) if collection_name else bpy.context.collection
    if collection is None:
        raise KeyError(f"Collection not found: {{collection_name}}")
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(vertices, edges, faces)
    mesh.validate(verbose=False)
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)
    result = object_summary(obj)
    result["mesh"] = {{"vertices": len(mesh.vertices), "edges": len(mesh.edges), "polygons": len(mesh.polygons)}}
elif cmd == "create_curve":
    name = params.get("name") or "Curve"
    points = params.get("points") or []
    if len(points) < 2:
        raise ValueError("At least two curve points are required")
    curve = bpy.data.curves.new(name, "CURVE")
    curve.dimensions = "3D"
    curve.bevel_depth = float(params.get("bevel_depth", 0.0))
    curve.bevel_resolution = int(params.get("bevel_resolution", 2))
    spline = curve.splines.new("POLY")
    spline.points.add(len(points) - 1)
    for point, co in zip(spline.points, points):
        xyz = list(co)
        point.co = (*xyz[:3], xyz[3] if len(xyz) > 3 else 1.0)
    spline.use_cyclic_u = bool(params.get("cyclic", False))
    obj = bpy.data.objects.new(name, curve)
    bpy.context.collection.objects.link(obj)
    result = object_summary(obj)
elif cmd == "create_text_object":
    name = params.get("name") or "Text"
    curve = bpy.data.curves.new(name, "FONT")
    curve.body = params.get("text", "Text")
    curve.align_x = params.get("align_x", "LEFT").upper()
    curve.size = float(params.get("size", 1.0))
    curve.extrude = float(params.get("extrude", 0.0))
    curve.bevel_depth = float(params.get("bevel_depth", 0.0))
    obj = bpy.data.objects.new(name, curve)
    bpy.context.collection.objects.link(obj)
    obj.location = tuple(params.get("location") or (0, 0, 0))
    obj.rotation_euler = tuple(params.get("rotation") or (0, 0, 0))
    result = object_summary(obj)
elif cmd == "delete_object":
    obj = bpy.data.objects.get(params["name"])
    if obj is None:
        raise KeyError(f"Object not found: {{params['name']}}")
    other_scenes = [scene.name for scene in obj.users_scene if scene != bpy.context.scene]
    if other_scenes and not params.get("force_shared", False):
        raise ValueError(f"Object '{{obj.name}}' is also used by scenes: {{', '.join(other_scenes)}}")
    bpy.data.objects.remove(obj, do_unlink=True)
    result = {{"deleted": params["name"], "removed_from_scenes": [bpy.context.scene.name] + other_scenes}}
elif cmd == "modify_object":
    obj = bpy.data.objects.get(params["name"])
    if obj is None:
        raise KeyError(f"Object not found: {{params['name']}}")
    if "location" in params:
        obj.location = tuple(params["location"])
    if "rotation" in params:
        obj.rotation_euler = tuple(params["rotation"])
    if "scale" in params:
        obj.scale = tuple(params["scale"])
    if "visible" in params:
        obj.hide_viewport = not bool(params["visible"])
        obj.hide_render = not bool(params["visible"])
    result = object_summary(obj)
elif cmd == "set_parent":
    child = bpy.data.objects.get(params["child_name"])
    parent_name = params.get("parent_name")
    parent = bpy.data.objects.get(parent_name) if parent_name else None
    if child is None:
        raise KeyError(f"Object not found: {{params['child_name']}}")
    if parent_name and parent is None:
        raise KeyError(f"Parent object not found: {{parent_name}}")
    if child == parent:
        raise ValueError("An object cannot be parented to itself")
    world_matrix = child.matrix_world.copy()
    child.parent = parent
    if params.get("keep_transform", True):
        child.matrix_world = world_matrix
    result = {{"child": child.name, "parent": parent.name if parent else None}}
elif cmd == "apply_transform":
    obj = bpy.data.objects.get(params["object_name"])
    if obj is None:
        raise KeyError(f"Object not found: {{params['object_name']}}")
    if bpy.context.object and bpy.context.object.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.transform_apply(
        location=bool(params.get("location", False)),
        rotation=bool(params.get("rotation", False)),
        scale=bool(params.get("scale", True)),
    )
    result = object_summary(obj)
elif cmd == "convert_object_to_mesh":
    source = bpy.data.objects.get(params["object_name"])
    if source is None:
        raise KeyError(f"Object not found: {{params['object_name']}}")
    if source.type not in {{"MESH", "CURVE", "SURFACE", "FONT", "META"}}:
        raise TypeError(f"Object '{{source.name}}' cannot be converted to a mesh")
    if source.mode != "OBJECT":
        raise ValueError("convert_object_to_mesh requires the source to be in Object Mode")
    other_scenes = [scene.name for scene in source.users_scene if scene != bpy.context.scene]
    if not params.get("keep_original", True) and other_scenes and not params.get("force_shared", False):
        raise ValueError(f"Object '{{source.name}}' is also used by scenes: {{', '.join(other_scenes)}}")
    depsgraph = bpy.context.evaluated_depsgraph_get()
    source_for_mesh = source.evaluated_get(depsgraph) if params.get("apply_modifiers", True) else source
    mesh = bpy.data.meshes.new_from_object(source_for_mesh, preserve_all_data_layers=True, depsgraph=depsgraph)
    if mesh is None:
        raise RuntimeError(f"Object '{{source.name}}' produced no mesh geometry")
    new_name = params.get("new_name") or f"{{source.name}}_Mesh"
    mesh.name = new_name
    converted = bpy.data.objects.new(new_name, mesh)
    converted.matrix_world = source.matrix_world.copy()
    active_collections = [
        collection for collection in source.users_collection
        if collection_in_tree(bpy.context.scene.collection, collection)
    ] or [bpy.context.collection]
    for collection in active_collections:
        collection.objects.link(converted)
    source_name = source.name
    keep_original = bool(params.get("keep_original", True))
    if not keep_original:
        bpy.data.objects.remove(source, do_unlink=True)
    result = {{
        "source": source_name,
        "object": converted.name,
        "mesh": mesh.name,
        "kept_original": keep_original,
        "applied_modifiers": bool(params.get("apply_modifiers", True)),
        "counts": {{"vertices": len(mesh.vertices), "edges": len(mesh.edges), "polygons": len(mesh.polygons)}},
    }}
elif cmd == "join_mesh_objects":
    names = params.get("object_names") or []
    if len(names) < 2 or len(set(names)) != len(names):
        raise ValueError("object_names must contain at least two unique names")
    active_name = params["active_name"]
    if active_name not in names:
        raise ValueError("active_name must be included in object_names")
    objects = []
    for name in names:
        obj = bpy.data.objects.get(name)
        if obj is None:
            raise KeyError(f"Object not found: {{name}}")
        if obj.type != "MESH":
            raise TypeError(f"Object '{{name}}' is not a mesh")
        if bpy.context.view_layer.objects.get(obj.name) is None:
            raise ValueError(f"Object '{{name}}' is not in the active view layer")
        if obj.library or obj.data.library:
            raise ValueError(f"Object '{{name}}' uses linked library data")
        if obj.modifiers:
            raise ValueError(f"Apply or remove modifiers from '{{name}}' before joining")
        if obj.data.shape_keys:
            raise ValueError(f"Remove shape keys from '{{name}}' before joining")
        other_scenes = [scene.name for scene in obj.users_scene if scene != bpy.context.scene]
        if (other_scenes or obj.data.users > 1) and not params.get("force_shared", False):
            raise ValueError(f"Object or mesh '{{name}}' is shared")
        objects.append(obj)
    previous_selected = [obj.name for obj in bpy.context.selected_objects]
    previous_active = bpy.context.view_layer.objects.active.name if bpy.context.view_layer.objects.active else None
    previous_mode = bpy.context.object.mode if bpy.context.object else "OBJECT"
    try:
        if bpy.context.object and bpy.context.object.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        bpy.ops.object.select_all(action="DESELECT")
        for obj in objects:
            obj.select_set(True)
        active = bpy.data.objects[active_name]
        bpy.context.view_layer.objects.active = active
        bpy.ops.object.join()
        if params.get("new_name"):
            active.name = params["new_name"]
        result = {{
            "object": active.name,
            "joined": names,
            "mesh": active.data.name,
            "counts": {{
                "vertices": len(active.data.vertices),
                "edges": len(active.data.edges),
                "polygons": len(active.data.polygons),
            }},
        }}
    except Exception:
        bpy.ops.object.select_all(action="DESELECT")
        for name in previous_selected:
            item = bpy.data.objects.get(name)
            if item and bpy.context.view_layer.objects.get(item.name):
                item.select_set(True)
        if previous_active and bpy.data.objects.get(previous_active):
            bpy.context.view_layer.objects.active = bpy.data.objects[previous_active]
        if previous_mode != "OBJECT" and bpy.context.object:
            bpy.ops.object.mode_set(mode=previous_mode)
        raise
elif cmd == "set_object_origin":
    obj = bpy.data.objects.get(params["object_name"])
    if obj is None:
        raise KeyError(f"Object not found: {{params['object_name']}}")
    modes = {{
        "GEOMETRY": "ORIGIN_GEOMETRY",
        "CURSOR": "ORIGIN_CURSOR",
        "CENTER_OF_MASS": "ORIGIN_CENTER_OF_MASS",
        "CENTER_OF_VOLUME": "ORIGIN_CENTER_OF_VOLUME",
    }}
    mode = params.get("mode", "GEOMETRY").upper()
    center = params.get("center", "MEDIAN").upper()
    if mode not in modes:
        raise ValueError(f"Unsupported origin mode: {{mode}}")
    if center not in {{"MEDIAN", "BOUNDS"}}:
        raise ValueError(f"Unsupported center mode: {{center}}")
    previous_selected = [item.name for item in bpy.context.selected_objects]
    previous_active = bpy.context.view_layer.objects.active.name if bpy.context.view_layer.objects.active else None
    previous_mode = bpy.context.object.mode if bpy.context.object else "OBJECT"
    try:
        if bpy.context.object and bpy.context.object.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        bpy.ops.object.select_all(action="DESELECT")
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        bpy.ops.object.origin_set(type=modes[mode], center=center)
        result = object_summary(obj)
        result.update({{"origin_mode": mode, "center": center}})
        if mode == "CURSOR":
            result["cursor_location"] = list(bpy.context.scene.cursor.location)
    finally:
        bpy.ops.object.select_all(action="DESELECT")
        for name in previous_selected:
            item = bpy.data.objects.get(name)
            if item and bpy.context.view_layer.objects.get(item.name):
                item.select_set(True)
        if previous_active and bpy.data.objects.get(previous_active):
            bpy.context.view_layer.objects.active = bpy.data.objects[previous_active]
        if previous_mode != "OBJECT" and bpy.context.object:
            try:
                bpy.ops.object.mode_set(mode=previous_mode)
            except RuntimeError:
                pass
elif cmd == "set_material":
    obj = bpy.data.objects.get(params["object_name"])
    if obj is None:
        raise KeyError(f"Object not found: {{params['object_name']}}")
    if not (obj.data and hasattr(obj.data, "materials")):
        raise TypeError(f"Object '{{params['object_name']}}' does not support materials")
    mat_name = params.get("material_name") or f"{{params['object_name']}}_mat"
    mat = bpy.data.materials.get(mat_name) or bpy.data.materials.new(name=mat_name)
    mat.use_nodes = True
    bsdf = principled_bsdf(mat)
    color = params.get("color")
    if color:
        rgba = list(color) + [1.0] * (4 - len(color))
        bsdf.inputs["Base Color"].default_value = tuple(rgba[:4])
    if obj.data.materials:
        obj.data.materials[0] = mat
    else:
        obj.data.materials.append(mat)
    result = {{"object": params["object_name"], "material": mat.name, "color": color}}
elif cmd == "list_scenes":
    result = {{
        "active_scene": bpy.context.scene.name,
        "scenes": [{{"name": s.name, "object_count": len(s.objects)}} for s in bpy.data.scenes],
    }}
elif cmd == "create_scene":
    name = params.get("name") or "Scene"
    scene = bpy.data.scenes.new(name)
    if params.get("make_active", True):
        bpy.context.window.scene = scene
    result = {{"scene": scene.name, "active": bpy.context.scene.name}}
elif cmd == "set_active_scene":
    scene = bpy.data.scenes.get(params["name"])
    if scene is None:
        raise KeyError(f"Scene not found: {{params['name']}}")
    bpy.context.window.scene = scene
    result = {{"active_scene": scene.name}}
elif cmd == "delete_scene":
    scene = bpy.data.scenes.get(params["name"])
    if scene is None:
        raise KeyError(f"Scene not found: {{params['name']}}")
    if len(bpy.data.scenes) == 1:
        raise ValueError("Cannot delete the only scene")
    was_active = scene == bpy.context.scene
    bpy.data.scenes.remove(scene)
    if was_active:
        bpy.context.window.scene = bpy.data.scenes[0]
    result = {{"deleted": params["name"], "active_scene": bpy.context.scene.name}}
elif cmd == "list_cameras":
    scene = bpy.context.scene
    result = {{
        "active_camera": scene.camera.name if scene.camera else None,
        "cameras": [object_summary(o) for o in scene.objects if o.type == "CAMERA"],
    }}
elif cmd == "set_active_camera":
    obj = bpy.data.objects.get(params["name"])
    if obj is None or obj.type != "CAMERA":
        raise KeyError(f"Camera not found: {{params['name']}}")
    bpy.context.scene.camera = obj
    result = {{"active_camera": obj.name}}
elif cmd == "aim_camera_at":
    camera = bpy.data.objects.get(params["camera_name"])
    target = bpy.data.objects.get(params.get("target_name", ""))
    if camera is None or camera.type != "CAMERA":
        raise KeyError(f"Camera not found: {{params['camera_name']}}")
    target_location = Vector(params["target_location"]) if "target_location" in params else None
    if target_location is None:
        if target is None:
            raise KeyError("target_name or target_location is required")
        target_location = target.matrix_world.translation
    camera_location = camera.matrix_world.translation
    direction = target_location - camera_location
    rotation = direction.to_track_quat("-Z", "Y")
    camera.matrix_world = Matrix.Translation(camera_location) @ rotation.to_matrix().to_4x4()
    result = object_summary(camera)
elif cmd == "zoom_camera":
    camera = bpy.data.objects.get(params["camera_name"])
    if camera is None or camera.type != "CAMERA":
        raise KeyError(f"Camera not found: {{params['camera_name']}}")
    if "lens" in params:
        camera.data.lens = float(params["lens"])
    if "ortho_scale" in params:
        camera.data.ortho_scale = float(params["ortho_scale"])
    if "type" in params:
        camera.data.type = params["type"].upper()
    result = {{"camera": camera.name, "type": camera.data.type, "lens": camera.data.lens, "ortho_scale": camera.data.ortho_scale}}
elif cmd == "select_objects":
    names = params.get("names") or []
    clear = params.get("clear", True)
    if clear:
        for obj in bpy.context.scene.objects:
            obj.select_set(False)
    selected = []
    for name in names:
        obj = bpy.data.objects.get(name)
        if obj is None:
            raise KeyError(f"Object not found: {{name}}")
        obj.select_set(True)
        selected.append(name)
    active_name = params.get("active") or (selected[-1] if selected else None)
    if active_name:
        bpy.context.view_layer.objects.active = bpy.data.objects[active_name]
    result = {{"selected": selected, "active": bpy.context.view_layer.objects.active.name if bpy.context.view_layer.objects.active else None}}
elif cmd == "list_materials":
    result = {{
        "materials": [{{"name": m.name, "users": m.users, "use_nodes": m.use_nodes}} for m in bpy.data.materials]
    }}
elif cmd == "assign_material":
    obj = bpy.data.objects.get(params["object_name"])
    mat = bpy.data.materials.get(params["material_name"])
    if obj is None:
        raise KeyError(f"Object not found: {{params['object_name']}}")
    if mat is None:
        raise KeyError(f"Material not found: {{params['material_name']}}")
    if not (obj.data and hasattr(obj.data, "materials")):
        raise TypeError(f"Object '{{obj.name}}' does not support materials")
    slot = int(params.get("slot", 0))
    while len(obj.data.materials) <= slot:
        obj.data.materials.append(None)
    obj.data.materials[slot] = mat
    result = {{"object": obj.name, "material": mat.name, "slot": slot}}
elif cmd == "set_render_settings":
    scene = bpy.context.scene
    if "engine" in params:
        scene.render.engine = params["engine"]
    if "resolution" in params:
        scene.render.resolution_x = int(params["resolution"][0])
        scene.render.resolution_y = int(params["resolution"][1])
    if "fps" in params:
        scene.render.fps = int(params["fps"])
    if "frame_start" in params:
        scene.frame_start = int(params["frame_start"])
    if "frame_end" in params:
        scene.frame_end = int(params["frame_end"])
    result = {{
        "engine": scene.render.engine,
        "resolution": [scene.render.resolution_x, scene.render.resolution_y],
        "fps": scene.render.fps,
        "frame_range": [scene.frame_start, scene.frame_end],
    }}
elif cmd == "clear_scene":
    keep_cameras = bool(params.get("keep_cameras", False))
    keep_lights = bool(params.get("keep_lights", False))
    force_shared = bool(params.get("force_shared", False))
    removed = []
    skipped_shared = []
    released_data = {{}}
    data_collections = {{
        "MESH": bpy.data.meshes,
        "CURVE": bpy.data.curves,
        "SURFACE": bpy.data.curves,
        "FONT": bpy.data.curves,
        "META": bpy.data.metaballs,
        "LIGHT": bpy.data.lights,
        "CAMERA": bpy.data.cameras,
        "ARMATURE": bpy.data.armatures,
        "LATTICE": bpy.data.lattices,
    }}
    for obj in list(bpy.context.scene.objects):
        if keep_cameras and obj.type == "CAMERA":
            continue
        if keep_lights and obj.type == "LIGHT":
            continue
        other_scenes = [scene.name for scene in obj.users_scene if scene != bpy.context.scene]
        if other_scenes and not force_shared:
            skipped_shared.append({{"object": obj.name, "other_scenes": other_scenes}})
            continue
        datablocks = data_collections.get(obj.type)
        if datablocks is not None and obj.data is not None:
            released_data[obj.data.as_pointer()] = (datablocks, obj.data)
        removed.append(obj.name)
        bpy.data.objects.remove(obj, do_unlink=True)
    purged_data = []
    if params.get("purge_orphans", True):
        for datablocks, block in released_data.values():
            if block.users == 0:
                purged_data.append(block.name)
                datablocks.remove(block)
    result = {{
        "removed": removed,
        "count": len(removed),
        "skipped_shared": skipped_shared,
        "purged_data": purged_data,
    }}
elif cmd == "rename_object":
    obj = bpy.data.objects.get(params["name"])
    if obj is None:
        raise KeyError(f"Object not found: {{params['name']}}")
    old = obj.name
    obj.name = params["new_name"]
    result = {{"old_name": old, "new_name": obj.name}}
elif cmd == "duplicate_object":
    obj = bpy.data.objects.get(params["name"])
    if obj is None:
        raise KeyError(f"Object not found: {{params['name']}}")
    dup = obj.copy()
    if obj.data:
        dup.data = obj.data.copy() if params.get("linked", False) is False else obj.data
    dup.name = params.get("new_name") or f"{{obj.name}}_copy"
    bpy.context.collection.objects.link(dup)
    if "location" in params:
        dup.location = tuple(params["location"])
    elif "offset" in params:
        dup.location = obj.location + Vector(params["offset"])
    if "rotation" in params:
        dup.rotation_euler = tuple(params["rotation"])
    if "scale" in params:
        dup.scale = tuple(params["scale"])
    result = object_summary(dup)
elif cmd == "delete_objects_by_pattern":
    import fnmatch
    pattern = params["pattern"]
    force_shared = bool(params.get("force_shared", False))
    removed = []
    skipped_shared = []
    for obj in list(bpy.context.scene.objects):
        if fnmatch.fnmatch(obj.name, pattern):
            other_scenes = [scene.name for scene in obj.users_scene if scene != bpy.context.scene]
            if other_scenes and not force_shared:
                skipped_shared.append({{"object": obj.name, "other_scenes": other_scenes}})
                continue
            removed.append(obj.name)
            bpy.data.objects.remove(obj, do_unlink=True)
    result = {{"pattern": pattern, "removed": removed, "count": len(removed), "skipped_shared": skipped_shared}}
elif cmd == "create_material":
    mat = bpy.data.materials.get(params["name"]) or bpy.data.materials.new(name=params["name"])
    mat.use_nodes = True
    color = params.get("color")
    bsdf = principled_bsdf(mat)
    if color:
        rgba = list(color) + [1.0] * (4 - len(color))
        bsdf.inputs["Base Color"].default_value = tuple(rgba[:4])
    for key, socket_name in (("roughness", "Roughness"), ("metallic", "Metallic"), ("alpha", "Alpha")):
        if key in params:
            if socket_name not in bsdf.inputs:
                raise RuntimeError(f"Principled input not available: {{socket_name}}")
            bsdf.inputs[socket_name].default_value = float(params[key])
    result = {{"material": mat.name, "color": color, "users": mat.users}}
elif cmd == "update_material":
    mat = bpy.data.materials.get(params["name"])
    if mat is None:
        raise KeyError(f"Material not found: {{params['name']}}")
    mat.use_nodes = True
    bsdf = principled_bsdf(mat)
    if "color" in params:
        rgba = list(params["color"]) + [1.0] * (4 - len(params["color"]))
        bsdf.inputs["Base Color"].default_value = tuple(rgba[:4])
    for key, socket_name in (("roughness", "Roughness"), ("metallic", "Metallic"), ("alpha", "Alpha"), ("emission_strength", "Emission Strength")):
        if key in params:
            if socket_name not in bsdf.inputs:
                raise RuntimeError(f"Principled input not available: {{socket_name}}")
            bsdf.inputs[socket_name].default_value = float(params[key])
    result = {{"material": mat.name, "updated": True}}
elif cmd == "set_image_texture":
    obj = bpy.data.objects.get(params["object_name"])
    if obj is None:
        raise KeyError(f"Object not found: {{params['object_name']}}")
    if not os.path.isfile(params["image_path"]):
        raise FileNotFoundError(params["image_path"])
    mat_name = params.get("material_name") or f"{{obj.name}}_image_mat"
    mat = bpy.data.materials.get(mat_name) or bpy.data.materials.new(mat_name)
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    bsdf = principled_bsdf(mat)
    tex = nodes.new(type="ShaderNodeTexImage")
    tex.image = bpy.data.images.load(params["image_path"], check_existing=True)
    mat.node_tree.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
    if obj.data.materials:
        obj.data.materials[0] = mat
    else:
        obj.data.materials.append(mat)
    result = {{"object": obj.name, "material": mat.name, "image": tex.image.name}}
elif cmd == "list_object_materials":
    obj = bpy.data.objects.get(params["object_name"])
    if obj is None:
        raise KeyError(f"Object not found: {{params['object_name']}}")
    result = {{"object": obj.name, "materials": [slot.material.name if slot.material else None for slot in obj.material_slots]}}
elif cmd == "frame_camera_to_object":
    camera = bpy.data.objects.get(params["camera_name"])
    obj = bpy.data.objects.get(params["object_name"])
    if camera is None or camera.type != "CAMERA":
        raise KeyError(f"Camera not found: {{params['camera_name']}}")
    if obj is None:
        raise KeyError(f"Object not found: {{params['object_name']}}")
    center = sum((obj.matrix_world @ Vector(c) for c in obj.bound_box), Vector()) / 8
    radius = max((obj.matrix_world @ Vector(c) - center).length for c in obj.bound_box) or 1
    margin = float(params.get("margin", 1.1))
    if margin < 1.0:
        raise ValueError("margin must be at least 1.0")
    if "lens" in params:
        camera.data.lens = float(params["lens"])
    if camera.data.type == "ORTHO":
        camera.data.ortho_scale = radius * 2.0 * margin
        default_distance = radius * 3.0
    else:
        field_of_view = min(camera.data.angle_x, camera.data.angle_y)
        default_distance = radius * margin / max(math.sin(field_of_view / 2.0), 0.01)
    distance = float(params.get("distance", default_distance))
    direction = Vector(params.get("direction", [1, -1, 0.7])).normalized()
    camera_location = center + direction * distance
    rotation = (center - camera_location).to_track_quat("-Z", "Y")
    camera.matrix_world = Matrix.Translation(camera_location) @ rotation.to_matrix().to_4x4()
    bpy.context.scene.camera = camera
    result = object_summary(camera)
    result.update({{"target_center": list(center), "distance": distance, "margin": margin}})
elif cmd == "create_light_setup":
    preset = (params.get("preset") or "studio").lower()
    target = Vector(params.get("target", [0, 0, 0]))
    strength = float(params.get("strength", 500))
    created = []
    setups = {{
        "studio": [([3, -4, 5], "AREA", strength, 5), ([-4, 3, 4], "AREA", strength * 0.35, 6), ([0, 5, 3], "POINT", strength * 0.25, 1)],
        "dramatic": [([2, -3, 5], "SPOT", strength * 1.5, 1), ([-3, 4, 2], "POINT", strength * 0.15, 1)],
        "softbox": [([0, -4, 5], "AREA", strength, 7)],
    }}
    for i, (loc, light_type, power, size) in enumerate(setups.get(preset, setups["studio"]), 1):
        bpy.ops.object.light_add(type=light_type, location=tuple(Vector(loc) + target))
        light = bpy.context.object
        light.name = f"{{preset}}_light_{{i}}"
        light.data.energy = power
        if hasattr(light.data, "size"):
            light.data.size = size
        light.rotation_euler = (target - light.location).to_track_quat("-Z", "Y").to_euler()
        created.append(light.name)
    result = {{"preset": preset, "created": created}}
elif cmd == "add_modifier":
    obj = bpy.data.objects.get(params["object_name"])
    if obj is None:
        raise KeyError(f"Object not found: {{params['object_name']}}")
    mod = obj.modifiers.new(params.get("name") or params["type"].title(), params["type"].upper())
    try:
        settings = params.get("settings", {{}})
        for key in settings:
            if key in {{"name", "type"}}:
                raise ValueError(f"Unsupported modifier setting: {{key}}")
        rna_set(mod, settings)
    except Exception:
        obj.modifiers.remove(mod)
        raise
    result = {{"object": obj.name, "modifier": mod.name, "type": mod.type}}
elif cmd == "list_modifiers":
    obj = bpy.data.objects.get(params["object_name"])
    if obj is None:
        raise KeyError(f"Object not found: {{params['object_name']}}")
    result = {{"object": obj.name, "modifiers": [{{"name": m.name, "type": m.type, "show_viewport": m.show_viewport}} for m in obj.modifiers]}}
elif cmd == "remove_modifier":
    obj = bpy.data.objects.get(params["object_name"])
    if obj is None:
        raise KeyError(f"Object not found: {{params['object_name']}}")
    mod = obj.modifiers.get(params["modifier_name"])
    if mod is None:
        raise KeyError(f"Modifier not found: {{params['modifier_name']}}")
    obj.modifiers.remove(mod)
    result = {{"object": obj.name, "removed": params["modifier_name"]}}
elif cmd == "apply_modifier":
    obj = bpy.data.objects.get(params["object_name"])
    if obj is None:
        raise KeyError(f"Object not found: {{params['object_name']}}")
    name = params["modifier_name"]
    if obj.modifiers.get(name) is None:
        raise KeyError(f"Modifier not found on '{{obj.name}}': {{name}}")
    if bpy.context.object and bpy.context.object.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.select_all(action="DESELECT")
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    outcome = bpy.ops.object.modifier_apply(modifier=name)
    if "FINISHED" not in outcome or obj.modifiers.get(name) is not None:
        raise RuntimeError(f"Modifier '{{name}}' was not applied; operator returned {{sorted(outcome)}}")
    result = {{"object": obj.name, "applied": name}}
elif cmd == "shade_smooth":
    obj = bpy.data.objects.get(params["object_name"])
    if obj is None:
        raise KeyError(f"Object not found: {{params['object_name']}}")
    if obj.type != "MESH":
        raise TypeError(f"Object '{{obj.name}}' is not a mesh")
    smooth = bool(params.get("smooth", True))
    for poly in obj.data.polygons:
        poly.use_smooth = smooth
    result = {{"object": obj.name, "smooth": smooth}}
elif cmd == "list_constraints":
    obj = bpy.data.objects.get(params["object_name"])
    if obj is None:
        raise KeyError(f"Object not found: {{params['object_name']}}")
    result = {{"object": obj.name, "constraints": [{{
        "name": constraint.name,
        "type": constraint.type,
        "influence": constraint.influence,
        "mute": constraint.mute,
        "target": constraint.target.name if hasattr(constraint, "target") and constraint.target else None,
    }} for constraint in obj.constraints]}}
elif cmd == "add_constraint":
    obj = bpy.data.objects.get(params["object_name"])
    if obj is None:
        raise KeyError(f"Object not found: {{params['object_name']}}")
    constraint = obj.constraints.new(params["type"].upper())
    if params.get("name"):
        constraint.name = params["name"]
    if "target_name" in params:
        target = bpy.data.objects.get(params["target_name"])
        if target is None:
            obj.constraints.remove(constraint)
            raise KeyError(f"Target object not found: {{params['target_name']}}")
        if not hasattr(constraint, "target"):
            obj.constraints.remove(constraint)
            raise TypeError(f"Constraint {{constraint.type}} does not support a target")
        constraint.target = target
    try:
        for key, value in params.get("settings", {{}}).items():
            if not hasattr(constraint, key) or key in {{"name", "type", "target"}}:
                raise ValueError(f"Unsupported constraint setting: {{key}}")
            setattr(constraint, key, value)
    except Exception:
        obj.constraints.remove(constraint)
        raise
    result = {{"object": obj.name, "constraint": constraint.name, "type": constraint.type}}
elif cmd == "remove_constraint":
    obj = bpy.data.objects.get(params["object_name"])
    if obj is None:
        raise KeyError(f"Object not found: {{params['object_name']}}")
    constraint = obj.constraints.get(params["constraint_name"])
    if constraint is None:
        raise KeyError(f"Constraint not found: {{params['constraint_name']}}")
    obj.constraints.remove(constraint)
    result = {{"object": obj.name, "removed": params["constraint_name"]}}
elif cmd == "create_collection":
    col = bpy.data.collections.get(params["name"]) or bpy.data.collections.new(params["name"])
    if col.name not in bpy.context.scene.collection.children:
        try:
            bpy.context.scene.collection.children.link(col)
        except RuntimeError:
            pass
    result = {{"collection": col.name}}
elif cmd == "list_collections":
    scene_collection = bpy.context.scene.collection
    result = {{
        "scene": bpy.context.scene.name,
        "collections": [{{
            "name": col.name,
            "objects": [obj.name for obj in col.objects],
            "children": [child.name for child in col.children],
            "hide_viewport": col.hide_viewport,
            "hide_render": col.hide_render,
        }} for col in bpy.data.collections],
        "root_children": [col.name for col in scene_collection.children],
    }}
elif cmd == "move_to_collection":
    obj = bpy.data.objects.get(params["object_name"])
    col = bpy.data.collections.get(params["collection_name"])
    if obj is None:
        raise KeyError(f"Object not found: {{params['object_name']}}")
    other_scenes = [scene.name for scene in obj.users_scene if scene != bpy.context.scene]
    if other_scenes and not params.get("allow_shared", False):
        raise ValueError(f"Object '{{obj.name}}' is also used by scenes: {{', '.join(other_scenes)}}")
    if col is None:
        col = bpy.data.collections.new(params["collection_name"])
        bpy.context.scene.collection.children.link(col)
    elif not collection_in_tree(bpy.context.scene.collection, col):
        bpy.context.scene.collection.children.link(col)
    for c in list(obj.users_collection):
        c.objects.unlink(obj)
    col.objects.link(obj)
    result = {{"object": obj.name, "collection": col.name}}
elif cmd == "set_world_color":
    world = bpy.context.scene.world or bpy.data.worlds.new("World")
    bpy.context.scene.world = world
    color = params.get("color", [0.05, 0.05, 0.05])
    world.color = tuple(color[:3])
    if world.use_nodes and world.node_tree:
        background = world.node_tree.nodes.get("Background")
        if background:
            background.inputs["Color"].default_value = tuple(list(color[:3]) + [1.0])
    result = {{"world": world.name, "color": list(world.color)}}
elif cmd == "set_frame":
    scene = bpy.context.scene
    scene.frame_set(int(params["frame"]), subframe=float(params.get("subframe", 0.0)))
    result = {{"frame": scene.frame_current, "subframe": scene.frame_subframe}}
elif cmd == "insert_keyframe":
    obj = bpy.data.objects.get(params["object_name"])
    if obj is None:
        raise KeyError(f"Object not found: {{params['object_name']}}")
    options = set(params.get("options") or [])
    success = obj.keyframe_insert(
        data_path=params["data_path"],
        index=int(params.get("index", -1)),
        frame=float(params.get("frame", bpy.context.scene.frame_current)),
        group=params.get("group", ""),
        options=options,
        keytype=params.get("keytype", "KEYFRAME"),
    )
    result = {{"object": obj.name, "data_path": params["data_path"], "inserted": bool(success)}}
elif cmd == "delete_keyframe":
    obj = bpy.data.objects.get(params["object_name"])
    if obj is None:
        raise KeyError(f"Object not found: {{params['object_name']}}")
    success = obj.keyframe_delete(
        data_path=params["data_path"],
        index=int(params.get("index", -1)),
        frame=float(params.get("frame", bpy.context.scene.frame_current)),
        group=params.get("group", ""),
    )
    result = {{"object": obj.name, "data_path": params["data_path"], "deleted": bool(success)}}
elif cmd == "list_animation_data":
    obj = bpy.data.objects.get(params["object_name"])
    if obj is None:
        raise KeyError(f"Object not found: {{params['object_name']}}")
    animation_data = obj.animation_data
    action = animation_data.action if animation_data else None
    fcurves = action_fcurves(obj)
    result = {{
        "object": obj.name,
        "action": action.name if action else None,
        "nla_tracks": [track.name for track in animation_data.nla_tracks] if animation_data else [],
        "fcurves": [{{
            "data_path": curve.data_path,
            "array_index": curve.array_index,
            "keyframes": [list(point.co) for point in curve.keyframe_points],
            "interpolation": [point.interpolation for point in curve.keyframe_points],
        }} for curve in fcurves],
    }}
elif cmd == "set_keyframe_interpolation":
    obj = bpy.data.objects.get(params["object_name"])
    if obj is None: raise KeyError("Object not found")
    interpolation = params["interpolation"].upper()
    if interpolation not in {{"CONSTANT", "LINEAR", "BEZIER"}}:
        raise ValueError("Unsupported interpolation")
    count = 0
    for curve in action_fcurves(obj):
        if params.get("data_path") and curve.data_path != params["data_path"]: continue
        for point in curve.keyframe_points:
            point.interpolation = interpolation
            count += 1
        curve.update()
    result = {{"object": obj.name, "updated_keys": count, "interpolation": interpolation}}
elif cmd == "set_transform_keyframes":
    obj = bpy.data.objects.get(params["object_name"])
    if obj is None:
        raise KeyError(f"Object not found: {{params['object_name']}}")
    frames = [float(frame) for frame in params.get("frames", [])]
    if not frames or len(frames) > 10000:
        raise ValueError("frames must contain between 1 and 10000 values")
    if not all(math.isfinite(frame) for frame in frames):
        raise ValueError("frames must contain only finite values")
    channels = {{
        "location": params.get("locations"),
        "rotation_euler": params.get("rotations"),
        "scale": params.get("scales"),
    }}
    enabled = {{name: values for name, values in channels.items() if values is not None}}
    if not enabled:
        raise ValueError("At least one transform channel must be provided")
    for name, values in enabled.items():
        if len(values) != len(frames):
            raise ValueError(f"{{name}} values must match the number of frames")
        if any(len(value) != 3 or not all(math.isfinite(float(component)) for component in value) for value in values):
            raise ValueError(f"{{name}} values must be finite XYZ triples")
    rotation_mode = params.get("rotation_mode")
    if rotation_mode is not None:
        rotation_mode = rotation_mode.upper()
        if rotation_mode not in {{"XYZ", "XZY", "YXZ", "YZX", "ZXY", "ZYX"}}:
            raise ValueError("rotation_mode must be an Euler order")
    elif "rotation_euler" in enabled and obj.rotation_mode in {{"QUATERNION", "AXIS_ANGLE"}}:
        raise ValueError("rotation_mode is required when keying Euler rotation on this object")
    interpolation = params.get("interpolation", "BEZIER").upper()
    if interpolation not in {{"CONSTANT", "LINEAR", "BEZIER"}}:
        raise ValueError("interpolation must be CONSTANT, LINEAR, or BEZIER")
    original = {{
        "location": obj.location.copy(),
        "rotation_mode": obj.rotation_mode,
        "rotation_euler": obj.rotation_euler.copy(),
        "rotation_quaternion": obj.rotation_quaternion.copy(),
        "rotation_axis_angle": list(obj.rotation_axis_angle),
        "scale": obj.scale.copy(),
    }}
    preferences = bpy.context.preferences.edit
    previous_interpolation = preferences.keyframe_new_interpolation_type
    inserted = 0
    try:
        preferences.keyframe_new_interpolation_type = interpolation
        if rotation_mode is not None:
            obj.rotation_mode = rotation_mode
        for index, frame in enumerate(frames):
            for data_path, values in enabled.items():
                setattr(obj, data_path, tuple(values[index]))
                if not obj.keyframe_insert(data_path=data_path, frame=frame):
                    raise RuntimeError(f"Failed to insert {{data_path}} keyframe at frame {{frame}}")
                inserted += 3
    finally:
        preferences.keyframe_new_interpolation_type = previous_interpolation
        obj.rotation_mode = rotation_mode if rotation_mode is not None else original["rotation_mode"]
        obj.location = original["location"]
        obj.rotation_euler = original["rotation_euler"]
        obj.rotation_quaternion = original["rotation_quaternion"]
        obj.rotation_axis_angle = original["rotation_axis_angle"]
        obj.scale = original["scale"]
    result = {{
        "object": obj.name,
        "frames": frames,
        "channels": list(enabled),
        "inserted_keys": inserted,
        "interpolation": interpolation,
        "rotation_mode": obj.rotation_mode if rotation_mode is None else rotation_mode,
    }}
elif cmd == "set_output_path":
    bpy.context.scene.render.filepath = params["path"]
    result = {{"output_path": bpy.context.scene.render.filepath}}
elif cmd == "render_still":
    if "path" in params:
        bpy.context.scene.render.filepath = params["path"]
    bpy.ops.render.render(write_still=True)
    result = {{"rendered": True, "path": bpy.context.scene.render.filepath}}
elif cmd == "render_preview":
    scene = bpy.context.scene
    if scene.camera is None:
        raise RuntimeError("The active scene has no camera")
    max_size = int(params.get("max_size", 512))
    if not 64 <= max_size <= 4096:
        raise ValueError("max_size must be between 64 and 4096")
    render = scene.render
    original = (render.resolution_x, render.resolution_y, render.resolution_percentage,
                render.image_settings.file_format, render.image_settings.color_mode, render.image_settings.color_depth)
    current_width = max(1, round(render.resolution_x * render.resolution_percentage / 100))
    current_height = max(1, round(render.resolution_y * render.resolution_percentage / 100))
    scale = min(1.0, max_size / max(current_width, current_height))
    width = max(1, round(current_width * scale))
    height = max(1, round(current_height * scale))
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp.close()
    filepath = tmp.name
    try:
        render.resolution_x = width
        render.resolution_y = height
        render.resolution_percentage = 100
        render.image_settings.file_format = "PNG"
        outcome = bpy.ops.render.render()
        if "FINISHED" not in outcome:
            raise RuntimeError(f"Preview render did not finish: {{sorted(outcome)}}")
        image = bpy.data.images.get("Render Result")
        if image is None:
            raise RuntimeError("Blender did not produce a Render Result")
        image.save_render(filepath=filepath, scene=scene)
        with open(filepath, "rb") as file:
            data = file.read()
    finally:
        (render.resolution_x, render.resolution_y, render.resolution_percentage,
         render.image_settings.file_format, render.image_settings.color_mode, render.image_settings.color_depth) = original
        try:
            os.unlink(filepath)
        except OSError:
            pass
    result = {{"format": "png", "width": width, "height": height, "data": base64.b64encode(data).decode("ascii")}}
elif cmd == "render_animation":
    scene = bpy.context.scene
    if "path" in params:
        scene.render.filepath = params["path"]
    bpy.ops.render.render(animation=True)
    result = {{
        "rendered": True,
        "path": scene.render.filepath,
        "frame_range": [scene.frame_start, scene.frame_end],
    }}
elif cmd == "import_asset":
    path = os.path.abspath(params["path"])
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    format_name = (params.get("format") or os.path.splitext(path)[1].lstrip(".")).lower()
    before = set(bpy.data.objects)
    if format_name in {{"gltf", "glb"}}:
        bpy.ops.import_scene.gltf(filepath=path)
    elif format_name == "fbx":
        bpy.ops.import_scene.fbx(filepath=path)
    elif format_name == "obj":
        if operator_available(bpy.ops.wm.obj_import):
            bpy.ops.wm.obj_import(filepath=path)
        else:
            bpy.ops.import_scene.obj(filepath=path)
    elif format_name == "stl":
        if operator_available(bpy.ops.wm.stl_import):
            bpy.ops.wm.stl_import(filepath=path)
        else:
            bpy.ops.import_mesh.stl(filepath=path)
    else:
        raise ValueError(f"Unsupported import format: {{format_name}}")
    imported = [obj for obj in bpy.data.objects if obj not in before]
    collection_name = params.get("collection_name")
    if collection_name:
        collection = bpy.data.collections.get(collection_name)
        if collection is None:
            collection = bpy.data.collections.new(collection_name)
            bpy.context.scene.collection.children.link(collection)
        for obj in imported:
            for current in list(obj.users_collection):
                current.objects.unlink(obj)
            collection.objects.link(obj)
    result = {{"path": path, "format": format_name, "objects": [obj.name for obj in imported]}}
elif cmd == "export_scene":
    path = os.path.abspath(params["path"])
    format_name = (params.get("format") or os.path.splitext(path)[1].lstrip(".")).lower()
    object_names = params.get("object_names")
    use_selection = bool(params.get("selected_only", False) or object_names)
    if object_names:
        objects = []
        for name in object_names:
            obj = bpy.data.objects.get(name)
            if obj is None:
                raise KeyError(f"Object not found: {{name}}")
            objects.append(obj)
        bpy.ops.object.select_all(action="DESELECT")
        for obj in objects:
            obj.select_set(True)
    if format_name in {{"gltf", "glb"}}:
        bpy.ops.export_scene.gltf(
            filepath=path,
            export_format="GLB" if format_name == "glb" else "GLTF_SEPARATE",
            use_selection=use_selection,
        )
    elif format_name == "fbx":
        bpy.ops.export_scene.fbx(filepath=path, use_selection=use_selection)
    elif format_name == "obj":
        if operator_available(bpy.ops.wm.obj_export):
            bpy.ops.wm.obj_export(filepath=path, export_selected_objects=use_selection)
        else:
            bpy.ops.export_scene.obj(filepath=path, use_selection=use_selection)
    elif format_name == "stl":
        if operator_available(bpy.ops.wm.stl_export):
            bpy.ops.wm.stl_export(filepath=path, export_selected_objects=use_selection)
        else:
            bpy.ops.export_mesh.stl(filepath=path, use_selection=use_selection)
    else:
        raise ValueError(f"Unsupported export format: {{format_name}}")
    result = {{"path": path, "format": format_name, "selected_only": use_selection}}
elif cmd == "save_blend_file":
    path = params.get("path")
    if path:
        bpy.ops.wm.save_as_mainfile(filepath=os.path.abspath(path), check_existing=False)
    elif bpy.data.filepath:
        bpy.ops.wm.save_mainfile()
    else:
        raise ValueError("A path is required for an unsaved Blender file")
    result = {{"saved": True, "path": bpy.data.filepath}}
elif cmd == "execute_code":
    namespace = {{"bpy": bpy, "__name__": "__blender_mcp__"}}
    buf = io.StringIO()
    with redirect_stdout(buf):
        exec(compile(params["code"], "<mcp>", "exec"), namespace)
    result = {{"output": buf.getvalue()}}
elif cmd == "get_viewport_screenshot":
    if bpy.app.background:
        raise RuntimeError("No visible 3D viewport in background mode; use render_preview")
    max_size = int(params.get("max_size") or 800)
    target_window = None
    target_area = None
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                target_window, target_area = window, area
                break
        if target_area:
            break
    if target_area is None:
        raise RuntimeError("No 3D viewport found")
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp.close()
    filepath = tmp.name
    try:
        with bpy.context.temp_override(window=target_window, area=target_area):
            bpy.ops.screen.screenshot_area(filepath=filepath)
        img = bpy.data.images.load(filepath, check_existing=False)
        try:
            w, h = img.size
            if max(w, h) > max_size:
                scale = max_size / max(w, h)
                img.scale(int(w * scale), int(h * scale))
                img.filepath_raw = filepath
                img.file_format = "PNG"
                img.save()
        finally:
            bpy.data.images.remove(img)
        with open(filepath, "rb") as f:
            data = f.read()
    finally:
        try:
            os.unlink(filepath)
        except OSError:
            pass
    result = {{"format": "png", "data": base64.b64encode(data).decode("ascii")}}
else:
    result = dispatch_advanced(cmd, params)
"""


@mcp.tool()
def get_scene_info() -> str:
    """List the active Blender scene and its top-level objects (name, type, location)."""
    return json.dumps(_call("get_scene_info"), indent=2)


@mcp.tool()
def get_blender_info() -> str:
    """Return Blender version, current file state, active scene, and render engine."""
    return json.dumps(_call("get_blender_info", {
        "bridge_version": __version__,
        "protocol": PROTOCOL,
    }), indent=2)


@mcp.tool()
def get_object_info(name: str) -> str:
    """Return detailed information about a single object: transform, mesh stats, materials, bounding box."""
    return json.dumps(_call("get_object_info", {"name": name}), indent=2)


@mcp.tool()
def audit_mesh(
    object_names: list[str] | None = None,
    include_indices: bool = False,
    max_indices: int = 100,
) -> str:
    """Audit mesh topology for loose, boundary, non-manifold, degenerate, and invalid geometry."""
    params: dict[str, Any] = {"include_indices": include_indices, "max_indices": max_indices}
    if object_names is not None:
        params["object_names"] = object_names
    return json.dumps(_call("audit_mesh", params), indent=2)


@mcp.tool()
def clean_mesh(
    object_name: str,
    merge_distance: float | None = None,
    remove_loose: bool = False,
    degenerate_distance: float | None = None,
    recalculate_normals: bool = False,
    allow_shared_data: bool = False,
) -> str:
    """Run explicitly enabled mesh cleanup operations and return before/after audits."""
    params: dict[str, Any] = {
        "object_name": object_name,
        "remove_loose": remove_loose,
        "recalculate_normals": recalculate_normals,
        "allow_shared_data": allow_shared_data,
    }
    if merge_distance is not None:
        params["merge_distance"] = merge_distance
    if degenerate_distance is not None:
        params["degenerate_distance"] = degenerate_distance
    return json.dumps(_call("clean_mesh", params), indent=2)


@mcp.tool()
def create_object(
    type: str = "cube",
    name: str | None = None,
    location: list[float] | None = None,
    rotation: list[float] | None = None,
    scale: list[float] | None = None,
    light_type: str = "POINT",
    geometry: dict[str, Any] | None = None,
) -> str:
    """Create a primitive object in the scene.

    type: cube | sphere | ico_sphere | cylinder | plane | cone | torus |
          circle | grid | monkey | empty | camera | light
    light_type (only when type=light): POINT | SUN | SPOT | AREA
    location/rotation/scale: 3-element XYZ lists; rotation is Euler radians.
    geometry: native primitive arguments, e.g. sphere segments/ring_count,
    cylinder vertices/radius/depth, torus major_radius/minor_radius.
    """
    params: dict[str, Any] = {"type": type, "light_type": light_type}
    if geometry is not None:
        params["geometry"] = geometry
    if name is not None:
        params["name"] = name
    if location is not None:
        params["location"] = location
    if rotation is not None:
        params["rotation"] = rotation
    if scale is not None:
        params["scale"] = scale
    return json.dumps(_call("create_object", params), indent=2)


@mcp.tool()
def create_mesh(
    name: str,
    vertices: list[list[float]],
    faces: list[list[int]],
    edges: list[list[int]] | None = None,
    collection_name: str | None = None,
) -> str:
    """Create custom geometry from vertex coordinates and zero-based edge/face indices.

    Use this instead of Python mesh.from_pydata for explicit mesh geometry.
    """
    params: dict[str, Any] = {
        "name": name,
        "vertices": vertices,
        "faces": faces,
        "edges": edges or [],
    }
    if collection_name is not None:
        params["collection_name"] = collection_name
    return json.dumps(_call("create_mesh", params), indent=2)


@mcp.tool()
def create_curve(
    name: str,
    points: list[list[float]],
    bevel_depth: float = 0.0,
    bevel_resolution: int = 2,
    cyclic: bool = False,
) -> str:
    """Create a 3D poly curve from XYZ points, optionally with round bevel geometry.

    Use bevel_depth for cables or pipes instead of generating Python code.
    """
    return json.dumps(_call("create_curve", {
        "name": name,
        "points": points,
        "bevel_depth": bevel_depth,
        "bevel_resolution": bevel_resolution,
        "cyclic": cyclic,
    }), indent=2)


@mcp.tool()
def create_text_object(
    text: str,
    name: str = "Text",
    location: list[float] | None = None,
    rotation: list[float] | None = None,
    size: float = 1.0,
    extrude: float = 0.0,
    bevel_depth: float = 0.0,
    align_x: str = "LEFT",
) -> str:
    """Create an editable Blender text object."""
    params: dict[str, Any] = {
        "text": text,
        "name": name,
        "size": size,
        "extrude": extrude,
        "bevel_depth": bevel_depth,
        "align_x": align_x,
    }
    if location is not None:
        params["location"] = location
    if rotation is not None:
        params["rotation"] = rotation
    return json.dumps(_call("create_text_object", params), indent=2)


@mcp.tool()
def delete_object(name: str, force_shared: bool = False) -> str:
    """Delete an object, refusing to affect other scenes unless explicitly forced."""
    return json.dumps(_call("delete_object", {"name": name, "force_shared": force_shared}))


@mcp.tool()
def modify_object(
    name: str,
    location: list[float] | None = None,
    rotation: list[float] | None = None,
    scale: list[float] | None = None,
    visible: bool | None = None,
) -> str:
    """Modify an existing object. Pass only the fields you want to change."""
    params: dict[str, Any] = {"name": name}
    if location is not None:
        params["location"] = location
    if rotation is not None:
        params["rotation"] = rotation
    if scale is not None:
        params["scale"] = scale
    if visible is not None:
        params["visible"] = visible
    return json.dumps(_call("modify_object", params), indent=2)


@mcp.tool()
def set_parent(child_name: str, parent_name: str | None = None, keep_transform: bool = True) -> str:
    """Parent an object, or clear its parent when parent_name is omitted."""
    return json.dumps(_call("set_parent", {
        "child_name": child_name,
        "parent_name": parent_name,
        "keep_transform": keep_transform,
    }), indent=2)


@mcp.tool()
def apply_transform(
    object_name: str,
    location: bool = False,
    rotation: bool = False,
    scale: bool = True,
) -> str:
    """Apply selected object transforms to its data; scale is applied by default."""
    return json.dumps(_call("apply_transform", {
        "object_name": object_name,
        "location": location,
        "rotation": rotation,
        "scale": scale,
    }), indent=2)


@mcp.tool()
def convert_object_to_mesh(
    object_name: str,
    new_name: str | None = None,
    apply_modifiers: bool = True,
    keep_original: bool = True,
    force_shared: bool = False,
) -> str:
    """Convert mesh-capable object geometry to a new mesh, preserving the original by default."""
    params: dict[str, Any] = {
        "object_name": object_name,
        "apply_modifiers": apply_modifiers,
        "keep_original": keep_original,
        "force_shared": force_shared,
    }
    if new_name is not None:
        params["new_name"] = new_name
    return json.dumps(_call("convert_object_to_mesh", params), indent=2)


@mcp.tool()
def join_mesh_objects(
    object_names: list[str],
    active_name: str,
    new_name: str | None = None,
    force_shared: bool = False,
) -> str:
    """Join validated mesh objects using Blender semantics; modifiers and shape keys must be resolved first."""
    params: dict[str, Any] = {
        "object_names": object_names,
        "active_name": active_name,
        "force_shared": force_shared,
    }
    if new_name is not None:
        params["new_name"] = new_name
    return json.dumps(_call("join_mesh_objects", params), indent=2)


@mcp.tool()
def set_object_origin(object_name: str, mode: Literal["GEOMETRY", "CURSOR", "CENTER_OF_MASS", "CENTER_OF_VOLUME"] = "GEOMETRY", center: Literal["MEDIAN", "BOUNDS"] = "MEDIAN") -> str:
    """Set object origin to geometry, cursor, center of mass, or center of volume."""
    return json.dumps(_call("set_object_origin", {
        "object_name": object_name,
        "mode": mode,
        "center": center,
    }), indent=2)


@mcp.tool()
def set_material(
    object_name: str,
    material_name: str | None = None,
    color: list[float] | None = None,
) -> str:
    """Apply (or create) a Principled-BSDF material on an object.

    color: RGB or RGBA list with values in 0..1.
    """
    params: dict[str, Any] = {"object_name": object_name}
    if material_name is not None:
        params["material_name"] = material_name
    if color is not None:
        params["color"] = color
    return json.dumps(_call("set_material", params), indent=2)


@mcp.tool()
def list_scenes() -> str:
    """List Blender scenes and report the active scene."""
    return json.dumps(_call("list_scenes"), indent=2)


@mcp.tool()
def create_scene(name: str, make_active: bool = True) -> str:
    """Create a new scene, optionally making it active."""
    return json.dumps(_call("create_scene", {"name": name, "make_active": make_active}), indent=2)


@mcp.tool()
def set_active_scene(name: str) -> str:
    """Switch the active Blender scene by name."""
    return json.dumps(_call("set_active_scene", {"name": name}), indent=2)


@mcp.tool()
def delete_scene(name: str) -> str:
    """Delete a scene by name. Refuses to delete the only scene."""
    return json.dumps(_call("delete_scene", {"name": name}), indent=2)


@mcp.tool()
def list_cameras() -> str:
    """List cameras in the active scene and show the active camera."""
    return json.dumps(_call("list_cameras"), indent=2)


@mcp.tool()
def set_active_camera(name: str) -> str:
    """Set the active scene camera by object name."""
    return json.dumps(_call("set_active_camera", {"name": name}), indent=2)


@mcp.tool()
def aim_camera_at(
    camera_name: str,
    target_name: str | None = None,
    target_location: list[float] | None = None,
) -> str:
    """Rotate a camera to look at an object or XYZ location."""
    params: dict[str, Any] = {"camera_name": camera_name}
    if target_name is not None:
        params["target_name"] = target_name
    if target_location is not None:
        params["target_location"] = target_location
    return json.dumps(_call("aim_camera_at", params), indent=2)


@mcp.tool()
def zoom_camera(
    camera_name: str,
    lens: float | None = None,
    ortho_scale: float | None = None,
    type: str | None = None,
) -> str:
    """Change camera zoom: lens for perspective, ortho_scale/type for orthographic."""
    params: dict[str, Any] = {"camera_name": camera_name}
    if lens is not None:
        params["lens"] = lens
    if ortho_scale is not None:
        params["ortho_scale"] = ortho_scale
    if type is not None:
        params["type"] = type
    return json.dumps(_call("zoom_camera", params), indent=2)


@mcp.tool()
def select_objects(names: list[str], active: str | None = None, clear: bool = True) -> str:
    """Select objects by name and optionally set an active object."""
    params: dict[str, Any] = {"names": names, "clear": clear}
    if active is not None:
        params["active"] = active
    return json.dumps(_call("select_objects", params), indent=2)


@mcp.tool()
def list_materials() -> str:
    """List materials in the current Blender file."""
    return json.dumps(_call("list_materials"), indent=2)


@mcp.tool()
def assign_material(object_name: str, material_name: str, slot: int = 0) -> str:
    """Assign an existing material to an object's material slot."""
    return json.dumps(_call("assign_material", {
        "object_name": object_name,
        "material_name": material_name,
        "slot": slot,
    }), indent=2)


@mcp.tool()
def set_render_settings(
    engine: str | None = None,
    resolution: list[int] | None = None,
    fps: int | None = None,
    frame_start: int | None = None,
    frame_end: int | None = None,
) -> str:
    """Set basic render settings: engine, resolution, fps, and frame range."""
    params: dict[str, Any] = {}
    if engine is not None:
        params["engine"] = engine
    if resolution is not None:
        params["resolution"] = resolution
    if fps is not None:
        params["fps"] = fps
    if frame_start is not None:
        params["frame_start"] = frame_start
    if frame_end is not None:
        params["frame_end"] = frame_end
    return json.dumps(_call("set_render_settings", params), indent=2)


@mcp.tool()
def clear_scene(
    keep_cameras: bool = False,
    keep_lights: bool = False,
    purge_orphans: bool = True,
    force_shared: bool = False,
) -> str:
    """Remove active-scene objects, skipping objects shared with other scenes unless forced."""
    return json.dumps(_call("clear_scene", {
        "keep_cameras": keep_cameras,
        "keep_lights": keep_lights,
        "purge_orphans": purge_orphans,
        "force_shared": force_shared,
    }), indent=2)


@mcp.tool()
def rename_object(name: str, new_name: str) -> str:
    """Rename an object."""
    return json.dumps(_call("rename_object", {"name": name, "new_name": new_name}), indent=2)


@mcp.tool()
def duplicate_object(
    name: str,
    new_name: str | None = None,
    offset: list[float] | None = None,
    location: list[float] | None = None,
    rotation: list[float] | None = None,
    scale: list[float] | None = None,
    linked: bool = False,
) -> str:
    """Duplicate an object, optionally moving/rotating/scaling the copy."""
    params: dict[str, Any] = {"name": name, "linked": linked}
    if new_name is not None:
        params["new_name"] = new_name
    if offset is not None:
        params["offset"] = offset
    if location is not None:
        params["location"] = location
    if rotation is not None:
        params["rotation"] = rotation
    if scale is not None:
        params["scale"] = scale
    return json.dumps(_call("duplicate_object", params), indent=2)


@mcp.tool()
def delete_objects_by_pattern(pattern: str, force_shared: bool = False) -> str:
    """Delete matching active-scene objects, skipping objects shared with other scenes unless forced."""
    return json.dumps(_call("delete_objects_by_pattern", {
        "pattern": pattern,
        "force_shared": force_shared,
    }), indent=2)


@mcp.tool()
def create_material(
    name: str,
    color: list[float] | None = None,
    roughness: float | None = None,
    metallic: float | None = None,
    alpha: float | None = None,
) -> str:
    """Create or update a basic Principled BSDF material."""
    params: dict[str, Any] = {"name": name}
    if color is not None:
        params["color"] = color
    if roughness is not None:
        params["roughness"] = roughness
    if metallic is not None:
        params["metallic"] = metallic
    if alpha is not None:
        params["alpha"] = alpha
    return json.dumps(_call("create_material", params), indent=2)


@mcp.tool()
def update_material(
    name: str,
    color: list[float] | None = None,
    roughness: float | None = None,
    metallic: float | None = None,
    alpha: float | None = None,
    emission_strength: float | None = None,
) -> str:
    """Update common Principled BSDF material properties."""
    params: dict[str, Any] = {"name": name}
    for key, value in {
        "color": color,
        "roughness": roughness,
        "metallic": metallic,
        "alpha": alpha,
        "emission_strength": emission_strength,
    }.items():
        if value is not None:
            params[key] = value
    return json.dumps(_call("update_material", params), indent=2)


@mcp.tool()
def set_image_texture(object_name: str, image_path: str, material_name: str | None = None) -> str:
    """Create/assign a material using an image texture as Base Color."""
    params: dict[str, Any] = {"object_name": object_name, "image_path": image_path}
    if material_name is not None:
        params["material_name"] = material_name
    return json.dumps(_call("set_image_texture", params), indent=2)


@mcp.tool()
def list_object_materials(object_name: str) -> str:
    """List material slots for one object."""
    return json.dumps(_call("list_object_materials", {"object_name": object_name}), indent=2)


@mcp.tool()
def frame_camera_to_object(
    camera_name: str,
    object_name: str,
    distance: float | None = None,
    direction: list[float] | None = None,
    lens: float | None = None,
    margin: float = 1.1,
) -> str:
    """Place and aim a camera so an object is framed."""
    params: dict[str, Any] = {
        "camera_name": camera_name,
        "object_name": object_name,
        "margin": margin,
    }
    if distance is not None:
        params["distance"] = distance
    if direction is not None:
        params["direction"] = direction
    if lens is not None:
        params["lens"] = lens
    return json.dumps(_call("frame_camera_to_object", params), indent=2)


@mcp.tool()
def create_light_setup(preset: str = "studio", target: list[float] | None = None, strength: float = 500) -> str:
    """Create a simple lighting preset: studio, dramatic, or softbox."""
    params: dict[str, Any] = {"preset": preset, "strength": strength}
    if target is not None:
        params["target"] = target
    return json.dumps(_call("create_light_setup", params), indent=2)


@mcp.tool()
def add_modifier(object_name: str, type: str, name: str | None = None, settings: dict[str, Any] | None = None) -> str:
    """Add a modifier to an object. Type examples: BEVEL, SUBSURF, SOLIDIFY, MIRROR, ARRAY.

    Pass Blender modifier properties in settings (e.g. width/segments for BEVEL,
    count/relative_offset_displace for ARRAY) instead of writing Python.
    Datablock pointers use {"id_type":"OBJECT","name":"Target"}, as in
    set_properties. Invalid settings remove the newly created modifier.
    """
    params: dict[str, Any] = {"object_name": object_name, "type": type, "settings": settings or {}}
    if name is not None:
        params["name"] = name
    return json.dumps(_call("add_modifier", params), indent=2)


@mcp.tool()
def list_modifiers(object_name: str) -> str:
    """List modifiers on an object."""
    return json.dumps(_call("list_modifiers", {"object_name": object_name}), indent=2)


@mcp.tool()
def remove_modifier(object_name: str, modifier_name: str) -> str:
    """Remove a modifier without applying it."""
    return json.dumps(_call("remove_modifier", {
        "object_name": object_name,
        "modifier_name": modifier_name,
    }), indent=2)


@mcp.tool()
def apply_modifier(object_name: str, modifier_name: str) -> str:
    """Apply a modifier on an object."""
    return json.dumps(_call("apply_modifier", {"object_name": object_name, "modifier_name": modifier_name}), indent=2)


@mcp.tool()
def shade_smooth(object_name: str, smooth: bool = True) -> str:
    """Set mesh polygons to smooth or flat shading."""
    return json.dumps(_call("shade_smooth", {"object_name": object_name, "smooth": smooth}), indent=2)


@mcp.tool()
def list_constraints(object_name: str) -> str:
    """List an object's constraints and their targets."""
    return json.dumps(_call("list_constraints", {"object_name": object_name}), indent=2)


@mcp.tool()
def add_constraint(
    object_name: str,
    type: str,
    name: str | None = None,
    target_name: str | None = None,
    settings: dict[str, Any] | None = None,
) -> str:
    """Add an object constraint such as COPY_LOCATION, TRACK_TO, or LIMIT_ROTATION."""
    params: dict[str, Any] = {"object_name": object_name, "type": type, "settings": settings or {}}
    if name is not None:
        params["name"] = name
    if target_name is not None:
        params["target_name"] = target_name
    return json.dumps(_call("add_constraint", params), indent=2)


@mcp.tool()
def remove_constraint(object_name: str, constraint_name: str) -> str:
    """Remove a named object constraint."""
    return json.dumps(_call("remove_constraint", {
        "object_name": object_name,
        "constraint_name": constraint_name,
    }), indent=2)


@mcp.tool()
def create_collection(name: str) -> str:
    """Create a collection in the active scene if it does not exist."""
    return json.dumps(_call("create_collection", {"name": name}), indent=2)


@mcp.tool()
def list_collections() -> str:
    """List collections, direct objects, children, and active-scene root collections."""
    return json.dumps(_call("list_collections"), indent=2)


@mcp.tool()
def move_to_collection(object_name: str, collection_name: str, allow_shared: bool = False) -> str:
    """Move an object into an active-scene collection; shared objects require explicit permission."""
    return json.dumps(_call("move_to_collection", {
        "object_name": object_name,
        "collection_name": collection_name,
        "allow_shared": allow_shared,
    }), indent=2)


@mcp.tool()
def set_world_color(color: list[float]) -> str:
    """Set the active scene world/background color."""
    return json.dumps(_call("set_world_color", {"color": color}), indent=2)


@mcp.tool()
def set_frame(frame: int, subframe: float = 0.0) -> str:
    """Set and evaluate the active scene frame."""
    return json.dumps(_call("set_frame", {"frame": frame, "subframe": subframe}), indent=2)


@mcp.tool()
def insert_keyframe(
    object_name: str,
    data_path: str,
    frame: float | None = None,
    index: int = -1,
    group: str | None = None,
    keytype: str = "KEYFRAME",
) -> str:
    """Insert an object property keyframe, for example data_path='location'."""
    params: dict[str, Any] = {
        "object_name": object_name,
        "data_path": data_path,
        "index": index,
        "keytype": keytype,
    }
    if frame is not None:
        params["frame"] = frame
    if group is not None:
        params["group"] = group
    return json.dumps(_call("insert_keyframe", params), indent=2)


@mcp.tool()
def delete_keyframe(
    object_name: str,
    data_path: str,
    frame: float | None = None,
    index: int = -1,
    group: str | None = None,
) -> str:
    """Delete an object property keyframe."""
    params: dict[str, Any] = {"object_name": object_name, "data_path": data_path, "index": index}
    if frame is not None:
        params["frame"] = frame
    if group is not None:
        params["group"] = group
    return json.dumps(_call("delete_keyframe", params), indent=2)


@mcp.tool()
def list_animation_data(object_name: str) -> str:
    """Inspect an object's action, NLA tracks, F-curves, and keyframe coordinates."""
    return json.dumps(_call("list_animation_data", {"object_name": object_name}), indent=2)


@mcp.tool()
def set_keyframe_interpolation(object_name: str, interpolation: str = "BEZIER", data_path: str | None = None) -> str:
    """Set existing action keys to CONSTANT/LINEAR/BEZIER, including Blender 5.x action slots.

    Optional data_path restricts editing to one channel, e.g. location.
    """
    return json.dumps(_call("set_keyframe_interpolation", {
        "object_name": object_name, "interpolation": interpolation, "data_path": data_path,
    }), indent=2)


@mcp.tool()
def set_transform_keyframes(
    object_name: str,
    frames: list[float],
    locations: list[list[float]] | None = None,
    rotations: list[list[float]] | None = None,
    scales: list[list[float]] | None = None,
    interpolation: str = "BEZIER",
    rotation_mode: str | None = None,
) -> str:
    """Insert validated transform keyframes for multiple frames in one operation.

    Use this instead of Python loops for location, rotation or scale animation.
    """
    params: dict[str, Any] = {
        "object_name": object_name,
        "frames": frames,
        "interpolation": interpolation,
    }
    for key, value in {
        "locations": locations,
        "rotations": rotations,
        "scales": scales,
        "rotation_mode": rotation_mode,
    }.items():
        if value is not None:
            params[key] = value
    return json.dumps(_call("set_transform_keyframes", params), indent=2)


@mcp.tool()
def set_output_path(path: str) -> str:
    """Set the render output path."""
    return json.dumps(_call("set_output_path", {"path": path}), indent=2)


@mcp.tool()
def render_still(path: str | None = None) -> str:
    """Render a still image to the configured output path or an explicit path."""
    params: dict[str, Any] = {}
    if path is not None:
        params["path"] = path
    return json.dumps(_call("render_still", params), indent=2)


@mcp.tool()
def render_preview(max_size: int = 512) -> Image:
    """Render the active camera to a bounded PNG and return it directly as an MCP image."""
    result = _call("render_preview", {"max_size": max_size})
    return Image(data=base64.b64decode(result["data"]), format="png")


@mcp.tool()
def render_animation(path: str | None = None) -> str:
    """Render the configured scene frame range to the output path."""
    params: dict[str, Any] = {}
    if path is not None:
        params["path"] = path
    return json.dumps(_call("render_animation", params), indent=2)


@mcp.tool()
def import_asset(path: str, format: str | None = None, collection_name: str | None = None) -> str:
    """Import a local GLTF/GLB, FBX, OBJ, or STL file into the active scene."""
    params: dict[str, Any] = {"path": path}
    if format is not None:
        params["format"] = format
    if collection_name is not None:
        params["collection_name"] = collection_name
    return json.dumps(_call("import_asset", params), indent=2)


@mcp.tool()
def export_scene(
    path: str,
    format: str | None = None,
    object_names: list[str] | None = None,
    selected_only: bool = False,
) -> str:
    """Export the scene or named/selected objects as GLTF/GLB, FBX, OBJ, or STL."""
    params: dict[str, Any] = {"path": path, "selected_only": selected_only}
    if format is not None:
        params["format"] = format
    if object_names is not None:
        params["object_names"] = object_names
    return json.dumps(_call("export_scene", params), indent=2)


@mcp.tool()
def save_blend_file(path: str | None = None) -> str:
    """Save the current Blender file; path is required if it has never been saved."""
    params: dict[str, Any] = {}
    if path is not None:
        params["path"] = path
    return json.dumps(_call("save_blend_file", params), indent=2)


@mcp.tool()
async def execute_blender_code(code: str, fallback_reason: str, tools_considered: list[str]) -> str:
    """LAST RESORT: execute only an operation that dedicated Blender tools cannot express.

    First check dedicated tools and their arguments. Required fallback_reason
    must explain the exact missing capability, and tools_considered must name
    the relevant tools checked. Use batch_tools for many supported operations;
    Discovery names search_tools/get_tool_help may accompany at least one
    dedicated Blender operation; discovery alone is insufficient.
    fewer calls or convenient Python loops are not a valid fallback reason.
    Use create_mesh/create_curve for explicit geometry, add_modifier with
    settings for modifiers, material tools for basic PBR and
    set_transform_keyframes for animation. Do supported work with those tools.

    Node graphs, rig weights, IK, collision proxies, drivers, physics baking,
    UV unwrap and common mesh edits now have dedicated
    tools. Check search_tools/get_tool_help for available capabilities.
    Do not bypass tool safeguards or switch
    to Python merely because a tool call failed. bpy is in scope; stdout is
    returned. Keep snippets small; the namespace is fresh on every call.
    """
    if not fallback_reason.strip():
        raise ValueError("fallback_reason must explain the exact capability missing from dedicated tools")
    if not tools_considered:
        raise ValueError("Check dedicated tools first and provide their names in tools_considered")
    native_names = {
        tool.name for tool in await mcp.list_tools()
        if tool.name in _native_functions
    }
    considered = set(tools_considered)
    unknown = sorted(considered - native_names - {"search_tools", "get_tool_help"})
    if unknown:
        raise ValueError(f"Unknown or ineligible tools_considered names: {', '.join(unknown)}")
    if not considered.intersection(native_names):
        raise ValueError("tools_considered must include at least one dedicated Blender operation, not only discovery tools")
    result = await asyncio.to_thread(_call, "execute_code", {"code": code})
    return result.get("output", "") or "(no stdout)"


@mcp.tool()
def get_viewport_screenshot(max_size: int = 800) -> Image:
    """Capture the active 3D viewport as a PNG image."""
    result = _call("get_viewport_screenshot", {"max_size": max_size})
    return Image(data=base64.b64decode(result["data"]), format="png")


_native_functions = {
    name: value for name, value in list(globals().items())
    if not name.startswith("_") and name != "execute_blender_code"
    and inspect.isfunction(value) and value.__module__ == __name__
}
_native_functions.update(register_tools(mcp, _call))
_native_functions.update(register_production_tools(mcp, _call))


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
async def search_tools(query: str, limit: int = 8) -> dict[str, Any]:
    """Find dedicated tools by task/name/description before using Python fallback.

    Examples: 'UV', 'light energy', 'shader nodes', 'armature', 'mesh bevel'.
    Returns relevant names, descriptions and argument schemas. Does not need
    a Blender connection. Use get_tool_help for an exact tool's full schema.
    """
    if not 1 <= limit <= 25 or not query.strip():
        raise ValueError("Provide a query and limit between 1 and 25")
    terms = query.lower().replace("_", " ").split()
    ranked = []
    for item in await mcp.list_tools():
        if item.name not in _native_functions:
            continue
        name = item.name.lower().replace("_", " ")
        description = (item.description or "").lower()
        score = sum(3 * (term in name) + (term in description) for term in terms)
        if score:
            ranked.append((score, item))
    ranked.sort(key=lambda pair: (-pair[0], pair[1].name))
    return {"tools": [{"name": item.name, "description": item.description, "input_schema": item.inputSchema}
                      for _, item in ranked[:limit]], "matches": len(ranked)}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
async def get_tool_help(name: str) -> dict[str, Any]:
    """Return a dedicated tool's complete description and MCP argument schema, without Blender."""
    for prefix in ("blender_", "mcp__blender__", "mcp_blender_local_", "mcp__blender_local__"):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    for item in await mcp.list_tools():
        if item.name == name:
            return {"name": name, "description": item.description, "input_schema": item.inputSchema,
                    "annotations": item.annotations.model_dump() if item.annotations else None}
    raise ValueError(f"Unknown tool: {name}")


@mcp.resource("blender://guide")
def workflow_guide() -> str:
    """Tool-first Blender workflow guide."""
    return TOOL_USAGE_INSTRUCTIONS


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
def get_error_log(limit: int = 20, tool: str | None = None, event_id: str | None = None) -> dict[str, Any]:
    """Read recent local sanitized error events, newest first, without contacting Blender.

    Filter by tool or event_id. Logs contain fixed summaries, frame locations and
    argument types/counts, never raw argument values, Python code or stdout.
    """
    known = {item.name for item in mcp._tool_manager.list_tools()} | {"unknown_tool", "server"}
    return _error_journal.read(limit=limit, tool=tool, event_id=event_id, known_tools=known)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
def get_error_log_status() -> dict[str, Any]:
    """Inspect local error logging path, enabled state, rotation and write failures."""
    return _error_journal.status()


@mcp.resource("blender://capabilities")
async def tool_catalog() -> str:
    """Complete tool catalog and schemas; available even while Blender is disconnected."""
    return json.dumps({"version": __version__, "tools": [tool.model_dump() for tool in await mcp.list_tools()]})


@mcp.prompt()
def blender_workflow(task: str) -> str:
    """Plan, execute and verify a Blender task with dedicated tools."""
    return TOOL_USAGE_INSTRUCTIONS + f"\nTask: {task}\nInspect the scene, identify the relevant tools, perform the work, then verify the result."


class BlenderToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: str = Field(description="Exact dedicated MCP tool name, e.g. create_object or add_modifier")
    arguments: dict[str, Any] = Field(default_factory=dict, description="The same arguments as a direct call to that tool")


@mcp.tool()
async def batch_tools(operations: list[BlenderToolCall], ctx: Context | None = None) -> CallToolResult:
    """Run 1-100 dedicated Blender tool calls in order without writing Python.

    Prefer this to execute_blender_code for repeated objects or multi-step
    scene construction. Each item has tool and arguments, using exactly the
    same tool names and argument schemas as individual calls. Example:
    [{"tool":"create_object","arguments":{"name":"Box","type":"cube"}},
     {"tool":"add_modifier","arguments":{"object_name":"Box","type":"BEVEL",
      "settings":{"width":0.1,"segments":3}}}].

    All tool names and argument schemas are validated before any execution.
    Calls stop at the first Blender error; completed edits are not rolled back.
    Results include zero-based indices and actual returned object names. Name
    arguments can reference earlier results with {"$ref":"0.name"}; nested
    settings may also use refs. Invalid forward references fail preflight.
    Sends one Blender request for the whole batch, preventing interleaving.
    Images are returned as MCP image content alongside the JSON summary.
    Python execution and nested batches are not allowed.
    """
    if not 1 <= len(operations) <= 100:
        raise ValueError("Provide between 1 and 100 operations")
    tools: dict[str, Tool] = {}
    commands = []
    for index, operation in enumerate(operations):
        if operation.tool not in _native_functions:
            raise ValueError(f"Operation {index}: '{operation.tool}' is not a dedicated Blender tool")
        if operation.tool not in tools:
            tools[operation.tool] = Tool.from_function(_native_functions[operation.tool])
        tool = tools[operation.tool]
        extra = set(operation.arguments) - tool.fn_metadata.arg_model.model_fields.keys()
        if extra:
            raise ValueError(f"Operation {index}: unknown arguments for {operation.tool}: {', '.join(sorted(extra))}")
        placeholders = {}

        def validate_refs(value):
            if isinstance(value, dict):
                if set(value) == {"$ref"}:
                    parts = str(value["$ref"]).split(".")
                    if len(parts) < 2 or not parts[0].isdigit() or not 0 <= int(parts[0]) < index:
                        raise ValueError(f"Operation {index}: invalid backward result reference {value['$ref']}")
                else:
                    for item in value.values(): validate_refs(item)
            elif isinstance(value, list):
                for item in value: validate_refs(item)

        validate_refs(operation.arguments)
        raw = dict(operation.arguments)
        for key, value in raw.items():
            if isinstance(value, dict) and set(value) == {"$ref"}:
                placeholder = f"__blender_batch_ref_{index}_{key}__"
                placeholders[placeholder] = value
                raw[key] = placeholder
        try:
            arguments = tool.fn_metadata.arg_model.model_validate(
                tool.fn_metadata.pre_parse_json(raw)
            ).model_dump()
        except ValueError as error:
            raise ValueError(f"Operation {index} ({operation.tool}): {error}") from error
        prepared = []
        marker = _prepared_commands.set(prepared)
        try:
            await tool.run(arguments)
        finally:
            _prepared_commands.reset(marker)
        if len(prepared) != 1:
            raise ValueError(f"Tool {tool.name} is not batchable")

        def restore_refs(value):
            if isinstance(value, str): return placeholders.get(value, value)
            if isinstance(value, dict): return {key: restore_refs(item) for key, item in value.items()}
            if isinstance(value, list): return [restore_refs(item) for item in value]
            return value

        commands.append(restore_refs(prepared[0]))

    if ctx is not None:
        await ctx.report_progress(0, len(commands), "Prepared Blender batch")

    def send():
        try:
            return _dispatch_to_blender(lambda: _get_conn().send_batch(commands))
        except Exception as error:
            if not _error_context.get() or not _error_context.get()["logged"]:
                _record_error("batch_tools", error, {"operations": commands}, phase="batch_call")
            raise

    summary = await asyncio.to_thread(send)
    for entry in summary["results"]:
        if "error" in entry:
            index = entry["index"]
            entry["error_id"] = _record_error(entry["tool"], RuntimeError(entry["error"]), commands[index]["arguments"],
                                             phase="batch_step", batch_index=index)
    images = []
    for entry in summary["results"]:
        if "result" in entry and entry["tool"] in {"render_preview", "get_viewport_screenshot"}:
            value = entry["result"]
            images.append(Image(data=base64.b64decode(value.pop("data")), format="png").to_image_content())
            value["image_content_index"] = len(images)
    if ctx is not None:
        await ctx.report_progress(summary["completed"], len(commands), "Blender batch finished")
    return CallToolResult(
        isError=summary["failed"],
        content=[TextContent(type="text", text=json.dumps(summary, indent=2)), *images],
    )


def main(argv: list[str] | None = None) -> None:
    global HOST, PORT, TIMEOUT, PROTOCOL, _error_journal
    parser = argparse.ArgumentParser(description=f"Tool-first local Blender MCP {__version__} (stdio)")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--timeout", type=float, default=TIMEOUT)
    parser.add_argument("--protocol", choices=["legacy", "length"], default=PROTOCOL)
    logging_options = parser.add_mutually_exclusive_group()
    logging_options.add_argument("--error-log", help="Local sanitized JSONL error journal (default: project logs or user state directory)")
    logging_options.add_argument("--no-error-log", action="store_true", help="Disable persistent error logging")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535 or not 0 < args.timeout < float("inf"):
        parser.error("port must be 1-65535 and timeout must be positive and finite")
    HOST, PORT, TIMEOUT, PROTOCOL = args.host, args.port, args.timeout, args.protocol
    try:
        _error_journal = ErrorJournal(None if args.no_error_log else args.error_log or default_log_path())
    except ValueError as error:
        parser.error(str(error))
    try:
        mcp.run()
    except Exception as error:
        _record_error("server", error, phase="server")
        raise


if __name__ == "__main__":
    main()
