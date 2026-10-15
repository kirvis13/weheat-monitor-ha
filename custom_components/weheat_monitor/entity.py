"""Shared base for the entities: one device per config entry."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import WeheatMonitorCoordinator


class WeheatMonitorEntity(CoordinatorEntity[WeheatMonitorCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: WeheatMonitorCoordinator, key: str) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Weheat Monitor",
            manufacturer="Weheat Monitor",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url="https://www.weheat-monitor.nl/settings/connections",
        )

    @property
    def available(self) -> bool:
        # The integration keeps working offline (queue, stored plan), so its
        # entities stay available even when a sync fails.
        return self.coordinator.data is not None
