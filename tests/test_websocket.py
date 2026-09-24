"""Tests for websocket.py's event-debouncing logic.

Deliberately does not spin up a real (or mocked) WebSocket connection -
_connect_and_listen()'s job is a thin, hard-to-usefully-mock wrapper
around aiohttp's ws_connect(); what's actually worth covering here is the
debounce behaviour (many rapid events -> exactly one refresh call), which
is plain asyncio logic independent of the transport.
"""

from __future__ import annotations

import asyncio

import pytest


@pytest.mark.asyncio
async def test_rapid_events_debounce_to_single_refresh(websocket_module) -> None:
    calls = 0

    async def on_event() -> None:
        nonlocal calls
        calls += 1

    listener = websocket_module.UniFiEventListener(
        client=None, on_event=on_event, debounce_seconds=0.05
    )

    # Simulate five frames arriving in quick succession (well within the
    # debounce window of each other).
    for _ in range(5):
        listener._schedule_refresh()
        await asyncio.sleep(0.01)

    # Wait past the debounce window from the *last* scheduled call.
    await asyncio.sleep(0.15)

    assert calls == 1


@pytest.mark.asyncio
async def test_events_after_debounce_window_each_refresh(websocket_module) -> None:
    calls = 0

    async def on_event() -> None:
        nonlocal calls
        calls += 1

    listener = websocket_module.UniFiEventListener(
        client=None, on_event=on_event, debounce_seconds=0.02
    )

    listener._schedule_refresh()
    await asyncio.sleep(0.05)  # past the debounce window -> first refresh fires
    listener._schedule_refresh()
    await asyncio.sleep(0.05)  # past the debounce window again -> second refresh fires

    assert calls == 2


@pytest.mark.asyncio
async def test_stop_cancels_pending_debounce(websocket_module) -> None:
    calls = 0

    async def on_event() -> None:
        nonlocal calls
        calls += 1

    listener = websocket_module.UniFiEventListener(
        client=None, on_event=on_event, debounce_seconds=0.05
    )
    listener._schedule_refresh()
    await listener.stop()
    await asyncio.sleep(0.1)

    assert calls == 0
    assert listener.connected is False
