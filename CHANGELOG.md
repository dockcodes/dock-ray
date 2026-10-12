# Changelog

## 1.5.0

- Add opt-in production dependency inventory, manual reports and hourly asyncio scheduling.
- Add dockray-inventory CLI for cron and short-lived applications.
- Use bounded installed metadata, production manifests, markers, extras and canonical PyPI names.
- Reuse project/environment credentials without a site URL; add verified HTTPS, private state, OS locks and backoff.
- Start inventory in ASGI lifespan and stop/wait on client close.
- Inventory supports POSIX; unsupported collection fails without clearing existing findings.

## 1.4.2

- Previous error and performance monitoring release.
