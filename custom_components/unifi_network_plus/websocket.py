"""Real-time UniFi controller event stream.

Connects to the controller's WebSocket event feed and uses incoming
messages purely as a "something changed, poll now" signal - it does
**not** attempt to parse/merge the WS payload schema into the data model
directly. Reverse-engineering and correctly merging UniFi's partial
sta:sync/device:sync event payloads is a much larger, more failure-prone
undertaking (see the "bugs found" section in README.md for how much even
the well-documented REST endpoints needed correcting against live data) -
treating any received frame as a trigger for an immediate, already-tested
REST refresh gets most of the practical benefit (near-real-time reaction
to client connects/disconnects, device state changes, etc. instead of
waiting up to a full poll interval) with a small, easy-to-reason-about
surface area. If the WebSocket is unavailable or drops, the integration
falls back to (and keeps using) its normal scheduled polling - the
DataUpdateCoordinator's own timer is never disabled, this only ever
makes refreshes happen *earlier* than they otherwise would.
"""

from __future__ import annotations

import asyncio
import logging
import random

import aiohttp

from .api import UniFiClient

_LOGGER = logging.getLogger(__name__)

_RECONNECT_MIN_DELAY = 2
_RECONNECT_MAX_DELAY = 60
_DEBOUNCE_SECONDS = 1.5
_WS_TIMEOUT = aiohttp.ClientWSTimeout(ws_close=10)


class UniFiEventListener:
    """Keeps a UniFi WebSocket connection alive in the background and
    awaits ``on_event`` (an async callable, debounced) shortly after any
    frame is received - pass ``coordinator.async_request_refresh``.
    """

    def __init__(
        self,
        client: UniFiClient,
        on_event,
        debounce_seconds: float = _DEBOUNCE_SECONDS,
    ) -> None:
        self._client = client
        self._on_event = on_event
        self._debounce_seconds = debounce_seconds
        self._task: asyncio.Task | None = None
        self._debounce_task: asyncio.Task | None = None
        self._stopping = False
        self.connected = False

    def start(self) -> None:
        """Start the background connect/listen/reconnect loop."""
        self._stopping = False
        self._task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        """Stop the listener and wait for its task to actually finish."""
        self._stopping = True
        if self._debounce_task:
            self._debounce_task.cancel()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self.connected = False

    async def _run(self) -> None:
        delay = _RECONNECT_MIN_DELAY
        while not self._stopping:
            try:
                await self._connect_and_listen()
                delay = _RECONNECT_MIN_DELAY  # reset backoff after a clean session
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - any failure just means "reconnect"
                _LOGGER.debug("UniFi event stream error, will reconnect: %s", err)
            self.connected = False
            if self._stopping:
                return
            # Jitter avoids every integration instance/HA restart hammering
            # the controller's login-attempt rate limit at the same instant.
            await asyncio.sleep(delay + random.uniform(0, 1))
            delay = min(delay * 2, _RECONNECT_MAX_DELAY)

    async def _connect_and_listen(self) -> None:
        await self._client.ensure_logged_in()
        url = self._client.events_url
        _LOGGER.debug("UniFi event stream connecting: %s", url)
        async with self._client.session.ws_connect(
            url,
            ssl=self._client.verify_ssl,
            headers=self._client.auth_headers(),
            heartbeat=30,
            timeout=_WS_TIMEOUT,
        ) as ws:
            self.connected = True
            _LOGGER.debug("UniFi event stream connected")
            async for msg in ws:
                if msg.type in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
                    self._schedule_refresh()
                elif msg.type in (
                    aiohttp.WSMsgType.ERROR,
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSED,
                ):
                    _LOGGER.debug("UniFi event stream closed: %s", msg)
                    break

    def _schedule_refresh(self) -> None:
        if self._debounce_task and not self._debounce_task.done():
            self._debounce_task.cancel()
        self._debounce_task = asyncio.ensure_future(self._debounced_fire())

    async def _debounced_fire(self) -> None:
        try:
            await asyncio.sleep(self._debounce_seconds)
        except asyncio.CancelledError:
            return
        await self._on_event()
