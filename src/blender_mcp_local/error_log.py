"""Private, bounded error journal. Stores diagnostics, never raw arguments/code."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import threading
import uuid


ERROR_TYPES = {"AttributeError", "ValueError", "KeyError", "TypeError", "RuntimeError",
               "OSError", "ConnectionError", "TimeoutError", "ValidationError", "ToolError", "CancelledError"}
FRAME_FILES = {"server.py", "advanced.py", "production.py", "addon.py", "tool_manager.py", "base.py",
               "tools.py", "func_metadata.py", "mcp_to_blender_server.py", "error_log.py",
               "<string>", "<execute-wire>", "<blender-mcp-command-library>", "<generated>"}
FRAME_FUNCTIONS = {"dispatch", "dispatch_advanced", "dispatch_production", "socket_at", "socket_defaults",
                   "rna_set", "rna_target", "property_at", "pointer_value", "call_tool", "run", "_call",
                   "send_command", "send_batch", "_library_request", "_execute_library_request", "execute_code",
                   "_request", "_result", "_read_execute_result", "_execute_code", "_dispatch", "threaded", "<module>",
                   "json_value", "transport_result", "expression_valid", "_dispatch_to_blender"}
SUMMARIES = [
    ("Connection refused", "The Blender endpoint refused the connection; start its MCP server and verify the host/port."),
    ("Node type", "The node type is unavailable or unsupported by this Blender version or node-tree type."),
    ("Node owner not found", "The material/world owning the requested node tree was not found."),
    ("Node tree not found", "The requested node tree is absent or disabled; create or enable it first."),
    ("Node not found", "The requested node was not found; inspect the actual node names."),
    ("Node names must be unique", "Node specifications contain duplicate names."),
    ("Node name already exists", "A requested node name already exists; use set_node_inputs or explicitly replace it."),
    ("A node to remove was not found", "A node selected for removal was not found; inspect the actual node names."),
    ("Link references a missing or removed node", "A node link references a missing or removed node."),
    ("Link references a disabled socket", "A node link references a disabled socket."),
    ("Incompatible node socket types", "The requested node link connects incompatible socket types."),
    ("Not an RNA pointer property", "A nested RNA path traverses a non-pointer or unknown property."),
    ("Unset pointer in property path", "A nested RNA path traverses an unset pointer."),
    ("is not a mesh", "Mesh inspection/editing was requested for a non-mesh object."),
    ("requires VERT domain", "Mesh operation requires the VERT domain; inspect the operation/domain mapping."),
    ("requires EDGE domain", "Mesh operation requires the EDGE domain; inspect the operation/domain mapping."),
    ("requires FACE domain", "Mesh operation requires the FACE domain; inspect the operation/domain mapping."),
    ("settings; inspect mesh_edit tool help", "Mesh settings are not supported by the selected operation."),
    ("is not in the active view layer", "An object is outside the active view layer; inspect scene membership before joining."),
    ("Unsupported origin mode", "The origin mode is unsupported; use the schema's documented enum."),
    ("Unsupported center mode", "The origin center mode is unsupported; use MEDIAN or BOUNDS."),
    ("Unknown tool:", "Tool help requested an unknown name; inspect the installed tool catalog."),
    ("RNA boolean requires", "An RNA boolean requires a true/false value, not another type."),
    ("RNA boolean array requires", "An RNA boolean array requires boolean components and the correct length."),
    ("RNA string requires", "An RNA string requires a string value."),
    ("Invalid RNA enum", "An RNA enum value is invalid; inspect the property's enum choices."),
    ("Apply or remove modifiers", "Join rejected unresolved modifiers; apply or remove them explicitly first."),
    ("shape keys", "The operation is incompatible with the object's shape-key state."),
    ("Camera not found", "The requested camera was not found; inspect its actual returned name."),
    ("Property is not writable", "The requested RNA property is read-only or a collection."),
    ("Out of range or nonfinite value", "An RNA numeric value is outside its supported finite range."),
    ("Wrong array", "The supplied RNA array has an incompatible shape or length."),
    ("Expected an RNA property path", "The supplied RNA property path is unsupported."),
    ("Array driver requires", "An array driver needs an explicit valid component index."),
    ("Scalar driver property", "A scalar driver must use index=-1 rather than an array component."),
    ("Blender rejected driver creation", "Blender rejected driver creation; verify property animatability, path and index."),
    ("Driver already exists", "A driver already exists; replacement must be explicitly requested."),
    ("Object of type Matrix", "An RNA matrix could not be serialized into JSON."),
    ("Object of type Vector", "An RNA vector could not be serialized into JSON."),
    ("Request cancelled before Blender dispatch", "Request cancelled before dispatch; no scene operation started."),
    ("Cannot load from the current blend file", "Blender rejected loading its current file as a library."),
    ("Cannot link from the current blend file", "Self-linking a blend library is not supported."),
    ("Socket name is missing or ambiguous", "Node socket selector is missing or ambiguous."),
    ("Modifier not found", "The requested modifier was not found."),
    ("was not applied; operator returned", "Modifier application did not complete."),
    ("Datablock pointer not found", "An RNA datablock pointer could not be resolved."),
    ("RNA target not found", "The requested RNA owner was not found."),
    ("Unknown RNA property", "The requested RNA property does not exist."),
    ("NoneType", "An operation used a missing/None value."),
    ("Blender closed the connection", "The Blender connection closed before the response completed."),
    ("timed out", "The operation or connection timed out."),
    ("validation error", "Tool argument validation failed; input values are omitted."),
    ("Object not found", "The requested object could not be found."),
    ("Library datablock names are missing or duplicated", "Library names were missing or duplicated."),
]
GENERIC_SUMMARY = "Operation failed; unstructured details are omitted. Use tool, frames and argument shape to investigate."
CANCEL_SUMMARY = "Client request was cancelled; inspect execution_state before retrying. In-flight scene work may continue."


def default_log_path():
    root = Path(__file__).resolve().parents[2]
    if (root / "pyproject.toml").is_file() and (root / "src" / "blender_mcp_local").is_dir():
        return root / "logs" / "errors.jsonl"
    return Path.home() / ".local" / "state" / "blender-mcp-local" / "errors.jsonl"


def argument_shape(arguments, keys):
    result = {}
    if not isinstance(arguments, dict):
        return result
    for key in sorted(set(arguments).intersection(keys)):
        value = arguments[key]
        type_name = type(value).__name__ if type(value) in (str, int, float, bool, list, dict, type(None)) else "other"
        result[key] = {"type": type_name}
        if type(value) in (list, dict):
            result[key]["items"] = len(value)
    return result


def diagnostic(error, include_remote=True):
    """Extract frame metadata without loading source lines or capturing locals."""
    frames = []
    text = str(error) if include_remote else ""
    remote_types = re.findall(r"^\s*([A-Za-z]+Error):", text, re.MULTILINE)
    error_type = next((name for name in reversed(remote_types) if name in ERROR_TYPES), type(error).__name__)
    if error_type not in ERROR_TYPES:
        error_type = "Error"

    def frame(filename, line, function):
        # Basenames only, and no generated/custom source or function names.
        basename = Path(filename).name
        if basename not in FRAME_FILES:
            basename = "[external source omitted]"
        safe_function = function if function in FRAME_FUNCTIONS else "[function omitted]"
        frames.append({"file": basename, "line": int(line), "function": safe_function})

    current = error
    for _ in range(5):
        if current is None:
            break
        tb = current.__traceback__
        while tb is not None and len(frames) < 40:
            frame(tb.tb_frame.f_code.co_filename, tb.tb_lineno, tb.tb_frame.f_code.co_name)
            tb = tb.tb_next
        current = current.__cause__
    if include_remote:
        for filename, line, function in re.findall(r'^\s*File "([^"\n]+)", line (\d+), in ([^\n]+)', text, re.MULTILINE):
            if len(frames) >= 40:
                break
            frame(filename, line, function.strip())
    summary = next((message for marker, message in SUMMARIES if marker.casefold() in text.casefold()), GENERIC_SUMMARY)
    if error_type == "CancelledError": summary = CANCEL_SUMMARY
    return error_type, summary, frames


class ErrorJournal:
    def __init__(self, path=None, *, max_bytes=5 * 1024 * 1024, backups=2):
        self.path = Path(path).absolute() if path is not None else None
        if self.path is not None:
            name = self.path.name.lower()
            if self.path.suffix != ".jsonl" or name.startswith(".env") or any(word in name for word in (
                    "secret", "token", "credential", "cert", "crt", "csr", "keystore", "truststore", "vault", "private", "pass", "enc")):
                raise ValueError("Error journal requires a non-sensitive .jsonl filename")
        self.max_bytes, self.backups = max_bytes, backups
        self.session_id = uuid.uuid4().hex
        self.lock = threading.Lock()
        self.last_write_error = None

    def paths(self):
        if self.path is None:
            return []
        return [self.path] + [self.path.with_name(f"{self.path.stem}.{index}.jsonl") for index in range(1, self.backups + 1)]

    def status(self):
        return {"enabled": self.path is not None, "path": str(self.path) if self.path else None,
                "session_id": self.session_id, "max_bytes": self.max_bytes,
                "backups": self.backups, "last_write_error": self.last_write_error}

    def record(self, *, tool, error, arguments=None, argument_keys=(), phase="tool", version=None, batch_index=None, execution_state=None):
        if self.path is None:
            return None
        error_type, summary, frames = diagnostic(error, include_remote=tool != "execute_blender_code")
        event = {"schema_version": 1, "event_id": uuid.uuid4().hex, "session_id": self.session_id,
                 "timestamp": datetime.now(timezone.utc).isoformat(), "bridge_version": version,
                 "tool": tool, "phase": phase, "error_type": error_type, "summary": summary,
                 "arguments": argument_shape(arguments, argument_keys), "frames": frames}
        if batch_index is not None:
            event["batch_index"] = batch_index
        if execution_state is not None:
            event["execution_state"] = execution_state
        data = (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            with self.lock:
                paths = self.paths()
                if self.path.parent.is_symlink() or any(path.is_symlink() for path in paths):
                    raise OSError("Journal symlinks are not supported")
                self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if self.path.exists() and self.path.stat().st_size + len(data) > self.max_bytes:
                    for index in range(self.backups, 0, -1):
                        source = paths[index - 1]
                        if source.exists():
                            source.replace(paths[index])
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
                with os.fdopen(fd, "ab") as output:
                    output.write(data)
                self.last_write_error = None
            return event["event_id"]
        except OSError as error:
            # Diagnostics must not replace the original operation failure.
            self.last_write_error = type(error).__name__
            return None

    def read(self, *, limit=20, tool=None, event_id=None, known_tools=None):
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        events, invalid_lines = [], 0
        with self.lock:
            for path in reversed(self.paths()):
                if path.is_symlink():
                    raise ValueError("Journal symlinks are not supported")
                if not path.is_file():
                    continue
                if path.stat().st_size > self.max_bytes + 65536:
                    raise ValueError("Journal file exceeds its bounded size")
                with path.open(encoding="utf-8") as source:
                    for line in source:
                        try:
                            event = json.loads(line)
                            if not isinstance(event, dict) or event.get("schema_version") != 1:
                                raise ValueError("Unknown journal record")
                            allowed = {"schema_version", "event_id", "session_id", "timestamp", "bridge_version",
                                       "tool", "phase", "error_type", "summary", "arguments", "frames", "batch_index", "execution_state"}
                            if set(event) - allowed or event.get("summary") not in {GENERIC_SUMMARY, CANCEL_SUMMARY, *(message for _, message in SUMMARIES)}:
                                raise ValueError("Unexpected journal fields")
                            if not all(isinstance(event.get(key), str) and re.fullmatch(r"[0-9a-f]{32}", event[key]) for key in ("event_id", "session_id")):
                                raise ValueError("Invalid event identity")
                            datetime.fromisoformat(event["timestamp"])
                            if event.get("error_type") not in ERROR_TYPES | {"Error"}:
                                raise ValueError("Invalid error type")
                            if known_tools is not None and event.get("tool") not in known_tools:
                                raise ValueError("Unknown logged tool")
                            if event.get("phase") not in {"tool", "blender_call", "batch_call", "batch_step", "returned_error", "cancelled", "server"}:
                                raise ValueError("Unknown phase")
                            version = event.get("bridge_version")
                            if version is not None and (not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version)):
                                raise ValueError("Invalid version")
                            for key, shape in event["arguments"].items():
                                if not re.fullmatch(r"[a-z_]{1,64}", key) or set(shape) - {"type", "items"} or shape.get("type") not in {"str", "int", "float", "bool", "list", "dict", "NoneType", "other"}:
                                    raise ValueError("Unexpected argument data")
                                if "items" in shape and (type(shape["items"]) is not int or shape["items"] < 0):
                                    raise ValueError("Invalid argument count")
                            for frame in event["frames"]:
                                if set(frame) != {"file", "line", "function"} or frame["file"] not in FRAME_FILES | {"[external source omitted]"} or frame["function"] not in FRAME_FUNCTIONS | {"[function omitted]"} or type(frame["line"]) is not int:
                                    raise ValueError("Unexpected frame data")
                            if "batch_index" in event and (type(event["batch_index"]) is not int or not 0 <= event["batch_index"] < 100):
                                raise ValueError("Invalid batch index")
                            if "execution_state" in event and event["execution_state"] not in {"not_started", "in_flight", "response_received"}:
                                raise ValueError("Invalid execution state")
                        except (ValueError, TypeError, KeyError, AttributeError):
                            invalid_lines += 1
                            continue
                        if (tool is None or event.get("tool") == tool) and (event_id is None or event.get("event_id") == event_id):
                            events.append(event)
        return {**self.status(), "events": list(reversed(events[-limit:])), "invalid_lines": invalid_lines}
