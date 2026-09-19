"""Local traces and error logs.

LangSmith can wait. Every LLM, tool, and graph span is appended as JSONL under
``logs/traces/``, and Python exceptions go to ``logs/errors.log`` so a failed
run is still inspectable after the process dies.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from langchain_core.tracers.base import BaseTracer
from langchain_core.tracers.schemas import Run

from .config import Settings, get_settings

log = logging.getLogger("control_machine")

_MAX_STRING = 8_000
_MAX_LIST = 40
_MAX_DEPTH = 12
_IMAGE_KEYS = frozenset({"image_url", "image", "image_b64", "b64_json"})
_SECRET_KEYS = frozenset(
    {"api_key", "authorization", "password", "secret", "token", "cookie"}
)
_SECRET_SUFFIXES = ("_token", "_secret", "_password", "_api_key")
_CONFIGURED = False


def configure_logging(settings: Settings | None = None) -> Path:
    """Install stderr + rotating file handlers. Safe to call more than once."""
    global _CONFIGURED
    settings = settings or get_settings()
    log_dir = settings.log_path
    traces = settings.traces_dir
    if _CONFIGURED:
        return log_dir

    level_name = (settings.log_level or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")

    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "control-machine.log",
        maxBytes=10_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)

    error_handler = logging.handlers.RotatingFileHandler(
        log_dir / "errors.log",
        maxBytes=5_000_000,
        backupCount=10,
        encoding="utf-8",
    )
    error_handler.setFormatter(formatter)
    error_handler.setLevel(logging.ERROR)

    stderr = logging.StreamHandler()
    stderr.setFormatter(formatter)
    stderr.setLevel(level)

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    root.addHandler(file_handler)
    root.addHandler(error_handler)
    root.addHandler(stderr)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("telegram.ext").setLevel(logging.INFO)

    _CONFIGURED = True
    log.info("Logging to %s (traces in %s)", log_dir, traces)
    return log_dir


def task_tracer(settings: Settings, *, task_id: int, thread_id: str) -> JsonlTracer:
    path = settings.traces_dir / f"task-{task_id}.jsonl"
    return JsonlTracer(path, task_id=task_id, thread_id=thread_id)


class JsonlTracer(BaseTracer):
    """Write one JSON object per LangGraph/LLM/tool span, flushed immediately."""

    name = "control_machine_jsonl"

    def __init__(
        self,
        path: Path,
        *,
        task_id: int,
        thread_id: str,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._path = path
        self._task_id = task_id
        self._thread_id = thread_id
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._write(
            {
                "event": "task_start",
                "task_id": task_id,
                "thread_id": thread_id,
                "ts": _now(),
            }
        )

    def _persist_run(self, run: Run) -> None:
        # Incremental start/end events already cover the root run.
        _ = run

    def _on_run_create(self, run: Run) -> None:
        self._emit("start", run)

    def _on_run_update(self, run: Run) -> None:
        failed = bool(getattr(run, "error", None))
        self._emit("error" if failed else "end", run)
        if failed:
            log.error(
                "task %s %s/%s failed: %s",
                self._task_id,
                getattr(run, "run_type", "?"),
                getattr(run, "name", "?"),
                run.error,
            )

    def _emit(self, event: str, run: Run) -> None:
        record: dict[str, Any] = {
            "event": event,
            "ts": _now(),
            "task_id": self._task_id,
            "thread_id": self._thread_id,
            "run_id": _as_str(getattr(run, "id", None)),
            "parent_run_id": _as_str(getattr(run, "parent_run_id", None)),
            "run_type": getattr(run, "run_type", None),
            "name": getattr(run, "name", None),
            "tags": list(getattr(run, "tags", None) or []),
            "elapsed_ms": _elapsed_ms(run),
        }
        if event != "end":
            record["inputs"] = sanitize(getattr(run, "inputs", None))
        if event != "start":
            record["outputs"] = sanitize(getattr(run, "outputs", None))
            error = getattr(run, "error", None)
            if error:
                record["error"] = sanitize(error)
        self._write(record)

    def _write(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False, default=str)
        try:
            with self._lock:
                with self._path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
                    handle.flush()
        except OSError:
            log.exception("Could not write trace event to %s", self._path)


def sanitize(value: Any, *, depth: int = 0) -> Any:
    """Drop screenshots and secrets; cap huge ARIA snapshots so traces stay greppable."""
    if depth > _MAX_DEPTH:
        return "[truncated]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bytes):
        return f"[bytes {len(value)}]"
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str):
        if value.startswith("data:image"):
            return "[image omitted]"
        if len(value) > _MAX_STRING:
            return value[:_MAX_STRING] + f"... [{len(value) - _MAX_STRING} more chars]"
        return value
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            lowered = name.lower()
            if _is_secret_key(name):
                out[name] = "[redacted]"
            elif lowered in _IMAGE_KEYS:
                out[name] = "[image omitted]"
            else:
                out[name] = sanitize(item, depth=depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        items = [sanitize(item, depth=depth + 1) for item in list(value)[:_MAX_LIST]]
        extra = len(value) - _MAX_LIST
        if extra > 0:
            items.append(f"... [{extra} more]")
        return items
    dumped = _try_dump(value)
    if dumped is not None and dumped is not value:
        return sanitize(dumped, depth=depth + 1)
    return sanitize(str(value), depth=depth + 1)


def _is_secret_key(name: str) -> bool:
    lowered = name.lower()
    return lowered in _SECRET_KEYS or lowered.endswith(_SECRET_SUFFIXES)


def _try_dump(value: Any) -> Any | None:
    for method in ("model_dump", "dict"):
        fn = getattr(value, method, None)
        if callable(fn):
            try:
                return fn()
            except Exception:  # noqa: BLE001 - fall through to str()
                continue
    return None


def _elapsed_ms(run: Any) -> int | None:
    start = getattr(run, "start_time", None)
    end = getattr(run, "end_time", None)
    if start is None or end is None:
        return None
    try:
        return int((end - start).total_seconds() * 1000)
    except Exception:  # noqa: BLE001
        return None


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
