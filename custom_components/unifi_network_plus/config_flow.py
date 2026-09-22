"""Config flow for UniFi Network+."""

from __future__ import annotations

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
    """Try to log in. Returns (ok, error_code)."""
    session = async_create_clientsession(hass, verify_ssl=verify_ssl)
    try:
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
    finally:
        await session.close()


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
