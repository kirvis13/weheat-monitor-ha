"""Coordinator: reads Home Assistant, runs the engine every minute, saves state."""

from __future__ import annotations

import json
import logging
from collections import deque
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID, ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import Context, Event, EventStateChangedData, HomeAssistant, State, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .api import AuthError, WeheatMonitorClient
from .const import (
    CONF_API_KEY,
    CONF_BASE_URL,
    CONF_CLIMATE,
    DEFAULT_BASE_URL,
    DOMAIN,
    SENSOR_FIELDS,
    SETPOINT_TOLERANCE,
    UPDATE_INTERVAL_SECONDS,
)
from .engine import SyncEngine
from .logic import clamp, is_manual_change, parse_time
from .units import celsius_to_fahrenheit, convert, fahrenheit_to_celsius, parse_number

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1


class WeheatMonitorCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL_SECONDS),
        )
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self._saved = ""
        self._own_contexts: deque[str] = deque(maxlen=20)
        client = WeheatMonitorClient(
            async_get_clientsession(hass),
            entry.data.get(CONF_BASE_URL) or DEFAULT_BASE_URL,
            entry.data[CONF_API_KEY],
        )
        self.engine = SyncEngine(client)

    # ─── Configuration (options win over data) ──────────────────────────────

    def _conf(self, key: str) -> Any:
        if key in self.config_entry.options:
            return self.config_entry.options[key]
        return self.config_entry.data.get(key)

    @property
    def climate_entity(self) -> str | None:
        return self._conf(CONF_CLIMATE) or None

    # ─── Start and stop ─────────────────────────────────────────────────────

    async def async_setup(self) -> None:
        """Load the stored queue and plan, and start watching the thermostat."""
        stored = await self._store.async_load()
        if stored:
            self.engine = SyncEngine(self.engine.client, stored)
        now = dt_util.utcnow()
        self.engine.snapshot_queue(now)

        climate = self.climate_entity
        if climate:
            # Changed while Home Assistant was down? Then a person did it.
            current = self._climate_target_c(self.hass.states.get(climate))
            last_set = self.engine.last_set
            if current is not None and last_set is not None and abs(current - last_set) >= SETPOINT_TOLERANCE:
                self.engine.manual_change(now)
            self.config_entry.async_on_unload(
                async_track_state_change_event(self.hass, [climate], self._on_climate_change)
            )

    async def async_save(self) -> None:
        text = json.dumps(self.engine.state, sort_keys=True)
        if text != self._saved:
            await self._store.async_save(self.engine.state)
            self._saved = text

    # ─── Reading Home Assistant ─────────────────────────────────────────────

    def _to_c(self, value: float | None) -> float | None:
        if value is None:
            return None
        if self.hass.config.units.temperature_unit == UnitOfTemperature.FAHRENHEIT:
            return round(fahrenheit_to_celsius(value), 2)
        return value

    def _from_c(self, value: float) -> float:
        if self.hass.config.units.temperature_unit == UnitOfTemperature.FAHRENHEIT:
            return celsius_to_fahrenheit(value)
        return value

    def _climate_target_c(self, state: State | None) -> float | None:
        if state is None:
            return None
        return self._to_c(parse_number(state.attributes.get(ATTR_TEMPERATURE)))

    def _read_fields(self) -> dict[str, float | None]:
        fields: dict[str, float | None] = {}
        for key, (kind, _classes) in SENSOR_FIELDS.items():
            entity_id = self._conf(key)
            if not entity_id:
                continue
            state = self.hass.states.get(entity_id)
            if state is None:
                continue
            fields[key] = convert(
                parse_number(state.state), kind, state.attributes.get("unit_of_measurement")
            )
        climate = self.climate_entity
        if climate and (state := self.hass.states.get(climate)) is not None:
            # An explicitly chosen sensor wins; the thermostat fills the gaps.
            if fields.get("room_temp") is None:
                fields["room_temp"] = self._to_c(parse_number(state.attributes.get("current_temperature")))
            if fields.get("setpoint") is None:
                fields["setpoint"] = self._climate_target_c(state)
        return fields

    def _thermostat_info(self, state: State | None) -> dict[str, Any] | None:
        """What Home Assistant knows about the thermostat: integration, make, model, limits.

        No name and no entity id: only what says which kind of thermostat it is.
        """
        climate = self.climate_entity
        if not climate or state is None:
            return None
        info: dict[str, Any] = {}
        entry = er.async_get(self.hass).async_get(climate)
        if entry is not None:
            info["integration"] = entry.platform
            device = dr.async_get(self.hass).async_get(entry.device_id) if entry.device_id else None
            if device is not None:
                if device.manufacturer:
                    info["manufacturer"] = device.manufacturer
                if device.model or device.model_id:
                    info["model"] = device.model or device.model_id
        step = parse_number(state.attributes.get("target_temp_step"))
        if step:
            fahrenheit = self.hass.config.units.temperature_unit == UnitOfTemperature.FAHRENHEIT
            info["target_temp_step"] = round(step * 5 / 9, 3) if fahrenheit else step
        for key in ("min_temp", "max_temp"):
            value = self._to_c(parse_number(state.attributes.get(key)))
            if value is not None:
                info[key] = value
        modes = state.attributes.get("hvac_modes")
        if isinstance(modes, list):
            info["hvac_modes"] = [str(m) for m in modes]
        return info or None

    def _quantize(self, state: State, target_c: float) -> float:
        """Round to the thermostat's step and limits, back in °C, within 12-24."""
        value = self._from_c(clamp(target_c))
        step = parse_number(state.attributes.get("target_temp_step"))
        if step and step > 0:
            value = round(round(value / step) * step, 2)
        low = parse_number(state.attributes.get("min_temp"))
        high = parse_number(state.attributes.get("max_temp"))
        if low is not None:
            value = max(value, low)
        if high is not None:
            value = min(value, high)
        return clamp(self._to_c(value))

    # ─── Steering ───────────────────────────────────────────────────────────

    @callback
    def _on_climate_change(self, event: Event[EventStateChangedData]) -> None:
        old, new = event.data["old_state"], event.data["new_state"]
        if old is None or new is None:
            return
        ctx = new.context
        own = ctx.id in self._own_contexts or (ctx.parent_id is not None and ctx.parent_id in self._own_contexts)
        if is_manual_change(self._climate_target_c(old), self._climate_target_c(new), self.engine.last_set, own):
            _LOGGER.info("Thermostat changed outside the plan: no steering for 4 hours")
            self.engine.manual_change(dt_util.utcnow())
            self.hass.async_create_task(self.async_save())
            self._publish()

    async def _set_temperature(self, target_c: float) -> None:
        climate = self.climate_entity
        state = self.hass.states.get(climate) if climate else None
        if state is None:
            raise RuntimeError("thermostat not available")
        context = Context()
        self._own_contexts.append(context.id)
        await self.hass.services.async_call(
            "climate",
            "set_temperature",
            {ATTR_ENTITY_ID: climate, ATTR_TEMPERATURE: self._from_c(target_c)},
            blocking=True,
            context=context,
        )

    async def async_set_follow(self, on: bool) -> None:
        self.engine.set_follow(on)
        await self.async_save()
        self._publish()

    @callback
    def _publish(self) -> None:
        """Show a state change now, without waiting for the next minute."""
        self.data = self.snapshot()
        self.async_update_listeners()

    # ─── The minute ─────────────────────────────────────────────────────────

    async def _async_update_data(self) -> dict[str, Any]:
        now = dt_util.utcnow()
        climate = self.climate_entity
        state = self.hass.states.get(climate) if climate else None
        # Only a thermostat with a single target temperature can be steered.
        steerable = (
            state is not None
            and state.state not in ("unavailable", "unknown")
            and state.attributes.get(ATTR_TEMPERATURE) is not None
        )
        current = self._climate_target_c(state) if steerable else None
        try:
            await self.engine.tick(
                now,
                self._read_fields(),
                current_setpoint=current,
                set_temperature=self._set_temperature if steerable else None,
                # Compare against what the thermostat can really show (step, limits, °F).
                quantize=(lambda t: self._quantize(state, t)) if steerable else None,
                thermostat=self._thermostat_info(state),
            )
        except AuthError as err:
            await self.async_save()
            raise ConfigEntryAuthFailed("The Weheat Monitor key was rejected") from err
        await self.async_save()
        return self.snapshot(now)

    def snapshot(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or dt_util.utcnow()
        s = self.engine.state
        until = s["control"].get("manual_until")
        return {
            "last_sync": parse_time(s["last_sync"]) if s.get("last_sync") else None,
            "last_result": s.get("last_result"),
            "last_error": s.get("last_error"),
            "queue_length": len(s["queue"]),
            "plan_live": s["plan_live"],
            "base_setpoint": s["base_setpoint"],
            "follow": s["follow"],
            "active_block": self.engine.active_block(now),
            "block_count": len(s["blocks"]),
            "decision": s.get("last_decision"),
            "manual_until": until if until and parse_time(until) > now else None,
        }
