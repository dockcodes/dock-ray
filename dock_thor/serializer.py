from __future__ import annotations

import json
import platform
from typing import Any

from .models import Event

SDK_NAME = "dock-thor-client"


class PayloadSerializer:
    """Turns an :class:`Event` into the JSON body the ingest endpoint expects.

    The panel reads a fixed set of keys — ``exception.values[]``,
    ``contexts.os``, ``contexts.runtime``, ``request``, ``user`` and, for
    transactions, ``contexts.trace.data`` plus ``tags["http.status_code"]``.
    Nothing outside that set is worth sending.
    """

    def __init__(self, sdk_version: str) -> None:
        self.sdk_version = sdk_version

    def serialize(self, event: Event) -> str:
        return json.dumps(self.to_dict(event), ensure_ascii=False)

    def to_dict(self, event: Event) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "event_id": event.event_id,
            "timestamp": event.timestamp,
            "platform": event.platform,
            "level": event.level,
            "message": event.message,
            "server_name": event.server_name,
            "environment": event.environment,
            "sdk": {"name": SDK_NAME, "version": self.sdk_version},
            "extra": event.extra,
            "tags": dict(event.tags),
            "contexts": _contexts(),
        }

        if event.user:
            payload["user"] = event.user

        if event.request:
            payload["request"] = {
                "url": event.request.get("url", ""),
                "method": event.request.get("method", ""),
                "headers": event.request.get("headers", {}),
            }

        if event.exception:
            payload["exception"] = {"values": [event.exception]}

        if event.transaction:
            _add_transaction(payload, event)

        return payload


def _contexts() -> dict[str, Any]:
    return {
        "os": {
            "name": platform.system(),
            "version": platform.version(),
            "build": platform.release(),
            "kernel_version": platform.platform(),
        },
        "runtime": [platform.python_implementation(), platform.python_version()],
    }


def _add_transaction(payload: dict[str, Any], event: Event) -> None:
    """Fills in what the transaction endpoint refuses to store an event without.

    ``contexts.trace.data`` must carry the URL and the method, and the response
    status must be a tag. Both come from the root span — the one describing the
    request itself.
    """
    payload["transaction"] = event.transaction
    payload["sent_at"] = event.timestamp

    trace: dict[str, Any] = {"data": {"url": "", "method": ""}}
    spans = event.spans or []

    for span in spans:
        if span.parent_span_id is None:
            trace["trace_id"] = span.trace_id
            trace["span_id"] = span.span_id

            if span.data:
                trace["data"]["url"] = span.data.get("url", span.data.get("path", ""))
                trace["data"]["method"] = span.data.get("method", "")

            if span.status:
                payload["tags"]["http.status_code"] = span.status

            break

    payload["contexts"]["trace"] = trace
    payload["spans"] = [_span(span) for span in spans]


def _span(span) -> dict[str, Any]:
    result: dict[str, Any] = {
        "span_id": span.span_id,
        "trace_id": span.trace_id,
        "start_timestamp": span.start_timestamp,
    }

    if span.parent_span_id:
        result["parent_span_id"] = span.parent_span_id
    if span.end_timestamp:
        result["timestamp"] = span.end_timestamp
    if span.status:
        result["status"] = span.status
    if span.description:
        result["description"] = span.description
    if span.op:
        result["op"] = span.op
    if span.data:
        result["data"] = span.data
    if span.tags:
        result["tags"] = span.tags

    return result
