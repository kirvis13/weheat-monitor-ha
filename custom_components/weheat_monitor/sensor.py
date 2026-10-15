"""Sensors: last sync and the active block of the plan."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import WeheatMonitorConfigEntry
from .entity import WeheatMonitorEntity
from .logic import clamp


async def async_setup_entry(
    hass: HomeAssistant, entry: WeheatMonitorConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    async_add_entities([LastSyncSensor(coordinator, "last_sync"), ActiveSetpointSensor(coordinator, "active_setpoint")])


class LastSyncSensor(WeheatMonitorEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self) -> datetime | None:
        return self.coordinator.data["last_sync"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        d = self.coordinator.data
        result = d.get("last_result") or {}
        return {
            "queue_length": d["queue_length"],
            "stored": result.get("stored"),
            "duplicates": result.get("duplicates"),
            "skipped": result.get("skipped"),
            "last_error": d.get("last_error"),
        }


class ActiveSetpointSensor(WeheatMonitorEntity, SensorEntity):
    """Setpoint of the block that applies now; empty when no block applies."""

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_suggested_display_precision = 1

    @property
    def native_value(self) -> float | None:
        block = self.coordinator.data["active_block"]
        return clamp(block["setpoint"]) if block else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        d = self.coordinator.data
        block = d["active_block"] or {}
        return {
            "plan_id": block.get("plan_id"),
            "reason": block.get("reason"),
            "start": block.get("start"),
            "end": block.get("end"),
            "base_setpoint": d["base_setpoint"],
            "blocks_stored": d["block_count"],
            "decision": d["decision"],
            "manual_pause_until": d["manual_until"],
        }
