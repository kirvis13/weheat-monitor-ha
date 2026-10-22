"""Pure decision rules: queue, block choice, clamping and the manual-override rule.

No Home Assistant imports and no I/O: everything here is tested directly.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .const import (
    MANUAL_PAUSE_SECONDS,
    MAX_SETPOINT,
    MIN_SETPOINT,
    QUEUE_MAX_AGE_SECONDS,
    QUEUE_MAX_LENGTH,
    SETPOINT_TOLERANCE,
    SOURCE,
)


def parse_time(value: str) -> datetime:
    """ISO time, also with a trailing 'Z'; naive values are taken as UTC."""
    t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def clamp(setpoint: float) -> float:
    """Every setpoint this integration writes lies between 12 and 24 °C."""
    return max(MIN_SETPOINT, min(MAX_SETPOINT, float(setpoint)))


# ─── Measurements and the queue ─────────────────────────────────────────────


def build_measurement(now: datetime, fields: dict[str, float | None], manual: bool) -> dict[str, Any]:
    """One measurement for the current minute. Missing values are left out."""
    m: dict[str, Any] = {
        "measured_at": iso(now.replace(second=0, microsecond=0)),
        "source": SOURCE,
    }
    for key, value in fields.items():
        if value is not None:
            m[key] = round(value, 4)
    if manual:
        m["manual"] = True
    return m


def enqueue(queue: list[dict[str, Any]], measurement: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    """Add a measurement and drop what the server would no longer accept.

    A second measurement in the same minute replaces the first (the server
    keeps only one per minute), but a manual flag is never lost.
    """
    q = list(queue)
    if q and q[-1].get("measured_at") == measurement["measured_at"]:
        manual = q[-1].get("manual") or measurement.get("manual")
        measurement = dict(measurement)
        if manual:
            measurement["manual"] = True
        q[-1] = measurement
    else:
        q.append(measurement)
    return prune(q, now)


def prune(queue: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """Keep at most 24 hours and at most QUEUE_MAX_LENGTH measurements."""
    cutoff = now - timedelta(seconds=QUEUE_MAX_AGE_SECONDS)
    kept = []
    for m in queue:
        try:
            if parse_time(m["measured_at"]) > cutoff:
                kept.append(m)
        except (KeyError, TypeError, ValueError):
            continue
    return kept[-QUEUE_MAX_LENGTH:]


# ─── Blocks ─────────────────────────────────────────────────────────────────


def pick_block(blocks: list[dict[str, Any]], now: datetime) -> dict[str, Any] | None:
    """The block that applies now: start <= now < end; on overlap the newest created_at."""
    best = None
    best_created = None
    for b in blocks or []:
        try:
            start, end = parse_time(b["start"]), parse_time(b["end"])
            created = parse_time(b["created_at"]) if b.get("created_at") else datetime.min.replace(tzinfo=timezone.utc)
            float(b["setpoint"])
        except (KeyError, TypeError, ValueError):
            continue
        if start <= now < end and (best_created is None or created > best_created):
            best, best_created = b, created
    return best


# ─── Steering ───────────────────────────────────────────────────────────────


@dataclass
class Decision:
    target: float | None  # setpoint to write (°C), or None
    plan_id: int | None  # block in effect, to report via `applied`
    reason: str


def initial_control() -> dict[str, Any]:
    return {
        "last_set": None,  # what this integration last wrote (°C)
        "steering": False,  # True while a block from the plan is in effect
        "plan_id": None,
        "manual_until": None,  # ISO time; no steering before this
        "manual_pending": False,  # next measurement carries manual: true
    }


def is_manual_change(old: float | None, new: float | None, last_set: float | None, own_context: bool) -> bool:
    """Did someone else (a person or another automation) change the setpoint?

    Our own service call is recognised by its context. A cloud thermostat may
    report our value a minute later under a fresh context, so a new value equal
    to what we last wrote never counts either.
    """
    if new is None or own_context:
        return False
    if old is not None and abs(new - old) < SETPOINT_TOLERANCE:
        return False
    if last_set is not None and abs(new - last_set) < SETPOINT_TOLERANCE:
        return False
    return True


def manual_override(control: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Someone changed the thermostat: four hours hands off, flag the next measurement."""
    c = dict(control)
    c["manual_until"] = iso(now + timedelta(seconds=MANUAL_PAUSE_SECONDS))
    c["manual_pending"] = True
    c["steering"] = False
    c["plan_id"] = None
    c["last_set"] = None
    return c


def decide(
    control: dict[str, Any],
    now: datetime,
    *,
    follow: bool,
    blocks: list[dict[str, Any]],
    base_setpoint: float | None,
    current: float | None,
    quantize: Callable[[float], float] | None = None,
) -> tuple[Decision, dict[str, Any]]:
    """What should the integration write to the thermostat now?

    Steers only when the user's "Follow plan" switch is on. The blocks are
    trusted as the server sends them: with plan_live off it sends only the
    blocks of a thermostat test (an older server sends none), so plan_live is
    no gate here. Without a valid block it returns to base_setpoint once and
    then leaves the thermostat alone. `quantize` rounds a target to what the
    thermostat can show (its step and limits); the result is clamped again,
    so nothing outside 12-24 °C ever leaves this function.
    """
    c = dict(control)

    def target_for(setpoint: float) -> float:
        t = clamp(setpoint)
        return clamp(quantize(t)) if quantize else t

    until = c.get("manual_until")
    if until and parse_time(until) > now:
        return Decision(None, None, "manual pause"), c
    c["manual_until"] = None

    if not follow:
        c["steering"] = False
        c["plan_id"] = None
        return Decision(None, None, "follow plan is off"), c

    block = pick_block(blocks, now)
    if block is not None:
        target = target_for(block["setpoint"])
        plan_id = block.get("plan_id")
        c["steering"] = True
        c["plan_id"] = plan_id
        c["last_set"] = target
        if current is None or abs(current - target) >= SETPOINT_TOLERANCE:
            return Decision(target, plan_id, f"plan {plan_id}"), c
        return Decision(None, plan_id, f"plan {plan_id} already set"), c

    if c.get("steering"):
        c["steering"] = False
        c["plan_id"] = None
        if base_setpoint is None:
            c["last_set"] = None
            return Decision(None, None, "no plan, no base setpoint"), c
        base = target_for(base_setpoint)
        c["last_set"] = base
        if current is not None and abs(current - base) < SETPOINT_TOLERANCE:
            return Decision(None, None, "no plan, already at base"), c
        return Decision(base, None, "no plan, back to base"), c

    return Decision(None, None, "no plan"), c


# ─── Status ─────────────────────────────────────────────────────────────────

PLAN_STATUSES = [
    "test_paused",
    "test_scheduled",
    "test_waiting",
    "test_step",
    "test_rest",
    "follow_off",
    "following",
    "plan_idle",
    "no_plan",
]


def plan_status(state: dict[str, Any], now: datetime) -> str:
    """Why the thermostat does (or does not) follow a block now: the first rule that fits."""
    block = pick_block(state.get("blocks") or [], now)
    test = state.get("test")
    if isinstance(test, dict):
        status = test.get("status")
        if status == "paused":
            return "test_paused"
        if status == "planned":
            try:
                planned_at = parse_time(test["planned_at"])
            except (KeyError, TypeError, ValueError, AttributeError):
                planned_at = None
            return "test_scheduled" if planned_at is not None and planned_at > now else "test_waiting"
        if status == "running":
            return "test_step" if block is not None else "test_rest"
    if block is not None:
        return "following" if state.get("follow") else "follow_off"
    if state.get("plan_live"):
        return "plan_idle"
    return "no_plan"


def next_block(blocks: list[dict[str, Any]], now: datetime) -> dict[str, Any] | None:
    """The block that starts next: start > now, the earliest start."""
    best = None
    best_start = None
    for b in blocks or []:
        try:
            start = parse_time(b["start"])
            float(b["setpoint"])
        except (KeyError, TypeError, ValueError):
            continue
        if start > now and (best_start is None or start < best_start):
            best, best_start = b, start
    return best
