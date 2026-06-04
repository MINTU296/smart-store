"""Structured JSON logging.

Every API request emits one log line with:
  trace_id, store_id, endpoint, method, latency_ms, event_count, status_code
"""
from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from typing import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:  # noqa: D401
        payload: dict = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in (
            "trace_id",
            "store_id",
            "endpoint",
            "method",
            "latency_ms",
            "event_count",
            "status_code",
        ):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.handlers.clear()
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(JsonFormatter())
    root.addHandler(h)
    root.setLevel(level.upper())
    # uvicorn access logs are noisy; we emit our own
    logging.getLogger("uvicorn.access").disabled = True


class RequestLogMiddleware(BaseHTTPMiddleware):
    """Attach trace_id, time the request, emit one structured log line."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        trace_id = request.headers.get("x-trace-id") or uuid.uuid4().hex[:12]
        request.state.trace_id = trace_id
        start = time.perf_counter()
        store_id = request.path_params.get("store_id") or request.path_params.get("id") or "-"
        try:
            response = await call_next(request)
            status_code = response.status_code
        except Exception:
            latency_ms = int((time.perf_counter() - start) * 1000)
            logging.getLogger("api").exception(
                "request.failed",
                extra={
                    "trace_id": trace_id,
                    "store_id": store_id,
                    "endpoint": request.url.path,
                    "method": request.method,
                    "latency_ms": latency_ms,
                    "status_code": 500,
                },
            )
            raise
        latency_ms = int((time.perf_counter() - start) * 1000)
        event_count = getattr(request.state, "event_count", None)
        logging.getLogger("api").info(
            "request",
            extra={
                "trace_id": trace_id,
                "store_id": store_id,
                "endpoint": request.url.path,
                "method": request.method,
                "latency_ms": latency_ms,
                "event_count": event_count,
                "status_code": status_code,
            },
        )
        response.headers["x-trace-id"] = trace_id
        return response
