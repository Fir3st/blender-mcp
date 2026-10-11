"""Blender addon: TCP server that executes scene/object/material/code commands
sent by the local MCP bridge. No external services, no telemetry."""

bl_info = {
    "name": "Blender MCP (Local)",
    "author": "blende-mcp",
    "version": (1, 0, 7),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar > MCP",
    "description": "Local MCP bridge — exposes a TCP socket so an MCP server can drive Blender.",
    "category": "Development",
}

import base64
import io
import hashlib
import json
import os
import socket
import struct
import tempfile
import traceback
from contextlib import redirect_stdout

import bpy
from mathutils import Vector


# ---------------------------------------------------------------------------
# Command handlers (run on Blender's main thread)
# ---------------------------------------------------------------------------

_command_library = None
_library_id = None


class LibraryNotLoaded(RuntimeError):
    pass


def cmd_bridge_info(_params):
    return {"api_version": 1, "addon_version": "1.0.7", "library_id": _library_id}


def cmd_load_command_library(params):
    global _command_library, _library_id
    source = params["source"]
    library_id = hashlib.sha256(source.encode()).hexdigest()
    if library_id != params["library_id"]:
        raise ValueError("Command library checksum mismatch")
    namespace = {"__name__": "__blender_mcp_commands__"}
    exec(compile(source, "<blender-mcp-command-library>", "exec"), namespace)
    if not callable(namespace.get("dispatch")):
        raise ValueError("Command library has no dispatch function")
    _command_library, _library_id = namespace["dispatch"], library_id
    return {"library_id": library_id}


def _require_library(params):
    if _command_library is None or params.get("library_id") != _library_id:
        raise LibraryNotLoaded("Blender command library must be loaded")


def cmd_command(params):
    _require_library(params)
    return _command_library(params["command"], params.get("arguments") or {})


def _resolve_batch_refs(value, results):
    if isinstance(value, dict):
        if set(value) == {"$ref"}:
            parts = str(value["$ref"]).split(".")
            index = int(parts[0])
            if not 0 <= index < len(results) or "error" in results[index]:
                raise ValueError(f"Unavailable batch result: {value['$ref']}")
            resolved = results[index]["result"]
            for part in parts[1:]:
                resolved = resolved[int(part)] if isinstance(resolved, list) else resolved[part]
            return resolved
        return {key: _resolve_batch_refs(item, results) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_batch_refs(item, results) for item in value]
    return value


def cmd_batch(params):
    _require_library(params)
    operations = params["operations"]
    if not isinstance(operations, list) or not 1 <= len(operations) <= 100:
        raise ValueError("A batch must contain 1-100 operations")
    if any(op.get("command") in {"execute", "execute_code", "batch", "load_command_library"} for op in operations):
        raise ValueError("Python execution and nested batches are not allowed")
    results = []
    for index, operation in enumerate(operations):
        try:
            arguments = _resolve_batch_refs(operation.get("arguments") or {}, results)
            result = _command_library(operation["command"], arguments)
            json.dumps(result, allow_nan=False)
            results.append({"index": index, "tool": operation["command"], "result": result})
        except Exception as error:
            results.append({"index": index, "tool": operation["command"], "error": f"{type(error).__name__}: {error}\n{traceback.format_exc()}"})
            return {"completed": index, "total": len(operations), "results": results, "failed": True}
    return {"completed": len(results), "total": len(operations), "results": results, "failed": False}


def _object_summary(obj):
    return {
        "name": obj.name,
        "type": obj.type,
        "location": list(obj.location),
        "rotation_euler": list(obj.rotation_euler),
        "scale": list(obj.scale),
        "visible": not obj.hide_viewport,
    }


def cmd_get_scene_info(_params):
    scene = bpy.context.scene
    objects = [
        {"name": o.name, "type": o.type, "location": list(o.location)}
        for o in scene.objects
    ]
    return {
        "scene": scene.name,
        "frame_current": scene.frame_current,
        "frame_start": scene.frame_start,
        "frame_end": scene.frame_end,
        "object_count": len(objects),
        "objects": objects,
    }


def cmd_get_object_info(params):
    name = params["name"]
    obj = bpy.data.objects.get(name)
    if obj is None:
        raise KeyError(f"Object not found: {name}")

    info = _object_summary(obj)
    info["materials"] = [m.name for m in obj.material_slots if m.material]
    if obj.type == "MESH" and obj.data:
        info["mesh"] = {
            "vertices": len(obj.data.vertices),
            "edges": len(obj.data.edges),
            "polygons": len(obj.data.polygons),
        }
    info["bounding_box_world"] = [
        list(obj.matrix_world @ Vector(c)) for c in obj.bound_box
    ]
    return info


def cmd_create_object(params):
    type_ = (params.get("type") or "cube").lower()
    location = tuple(params.get("location") or (0, 0, 0))
    rotation = tuple(params.get("rotation") or (0, 0, 0))
    scale = tuple(params.get("scale") or (1, 1, 1))

    if type_ == "cube":
        bpy.ops.mesh.primitive_cube_add(location=location, rotation=rotation, scale=scale)
    elif type_ == "sphere":
        bpy.ops.mesh.primitive_uv_sphere_add(location=location, rotation=rotation, scale=scale)
    elif type_ == "cylinder":
        bpy.ops.mesh.primitive_cylinder_add(location=location, rotation=rotation, scale=scale)
    elif type_ == "plane":
        bpy.ops.mesh.primitive_plane_add(location=location, rotation=rotation, scale=scale)
    elif type_ == "cone":
        bpy.ops.mesh.primitive_cone_add(location=location, rotation=rotation, scale=scale)
    elif type_ == "torus":
        bpy.ops.mesh.primitive_torus_add(location=location, rotation=rotation)
    elif type_ == "empty":
        bpy.ops.object.empty_add(location=location, rotation=rotation, scale=scale)
    elif type_ == "camera":
        bpy.ops.object.camera_add(location=location, rotation=rotation)
    elif type_ == "light":
        light_type = (params.get("light_type") or "POINT").upper()
        bpy.ops.object.light_add(type=light_type, location=location, rotation=rotation)
    else:
        raise ValueError(f"Unknown object type: {type_}")

    obj = bpy.context.active_object
    name = params.get("name")
    if name:
        obj.name = name
    if type_ in ("torus", "camera", "light"):
        obj.scale = scale
    return _object_summary(obj)


def cmd_delete_object(params):
    name = params["name"]
    obj = bpy.data.objects.get(name)
    if obj is None:
        raise KeyError(f"Object not found: {name}")
    bpy.data.objects.remove(obj, do_unlink=True)
    return {"deleted": name}


def cmd_modify_object(params):
    name = params["name"]
    obj = bpy.data.objects.get(name)
    if obj is None:
        raise KeyError(f"Object not found: {name}")
    if "location" in params:
        obj.location = tuple(params["location"])
    if "rotation" in params:
        obj.rotation_euler = tuple(params["rotation"])
    if "scale" in params:
        obj.scale = tuple(params["scale"])
    if "visible" in params:
        obj.hide_viewport = not bool(params["visible"])
    return _object_summary(obj)


def cmd_set_material(params):
    name = params["object_name"]
    obj = bpy.data.objects.get(name)
    if obj is None:
        raise KeyError(f"Object not found: {name}")
    if not (obj.data and hasattr(obj.data, "materials")):
        raise TypeError(f"Object '{name}' does not support materials")

    mat_name = params.get("material_name") or f"{name}_mat"
    mat = bpy.data.materials.get(mat_name)
    if mat is None:
        mat = bpy.data.materials.new(name=mat_name)
    mat.use_nodes = True

    color = params.get("color")
    if color:
        rgba = list(color) + [1.0] * (4 - len(color))
        bsdf = mat.node_tree.nodes.get("Principled BSDF")
        if bsdf and "Base Color" in bsdf.inputs:
            bsdf.inputs["Base Color"].default_value = tuple(rgba[:4])

    if obj.data.materials:
        obj.data.materials[0] = mat
    else:
        obj.data.materials.append(mat)
    return {"object": name, "material": mat.name, "color": color}


def cmd_execute_code(params):
    code = params["code"]
    namespace = {"bpy": bpy, "__name__": "__blender_mcp__"}
    buf = io.StringIO()
    with redirect_stdout(buf):
        exec(compile(code, "<mcp>", "exec"), namespace)
    return {"output": buf.getvalue()}


def cmd_execute(params):
    namespace = {"bpy": bpy, "__name__": "__blender_mcp__"}
    buf = io.StringIO()
    with redirect_stdout(buf):
        exec(compile(params["code"], "<mcp>", "exec"), namespace)
    result = namespace.get("result")
    if result is None and buf.getvalue():
        result = {"output": buf.getvalue()}
    if params.get("strict_json", True):
        json.dumps(result)
    return result


def cmd_get_viewport_screenshot(params):
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

    return {"format": "png", "data": base64.b64encode(data).decode("ascii")}


HANDLERS = {
    "bridge_info": cmd_bridge_info,
    "load_command_library": cmd_load_command_library,
    "command": cmd_command,
    "batch": cmd_batch,
    "execute": cmd_execute,
    "get_scene_info": cmd_get_scene_info,
    "get_object_info": cmd_get_object_info,
    "create_object": cmd_create_object,
    "delete_object": cmd_delete_object,
    "modify_object": cmd_modify_object,
    "set_material": cmd_set_material,
    "execute_code": cmd_execute_code,
    "get_viewport_screenshot": cmd_get_viewport_screenshot,
}


# ---------------------------------------------------------------------------
# Socket server (non-blocking, driven by bpy.app.timers — main-thread safe)
# ---------------------------------------------------------------------------

class BlenderMCPServer:
    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.listen_sock: socket.socket | None = None
        self.client: socket.socket | None = None
        self.recv_buf = bytearray()
        self.send_buf = bytearray()
        self.client_protocol = None

    def start(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.host, self.port))
        s.listen(1)
        s.setblocking(False)
        self.listen_sock = s
        if not bpy.app.timers.is_registered(self._tick):
            bpy.app.timers.register(self._tick, persistent=True)
        print(f"[blender-mcp-local] listening on {self.host}:{self.port}")

    def stop(self) -> None:
        if bpy.app.timers.is_registered(self._tick):
            bpy.app.timers.unregister(self._tick)
        self._drop_client()
        if self.listen_sock is not None:
            try:
                self.listen_sock.close()
            except OSError:
                pass
            self.listen_sock = None
        print("[blender-mcp-local] stopped")

    def _drop_client(self) -> None:
        if self.client is not None:
            try:
                self.client.close()
            except OSError:
                pass
            self.client = None
        self.recv_buf.clear()
        self.send_buf.clear()
        self.client_protocol = None

    def _tick(self) -> float:
        if self.listen_sock is None:
            return None  # unregister

        # Accept one client at a time.
        if self.client is None:
            try:
                client, _addr = self.listen_sock.accept()
                client.setblocking(False)
                self.client = client
                self.recv_buf.clear()
            except BlockingIOError:
                pass
            except OSError:
                pass

        if self.client is not None:
            try:
                while True:
                    chunk = self.client.recv(65536)
                    if not chunk:
                        self._drop_client()
                        break
                    self.recv_buf += chunk
                    if len(self.recv_buf) > 64 * 1024 * 1024 + 4:
                        self._drop_client()
                        break
            except BlockingIOError:
                pass
            except OSError:
                self._drop_client()

            if self.client is not None:
                self._process_messages()
                self._flush_send()

        return 0.05

    def _process_messages(self) -> None:
        while self.recv_buf:
            if self.client_protocol is None:
                self.client_protocol = "legacy" if self.recv_buf.startswith(b"{") else "length"
            if self.client_protocol == "legacy":
                if b"\0" not in self.recv_buf:
                    return
                end = self.recv_buf.index(0)
                if end > 64 * 1024 * 1024:
                    self._drop_client()
                    return
                payload = bytes(self.recv_buf[:end])
                del self.recv_buf[:end + 1]
            else:
                if len(self.recv_buf) < 4:
                    return
                (length,) = struct.unpack(">I", bytes(self.recv_buf[:4]))
                if length > 64 * 1024 * 1024:
                    self._drop_client()
                    return
                if len(self.recv_buf) < 4 + length:
                    return
                payload = bytes(self.recv_buf[4:4 + length])
                del self.recv_buf[:4 + length]
            response = self._dispatch(payload)
            self._send(response)

    def _dispatch(self, payload: bytes) -> dict:
        try:
            cmd = json.loads(payload.decode("utf-8"))
            if not isinstance(cmd, dict):
                raise ValueError("Expected a JSON object")
        except Exception as e:
            return {"status": "error", "message": f"invalid JSON: {e}"}

        cmd_type = cmd.get("type")
        params = cmd.get("params") or {}
        if cmd_type == "execute" and "code" in cmd:
            params = {"code": cmd["code"], "strict_json": cmd.get("strict_json", True)}
        handler = HANDLERS.get(cmd_type)
        if handler is None:
            return {"status": "error", "message": f"unknown command: {cmd_type}"}
        try:
            result = handler(params)
            json.dumps(result, allow_nan=False)
            return {"status": "success", "result": result}
        except Exception as e:
            return {
                "status": "error",
                "code": "LIBRARY_NOT_LOADED" if isinstance(e, LibraryNotLoaded) else "BLENDER_ERROR",
                "message": f"{type(e).__name__}: {e}\n{traceback.format_exc()}",
            }

    def _send(self, obj: dict) -> None:
        if self.client is None:
            return
        data = json.dumps(obj).encode("utf-8")
        framed = data + b"\0" if self.client_protocol == "legacy" else struct.pack(">I", len(data)) + data
        self.send_buf.extend(framed)

    def _flush_send(self) -> None:
        if self.client is None or not self.send_buf:
            return
        try:
            sent = self.client.send(self.send_buf)
            if sent == 0:
                self._drop_client()
                return
            del self.send_buf[:sent]
        except BlockingIOError:
            pass
        except OSError:
            self._drop_client()


_server: BlenderMCPServer | None = None


# ---------------------------------------------------------------------------
# Blender UI / Operators / Properties
# ---------------------------------------------------------------------------

class MCPL_OT_start(bpy.types.Operator):
    bl_idname = "mcpl.start_server"
    bl_label = "Start MCP Server"
    bl_description = "Start the local MCP TCP server"

    def execute(self, context):
        global _server
        if _server is not None:
            self.report({"INFO"}, "MCP server already running")
            return {"CANCELLED"}
        prefs = context.scene.blender_mcp_local
        try:
            _server = BlenderMCPServer(prefs.host, prefs.port)
            _server.start()
        except Exception as e:
            _server = None
            self.report({"ERROR"}, f"Failed to start: {e}")
            return {"CANCELLED"}
        prefs.is_running = True
        return {"FINISHED"}


class MCPL_OT_stop(bpy.types.Operator):
    bl_idname = "mcpl.stop_server"
    bl_label = "Stop MCP Server"
    bl_description = "Stop the local MCP TCP server"

    def execute(self, context):
        global _server
        if _server is not None:
            _server.stop()
            _server = None
        context.scene.blender_mcp_local.is_running = False
        return {"FINISHED"}


class MCPL_PT_panel(bpy.types.Panel):
    bl_label = "Blender MCP (Local)"
    bl_idname = "MCPL_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "MCP"

    def draw(self, context):
        layout = self.layout
        prefs = context.scene.blender_mcp_local
        col = layout.column(align=True)
        col.prop(prefs, "host")
        col.prop(prefs, "port")
        row = layout.row(align=True)
        if _server is not None and _server.listen_sock is not None:
            row.operator("mcpl.stop_server", icon="PAUSE")
            layout.label(text=f"Listening on {_server.host}:{_server.port}", icon="CHECKMARK")
        else:
            row.operator("mcpl.start_server", icon="PLAY")
            layout.label(text="Stopped", icon="CANCEL")


class MCPL_Props(bpy.types.PropertyGroup):
    host: bpy.props.StringProperty(name="Host", default="127.0.0.1")
    port: bpy.props.IntProperty(name="Port", default=9876, min=1, max=65535)
    is_running: bpy.props.BoolProperty(default=False)


_classes = (MCPL_Props, MCPL_OT_start, MCPL_OT_stop, MCPL_PT_panel)


def register():
    bpy.app.driver_namespace["blender_mcp_local_version"] = ".".join(map(str, bl_info["version"]))
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.blender_mcp_local = bpy.props.PointerProperty(type=MCPL_Props)


def unregister():
    global _server
    if _server is not None:
        _server.stop()
        _server = None
    bpy.app.driver_namespace.pop("blender_mcp_local_version", None)
    del bpy.types.Scene.blender_mcp_local
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
