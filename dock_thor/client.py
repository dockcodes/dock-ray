from __future__ import annotations

import asyncio
import logging
from typing import Optional

from .models import DEFAULT_URL, AuthData, Event, Span
from .transport import HttpTransport

logger = logging.getLogger("dock_thor")

SDK_VERSION = "1.2.0"


class DockThorClient:
    """Sends errors, messages and transactions to a DockTHOR project.

    Without a token and a private key the client stays inert — every call is a
    no-op. That way an application can wire it up unconditionally and leave the
    credentials out of a development environment.
    """

    def __init__(
        self,
        token: str = "",
        private_key: str = "",
        environment: str = "production",
        url: str = DEFAULT_URL,
        timeout: float = 5.0,
        compress: bool = True,
    ) -> None:
        self.environment = environment
        self.transport: Optional[HttpTransport] = None
        self._background: set[asyncio.Task] = set()

        if token and private_key:
            self.transport = HttpTransport(
                AuthData(token=token, private_key=private_key, url=url),
                sdk_version=SDK_VERSION,
                timeout=timeout,
                compress=compress,
            )

    @property
    def enabled(self) -> bool:
        return self.transport is not None

    async def capture_event(self, event: Event, transaction: bool = False) -> bool:
        if self.transport is None:
            return False

        return await self.transport.send(event, transaction=transaction)

    async def capture_exception(
        self,
        exc: BaseException,
        user: Optional[dict] = None,
        request: Optional[dict] = None,
    ) -> bool:
        event = Event.from_exception(
            exc, environment=self.environment, user=user, request=request
        )

        return await self.capture_event(event)

    async def capture_message(
        self,
        message: str,
        level: str = "info",
        user: Optional[dict] = None,
        request: Optional[dict] = None,
    ) -> bool:
        event = Event.from_message(
            message, level=level, environment=self.environment, user=user, request=request
        )

        return await self.capture_event(event)

    async def capture_transaction(
        self,
        name: str,
        spans: list[Span],
        user: Optional[dict] = None,
        request: Optional[dict] = None,
    ) -> bool:
        event = Event.from_transaction(
            name=name, spans=spans, environment=self.environment, user=user, request=request
        )

        return await self.capture_event(event, transaction=True)

    def send_later(self, coroutine) -> None:
        """Schedules a send without waiting for it.

        The task is held until it finishes: a task referenced only by the event
        loop can be garbage collected mid-flight, which silently drops the
        event. Exceptions are logged here rather than surfacing as an unhandled
        task error somewhere else.
        """
        if self.transport is None:
            coroutine.close()
            return

        task = asyncio.ensure_future(coroutine)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        task.add_done_callback(_log_failure)

    async def flush(self) -> None:
        """Waits for every scheduled send to finish."""
        if self._background:
            await asyncio.gather(*tuple(self._background), return_exceptions=True)

    async def close(self) -> None:
        await self.flush()

        if self.transport is not None:
            await self.transport.close()


def _log_failure(task: asyncio.Task) -> None:
    if task.cancelled():
        return

    error = task.exception()

    if error is not None:
        logger.warning("DockTHOR failed to report an event: %s", error)
