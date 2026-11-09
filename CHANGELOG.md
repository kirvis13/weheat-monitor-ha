# Changelog

Versions follow [Semantic Versioning](https://semver.org). The newest version is on top.

## 0.3.0 (2026-11-09)

- **Room sensors.** A new step in the setup and in **Configure**: pick temperature sensors in other rooms (up to 20). Each one goes to weheat-monitor.nl as a room, named after its area, with its floor. Humidity, CO2 and air pressure of the same device, or the only one in the same area, go along. A last step shows what will be sent.
- Only new values are sent, stamped with the time the sensor changed; a reading is never repeated.
- New sensor **Rooms** (`sensor.weheat_monitor_rooms`): how many room sensors reported in the last two hours, with the values per room in the attributes.
- New switch **Send rooms** (`switch.weheat_monitor_send_rooms`), on by default.
- A room sensor that stays silent for two hours becomes a repair issue, and disappears once it reports again.
- Room readings have their own queue (up to 5,000 readings, 29 days). A server without support for rooms keeps them waiting instead of losing them.

## 0.2.0 (2026-10-22)

- New sensor **Status** (`sensor.weheat_monitor_status`): says why the thermostat does or does not follow a block now, including where a thermostat test is (scheduled, waiting for the heat pump, step, rest, paused). Attributes for the next block and the test.
- "Active setpoint" gets the attribute `next_block_start`.
- Fix: a thermostat test now also steers when the plan is not live on the site. The server then sends only the test blocks, and **Follow plan** stays the local switch.

## 0.1.0 (2026-10-15)

First release.

- Connects one thermostat (`climate` entity) to weheat-monitor.nl: room temperature and setpoint every minute, optionally from a separate room sensor.
- Reports which thermostat it is (integration, make, model, step, limits) at start-up, after a change and once a day.
- Queue of up to 24 hours when offline, sent in batches of 100.
- Stores the 48-hour plan locally and, when switched on, lets the thermostat follow it: 12-24 °C, newest block wins, back to the base setpoint once, 4-hour pause after a manual change.
- Config flow tests the key right away; options flow to change the thermostat; re-authentication when the key is revoked.
