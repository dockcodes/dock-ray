import json
import platform
from datetime import datetime, timezone
from .models import Event

class PayloadSerializer:

    @staticmethod
    def serialize(event: Event, request: dict | None = None,
                  user: dict | None = None,
                  http_status_code: int | None = None) -> str:
        now_iso = datetime.now(timezone.utc).isoformat()

        payload = {
            "event_id": event.event_id,
            "timestamp": event.timestamp,
            "sdk": {
                "name": "dock-thor-client",
                "version": "1.1.0"
            },
            "environment": event.environment,
            "platform": event.platform,
            "server_name": event.server_name,
            "message": event.message,
            "extra": event.extra,
            "tags": event.tags,
            "user": user or {},
            "last_issued_at": now_iso,
            "status": "Pending",
            "contexts": {
                "os": {
                    "name": platform.system(),
                    "version": platform.version(),
                    "build": platform.release(),
                    "kernel_version": platform.platform()
                },
                "runtime": [platform.python_implementation(), platform.python_version()]
            }
        }

        if event.exception:
            payload["exception"] = {
                "values": [{
                    "type": event.exception.get("type"),
                    "value": event.exception.get("value"),
                    "stacktrace": event.exception.get("stacktrace", {}),
                }]
            }

        if request:
            payload["request"] = {
                "url": request.get("url", ""),
                "method": request.get("method", ""),
                "headers": request.get("headers", {})
            }

        if event.transaction:
            payload["transaction"] = event.transaction
            payload["sent_at"] = now_iso
            payload.setdefault("contexts", {}).setdefault("trace", {})["data"] = {}

            if event.spans:
                payload["spans"] = []
                for span in event.spans:
                    span_dict = {
                        "span_id": span.span_id,
                        "trace_id": span.trace_id,
                        "start_timestamp": span.start_timestamp,
                    }
                    if span.parent_span_id:
                        span_dict["parent_span_id"] = span.parent_span_id
                    if span.end_timestamp:
                        span_dict["timestamp"] = span.end_timestamp
                    if span.status:
                        span_dict["status"] = span.status
                        payload["tags"]["http.status_code"] = span.status
                    if span.description:
                        span_dict["description"] = span.description
                    if span.op:
                        span_dict["op"] = span.op
                    if span.data:
                        span_dict["data"] = span.data
                        payload["contexts"]["trace"]["data"]["url"] = span.data.get("path", "")
                        payload["contexts"]["trace"]["data"]["method"] = span.data.get("method", "")
                    if span.tags:
                        span_dict["tags"] = span.tags
                        payload["user"]["ip_address"] = span.tags.get("client_host", "")
                        payload["user"]["agent"] = span.tags.get("user_agent", "")
                    payload["spans"].append(span_dict)

        return json.dumps(payload, ensure_ascii=False)

    @staticmethod
    def _serialize_stacktrace(trace_str: str):
        if not trace_str:
            return []
        lines = trace_str.strip().splitlines()
        frames = []
        for line in lines:
            frames.append({
                "filename": line.strip(),
                "in_app": True,
            })
        return frames

    @staticmethod
    def _serialize_span(span):
        result = {
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