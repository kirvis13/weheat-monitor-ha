"""Switches: "Follow plan" (the local off switch for steering, off by default) and "Send rooms" (on by default)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import WeheatMonitorConfigEntry
from .entity import WeheatMonitorEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: WeheatMonitorConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([
        FollowPlanSwitch(entry.runtime_data, "follow_plan"),
        SendRoomsSwitch(entry.runtime_data, "send_rooms"),
    ])


class FollowPlanSwitch(WeheatMonitorEntity, SwitchEntity):
    """Off: never write the thermostat, keep measuring. The state survives a restart (stored with the queue)."""

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.engine.state["follow"])

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_follow(True)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_follow(False)


class SendRoomsSwitch(WeheatMonitorEntity, SwitchEntity):
    """Off: nothing about the room sensors goes to Weheat Monitor, and readings not yet sent are dropped."""

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.engine.state.get("send_rooms", True))

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_send_rooms(True)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_send_rooms(False)
