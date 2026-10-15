"""Binary sensor: does the server send a live plan for this device?"""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import WeheatMonitorConfigEntry
from .entity import WeheatMonitorEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: WeheatMonitorConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([PlanLiveBinarySensor(entry.runtime_data, "plan_live")])


class PlanLiveBinarySensor(WeheatMonitorEntity, BinarySensorEntity):
    """On when plan_live is switched on at weheat-monitor.nl/settings/connections."""

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.data["plan_live"])
