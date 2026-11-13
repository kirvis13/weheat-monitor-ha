"""Config flow: the key (tested right away), the thermostat with an optional room sensor, then the room sensors."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import ApiError, AuthError, WeheatMonitorClient, valid_key_format
from .const import (
    CONF_API_KEY,
    CONF_BASE_URL,
    CONF_CLIMATE,
    CONF_ROOM_EXTRAS,
    CONF_ROOM_SENSORS,
    DEFAULT_BASE_URL,
    DOMAIN,
    MAX_ROOM_SENSORS,
    SENSOR_FIELDS,
)
from .room_reader import overview_rows

_LOGGER = logging.getLogger(__name__)


async def _test_key(hass: HomeAssistant, base_url: str, key: str) -> str | None:
    """An empty sync: None when the key works, else an error code for the form."""
    if not valid_key_format(key):
        return "invalid_key_format"
    client = WeheatMonitorClient(async_get_clientsession(hass), base_url, key)
    try:
        await client.sync()
    except AuthError:
        return "invalid_auth"
    except ApiError as err:
        _LOGGER.debug("Key test failed: %s", err)
        return "cannot_connect"
    return None


def _unique_id(key: str) -> str:
    # A hash, so the key itself never ends up in core.config_entries as an id.
    return hashlib.sha256(key.strip().encode()).hexdigest()[:16]


def _thermostat_schema(current: Mapping[str, Any]) -> vol.Schema:
    """The thermostat (required) and an optional, better placed room sensor."""
    schema: dict[Any, Any] = {
        vol.Required(CONF_CLIMATE, description={"suggested_value": current.get(CONF_CLIMATE)}): EntitySelector(
            EntitySelectorConfig(domain="climate")
        )
    }
    for key, (_kind, classes) in SENSOR_FIELDS.items():
        schema[vol.Optional(key, description={"suggested_value": current.get(key)})] = EntitySelector(
            EntitySelectorConfig(domain="sensor", device_class=classes)
        )
    return vol.Schema(schema)


def _clean(user_input: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in user_input.items() if v}


def _rooms_schema(current: Mapping[str, Any]) -> vol.Schema:
    """Temperature sensors, several at once, and whether humidity, CO2 and pressure are looked up."""
    return vol.Schema({
        vol.Optional(CONF_ROOM_SENSORS, description={"suggested_value": current.get(CONF_ROOM_SENSORS) or []}): EntitySelector(
            EntitySelectorConfig(domain="sensor", device_class="temperature", multiple=True)
        ),
        vol.Optional(CONF_ROOM_EXTRAS, default=current.get(CONF_ROOM_EXTRAS, True)): BooleanSelector(),
    })


class _RoomSteps:
    """The steps Rooms and the overview, shared by the config flow and the options flow."""

    hass: HomeAssistant
    _rooms: dict[str, Any]

    def _current(self) -> Mapping[str, Any]:
        raise NotImplementedError

    def _finish(self) -> ConfigFlowResult:
        raise NotImplementedError

    async def async_step_rooms(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            sensors = list(user_input.get(CONF_ROOM_SENSORS) or [])
            if len(sensors) > MAX_ROOM_SENSORS:
                errors["base"] = "too_many_rooms"
            else:
                # Not through _clean: "look up humidity" off is a choice, not an empty field.
                self._rooms = {CONF_ROOM_SENSORS: sensors, CONF_ROOM_EXTRAS: bool(user_input.get(CONF_ROOM_EXTRAS, True))}
                return await self.async_step_rooms_confirm() if sensors else self._finish()
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="rooms", data_schema=_rooms_schema(user_input or self._current()), errors=errors,
            description_placeholders={"max": str(MAX_ROOM_SENSORS)},
        )

    async def async_step_rooms_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self._finish()
        rows = overview_rows(self.hass, self._rooms[CONF_ROOM_SENSORS], self._rooms[CONF_ROOM_EXTRAS], "-")
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="rooms_confirm", data_schema=vol.Schema({}), description_placeholders={"rows": rows},
        )


class WeheatMonitorConfigFlow(_RoomSteps, ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._options: dict[str, Any] = {}
        self._rooms: dict[str, Any] = {}

    def _current(self) -> Mapping[str, Any]:
        return {}

    def _finish(self) -> ConfigFlowResult:
        return self.async_create_entry(title="Weheat Monitor", data=self._data, options={**self._options, **self._rooms})

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            key = user_input[CONF_API_KEY].strip()
            base_url = (user_input.get(CONF_BASE_URL) or DEFAULT_BASE_URL).strip().rstrip("/")
            error = await _test_key(self.hass, base_url, key)
            if error:
                errors["base" if error == "cannot_connect" else CONF_API_KEY] = error
            else:
                # A flow left open with the same key (dialog closed midway) must not
                # block a fresh attempt; only a configured entry counts as a duplicate.
                await self.async_set_unique_id(_unique_id(key), raise_on_progress=False)
                self._abort_if_unique_id_configured()
                self._data = {CONF_API_KEY: key, CONF_BASE_URL: base_url}
                return await self.async_step_thermostat()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_API_KEY): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
                    vol.Optional(CONF_BASE_URL, default=DEFAULT_BASE_URL): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.URL)
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_thermostat(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._options = _clean(user_input)
            return await self.async_step_rooms()
        return self.async_show_form(step_id="thermostat", data_schema=_thermostat_schema({}))

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            key = user_input[CONF_API_KEY].strip()
            base_url = entry.data.get(CONF_BASE_URL) or DEFAULT_BASE_URL
            error = await _test_key(self.hass, base_url, key)
            if error:
                errors["base" if error == "cannot_connect" else CONF_API_KEY] = error
            else:
                return self.async_update_reload_and_abort(
                    entry, unique_id=_unique_id(key), data_updates={CONF_API_KEY: key}
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {vol.Required(CONF_API_KEY): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))}
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return WeheatMonitorOptionsFlow()


class WeheatMonitorOptionsFlow(_RoomSteps, OptionsFlow):
    """Change the thermostat, the room sensor or the room sensors later."""

    def __init__(self) -> None:
        self._options: dict[str, Any] = {}
        self._rooms: dict[str, Any] = {}

    def _current(self) -> Mapping[str, Any]:
        return self.config_entry.options

    def _finish(self) -> ConfigFlowResult:
        return self.async_create_entry(data={**self._options, **self._rooms})

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._options = _clean(user_input)
            return await self.async_step_rooms()
        return self.async_show_form(step_id="init", data_schema=_thermostat_schema(self.config_entry.options))
