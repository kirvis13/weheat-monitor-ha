"""One tick per minute: queue a measurement, sync, then steer the thermostat.

Home Assistant-free on purpose: the coordinator hands in the sensor values and
a callback that writes the thermostat, the tests hand in a fake API and a fake
thermostat. All persistent state lives in `self.state`, a plain dict the
coordinator saves with homeassistant.helpers.storage.Store.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from .api import ApiError, AuthError, BadRequestError
from .const import (
    MAX_APPLIED_PER_CALL,
    MAX_BATCHES_PER_TICK,
    MAX_MEASUREMENTS_PER_CALL,
    THERMOSTAT_REPORT_SECONDS,
)
from .logic import (
    Decision,
    build_measurement,
    decide,
    enqueue,
    initial_control,
    iso,
    manual_override,
    parse_time,
    pick_block,
    prune,
)

_LOGGER = logging.getLogger(__package__)

# Backoff after a failed sync: 1, 2, 4, 8, then every 15 minutes.
MAX_BACKOFF_SECONDS = 15 * 60

SetTemperature = Callable[[float], Awaitable[None]]


def initial_state() -> dict[str, Any]:
    return {
        "queue": [],
        "pending_applied": [],
        "applied_ids": [],  # recently reported plan_ids, so each is reported once
        "blocks": [],
        "plan_live": False,
        "base_setpoint": None,
        "follow": False,
        "control": initial_control(),
        "last_sync": None,
        "last_result": None,
        "last_error": None,
        "failures": 0,
        "retry_at": None,
        "last_decision": None,
        "thermostat_hash": None,  # what was last reported about the thermostat
        "thermostat_at": None,  # and when
    }


class SyncEngine:
    def __init__(self, client: Any, state: dict[str, Any] | None = None) -> None:
        self.client = client
        self.state = initial_state()
        if state:
            self.state.update(state)
            self.state["control"] = {**initial_control(), **(state.get("control") or {})}

    # ─── Events from Home Assistant ─────────────────────────────────────────

    def manual_change(self, now: datetime) -> None:
        self.state["control"] = manual_override(self.state["control"], now)

    def set_follow(self, on: bool) -> None:
        self.state["follow"] = bool(on)

    @property
    def last_set(self) -> float | None:
        return self.state["control"].get("last_set")

    def active_block(self, now: datetime) -> dict[str, Any] | None:
        if not self.state["plan_live"]:
            return None
        return pick_block(self.state["blocks"], now)

    # ─── The tick ───────────────────────────────────────────────────────────

    async def tick(
        self,
        now: datetime,
        fields: dict[str, float | None],
        *,
        current_setpoint: float | None = None,
        set_temperature: SetTemperature | None = None,
        quantize: Callable[[float], float] | None = None,
        thermostat: dict[str, Any] | None = None,
    ) -> Decision | None:
        """Queue this minute's measurement, sync, then steer.

        Raises AuthError (the coordinator starts a reauth); every other
        failure is kept in state and retried on a later tick.
        """
        control = dict(self.state["control"])
        manual = bool(control.get("manual_pending"))
        control["manual_pending"] = False
        self.state["control"] = control
        self.state["queue"] = enqueue(self.state["queue"], build_measurement(now, fields, manual), now)

        await self._sync(now, self._thermostat_due(thermostat, now))

        if set_temperature is None:
            return None
        return await self._steer(now, current_setpoint, set_temperature, quantize)

    def _thermostat_due(self, thermostat: dict[str, Any] | None, now: datetime) -> dict[str, Any] | None:
        """The thermostat info, if it changed or was last reported a day ago."""
        if not thermostat:
            return None
        at = self.state.get("thermostat_at")
        if (
            self.state.get("thermostat_hash") == _fingerprint(thermostat)
            and at
            and parse_time(at) > now - timedelta(seconds=THERMOSTAT_REPORT_SECONDS)
        ):
            return None
        return thermostat

    async def _sync(self, now: datetime, thermostat: dict[str, Any] | None = None) -> None:
        retry_at = self.state.get("retry_at")
        if retry_at and parse_time(retry_at) > now:
            return
        for _ in range(MAX_BATCHES_PER_TICK):
            batch = self.state["queue"][:MAX_MEASUREMENTS_PER_CALL]
            applied = self.state["pending_applied"][:MAX_APPLIED_PER_CALL]
            try:
                answer = await self.client.sync(batch, applied, thermostat=thermostat)
            except AuthError:
                self.state["last_error"] = "invalid_auth"
                raise
            except BadRequestError as err:
                # The server will never take this batch; keeping it would block the queue.
                _LOGGER.warning("Weheat Monitor rejected %d measurements: %s", len(batch), err)
                self.state["queue"] = self.state["queue"][len(batch):]
                self.state["pending_applied"] = self.state["pending_applied"][len(applied):]
                self.state["last_error"] = str(err)
                if not self.state["queue"] and not self.state["pending_applied"]:
                    return
                continue
            except ApiError as err:
                failures = int(self.state.get("failures") or 0) + 1
                delay = min(60 * 2 ** (failures - 1), MAX_BACKOFF_SECONDS)
                self.state["failures"] = failures
                self.state["retry_at"] = iso(now + timedelta(seconds=delay - 1))
                self.state["last_error"] = str(err)
                _LOGGER.debug(
                    "Sync failed (%s), %d measurements queued, retry in %d s",
                    err, len(self.state["queue"]), delay,
                )
                return

            self.state["queue"] = self.state["queue"][len(batch):]
            self.state["pending_applied"] = self.state["pending_applied"][len(applied):]
            self._store_answer(answer, now)
            if thermostat:
                self.state["thermostat_hash"] = _fingerprint(thermostat)
                self.state["thermostat_at"] = iso(now)
                thermostat = None
            if not self.state["queue"] and not self.state["pending_applied"]:
                return

    def _store_answer(self, answer: dict[str, Any], now: datetime) -> None:
        self.state["failures"] = 0
        self.state["retry_at"] = None
        self.state["last_error"] = None
        self.state["last_sync"] = iso(now)
        self.state["last_result"] = {
            k: answer.get(k) for k in ("stored", "duplicates", "skipped")
        }
        if isinstance(answer.get("plan_live"), bool):
            self.state["plan_live"] = answer["plan_live"]
        base = answer.get("base_setpoint")
        if isinstance(base, (int, float)):
            self.state["base_setpoint"] = float(base)
        blocks = answer.get("blocks")
        if isinstance(blocks, list):
            self.state["blocks"] = [b for b in blocks if isinstance(b, dict)]
        # Old blocks are useless; keep the stored list small.
        self.state["blocks"] = [
            b for b in self.state["blocks"]
            if _safe_end(b) is None or _safe_end(b) > now
        ]

    async def _steer(
        self,
        now: datetime,
        current: float | None,
        set_temperature: SetTemperature,
        quantize: Callable[[float], float] | None,
    ) -> Decision:
        decision, control = decide(
            self.state["control"],
            now,
            follow=self.state["follow"],
            plan_live=self.state["plan_live"],
            blocks=self.state["blocks"],
            base_setpoint=self.state["base_setpoint"],
            current=current,
            quantize=quantize,
        )
        self.state["control"] = control
        self.state["last_decision"] = decision.reason
        if decision.target is not None:
            try:
                await set_temperature(decision.target)
            except Exception as err:  # noqa: BLE001 - the thermostat integration may raise anything
                _LOGGER.warning("Setting the thermostat to %.1f failed: %s", decision.target, err)
                self.state["last_decision"] = f"{decision.reason} (failed)"
                return decision
        if decision.plan_id is not None and decision.plan_id not in self.state["applied_ids"]:
            self.state["pending_applied"].append(decision.plan_id)
            self.state["applied_ids"] = (self.state["applied_ids"] + [decision.plan_id])[-50:]
        return decision

    def snapshot_queue(self, now: datetime) -> None:
        """Drop expired measurements (also called on start, after a long outage)."""
        self.state["queue"] = prune(self.state["queue"], now)


def _fingerprint(thermostat: dict[str, Any]) -> str:
    return json.dumps(thermostat, sort_keys=True)


def _safe_end(block: dict[str, Any]) -> datetime | None:
    try:
        return parse_time(block["end"])
    except (KeyError, TypeError, ValueError):
        return None
