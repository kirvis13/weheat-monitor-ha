"""Constants for the Weheat Monitor integration.

This module has no Home Assistant imports, so the tests can load it on its own.
"""

from __future__ import annotations

DOMAIN = "weheat_monitor"

DEFAULT_BASE_URL = "https://www.weheat-monitor.nl"
SYNC_PATH = "/api/v1/devices/sync"

CONF_API_KEY = "api_key"
CONF_BASE_URL = "base_url"
CONF_CLIMATE = "climate_entity"
CONF_ROOM_SENSORS = "room_sensors"
CONF_ROOM_EXTRAS = "room_auto_extras"

# Contract limits of POST /api/v1/devices/sync.
MAX_MEASUREMENTS_PER_CALL = 100
MAX_APPLIED_PER_CALL = 20
# The server rejects measurements older than 24 h; drop them a bit earlier so a
# batch never arrives with rows the server will skip anyway.
QUEUE_MAX_AGE_SECONDS = 24 * 3600 - 5 * 60
# One day of minutes, plus a little slack.
QUEUE_MAX_LENGTH = 1500
# How many batches to send in one tick when a backlog is draining.
MAX_BATCHES_PER_TICK = 15

UPDATE_INTERVAL_SECONDS = 60
REQUEST_TIMEOUT_SECONDS = 15

# Safety rails for every setpoint this integration writes.
MIN_SETPOINT = 12.0
MAX_SETPOINT = 24.0
SETPOINT_TOLERANCE = 0.05
MANUAL_PAUSE_SECONDS = 4 * 3600

SOURCE = "ha"

# Optional sensor next to the thermostat: a room sensor that is better placed
# than the one in the thermostat. Value: (kind, device classes the selector
# offers); the kind decides the unit conversion (see units.py). Setpoint and,
# without this sensor, room temperature come from the climate entity.
SENSOR_FIELDS: dict[str, tuple[str, list[str]]] = {
    "room_temp": ("temperature", ["temperature"]),
}

# The thermostat's make and model go along with a sync at start-up, after a
# change, and otherwise once a day.
THERMOSTAT_REPORT_SECONDS = 24 * 3600

# ─── Room sensors (field `rooms` in the sync) ───────────────────────────────
# Each chosen temperature sensor is one item in `rooms`; humidity, CO2 and
# air pressure from the same device (or the only one in the same area) go
# along. Limits of the contract: 20 sensors per device and per call, 100
# readings per call over all sensors together.
MAX_ROOM_SENSORS = 20
MAX_ROOM_READINGS_PER_CALL = 100
# The server takes readings up to 29 days old; drop them an hour earlier.
ROOM_QUEUE_MAX_AGE_SECONDS = 29 * 86400 - 3600
# A few days of changes for six sensors; the queue is stored with the rest of
# the state every minute, so it is kept small.
ROOM_QUEUE_MAX_LENGTH = 5000
# A room sensor without a new value for this long becomes a repair issue.
ROOM_SILENT_SECONDS = 2 * 3600
# Device class in Home Assistant for each optional value of a room sensor.
ROOM_EXTRA_CLASSES: dict[str, str] = {
    "humidity": "humidity",
    "co2": "carbon_dioxide",
    "pressure": "atmospheric_pressure",
}
