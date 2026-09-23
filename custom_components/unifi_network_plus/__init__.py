"""UniFi Network+ integration - extended local statistics for UniFi controllers."""

from __future__ import annotations

import logging

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import UniFiAuthError, UniFiClient, UniFiConnectionError
from .const import (
    CONF_SCAN_INTERVAL,
    CONF_SITE,
    CONF_TOP_CLIENTS_COUNT,
    CONF_VERIFY_SSL,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SITE,
    DEFAULT_TOP_CLIENTS,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import UniFiNetworkPlusCoordinator

_LOGGER = logging.getLogger(__name__)

RUNTIME_COORDINATOR = "coordinator"
RUNTIME_SESSION = "session"


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up UniFi Network+ from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    data = dict(entry.data)
    options = dict(entry.options)

    # A dedicated session (rather than the shared HA clientsession) so a
    # bad/self-signed cert setting for this controller never affects other
    # integrations, and so its cookie jar (holding the UniFi OS TOKEN
    # cookie) is isolated per config entry.
    # UniFi controllers are almost always reached by bare IP on the local
    # network. aiohttp's default cookie jar refuses to store cookies for
    # numeric IP hosts (a conservative RFC 6265 interpretation) unless
    # created with ``unsafe=True`` - without this, the UniFi OS ``TOKEN``
    # session cookie set on login is silently dropped and every following
    # request comes back 401 even though the login itself succeeded.
    verify_ssl = bool(options.get(CONF_VERIFY_SSL, data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL)))
    session = async_create_clientsession(
        hass, verify_ssl=verify_ssl, cookie_jar=aiohttp.CookieJar(unsafe=True)
    )

    client = UniFiClient(
        session=session,
        host=str(data[CONF_HOST]),
        username=str(data[CONF_USERNAME]),
        password=str(data[CONF_PASSWORD]),
        site=str(data.get(CONF_SITE, DEFAULT_SITE)),
        verify_ssl=verify_ssl,
        port=int(data.get(CONF_PORT, DEFAULT_PORT)),
    )

    try:
        await client.login()
    except UniFiAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except UniFiConnectionError as err:
        raise ConfigEntryNotReady(str(err)) from err

    scan_interval = int(
        options.get(CONF_SCAN_INTERVAL, data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
    )
    top_clients_count = int(
        options.get(CONF_TOP_CLIENTS_COUNT, data.get(CONF_TOP_CLIENTS_COUNT, DEFAULT_TOP_CLIENTS))
    )

    coordinator = UniFiNetworkPlusCoordinator(
        hass=hass,
        entry_id=entry.entry_id,
        name=str(data.get(CONF_HOST)),
        client=client,
        scan_interval=scan_interval,
        top_clients_count=top_clients_count,
    )
    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = {
        RUNTIME_COORDINATOR: coordinator,
        RUNTIME_SESSION: session,
    }
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a UniFi Network+ config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        runtime = hass.data[DOMAIN].pop(entry.entry_id, None)
        if runtime:
            session: aiohttp.ClientSession = runtime[RUNTIME_SESSION]
            await session.close()
    return unload_ok
