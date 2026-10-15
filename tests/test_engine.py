"""The engine against a fake API over real HTTP: queue, retries, applied, steering."""

from datetime import timedelta

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from fake_api import KEY, FakeApi
from wm.api import AuthError, WeheatMonitorClient
from wm.engine import SyncEngine


class Thermostat:
    def __init__(self, setpoint=19.0):
        self.setpoint = setpoint
        self.writes = []

    async def set(self, value):
        self.writes.append(value)
        self.setpoint = value


@pytest.fixture
async def env():
    api = FakeApi()
    server = TestServer(api.app())
    await server.start_server()
    async with aiohttp.ClientSession() as session:
        client = WeheatMonitorClient(session, str(server.make_url("")), KEY)
        yield api, SyncEngine(client)
    await server.close()


def iso(t):
    return t.isoformat().replace("+00:00", "Z")


async def tick(engine, api, minutes, thermostat=None, fields=None, info=None):
    now = api.now + timedelta(minutes=minutes)
    return await engine.tick(
        now, fields or {"room_temp": 20.0},
        current_setpoint=thermostat.setpoint if thermostat else None,
        set_temperature=thermostat.set if thermostat else None,
        thermostat=info,
    )


async def test_one_measurement_per_minute(env):
    api, engine = env
    await tick(engine, api, 0)
    assert len(api.calls) == 1
    assert api.calls[0]["measurements"][0]["room_temp"] == 20.0
    assert api.calls[0]["measurements"][0]["source"] == "ha"
    assert engine.state["queue"] == []
    assert engine.state["last_result"] == {"stored": 1, "duplicates": 0, "skipped": 0}


async def test_queue_survives_outage_and_drains_in_batches_of_100(env):
    api, engine = env
    api.mode = "down"
    for minute in range(250):
        await tick(engine, api, minute)
    assert len(engine.state["queue"]) == 250
    assert api.rows == {}

    api.mode = "ok"
    minute = 250
    while engine.state["queue"]:
        await tick(engine, api, minute)
        minute += 1
        assert minute < 270, "backoff never ends"
    sizes = [len(c.get("measurements", [])) for c in api.calls]
    assert max(sizes) == 100
    assert sum(sizes) == len(api.rows)
    assert len(api.rows) == minute  # every minute arrived once, none lost


async def test_queue_is_capped_at_24_hours(env):
    api, engine = env
    api.mode = "down"
    for minute in range(26 * 60):
        await tick(engine, api, minute)
    assert len(engine.state["queue"]) < 24 * 60


async def test_resend_after_lost_answer_counts_as_duplicate(env):
    api, engine = env
    await tick(engine, api, 0)
    # The server stored it but the answer got lost: the same minute comes again.
    engine.state["queue"] = list(api.rows.values())
    await tick(engine, api, 1)
    assert api.calls[-1]["measurements"][0]["measured_at"] == iso(api.now)
    assert engine.state["last_result"]["duplicates"] == 1
    assert engine.state["queue"] == []


async def test_bad_key_raises_auth_error_and_keeps_queue(env):
    api, engine = env
    api.mode = "unauthorized"
    with pytest.raises(AuthError):
        await tick(engine, api, 0)
    assert len(engine.state["queue"]) == 1


async def test_follows_plan_reports_applied_and_runs_offline(env):
    api, engine = env
    engine.set_follow(True)
    t = api.now
    api.blocks = [
        {"plan_id": 11, "start": iso(t), "end": iso(t + timedelta(hours=1)), "setpoint": 21.0,
         "reason": "sun", "created_at": iso(t - timedelta(hours=1))},
        {"plan_id": 12, "start": iso(t + timedelta(hours=1)), "end": iso(t + timedelta(hours=2)),
         "setpoint": 18.0, "reason": "night", "created_at": iso(t - timedelta(hours=1))},
    ]
    th = Thermostat(19.0)
    await tick(engine, api, 0, th)
    assert th.writes == [21.0]
    await tick(engine, api, 1, th)
    assert api.applied == [11]  # reported once, with the next sync

    api.mode = "down"  # internet gone: the stored plan keeps running
    for minute in range(2, 130):
        await tick(engine, api, minute, th)
    assert th.writes == [21.0, 18.0, 19.0]  # block 12 on time, then base once

    api.mode = "ok"
    for minute in range(130, 150):
        await tick(engine, api, minute, th)
    assert 12 in api.applied


async def test_overlap_uses_newest_block(env):
    api, engine = env
    engine.set_follow(True)
    t = api.now
    api.blocks = [
        {"plan_id": 1, "start": iso(t), "end": iso(t + timedelta(hours=3)), "setpoint": 20.0,
         "reason": "a", "created_at": iso(t - timedelta(hours=3))},
        {"plan_id": 2, "start": iso(t), "end": iso(t + timedelta(hours=1)), "setpoint": 22.5,
         "reason": "b", "created_at": iso(t - timedelta(hours=1))},
    ]
    th = Thermostat(19.0)
    await tick(engine, api, 0, th)
    assert th.writes == [22.5]
    await tick(engine, api, 61, th)
    assert th.writes == [22.5, 20.0]


async def test_switch_off_or_plan_not_live_means_hands_off(env):
    api, engine = env
    t = api.now
    api.blocks = [{"plan_id": 1, "start": iso(t), "end": iso(t + timedelta(hours=1)), "setpoint": 23.0,
                   "reason": "a", "created_at": iso(t)}]
    th = Thermostat(19.0)
    await tick(engine, api, 0, th)  # switch is off by default
    engine.set_follow(True)
    api.plan_live = False
    await tick(engine, api, 1, th)
    assert th.writes == []


async def test_out_of_range_setpoint_is_clamped(env):
    api, engine = env
    engine.set_follow(True)
    t = api.now
    api.blocks = [{"plan_id": 1, "start": iso(t), "end": iso(t + timedelta(hours=1)), "setpoint": 35.0,
                   "reason": "a", "created_at": iso(t)}]
    th = Thermostat(19.0)
    await tick(engine, api, 0, th)
    assert th.writes == [24.0]


async def test_manual_change_four_hours_and_flagged_measurement(env):
    api, engine = env
    engine.set_follow(True)
    t = api.now
    api.blocks = [{"plan_id": 5, "start": iso(t), "end": iso(t + timedelta(hours=10)), "setpoint": 20.0,
                   "reason": "a", "created_at": iso(t)}]
    th = Thermostat(19.0)
    await tick(engine, api, 0, th)
    assert th.writes == [20.0]

    th.setpoint = 22.0  # a person turns the knob
    engine.manual_change(t + timedelta(minutes=1, seconds=10))
    await tick(engine, api, 2, th)
    flagged = [m for m in api.rows.values() if m.get("manual")]
    assert [m["measured_at"] for m in flagged] == [iso(t + timedelta(minutes=2))]  # only the next one

    for minute in range(3, 4 * 60 + 1):
        await tick(engine, api, minute, th)
    assert th.writes == [20.0]  # four hours: the person wins
    await tick(engine, api, 4 * 60 + 2, th)
    assert th.writes == [20.0, 20.0]  # then the plan again


async def test_failed_thermostat_write_is_retried_and_not_reported(env):
    api, engine = env
    engine.set_follow(True)
    t = api.now
    api.blocks = [{"plan_id": 3, "start": iso(t), "end": iso(t + timedelta(hours=1)), "setpoint": 21.0,
                   "reason": "a", "created_at": iso(t)}]
    th = Thermostat(19.0)
    calls = []

    async def broken(value):
        calls.append(value)
        raise RuntimeError("thermostat offline")

    await engine.tick(t, {}, current_setpoint=19.0, set_temperature=broken)
    await engine.tick(t + timedelta(minutes=1), {}, current_setpoint=19.0, set_temperature=th.set)
    assert calls == [21.0] and th.writes == [21.0]
    await tick(engine, api, 2, th)
    assert api.applied == [3]


LYRIC = {"integration": "honeywell", "manufacturer": "Honeywell", "model": "Lyric T6", "target_temp_step": 0.5}


async def test_thermostat_info_once_then_daily_or_on_change(env):
    api, engine = env
    for minute in range(3):
        await tick(engine, api, minute, info=LYRIC)
    assert api.thermostats == [LYRIC]  # first sync only, not every minute
    await tick(engine, api, 24 * 60 + 1, info=LYRIC)
    assert len(api.thermostats) == 2  # again after a day
    other = dict(LYRIC, model="Lyric T6R")
    await tick(engine, api, 24 * 60 + 2, info=other)
    assert api.thermostats[-1] == other  # and right away after a change


async def test_thermostat_info_retried_after_failed_sync(env):
    api, engine = env
    api.mode = "down"
    await tick(engine, api, 0, info=LYRIC)
    assert api.thermostats == []
    api.mode = "ok"
    for minute in range(1, 5):
        await tick(engine, api, minute, info=LYRIC)
    assert api.thermostats == [LYRIC]


async def test_no_thermostat_info_without_thermostat(env):
    api, engine = env
    await tick(engine, api, 0)
    assert api.thermostats == [] and "thermostat" not in api.calls[0]
