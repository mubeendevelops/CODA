"""Structured JSON logging with trace_id propagation from the envelope.

architecture.md §8: "Logs: structured JSON, trace_id on every line, no PII".
`trace_id` is threaded through a contextvar rather than passed to every log
call by hand, so a handler's ordinary `logger.info(...)` calls automatically
carry the trace of the message currently being processed — the same
one-consultation-one-trace property the Go side gets from putting trace_id
on every envelope, result, and audit_log row.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager

_trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("coda_trace_id", default="")


def current_trace_id() -> str:
    return _trace_id_var.get()


@contextmanager
def trace_context(trace_id: str) -> Iterator[None]:
    """Binds trace_id for the duration of the block — wrap one message's
    processing in this so every log line emitted while handling it, however
    deep the call stack, carries the same trace_id.
    """
    token = _trace_id_var.set(trace_id or "")
    try:
        yield
    finally:
        _trace_id_var.reset(token)


class _JSONFormatter(logging.Formatter):
    def __init__(self, service_name: str) -> None:
        super().__init__()
        self._service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "service": self._service_name,
            "level": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
        }
        trace_id = current_trace_id()
        if trace_id:
            payload["trace_id"] = trace_id
        for key, value in getattr(record, "extra_fields", {}).items():
            payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(service_name: str, level: str = "INFO") -> logging.Logger:
    """Installs the JSON formatter on the root handler and returns a logger
    named after the service, the same pattern asr-service/nlp-service's
    server.py already used pre-SDK.
    """
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(_JSONFormatter(service_name))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    return logging.getLogger(service_name)


def log_extra(**fields: object) -> dict[str, dict[str, object]]:
    """Usage: logger.info("message", extra=log_extra(job_id=..., stage=...))"""
    return {"extra_fields": fields}


__all__ = ["configure_logging", "current_trace_id", "trace_context", "log_extra"]
