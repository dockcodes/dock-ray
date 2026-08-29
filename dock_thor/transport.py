from __future__ import annotations

import gzip
import logging

import httpx

from .models import AuthData, Event
from .serializer import PayloadSerializer

logger = logging.getLogger("dock_thor")

COMPRESSION_THRESHOLD = 1024


class HttpTransport:
    """Delivers events to the ingest endpoints.

    Never raises into the caller: an outage of the monitoring service must not
    become an outage of the application it monitors. Failures are logged and
    the call returns ``False``.
    """

    def __init__(
        self,
        auth: AuthData,
        sdk_version: str,
        timeout: float = 5.0,
        compress: bool = True,
    ) -> None:
        self.auth = auth
        self.compress = compress
        self.serializer = PayloadSerializer(sdk_version)
        self.client = httpx.AsyncClient(
            timeout=timeout,
            headers={"User-Agent": f"dock-thor-client/{sdk_version}"},
        )

    async def send(self, event: Event, transaction: bool = False) -> bool:
        url = self.auth.transaction_url() if transaction else self.auth.project_url()
        body = self.serializer.serialize(event).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.auth.private_key}",
        }

        if self.compress and len(body) > COMPRESSION_THRESHOLD:
            body = gzip.compress(body)
            headers["Content-Encoding"] = "gzip"

        try:
            response = await self.client.post(url, headers=headers, content=body)
        except httpx.HTTPError as error:
            logger.warning("DockTHOR could not be reached: %s", error)
            return False

        if response.status_code >= 400:
            logger.warning(
                "DockTHOR rejected the event with HTTP %s", response.status_code
            )
            return False

        # A 200 with success=false means the account is over its monthly quota.
        try:
            return bool(response.json().get("success", False))
        except ValueError:
            logger.warning("DockTHOR returned a response that is not JSON")
            return False

    async def close(self) -> None:
        await self.client.aclose()
