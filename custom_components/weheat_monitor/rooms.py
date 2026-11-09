"""Room sensors: which values go along, the queue, and the `rooms` field of a sync.

No Home Assistant imports: the coordinator reads Home Assistant and hands in
plain dicts, the tests do the same.

Each chosen temperature sensor is one item in `rooms`. The server keeps track
of which room it is in; the name (the area in Home Assistant) only chooses the
room for a new sensor. A reading is sent when one of the sensor's values
changed, with the time of that change, never a repeat of an old value.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta
from typing import Any

from .const import (
    MAX_ROOM_READINGS_PER_CALL,
    MAX_ROOM_SENSORS,
    ROOM_EXTRA_CLASSES,
    ROOM_QUEUE_MAX_AGE_SECONDS,
    ROOM_QUEUE_MAX_LENGTH,
    ROOM_SILENT_SECONDS,
)
from .logic import parse_time as _time

VALUE_FIELDS = ("temp", "humidity", "co2", "pressure")

# Words a temperature sensor's name often ends with, left out of a room name.
_SUFFIX = re.compile(r"[\s_-]*(temperatuur|temperature|temp|sensor)$", re.IGNORECASE)


def room_key(entity_id: str) -> str:
    """Stable key for the server: a hash of the entity id, so renaming in Home Assistant keeps the sensor."""
    return hashlib.sha256(entity_id.encode()).hexdigest()[:16]


def room_name(area_name: str | None, friendly_name: str | None) -> str:
    """The area's name; without an area the sensor's name without "Temperatuur"; at most 40 characters."""
    name = (area_name or "").strip()
    if not name:
        name = _SUFFIX.sub("", (friendly_name or "").strip()).strip()
    name = re.sub(r"\s+", " ", name)[:40].strip()
    return name or "Ruimte"


def floor_name(name: str | None) -> str | None:
    text = re.sub(r"\s+", " ", (name or "").strip())[:20].strip()
    return text or None


def pick_companions(target: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, str]:
    """Humidity, CO2 and pressure for one temperature sensor.

    First a sensor of that kind on the same device; otherwise the only one of
    that kind in the same area. Two or more in the area: none, because a
    wrong sensor is worse than no value.

    target: {"device_id", "area_id"}; candidates: [{"entity_id", "kind", "device_id", "area_id"}].
    """
    found: dict[str, str] = {}
    for kind in ROOM_EXTRA_CLASSES:
        of_kind = [c for c in candidates if c.get("kind") == kind]
        device = target.get("device_id")
        same_device = sorted(c["entity_id"] for c in of_kind if device and c.get("device_id") == device)
        if same_device:
            found[kind] = same_device[0]
            continue
        area = target.get("area_id")
        same_area = [c["entity_id"] for c in of_kind if area and c.get("area_id") == area]
        if len(same_area) == 1:
            found[kind] = same_area[0]
    return found


def is_silent(last_updated: datetime | None, now: datetime) -> bool:
    """No new value for two hours (or never): a repair issue."""
    return last_updated is None or now - last_updated > timedelta(seconds=ROOM_SILENT_SECONDS)


def _reading(snapshot: dict[str, Any]) -> dict[str, Any]:
    r: dict[str, Any] = {"key": snapshot["key"], "measured_at": snapshot["measured_at"]}
    for field in VALUE_FIELDS:
        value = snapshot.get(field)
        if value is not None:
            r[field] = round(float(value), 2)
    return r


def prune_room_queue(queue: list[dict[str, Any]], keys: set[str], now: datetime) -> list[dict[str, Any]]:
    """Drop readings the server would no longer take, of sensors no longer chosen, and the oldest above the cap."""
    oldest = now - timedelta(seconds=ROOM_QUEUE_MAX_AGE_SECONDS)
    kept = [r for r in queue if r["key"] in keys and _time(r["measured_at"]) >= oldest]
    return kept[-ROOM_QUEUE_MAX_LENGTH:]


def enqueue_room_readings(
    queue: list[dict[str, Any]],
    last: dict[str, str],
    snapshots: list[dict[str, Any]],
    now: datetime,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Add a reading for every sensor whose newest change is newer than what was queued before.

    A sensor without a temperature (unavailable) adds nothing.
    """
    queue = list(queue)
    last = {k: v for k, v in last.items() if any(s["key"] == k for s in snapshots)}
    for s in snapshots:
        if s.get("temp") is None or not s.get("measured_at"):
            continue
        before = last.get(s["key"])
        if before is not None and _time(s["measured_at"]) <= _time(before):
            continue
        queue.append(_reading(s))
        last[s["key"]] = s["measured_at"]
    return prune_room_queue(queue, {s["key"] for s in snapshots}, now), last


def build_rooms_payload(
    meta: dict[str, dict[str, Any]], queue: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], int]:
    """The `rooms` field for one call, and how many queued readings it takes (from the front).

    Every chosen sensor goes along, also without new readings, so the server
    sees a new name and that the sensor is still there. At most 20 sensors and
    100 readings.
    """
    keys = list(meta)[:MAX_ROOM_SENSORS]
    taken = 0
    readings: dict[str, list[dict[str, Any]]] = {k: [] for k in keys}
    for r in queue[:MAX_ROOM_READINGS_PER_CALL]:
        if r["key"] not in readings:
            break  # cannot happen after prune_room_queue; never skip over a reading
        readings[r["key"]].append({k: v for k, v in r.items() if k != "key"})
        taken += 1
    payload = []
    for k in keys:
        item: dict[str, Any] = {"key": k, "name": meta[k]["name"], "readings": readings[k]}
        if meta[k].get("floor"):
            item["floor"] = meta[k]["floor"]
        payload.append(item)
    return payload, taken
