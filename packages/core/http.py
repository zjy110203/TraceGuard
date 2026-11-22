"""Request-id middleware shared by both services."""

from __future__ import annotations

import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from packages.core.logging import request_id_var


class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        token = request_id_var.set(request.headers.get("X-Request-ID", uuid.uuid4().hex[:12]))
        try:
            return await call_next(request)
        finally:
            request_id_var.reset(token)
