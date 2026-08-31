"""Bounded structured runtime events for persistent journal ingestion."""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any, Mapping


EVENT_SCHEMA_VERSION = 1
MAX_EVENT_BYTES = 16 * 1024
MAX_STRING_LENGTH = 512
MAX_COLLECTION_ITEMS = 32
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_FIELD_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,63}$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_SENSITIVE_KEY = re.compile(
    r"(?:authorization|cookie|secret|token|api[_-]?key|password|credential|"
    r"connection[_-]?string|prompt|model[_-]?(?:response|output)|upstream[_-]?body|"
    r"response[_-]?body|readme)",
    re.IGNORECASE,
)
_BEARER = re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]+")
_ASSIGNMENT = re.compile(
    r"(?i)(?:[\"']?)(?:token|secret|api[_-]?key|password|authorization|cookie|credential)"
    r"(?:[\"']?)\s*[:=]\s*(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;]+)"
)
_DATABASE_URL = re.compile(r"(?i)\b(?:postgres(?:ql)?|mysql|mongodb|redis)://[^\s]+")
_WINDOWS_ABSOLUTE = re.compile(r"(?i)(?<![A-Za-z0-9])(?:[A-Z]:\\|\\\\)[^\s\"']+")
_POSIX_ABSOLUTE = re.compile(r"(?<![:A-Za-z0-9])/(?:opt|var|etc|home|root|srv|tmp)/[^\s\"']+")
_WRITE_LOCK = threading.Lock()
_PROCESS_RUN_ID = str(uuid.uuid4())


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _release_sha() -> str:
    configured = os.environ.get("RARDAR_RELEASE_SHA", "")
    if _SHA.fullmatch(configured):
        return configured
    candidates: list[Path] = []
    home = os.environ.get("RARDAR_HOME")
    if home:
        candidates.append(Path(home) / "release-manifest.json")
    candidates.append(Path(__file__).resolve().parents[1] / "release-manifest.json")
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        commit = payload.get("commitSha") if isinstance(payload, dict) else None
        if isinstance(commit, str) and _SHA.fullmatch(commit):
            return commit
    return "unknown"


def new_run_id() -> str:
    return str(uuid.uuid4())


def process_run_id() -> str:
    return _PROCESS_RUN_ID


def _clean_string(value: str) -> str:
    cleaned = value.replace("\r", " ").replace("\n", " ").replace("\x00", " ")
    cleaned = _BEARER.sub("[REDACTED]", cleaned)
    cleaned = _ASSIGNMENT.sub("[REDACTED]", cleaned)
    cleaned = _DATABASE_URL.sub("[REDACTED]", cleaned)
    cleaned = _WINDOWS_ABSOLUTE.sub("[REDACTED_PATH]", cleaned)
    cleaned = _POSIX_ABSOLUTE.sub("[REDACTED_PATH]", cleaned)
    return cleaned[:MAX_STRING_LENGTH]


def _safe_value(value: object, *, key: str = "", depth: int = 0) -> object:
    if _SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _clean_string(value)
    if depth >= 3:
        return "[TRUNCATED]"
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for raw_key in sorted(value, key=lambda item: str(item))[:MAX_COLLECTION_ITEMS]:
            normalized = _clean_string(str(raw_key))[:64]
            result[normalized] = _safe_value(value[raw_key], key=normalized, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple, set)):
        return [
            _safe_value(item, key=key, depth=depth + 1)
            for item in list(value)[:MAX_COLLECTION_ITEMS]
        ]
    return _clean_string(str(value))


class StructuredLogger:
    """Emit one bounded JSON object per operational event."""

    def __init__(self, service: str, *, stream: IO[str] | None = None) -> None:
        if _NAME.fullmatch(service) is None:
            raise ValueError("service must use lowercase snake_case")
        self.service = service
        self.stream = stream

    def emit(
        self,
        event: str,
        *,
        level: str = "info",
        state: str,
        run_id: str | None = None,
        **fields: object,
    ) -> dict[str, object]:
        if _NAME.fullmatch(event) is None:
            raise ValueError("event must use lowercase snake_case")
        if level not in {"debug", "info", "warning", "error", "critical"}:
            raise ValueError("unsupported structured log level")
        payload: dict[str, object] = {
            "timestamp": _utc_now(),
            "level": level,
            "service": self.service,
            "event": event,
            "eventSchemaVersion": EVENT_SCHEMA_VERSION,
            "processId": os.getpid(),
            "releaseSha": _release_sha(),
            "runId": run_id or _PROCESS_RUN_ID,
            "state": _clean_string(state),
        }
        for key, value in fields.items():
            if not isinstance(key, str) or _FIELD_NAME.fullmatch(key) is None:
                continue
            payload[key] = _safe_value(value, key=key)
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_EVENT_BYTES:
            payload = {
                **{key: payload[key] for key in tuple(payload)[:9]},
                "truncated": True,
            }
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        destination = self.stream or (sys.stderr if level in {"error", "critical"} else sys.stdout)
        with _WRITE_LOCK:
            destination.write(encoded + "\n")
            destination.flush()
        return payload


def emit_runtime_event(
    service: str,
    event: str,
    *,
    state: str,
    level: str = "info",
    run_id: str | None = None,
    **fields: object,
) -> dict[str, object]:
    return StructuredLogger(service).emit(
        event,
        state=state,
        level=level,
        run_id=run_id,
        **fields,
    )


__all__ = [
    "EVENT_SCHEMA_VERSION",
    "MAX_EVENT_BYTES",
    "MAX_STRING_LENGTH",
    "StructuredLogger",
    "emit_runtime_event",
    "new_run_id",
    "process_run_id",
]
