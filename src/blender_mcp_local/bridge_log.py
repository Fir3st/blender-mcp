"""Private, rotating operational log for the MCP bridge.

This is intentionally separate from the sanitized error journal. It records
lifecycle and tool events, but never raw tool arguments, custom Python code, or
MCP stdio payloads.
"""

from __future__ import annotations

import json
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any


MAX_BYTES = 5 * 1024 * 1024
BACKUPS = 2
_SENSITIVE_NAME_PARTS = (
    "secret", "token", "credential", "cert", "crt", "csr", "keystore",
    "truststore", "vault", "private", "pass", "enc",
)


def default_bridge_log_path() -> Path:
    """Return the operational log next to the error journal."""
    from .error_log import default_log_path

    return default_log_path().with_name("bridge.log")


def _validate_path(path: Path) -> None:
    name = path.name.lower()
    if path.suffix != ".log" or name.startswith(".env") or any(
        word in name for word in _SENSITIVE_NAME_PARTS
    ):
        raise ValueError("Bridge log requires a non-sensitive .log filename")


class _SecureRotatingFileHandler(RotatingFileHandler):
    """Rotating handler that creates private files and refuses symlinks."""

    def _open(self):
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.baseFilename, flags, 0o600)
        return os.fdopen(descriptor, self.mode, encoding=self.encoding, errors=self.errors)


class BridgeLog:
    """Small logging facade with bounded, private, thread-safe output."""

    def __init__(self, path=None, *, max_bytes: int = MAX_BYTES, backups: int = BACKUPS):
        self.path = Path(path).absolute() if path is not None else None
        self.max_bytes = max_bytes
        self.backups = backups
        self.last_write_error = None
        self._handler = None
        self._logger = logging.getLogger(f"blender_mcp_local.bridge.{id(self)}")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        if self.path is not None:
            self._open()

    def _open(self) -> None:
        _validate_path(self.path)
        handler = None
        try:
            if self.path.parent.is_symlink() or self.path.is_symlink():
                raise OSError("Bridge log symlinks are not supported")
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            handler = _SecureRotatingFileHandler(
                self.path,
                maxBytes=self.max_bytes,
                backupCount=self.backups,
                encoding="utf-8",
                delay=False,
            )
            handler.setFormatter(logging.Formatter(
                "%(asctime)s %(levelname)s %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S%z",
            ))
            os.chmod(self.path, 0o600)
            self._logger.addHandler(handler)
            self._handler = handler
            self.last_write_error = None
        except OSError as error:
            # A diagnostic log must never prevent the bridge from starting.
            if handler is not None:
                handler.close()
            self.last_write_error = type(error).__name__

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.path is not None,
            "path": str(self.path) if self.path is not None else None,
            "max_bytes": self.max_bytes,
            "backups": self.backups,
            "last_write_error": self.last_write_error,
        }

    def _write(self, level: int, event: str, fields: dict[str, Any]) -> None:
        if self._handler is None:
            return
        parts = []
        for key, value in fields.items():
            if value is None:
                continue
            try:
                rendered = json.dumps(value, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                rendered = json.dumps(str(value), ensure_ascii=False)
            if len(rendered) > 1000:
                rendered = rendered[:997] + "..."
            parts.append(f"{key}={rendered}")
        suffix = " " + " ".join(parts) if parts else ""
        try:
            self._logger.info("%s%s", event, suffix) if level == logging.INFO else self._logger.log(
                level, "%s%s", event, suffix
            )
        except Exception as error:
            # Logging failures must not replace the operation's result.
            self.last_write_error = type(error).__name__

    def info(self, event: str, **fields: Any) -> None:
        self._write(logging.INFO, event, fields)

    def warning(self, event: str, **fields: Any) -> None:
        self._write(logging.WARNING, event, fields)

    def error(self, event: str, **fields: Any) -> None:
        self._write(logging.ERROR, event, fields)

    def close(self) -> None:
        if self._handler is None:
            return
        self._logger.removeHandler(self._handler)
        self._handler.close()
        self._handler = None
