"""Structured logging. Standard library `logging` module. No `print` calls
anywhere in the codebase — this is enforced by ruff (T20)."""

from __future__ import annotations

import json
import logging
from typing import Any

from ai_oncall.settings import settings


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key.startswith("_") or key in {"args", "msg", "levelname", "levelno", "name", "pathname",
                                              "filename", "module", "exc_info", "exc_text", "stack_info",
                                              "lineno", "funcName", "created", "msecs", "relativeCreated",
                                              "thread", "threadName", "processName", "process",
                                              "getMessage", "taskName"}:
                continue
            payload[key] = value
        return json.dumps(payload, default=str)


def configure() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(_JsonFormatter() if settings.log_json else logging.Formatter(
        "%(asctime)s %(levelname)-5s %(name)s — %(message)s"
    ))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)

    # Surface unsafe-config warnings once at startup so misconfigurations
    # don't silently degrade. Late import to avoid a settings circular dep.
    from ai_oncall.settings import warn_unsafe_settings

    for warning in warn_unsafe_settings():
        logging.getLogger("ai_oncall.settings").warning(warning)
