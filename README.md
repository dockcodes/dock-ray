# dock-ray

Python client for [DockRay](https://dockray.io). Reports exceptions, messages
and HTTP transactions from asyncio applications.

## Installation

```bash
pip install dock-ray
```

## Usage

Every reporting method is a coroutine, so calls are awaited:

```python
from dock_ray import DockRayClient

client = DockRayClient(
    token="project-token",
    private_key="project-private-key",
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
from dock_ray import DockRayClient, DockRayFastAPIMiddleware

client = DockRayClient(token="...", private_key="...")
app = FastAPI()

app.add_middleware(
    DockRayFastAPIMiddleware,
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

## Package inventory and vulnerability alerts

Version 1.5 adds opt-in reporting of installed production Python packages.
Upgrade the DockRay API with Python inventory support before enabling it.
The inventory uses the same project token, private key and environment as errors;
no inventory site URL is configured or sent.

```python
import os
from dock_ray import DockRayClient

client = DockRayClient(
    token=os.environ["RAY_TOKEN"],
    private_key=os.environ["RAY_PRIVATE_KEY"],
    environment="production",
    inventory_enabled=True,
    inventory_exclude_packages=["company-*"],
    # Optional: directory with the deployed production manifest.
    # inventory_project_directory="/srv/my-app",
    # Optional: private persistent state, outside public assets.
    # inventory_state_directory="/var/lib/my-app/dockray-inventory",
    # Optional: override manifest selection.
    # inventory_requirements_file="requirements/production.txt",
    # Optional: production feature groups from project.optional-dependencies.
    # inventory_optional_groups=["postgres"],
)
```

Or set `RAY_INVENTORY=true` and `RAY_ENVIRONMENT=production` alongside the
existing credentials. Explicit `inventory_enabled=False` overrides the flag.
The client also reads `RAY_INVENTORY_PROJECT_DIR`, `RAY_INVENTORY_STATE_DIR`,
`RAY_INVENTORY_REQUIREMENTS_FILE`, comma-separated
`RAY_INVENTORY_EXCLUDE_PACKAGES` and `RAY_INVENTORY_OPTIONAL_GROUPS` when the
corresponding option is unset. Environment defaults to RAY_ENVIRONMENT or
production. prod/production map to prod; dev/development/staging/preview/test/local
map to dev. Unknown values fail without sending. Development requires a
configured development environment in the DockRay project.

### Scheduling

When created in a running asyncio loop, the client starts inventory once in
the background and then checks hourly. When created before the loop starts,
call `client.start_inventory()` in application startup. The ASGI middleware
also calls it once during lifespan startup; it never collects inside HTTP
request handling. A FastAPI lifespan is a clear place to start and stop it:

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from dock_ray import DockRayFastAPIMiddleware

@asynccontextmanager
async def lifespan(app):
    client.start_inventory()
    try:
        yield
    finally:
        await client.close()

app = FastAPI(lifespan=lifespan)
app.add_middleware(DockRayFastAPIMiddleware, client=client)
```

Changed inventory is sent at the next check; unchanged inventory is refreshed
approximately every 12 hours. `await client.report_inventory(force=True)`
requests a deployment/manual report. `await client.close()` stops the schedule,
waits for pending inventory and event sends, and closes transport resources.
Manual force never bypasses retry backoff or backend rate limits.

For short-lived processes, serverless or a separate scheduler, keep the
client's automatic inventory disabled and run the installed command hourly
using the same Python interpreter/virtual environment as the application:

```dotenv
RAY_TOKEN=project-token
RAY_PRIVATE_KEY=project-private-key
RAY_INVENTORY=true
RAY_ENVIRONMENT=production
RAY_INVENTORY_PROJECT_DIR=/srv/my-app
RAY_INVENTORY_STATE_DIR=/var/lib/my-app/dockray-inventory
```

```sh
dockray-inventory --force  # first/deployment report
dockray-inventory          # schedule hourly in cron
# Equivalent: python -m dock_ray.inventory_cli
```

The command does not read .env automatically and never accepts credentials in
command arguments. Provide them securely through the process environment.
Run one schedule per application or share private state on the same machine;
separate machines need scheduler coordination. A standalone InventoryReporter
supports `await reporter.report()`, `reporter.start()` and `await reporter.stop()`.

### Collection boundaries and privacy

The collector reads bounded METADATA files from installed `.dist-info`
distributions in this interpreter's site-packages. It does not import package
code, execute setup.py or pip, or read application source, .env, user data,
repository URLs or direct_url.json contents. Only canonical package names and
PEP 440 versions, the normalized environment and Python/SDK versions are sent.
DockRay matches these reports against its existing PyPI advisory sources,
displays findings in the Software tab and uses existing project notifications.

Production roots come from static `[project].dependencies` in pyproject.toml,
plus explicitly selected optional groups. Otherwise use requirements.txt, or
set inventory_requirements_file explicitly. Dependencies are followed from
installed Requires-Dist metadata with version checks, markers and extras.
Development groups, unrelated installed packages and the application itself
are excluded. Excluded/private/direct-URL packages are omitted from the report,
but their public dependencies are still followed. Use name/glob exclusions for
private packages installed from an index: their names alone do not reveal
whether a distribution is public. An isolated production virtual environment
is recommended. Requirements containing development dependencies will report
them too; choose an actual production manifest.

Requirements files support plain PEP 508 requirements, relative `-r` includes
inside the project, continuation lines and SHA-256 hashes. Index/trusted-host
options are ignored and never used for network access. Editable requirements,
constraints, dynamic setup.py/Poetry dependencies, legacy egg metadata and
standalone/bundled environments without installed metadata are unsupported.
Select a static production requirements file for these deployments. Missing,
ambiguous, mismatched or malformed metadata fails without sending a partial or
empty snapshot. Explicitly declaring no production dependencies can send an
empty snapshot. Unsupported collected data never silently clears existing findings.

Delivery requires verified HTTPS, uses a Bearer header and refuses redirects.
It has an eight-second total HTTP deadline, payload/response bounds, generic
failure logs and Retry-After/backoff handling. Inventory currently supports
**Linux and macOS/POSIX**; Windows returns invalid_configuration without sending.
Cross-process OS file locks are released on exit/crash. State directories must
be private (0700); files are created as 0600 and contain only hashes/timings.
Use persistent private state for containers; do not put it under public assets.

## Manual transactions

Useful for background jobs:

```python
import time, uuid
from dock_ray import Span

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
rejected events and malformed responses are logged to the `dock_ray` logger
and reported as `False`. A `200` response carrying `{"success": false}` means
the account reached its monthly error quota.

## Tests

```sh
pip install -e '.[dev]'
pytest tests
```

## Contributing

Issues and pull requests: <https://github.com/dockcodes/dock-ray/issues>
