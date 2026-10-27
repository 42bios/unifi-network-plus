"""Config flow for UniFi Network+."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry, ConfigFlow
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.data_entry_flow import FlowResult, section
from homeassistant.helpers import selector as sel
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import UniFiAuthError, UniFiClient, UniFiConnectionError
from .const import (
    CONF_SCAN_INTERVAL,
    CONF_SITE,
    CONF_TOP_CLIENTS_COUNT,
    CONF_TRACK_CLIENTS,
    CONF_VERIFY_SSL,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SITE,
    DEFAULT_TOP_CLIENTS,
    DEFAULT_TRACK_CLIENTS,
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


SECTION_ADVANCED = "advanced"


def _clean_host(value: str) -> str:
    """Accept ``https://192.168.1.1/`` as well as a bare host."""
    host = value.strip()
    for prefix in ("https://", "http://"):
        if host.lower().startswith(prefix):
            host = host[len(prefix):]
    return host.split("/")[0].strip()


def _connection_data(user_input: Mapping[str, Any], fallback: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Flatten the form (the collapsed "advanced" section is nested) into the
    flat shape stored in the config entry; ``fallback`` fills what the form
    doesn't offer (reconfigure keeps the site and an unchanged password)."""
    fallback = fallback or {}
    advanced = user_input.get(SECTION_ADVANCED) or {}
    return {
        CONF_HOST: _clean_host(str(user_input[CONF_HOST])),
        CONF_USERNAME: str(user_input[CONF_USERNAME]).strip(),
        CONF_PASSWORD: user_input.get(CONF_PASSWORD) or fallback.get(CONF_PASSWORD, ""),
        CONF_SITE: str(advanced.get(CONF_SITE) or fallback.get(CONF_SITE) or DEFAULT_SITE).strip() or DEFAULT_SITE,
        CONF_PORT: int(advanced.get(CONF_PORT, fallback.get(CONF_PORT, DEFAULT_PORT))),
        CONF_VERIFY_SSL: bool(advanced.get(CONF_VERIFY_SSL, fallback.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL))),
    }


def _connection_schema(defaults: Mapping[str, Any], *, new_entry: bool) -> vol.Schema:
    """Host / account up front, port / SSL (and the site when adding) tucked
    into a collapsed section. ``new_entry`` makes the password required;
    when reconfiguring, leaving it empty keeps the stored one."""
    advanced: dict[Any, Any] = {}
    if new_entry:
        advanced[vol.Optional(CONF_SITE, default=defaults.get(CONF_SITE, DEFAULT_SITE))] = sel.TextSelector()
    advanced[vol.Required(CONF_PORT, default=int(defaults.get(CONF_PORT, DEFAULT_PORT)))] = sel.NumberSelector(
        sel.NumberSelectorConfig(min=1, max=65535, step=1, mode=sel.NumberSelectorMode.BOX)
    )
    advanced[
        vol.Required(CONF_VERIFY_SSL, default=bool(defaults.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL)))
    ] = sel.BooleanSelector()
    password_key = vol.Required(CONF_PASSWORD) if new_entry else vol.Optional(CONF_PASSWORD)
    return vol.Schema(
        {
            vol.Required(CONF_HOST, description={"suggested_value": defaults.get(CONF_HOST)}): sel.TextSelector(
                sel.TextSelectorConfig(type=sel.TextSelectorType.URL, autocomplete="off")
            ),
            vol.Required(
                CONF_USERNAME, description={"suggested_value": defaults.get(CONF_USERNAME)}
            ): sel.TextSelector(sel.TextSelectorConfig(type=sel.TextSelectorType.TEXT, autocomplete="username")),
            password_key: sel.TextSelector(
                sel.TextSelectorConfig(type=sel.TextSelectorType.PASSWORD, autocomplete="current-password")
            ),
            vol.Required(SECTION_ADVANCED): section(vol.Schema(advanced), {"collapsed": True}),
        }
    )


class UniFiNetworkPlusOptionsFlow(config_entries.OptionsFlow):
    """Options flow for UniFi Network+."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data={k: v if isinstance(v, bool) else int(v) for k, v in user_input.items()})
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
                ): sel.NumberSelector(
                    sel.NumberSelectorConfig(
                        min=15, max=3600, step=5, mode=sel.NumberSelectorMode.SLIDER, unit_of_measurement="s"
                    )
                ),
                vol.Required(
                    CONF_TOP_CLIENTS_COUNT,
                    default=options.get(
                        CONF_TOP_CLIENTS_COUNT, data.get(CONF_TOP_CLIENTS_COUNT, DEFAULT_TOP_CLIENTS)
                    ),
                ): sel.NumberSelector(
                    sel.NumberSelectorConfig(min=1, max=25, step=1, mode=sel.NumberSelectorMode.SLIDER)
                ),
                vol.Required(
                    CONF_TRACK_CLIENTS, default=options.get(CONF_TRACK_CLIENTS, DEFAULT_TRACK_CLIENTS)
                ): sel.BooleanSelector(),
            }
        )


class UniFiNetworkPlusConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for UniFi Network+."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        defaults: dict[str, Any] = {}
        if user_input is not None:
            data = _connection_data(user_input)
            defaults = {**data, CONF_PASSWORD: ""}
            await self.async_set_unique_id(f"{data[CONF_HOST]}::{data[CONF_SITE]}")
            self._abort_if_unique_id_configured()

            ok, error = await _test_connection(
                self.hass,
                host=data[CONF_HOST],
                port=data[CONF_PORT],
                username=data[CONF_USERNAME],
                password=data[CONF_PASSWORD],
                site=data[CONF_SITE],
                verify_ssl=data[CONF_VERIFY_SSL],
            )
            if ok:
                return self.async_create_entry(title=f"{data[CONF_HOST]} ({data[CONF_SITE]})", data=data)
            errors["base"] = error or "cannot_connect"

        return self.async_show_form(
            step_id="user", data_schema=_connection_schema(defaults, new_entry=True), errors=errors
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Change host, port, account or SSL setting without removing the entry."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        defaults: dict[str, Any] = {**entry.data, CONF_PASSWORD: ""}
        if user_input is not None:
            data = _connection_data(user_input, entry.data)
            defaults = {**data, CONF_PASSWORD: ""}
            unique_id = f"{data[CONF_HOST]}::{data[CONF_SITE]}"
            if any(
                other.unique_id == unique_id and other.entry_id != entry.entry_id
                for other in self._async_current_entries(include_ignore=False)
            ):
                return self.async_abort(reason="already_configured")
            ok, error = await _test_connection(
                self.hass,
                host=data[CONF_HOST],
                port=data[CONF_PORT],
                username=data[CONF_USERNAME],
                password=data[CONF_PASSWORD],
                site=data[CONF_SITE],
                verify_ssl=data[CONF_VERIFY_SSL],
            )
            if ok:
                return self.async_update_reload_and_abort(
                    entry,
                    unique_id=unique_id,
                    title=f"{data[CONF_HOST]} ({data[CONF_SITE]})",
                    data_updates=data,
                )
            errors["base"] = error or "cannot_connect"

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_connection_schema(defaults, new_entry=False),
            errors=errors,
        )

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

    @staticmethod
    def async_get_options_flow(config_entry: ConfigEntry) -> UniFiNetworkPlusOptionsFlow:
        return UniFiNetworkPlusOptionsFlow(config_entry)
