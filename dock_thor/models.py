from __future__ import annotations

import os
import platform
import socket
import sys
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlsplit

DEFAULT_URL = "https://dockray.io"

API_PATH = "/api/v1"


@dataclass(frozen=True)
class AuthData:
    """Ingest address and the credentials of a single project.

    ``token`` is the public identifier that goes into the URL; ``private_key``
    is the project secret sent as a bearer token.
    """

    token: str
    private_key: str
    url: str = DEFAULT_URL

    def base_url(self) -> str:
        parts = urlsplit(self.url)

        if not parts.netloc:
            raise ValueError(f"{self.url!r} is not a valid DockTHOR server URL")

        origin = f"{parts.scheme or 'https'}://{parts.netloc}{parts.path.rstrip('/')}"

        return f"{origin}{API_PATH}/{self.token}"

    def project_url(self) -> str:
        return f"{self.base_url()}/project"

    def transaction_url(self) -> str:
        return f"{self.base_url()}/transaction"


@dataclass
class Span:
    span_id: str
    trace_id: str
    start_timestamp: float
    end_timestamp: Optional[float] = None
    parent_span_id: Optional[str] = None
    status: Optional[str] = None
    description: Optional[str] = None
    op: Optional[str] = None
    data: Optional[dict] = None
    tags: Optional[dict] = None


def _event_id() -> str:
    return os.urandom(16).hex()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _runtime_context() -> dict:
    return {
        "python_version": sys.version.split()[0],
        "cwd": os.getcwd(),
    }


def _default_tags() -> dict:
    return {
        "os": platform.system(),
        "release": platform.release(),
    }


@dataclass
class Event:
    event_id: str
    timestamp: str
    level: str
    message: str
    platform: str
    server_name: str
    environment: str
    extra: dict = field(default_factory=dict)
    tags: dict = field(default_factory=dict)
    exception: Optional[dict] = None
    transaction: Optional[str] = None
    spans: Optional[list[Span]] = None
    user: Optional[dict] = None
    request: Optional[dict] = None

    @classmethod
    def from_exception(
        cls,
        exc: BaseException,
        level: str = "error",
        environment: str = "production",
        user: Optional[dict] = None,
        request: Optional[dict] = None,
    ) -> "Event":
        event = cls._base(str(exc), level, environment, user, request)
        event.exception = {
            "type": type(exc).__name__,
            "value": str(exc),
            "stacktrace": {"frames": _frames(exc)},
        }

        return event

    @classmethod
    def from_message(
        cls,
        message: str,
        level: str = "info",
        environment: str = "production",
        user: Optional[dict] = None,
        request: Optional[dict] = None,
    ) -> "Event":
        return cls._base(message, level, environment, user, request)

    @classmethod
    def from_transaction(
        cls,
        name: str,
        spans: list[Span],
        environment: str = "production",
        user: Optional[dict] = None,
        request: Optional[dict] = None,
    ) -> "Event":
        event = cls._base(f"Transaction: {name}", "info", environment, user, request)
        event.transaction = name
        event.spans = spans
        event.tags = {}

        return event

    @classmethod
    def _base(
        cls,
        message: str,
        level: str,
        environment: str,
        user: Optional[dict],
        request: Optional[dict],
    ) -> "Event":
        return cls(
            event_id=_event_id(),
            timestamp=_now(),
            level=level,
            message=message,
            platform="python",
            server_name=socket.gethostname(),
            environment=environment,
            extra=_runtime_context(),
            tags=_default_tags(),
            user=user,
            request=request,
        )


def _frames(exc: BaseException) -> list[dict[str, Any]]:
    """Stack frames with source context, innermost call last.

    Reading the source is best effort: a frame from a zipped egg, a REPL or a
    file that has since changed still has to produce a usable frame.
    """
    frames = []

    for frame in traceback.extract_tb(exc.__traceback__):
        pre_context, context_line, post_context = _source(frame.filename, frame.lineno)

        data: dict[str, Any] = {
            "filename": os.path.basename(frame.filename),
            "abs_path": os.path.abspath(frame.filename),
            "lineno": frame.lineno,
            "function": frame.name or "<unknown>",
            "in_app": "site-packages" not in frame.filename,
        }

        if pre_context:
            data["pre_context"] = pre_context
        if context_line:
            data["context_line"] = context_line
        if post_context:
            data["post_context"] = post_context

        frames.append(data)

    return frames


def _source(filename: str, lineno: Optional[int]) -> tuple[list[str], str, list[str]]:
    if not lineno or lineno < 1:
        return [], "", []

    try:
        with open(filename, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return [], "", []

    if lineno > len(lines):
        return [], "", []

    start = max(0, lineno - 4)
    end = min(len(lines), lineno + 3)

    return lines[start:lineno - 1], lines[lineno - 1], lines[lineno:end]
