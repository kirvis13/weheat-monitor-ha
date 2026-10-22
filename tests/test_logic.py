"""Block choice, the 12-24 limit and the four-hour rule, without any I/O."""

from datetime import datetime, timedelta, timezone

from wm.const import MAX_SETPOINT, MIN_SETPOINT
from wm.logic import (
    build_measurement,
    clamp,
    decide,
    enqueue,
    initial_control,
    is_manual_change,
    manual_override,
    next_block,
    pick_block,
    plan_status,
)
from wm.units import convert, parse_number

T0 = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


def iso(t):
    return t.isoformat().replace("+00:00", "Z")


def block(plan_id, start, end, setpoint, created):
    return {"plan_id": plan_id, "start": iso(start), "end": iso(end), "setpoint": setpoint,
            "reason": "test", "created_at": iso(created)}


def run(control, now, blocks=(), current=19.0, follow=True, base=19.0, quantize=None):
    return decide(control, now, follow=follow, blocks=list(blocks),
                  base_setpoint=base, current=current, quantize=quantize)


# ─── Block choice ───────────────────────────────────────────────────────────

def test_pick_block_inside_window_only():
    b = block(1, T0, T0 + timedelta(hours=1), 20, T0 - timedelta(hours=1))
    assert pick_block([b], T0) is b  # start is inclusive
    assert pick_block([b], T0 - timedelta(seconds=1)) is None
    assert pick_block([b], T0 + timedelta(hours=1)) is None  # end is exclusive


def test_pick_block_overlap_newest_created_at_wins():
    old = block(1, T0 - timedelta(hours=1), T0 + timedelta(hours=3), 20, T0 - timedelta(hours=5))
    new = block(2, T0, T0 + timedelta(hours=1), 18, T0 - timedelta(hours=2))
    older_again = block(3, T0, T0 + timedelta(hours=2), 22, T0 - timedelta(hours=9))
    assert pick_block([old, new, older_again], T0 + timedelta(minutes=30))["plan_id"] == 2
    # After the newest one ends, the next newest of the remaining two applies.
    assert pick_block([old, new, older_again], T0 + timedelta(minutes=90))["plan_id"] == 1
    assert pick_block([new, older_again], T0 + timedelta(minutes=90))["plan_id"] == 3


def test_pick_block_ignores_broken_blocks():
    good = block(1, T0, T0 + timedelta(hours=1), 20, T0)
    assert pick_block([{"plan_id": 9}, {"start": "x", "end": "y", "setpoint": 1}, good], T0) is good


# ─── The 12-24 limit ───────────────────────────────────────────────────────

def test_clamp():
    assert clamp(30) == MAX_SETPOINT == 24.0
    assert clamp(5) == MIN_SETPOINT == 12.0
    assert clamp(19.5) == 19.5


def test_plan_setpoints_are_clamped():
    hot = block(1, T0, T0 + timedelta(hours=1), 30, T0)
    cold = block(2, T0, T0 + timedelta(hours=1), 3, T0)
    assert run(initial_control(), T0, [hot])[0].target == 24.0
    assert run(initial_control(), T0, [cold])[0].target == 12.0


def test_base_setpoint_is_clamped():
    b = block(1, T0, T0 + timedelta(hours=1), 20, T0)
    _, c = run(initial_control(), T0, [b], current=19.0)
    d, _ = run(c, T0 + timedelta(hours=2), [b], current=20.0, base=40)
    assert d.target == 24.0


def test_quantize_cannot_escape_the_limit():
    b = block(1, T0, T0 + timedelta(hours=1), 23.9, T0)
    d, _ = run(initial_control(), T0, [b], quantize=lambda t: t + 5)
    assert d.target == 24.0
    d, _ = run(initial_control(), T0, [b], quantize=lambda t: round(t * 2) / 2)
    assert d.target == 24.0


# ─── Steering ───────────────────────────────────────────────────────────────

def test_follows_block_then_back_to_base_once():
    b = block(7, T0, T0 + timedelta(hours=1), 20.5, T0)
    d, c = run(initial_control(), T0, [b], current=19.0)
    assert (d.target, d.plan_id) == (20.5, 7)
    d, c = run(c, T0 + timedelta(minutes=1), [b], current=20.5)
    assert d.target is None and d.plan_id == 7  # already set, still in effect
    d, c = run(c, T0 + timedelta(hours=1), [b], current=20.5)
    assert d.target == 19.0  # block over: back to base
    d, c = run(c, T0 + timedelta(hours=1, minutes=1), [b], current=19.0)
    assert d.target is None
    d, c = run(c, T0 + timedelta(hours=2), [], current=21.0)
    assert d.target is None  # someone else's 21 is left alone afterwards


def test_no_steering_without_switch():
    b = block(1, T0, T0 + timedelta(hours=1), 21, T0)
    assert run(initial_control(), T0, [b], follow=False)[0].target is None


def test_blocks_steer_without_plan_live():
    # With plan_live off the server sends only test blocks; those must steer.
    b = block(1, T0, T0 + timedelta(hours=1), 21, T0)
    d, _ = run(initial_control(), T0, [b])
    assert (d.target, d.plan_id) == (21.0, 1)


def test_plan_live_off_while_steering_returns_to_base():
    # Plan switched off on the site: the server stops sending its blocks.
    b = block(1, T0, T0 + timedelta(hours=1), 21, T0)
    _, c = run(initial_control(), T0, [b])
    d, _ = run(c, T0 + timedelta(minutes=5), [], current=21)
    assert d.target == 19.0


def test_never_steered_never_returns_to_base():
    assert run(initial_control(), T0, [], current=22.0)[0].target is None


# ─── The four-hour rule ─────────────────────────────────────────────────────

def test_manual_change_pauses_four_hours():
    b = block(1, T0 - timedelta(hours=1), T0 + timedelta(hours=8), 20, T0 - timedelta(hours=2))
    _, c = run(initial_control(), T0, [b], current=19.0)
    c = manual_override(c, T0 + timedelta(minutes=1))
    assert c["manual_pending"] is True
    for minutes in (2, 60, 120, 240):
        d, c = run(c, T0 + timedelta(minutes=minutes), [b], current=22.0)
        assert d.target is None, minutes
        assert d.reason == "manual pause"
    d, c = run(c, T0 + timedelta(hours=4, minutes=2), [b], current=22.0)
    assert d.target == 20.0
    assert c["manual_until"] is None


def test_manual_pause_also_blocks_return_to_base():
    b = block(1, T0, T0 + timedelta(hours=1), 21, T0)
    _, c = run(initial_control(), T0, [b])
    c = manual_override(c, T0 + timedelta(minutes=10))
    d, c = run(c, T0 + timedelta(hours=2), [], current=17)
    assert d.target is None  # block ended during the pause, base would be 19
    d, c = run(c, T0 + timedelta(hours=5), [], current=17)
    assert d.target is None  # after the pause: the person's 17 stays


def test_is_manual_change():
    assert is_manual_change(19.0, 21.0, None, own_context=False)
    assert not is_manual_change(19.0, 21.0, None, own_context=True)  # our own call
    assert not is_manual_change(19.0, 20.5, 20.5, own_context=False)  # our value, late, new context
    assert not is_manual_change(19.0, 19.01, None, own_context=False)  # rounding
    assert not is_manual_change(19.0, None, None, own_context=False)  # unavailable


# ─── Measurements and queue ─────────────────────────────────────────────────

def test_measurement_leaves_out_missing_values():
    m = build_measurement(T0 + timedelta(seconds=42), {"room_temp": 20.1, "grid_w": None}, manual=False)
    assert m == {"measured_at": "2026-11-01T12:00:00Z", "source": "ha", "room_temp": 20.1}
    assert build_measurement(T0, {}, manual=True)["manual"] is True


def test_queue_keeps_24_hours_and_merges_a_minute():
    q = []
    for i in range(30 * 60):
        t = T0 + timedelta(minutes=i)
        q = enqueue(q, build_measurement(t, {}, False), t)
    assert len(q) < 24 * 60
    now = T0 + timedelta(minutes=30 * 60 - 1)
    oldest = datetime.fromisoformat(q[0]["measured_at"].replace("Z", "+00:00"))
    assert now - oldest < timedelta(hours=24)

    q = enqueue([], build_measurement(T0, {}, True), T0)
    q = enqueue(q, build_measurement(T0 + timedelta(seconds=30), {"room_temp": 20}, False), T0)
    assert len(q) == 1 and q[0]["manual"] is True and q[0]["room_temp"] == 20


def test_units():
    assert parse_number("unavailable") is None and parse_number("unknown") is None
    assert parse_number("0") == 0.0
    assert convert(1.5, "power", "kW") == 1500
    assert convert(2500, "energy", "Wh") == 2.5
    assert convert(68, "temperature", "°F") == 20.0
    assert convert(12, "power", "VA") is None  # unknown unit: better nothing than wrong


# ─── Plan status ────────────────────────────────────────────────────────────

NOW_BLOCK = block(1, T0 - timedelta(minutes=10), T0 + timedelta(minutes=20), 21, T0 - timedelta(hours=1))
LATER = block(2, T0 + timedelta(hours=1), T0 + timedelta(hours=2), 30, T0 - timedelta(hours=1))


def status(test=None, blocks=(), follow=True, plan_live=True, now=T0):
    return plan_status({"test": test, "blocks": list(blocks), "follow": follow, "plan_live": plan_live}, now)


def test_status_every_row():
    assert status({"status": "paused", "pause_reason": "x"}) == "test_paused"
    assert status({"status": "planned", "planned_at": iso(T0 + timedelta(hours=1))}) == "test_scheduled"
    assert status({"status": "planned", "planned_at": iso(T0 - timedelta(hours=1))}) == "test_waiting"
    assert status({"status": "running", "phase": "A"}, [NOW_BLOCK]) == "test_step"
    assert status({"status": "running", "phase": "B"}, [LATER]) == "test_rest"
    assert status(None, [NOW_BLOCK], follow=False) == "follow_off"
    assert status(None, [NOW_BLOCK]) == "following"
    assert status(None, [LATER]) == "plan_idle"
    assert status(None, [LATER], plan_live=False) == "no_plan"


def test_status_order_on_overlap():
    # A paused or planned test wins over a block that applies now, and over the switch.
    assert status({"status": "paused"}, [NOW_BLOCK], follow=False) == "test_paused"
    assert status({"status": "planned", "planned_at": iso(T0 + timedelta(hours=1))}, [NOW_BLOCK]) == "test_scheduled"
    # A running test step with Follow plan off sends nothing: say so.
    assert status({"status": "running"}, [NOW_BLOCK], follow=False) == "follow_off"
    assert status({"status": "running"}, [LATER], follow=False) == "test_rest"
    # A block from a test while plan_live is off is still followed.
    assert status(None, [NOW_BLOCK], plan_live=False) == "following"


def test_status_planned_at_equal_to_now_is_waiting():
    assert status({"status": "planned", "planned_at": iso(T0)}) == "test_waiting"
    assert status({"status": "planned", "planned_at": None}) == "test_waiting"


def test_status_without_test_field_old_server():
    assert plan_status({"blocks": [NOW_BLOCK], "follow": True, "plan_live": True}, T0) == "following"
    assert plan_status({"blocks": [], "plan_live": False}, T0) == "no_plan"
    assert status("garbage", [LATER]) == "plan_idle"


def test_next_block_earliest_future_start():
    soon = block(3, T0 + timedelta(minutes=30), T0 + timedelta(hours=1), 19, T0)
    assert next_block([LATER, NOW_BLOCK, soon], T0) is soon
    assert next_block([NOW_BLOCK], T0) is None  # already started
    starts_now = block(4, T0, T0 + timedelta(hours=1), 19, T0)
    assert next_block([starts_now], T0) is None  # start must be later than now
    assert next_block([{"start": "x", "setpoint": 1}, LATER], T0) is LATER
