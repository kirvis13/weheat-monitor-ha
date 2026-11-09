"""Reading room sensors from Home Assistant: companions, snapshots and the overview in the config flow.

The decisions (which companion, the name, the units) are in rooms.py and
units.py, which have no Home Assistant imports and are tested on their own.
This module only looks things up in the registries and the state machine.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import floor_registry as fr

from .const import MAX_ROOM_SENSORS, ROOM_EXTRA_CLASSES
from .logic import iso
from .rooms import floor_name, pick_companions, room_key, room_name
from .units import convert, parse_number

_KIND_OF_CLASS = {cls: kind for kind, cls in ROOM_EXTRA_CLASSES.items()}
_UNITS = {"humidity": "%", "co2": "ppm", "pressure": "hPa"}


def _area_id(hass: HomeAssistant, entry: er.RegistryEntry | None) -> str | None:
    """The entity's own area, else that of its device."""
    if entry is None:
        return None
    if entry.area_id:
        return entry.area_id
    if entry.device_id and (device := dr.async_get(hass).async_get(entry.device_id)) is not None:
        return device.area_id
    return None


def companions(hass: HomeAssistant, sensors: list[str], extras: bool) -> dict[str, dict[str, str]]:
    """Per chosen temperature sensor: the humidity, CO2 and pressure sensors that go along."""
    if not extras:
        return {s: {} for s in sensors}
    registry = er.async_get(hass)
    candidates: list[dict[str, Any]] = []
    for entry in registry.entities.values():
        if entry.domain != "sensor" or entry.disabled_by is not None:
            continue
        kind = _KIND_OF_CLASS.get(entry.device_class or entry.original_device_class or "")
        if kind:
            candidates.append({
                "entity_id": entry.entity_id, "kind": kind,
                "device_id": entry.device_id, "area_id": _area_id(hass, entry),
            })
    result: dict[str, dict[str, str]] = {}
    for sensor in sensors:
        entry = registry.async_get(sensor)
        target = {"device_id": entry.device_id if entry else None, "area_id": _area_id(hass, entry)}
        result[sensor] = pick_companions(target, [c for c in candidates if c["entity_id"] != sensor])
    return result


def _names(hass: HomeAssistant, sensor: str, state: State | None) -> tuple[str, str | None]:
    area_id = _area_id(hass, er.async_get(hass).async_get(sensor))
    area = ar.async_get(hass).async_get_area(area_id) if area_id else None
    floor = fr.async_get(hass).async_get_floor(area.floor_id) if area and area.floor_id else None
    return (
        room_name(area.name if area else None, state.name if state else None),
        floor_name(floor.name if floor else None),
    )


def _value(state: State | None, kind: str) -> float | None:
    if state is None:
        return None
    return convert(parse_number(state.state), kind, state.attributes.get("unit_of_measurement"))


def snapshots(hass: HomeAssistant, sensors: list[str], found: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
    """One snapshot per chosen sensor, for the engine.

    measured_at is the newest change of the temperature or one of its
    companions, so a change in humidity alone also counts as a new reading.
    Without a temperature (unavailable) there is no measured_at.
    """
    result = []
    for sensor in sensors[:MAX_ROOM_SENSORS]:
        state = hass.states.get(sensor)
        name, floor = _names(hass, sensor, state)
        snap: dict[str, Any] = {
            "key": room_key(sensor), "name": name, "floor": floor, "entity_id": sensor,
            "measured_at": None, "temp": _value(state, "temperature"),
            "last_updated": state.last_updated if state else None,
        }
        if snap["temp"] is not None and state is not None:
            times = [state.last_updated]
            for kind, companion in found.get(sensor, {}).items():
                other = hass.states.get(companion)
                value = _value(other, kind)
                if value is not None and other is not None:
                    snap[kind] = value
                    times.append(other.last_updated)
            # Milliseconds, like the server stores them, so a repeat is recognised there too.
            newest = max(times)
            snap["measured_at"] = iso(newest.replace(microsecond=newest.microsecond // 1000 * 1000))
        result.append(snap)
    return result


def overview_rows(hass: HomeAssistant, sensors: list[str], extras: bool, missing: str) -> str:
    """Markdown rows for the confirmation step: room, temperature and what was found."""
    found = companions(hass, sensors, extras)
    rows = []
    for sensor in sensors:
        state = hass.states.get(sensor)
        name, _floor = _names(hass, sensor, state)
        temp = _value(state, "temperature")
        cells = [name.replace("|", "/"), f"{temp:.1f} °C" if temp is not None else missing]
        for kind in ROOM_EXTRA_CLASSES:
            companion = found.get(sensor, {}).get(kind)
            value = _value(hass.states.get(companion), kind) if companion else None
            cells.append(f"{value:g} {_UNITS[kind]}" if value is not None else missing)
        rows.append("| " + " | ".join(cells) + " |")
    return "\n".join(rows)
