# dock-thor-client

Python client for [DockTHOR](https://dock.codes). Reports exceptions, messages
and HTTP transactions from asyncio applications.

## Installation

```bash
pip install dock-thor-client
```

## Usage

Every reporting method is a coroutine, so calls are awaited:

```python
from dock_thor import DockThorClient

client = DockThorClient(
    token="project-token",
    private_key="project-private-key",
    url="https://thor.dock.codes",
    environment="production",
)

try:
    settle(order)
except Exception as error:
    await client.capture_exception(error)

await client.capture_message("Nightly import finished late", level="warning")
```

Constructed without credentials the client is inert: `client.enabled` is
`False` and every call returns `False` without touching the network. Wire it up
unconditionally and leave the credentials out of development.

To report without waiting for the request, hand the coroutine to
`send_later()`. It keeps a reference to the task until it finishes — a task
held only by the event loop can be collected mid-flight, which drops the event
silently — and logs failures instead of raising them somewhere unrelated:

```python
client.send_later(client.capture_exception(error))
```

Call `await client.close()` on shutdown; it flushes pending sends before
closing the connection pool.

## When events are sent

Never during the part of the request the visitor is waiting on. `send_later()`
schedules the send without awaiting it, and the middleware reports a
transaction only after the application has finished writing the response. A
slow or unreachable panel costs the visitor nothing.

## ASGI and FastAPI

The middleware opens one transaction per request and reports unhandled
exceptions. It is plain ASGI, so it works under FastAPI, Starlette and anything
else that speaks the protocol, and it does not buffer streaming responses.

```python
from fastapi import FastAPI
from dock_thor import DockThorClient, DockThorFastAPIMiddleware

client = DockThorClient(token="...", private_key="...")
app = FastAPI()

app.add_middleware(
    DockThorFastAPIMiddleware,
    client=client,
    exclude_paths=["/health", "/metrics"],
)

@app.on_event("shutdown")
async def shutdown() -> None:
    await client.close()
```

Each request is reported with its method, path, duration and response status.
`HTTPException` is handled by FastAPI itself and never reaches the middleware,
so expected `404` and `422` responses do not turn into errors — they are
recorded as transactions with their status.

The client IP is read from `X-Forwarded-For` or `X-Real-IP` before falling back
to the socket address, and `Authorization`, `Cookie` and `X-Api-Key` are
stripped from the reported headers.

## Manual transactions

Useful for background jobs:

```python
import time, uuid
from dock_thor import Span

started = time.time()
await run_daily_cleanup()
ended = time.time()

span = Span(
    span_id=uuid.uuid4().hex[:16],
    trace_id=uuid.uuid4().hex,
    start_timestamp=started,
    end_timestamp=ended,
    status="200",
    description="Daily cleanup",
    op="worker.task",
    data={"url": "job:daily_cleanup", "method": "CLI"},
)

await client.capture_transaction(name="job:daily_cleanup", spans=[span])
```

The root span — the one without a `parent_span_id` — carries the URL, the
method and the status the panel indexes the transaction by.

## Failure behaviour

Nothing in this client raises into the application it monitors. Network errors,
rejected events and malformed responses are logged to the `dock_thor` logger
and reported as `False`. A `200` response carrying `{"success": false}` means
the account reached its monthly error quota.

## Contributing

Issues and pull requests: <https://github.com/dockcodes/dock-thor/issues>
