"""One server-generated correlation ID across API envelopes, headers and model traces."""
from __future__ import annotations

from contextvars import ContextVar
import uuid

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)


def current_request_id() -> str:
    return _request_id.get() or uuid.uuid4().hex


class RequestIdMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        identifier = uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = identifier
        token = _request_id.set(identifier)

        async def send_with_id(message):
            if message["type"] == "http.response.start":
                message = dict(message)
                headers = [(key, value) for key, value in message.get("headers", [])
                           if key.lower() != b"x-request-id"]
                message["headers"] = [*headers, (b"x-request-id", identifier.encode("ascii"))]
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            _request_id.reset(token)
