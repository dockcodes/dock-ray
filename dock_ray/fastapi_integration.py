from __future__ import annotations

import time
import uuid
from typing import Iterable, Optional

from .models import Span

EXCLUDED_BY_DEFAULT = ("/health", "/metrics")


class DockRayFastAPIMiddleware:
    """ASGI middleware: one transaction per request, plus unhandled exceptions.

    Written against the raw ASGI interface rather than Starlette's
    ``BaseHTTPMiddleware`` so it works under FastAPI, Starlette and any other
    ASGI application, and so streaming responses are not buffered.
    """

    def __init__(
        self,
        app,
        client,
        exclude_paths: Optional[Iterable[str]] = None,
        capture_transactions: bool = True,
    ) -> None:
        self.app = app
        self.client = client
        self.exclude_paths = tuple(
            exclude_paths if exclude_paths is not None else EXCLUDED_BY_DEFAULT
        )
        self.capture_transactions = capture_transactions

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not self.client.enabled:
            await self.app(scope, receive, send)
            return

        request = _request_data(scope)
        excluded = any(scope["path"].startswith(path) for path in self.exclude_paths)

        started_at = time.time()
        status = {"code": 500}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]

            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as error:
            self.client.send_later(
                self.client.capture_exception(error, user=request["user"], request=request["http"])
            )
            raise
        finally:
            if self.capture_transactions and not excluded:
                self._report(scope, request, started_at, status["code"])

    def _report(self, scope, request, started_at: float, status_code: int) -> None:
        ended_at = time.time()
        method = scope["method"]
        path = scope["path"]

        span = Span(
            span_id=uuid.uuid4().hex[:16],
            trace_id=uuid.uuid4().hex,
            start_timestamp=started_at,
            end_timestamp=ended_at,
            status=str(status_code),
            description=f"{method} {path}",
            op="http.server",
            data={
                "url": request["http"]["url"],
                "method": method,
                "duration_ms": round((ended_at - started_at) * 1000, 2),
            },
            tags={"http.status_code": status_code},
        )

        self.client.send_later(
            self.client.capture_transaction(
                name=f"{method} {path}",
                spans=[span],
                user=request["user"],
                request=request["http"],
            )
        )


def _request_data(scope) -> dict:
    """Splits the ASGI scope into what the panel stores as request and as user.

    The client address is taken from the proxy headers first: behind a load
    balancer ``scope["client"]`` is the balancer, not the visitor.
    """
    headers = {
        key.decode("latin-1"): value.decode("latin-1")
        for key, value in scope.get("headers", [])
    }

    forwarded = headers.get("x-forwarded-for")
    client_host = (
        forwarded.split(",")[0].strip()
        if forwarded
        else headers.get("x-real-ip")
        or (scope["client"][0] if scope.get("client") else None)
    )

    query = scope.get("query_string", b"").decode("latin-1")
    path = scope["path"]

    return {
        "http": {
            "url": f"{path}?{query}" if query else path,
            "method": scope["method"],
            "headers": {
                key: value
                for key, value in headers.items()
                if key not in ("authorization", "cookie", "x-api-key")
            },
        },
        "user": {
            "ip_address": client_host,
            "agent": headers.get("user-agent"),
        },
    }
