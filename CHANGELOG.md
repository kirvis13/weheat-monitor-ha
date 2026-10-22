# Changelog

Versions follow [Semantic Versioning](https://semver.org). The newest version is on top.

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
