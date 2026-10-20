"""Config flow for UniFi Network+."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry, ConfigFlow
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.data_entry_flow import FlowResult
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
)


async def _test_connection(
    hass,
    host: str,
    port: int,
    username: str,
    password: str,
    site: str,
    verify_ssl: bool,
) -> tuple[bool, str | None]:
    """Try to log in. Returns (ok, error_code).

    Uses ``async_create_clientsession`` with its default ``auto_cleanup=True``:
    Home Assistant registers its own shutdown listener to close this session,
    so we must *not* close it ourselves here (doing so previously tripped
    HA's "integration closes a HA-managed aiohttp session" frame-helper
    warning).
    """
    # UniFi controllers are almost always reached by bare IP on the local
    # network. aiohttp's default cookie jar refuses to store cookies for
    # numeric IP hosts (a conservative RFC 6265 interpretation) unless
    # created with ``unsafe=True`` - without this, the UniFi OS ``TOKEN``
    # session cookie set on login is silently dropped and every following
    # request comes back 401 even though the login itself succeeded.
    session = async_create_clientsession(
        hass, verify_ssl=verify_ssl, cookie_jar=aiohttp.CookieJar(unsafe=True)
    )
    client = UniFiClient(
        session=session,
        host=host,
        username=username,
        password=password,
        site=site,
        verify_ssl=verify_ssl,
        port=port,
    )
    try:
        await client.login()
    except UniFiAuthError:
        return False, "invalid_auth"
    except UniFiConnectionError:
        return False, "cannot_connect"
    except aiohttp.ClientError:
        return False, "cannot_connect"
    return True, None


class UniFiNetworkPlusOptionsFlow(config_entries.OptionsFlow):
    """Options flow for UniFi Network+."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        return self.async_show_form(step_id="init", data_schema=self._build_schema())

    def _build_schema(self) -> vol.Schema:
        data = dict(self._entry.data)
        options = dict(self._entry.options)
        return vol.Schema(
            {
                vol.Required(
                    CONF_SCAN_INTERVAL,
                    default=options.get(
                        CONF_SCAN_INTERVAL, data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
                    ),
                ): vol.All(vol.Coerce(int), vol.Range(min=15, max=3600)),
                vol.Required(
                    CONF_TOP_CLIENTS_COUNT,
                    default=options.get(
                        CONF_TOP_CLIENTS_COUNT, data.get(CONF_TOP_CLIENTS_COUNT, DEFAULT_TOP_CLIENTS)
                    ),
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=25)),
            }
        )


class UniFiNetworkPlusConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for UniFi Network+."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host = str(user_input[CONF_HOST]).strip()
            site = str(user_input.get(CONF_SITE, DEFAULT_SITE)).strip() or DEFAULT_SITE
            await self.async_set_unique_id(f"{host}::{site}")
            self._abort_if_unique_id_configured()

            ok, error = await _test_connection(
                self.hass,
                host=host,
                port=int(user_input[CONF_PORT]),
                username=str(user_input[CONF_USERNAME]),
                password=str(user_input[CONF_PASSWORD]),
                site=site,
                verify_ssl=bool(user_input.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL)),
            )
            if ok:
                return self.async_create_entry(title=f"{host} ({site})", data=user_input)
            errors["base"] = error or "cannot_connect"

        schema = self._build_user_schema()
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> FlowResult:
        """Start re-authentication after the controller rejected the stored login."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Ask for new credentials, validate them and update the stored entry."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            ok, error = await _test_connection(
                self.hass,
                host=str(entry.data[CONF_HOST]),
                port=int(entry.data.get(CONF_PORT, DEFAULT_PORT)),
                username=str(user_input[CONF_USERNAME]),
                password=str(user_input[CONF_PASSWORD]),
                site=str(entry.data.get(CONF_SITE, DEFAULT_SITE)),
                verify_ssl=bool(entry.data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL)),
            )
            if ok:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_USERNAME: user_input[CONF_USERNAME],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                    },
                )
            errors["base"] = error or "cannot_connect"

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME, default=entry.data.get(CONF_USERNAME, "")): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
            description_placeholders={"host": str(entry.data.get(CONF_HOST, ""))},
        )

    def _build_user_schema(self) -> vol.Schema:
        return vol.Schema(
            {
                vol.Required(CONF_HOST): str,
                vol.Required(CONF_USERNAME): str,
                vol.Required(CONF_PASSWORD): str,
                vol.Optional(CONF_SITE, default=DEFAULT_SITE): str,
                vol.Required(CONF_PORT, default=DEFAULT_PORT): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=65535)
                ),
                vol.Optional(CONF_VERIFY_SSL, default=DEFAULT_VERIFY_SSL): bool,
                vol.Required(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): vol.All(
                    vol.Coerce(int), vol.Range(min=15, max=3600)
                ),
                vol.Required(CONF_TOP_CLIENTS_COUNT, default=DEFAULT_TOP_CLIENTS): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=25)
                ),
            }
        )

    @staticmethod
    def async_get_options_flow(config_entry: ConfigEntry) -> UniFiNetworkPlusOptionsFlow:
        return UniFiNetworkPlusOptionsFlow(config_entry)
