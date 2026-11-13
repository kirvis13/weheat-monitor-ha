"""Room sensors: key, name, which values go along, the queue, the `rooms` field, and the engine against the fake API."""

from datetime import datetime, timedelta, timezone

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from fake_api import KEY, FakeApi
from wm.api import WeheatMonitorClient
from wm.const import MAX_ROOM_READINGS_PER_CALL, MAX_ROOM_SENSORS, ROOM_QUEUE_MAX_LENGTH
from wm.engine import SyncEngine
from wm.rooms import (
    build_rooms_payload,
    enqueue_room_readings,
    floor_name,
    is_silent,
    pick_companions,
    room_key,
    room_name,
)
from wm.units import convert

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


def iso(t):
    return t.isoformat().replace("+00:00", "Z")


def snap(key="aaaaaaaaaaaaaaaa", name="Slaapkamer", minutes=0, temp=17.2, **extra):
    return {"key": key, "name": name, "floor": extra.pop("floor", None), "measured_at": iso(NOW + timedelta(minutes=minutes)),
            "temp": temp, **extra}


# ─── Key, name and companions ───────────────────────────────────────────────


def test_key_is_a_stable_hash_that_fits_the_contract():
    k = room_key("sensor.slaapkamer_temperatuur")
    assert k == room_key("sensor.slaapkamer_temperatuur")
    assert k != room_key("sensor.keuken_temperatuur")
    assert len(k) == 16 and all(c in "0123456789abcdef" for c in k)
    assert "slaapkamer" not in k


def test_name_is_the_area_else_the_sensor_name_without_temperature():
    assert room_name("Slaapkamer achter", "Whatever") == "Slaapkamer achter"
    assert room_name(None, "Zolder Temperatuur") == "Zolder"
    assert room_name("", "Kinderkamer temperature") == "Kinderkamer"
    assert room_name(None, "Badkamer_temp") == "Badkamer"
    assert room_name(None, None) == "Ruimte"
    assert room_name("  Woon   kamer  ", None) == "Woon kamer"
    assert len(room_name("W" * 60, None)) == 40
    assert floor_name("  Eerste   verdieping ") == "Eerste verdieping"
    assert floor_name("") is None


def test_companions_same_device_first_then_the_only_one_in_the_area():
    target = {"device_id": "dev1", "area_id": "slaap"}
    candidates = [
        {"entity_id": "sensor.slaap_vocht", "kind": "humidity", "device_id": "dev1", "area_id": "slaap"},
        {"entity_id": "sensor.ander_vocht", "kind": "humidity", "device_id": "dev2", "area_id": "slaap"},
        {"entity_id": "sensor.co2_meter", "kind": "co2", "device_id": "dev3", "area_id": "slaap"},
        {"entity_id": "sensor.druk_woon", "kind": "pressure", "device_id": "dev4", "area_id": "woon"},
    ]
    assert pick_companions(target, candidates) == {"humidity": "sensor.slaap_vocht", "co2": "sensor.co2_meter"}


def test_two_in_the_area_and_none_on_the_device_gives_nothing():
    target = {"device_id": "dev1", "area_id": "slaap"}
    candidates = [
        {"entity_id": "sensor.a", "kind": "humidity", "device_id": "dev2", "area_id": "slaap"},
        {"entity_id": "sensor.b", "kind": "humidity", "device_id": "dev3", "area_id": "slaap"},
    ]
    assert pick_companions(target, candidates) == {}
    assert pick_companions({"device_id": None, "area_id": None}, candidates) == {}


def test_units_for_co2_and_pressure():
    assert convert(1013.2, "pressure", "hPa") == 1013.2
    assert convert(1013.2, "pressure", "mbar") == 1013.2
    assert convert(101320, "pressure", "Pa") == 1013.2
    assert convert(29.92, "pressure", "inHg") == 1013.2
    assert convert(1013.2, "pressure", "") is None
    assert convert(640, "co2", "ppm") == 640
    assert convert(0.064, "co2", "%") is None


def test_silent_after_two_hours():
    assert not is_silent(NOW - timedelta(minutes=119), NOW)
    assert is_silent(NOW - timedelta(minutes=121), NOW)
    assert is_silent(None, NOW)


# ─── The queue and the payload ──────────────────────────────────────────────


def test_only_new_values_are_queued():
    q, last = enqueue_room_readings([], {}, [snap(minutes=0, humidity=63.04)], NOW)
    assert q == [{"key": "aaaaaaaaaaaaaaaa", "measured_at": iso(NOW), "temp": 17.2, "humidity": 63.04}]
    # Same last change again (the minute ticked, the sensor did not report): nothing new.
    q, last = enqueue_room_readings(q, last, [snap(minutes=0)], NOW + timedelta(minutes=1))
    assert len(q) == 1
    q, last = enqueue_room_readings(q, last, [snap(minutes=3, temp=17.1)], NOW + timedelta(minutes=3))
    assert [r["temp"] for r in q] == [17.2, 17.1]


def test_unavailable_sensor_adds_nothing():
    q, last = enqueue_room_readings([], {}, [snap(temp=None)], NOW)
    assert q == [] and last == {}


def test_queue_drops_removed_sensors_too_old_and_above_the_cap():
    old = {"key": "aaaaaaaaaaaaaaaa", "measured_at": iso(NOW - timedelta(days=30)), "temp": 18}
    gone = {"key": "bbbbbbbbbbbbbbbb", "measured_at": iso(NOW), "temp": 18}
    q, _ = enqueue_room_readings([old, gone], {}, [snap(minutes=0)], NOW)
    assert [r["measured_at"] for r in q] == [iso(NOW)]
    many = [{"key": "aaaaaaaaaaaaaaaa", "measured_at": iso(NOW - timedelta(minutes=i)), "temp": 18}
            for i in range(ROOM_QUEUE_MAX_LENGTH + 10, 0, -1)]
    q, _ = enqueue_room_readings(many, {}, [snap(minutes=0)], NOW)
    assert len(q) == ROOM_QUEUE_MAX_LENGTH and q[-1]["measured_at"] == iso(NOW)


def test_payload_sends_every_sensor_and_at_most_100_readings_oldest_first():
    meta = {"k1aaaaaaaaaaaaaa": {"name": "Slaapkamer", "floor": "1"}, "k2aaaaaaaaaaaaaa": {"name": "Keuken", "floor": None}}
    queue = [{"key": "k1aaaaaaaaaaaaaa", "measured_at": iso(NOW + timedelta(minutes=i)), "temp": 17} for i in range(150)]
    payload, taken = build_rooms_payload(meta, queue)
    assert taken == MAX_ROOM_READINGS_PER_CALL
    assert [p["key"] for p in payload] == ["k1aaaaaaaaaaaaaa", "k2aaaaaaaaaaaaaa"]
    assert payload[0]["floor"] == "1" and "floor" not in payload[1]
    assert payload[0]["readings"][0] == {"measured_at": iso(NOW), "temp": 17}
    assert payload[1]["readings"] == []


def test_payload_never_more_than_20_sensors():
    meta = {f"k{i:015d}": {"name": f"R{i}", "floor": None} for i in range(25)}
    payload, _ = build_rooms_payload(meta, [])
    assert len(payload) == MAX_ROOM_SENSORS


# ─── The engine against the fake API ────────────────────────────────────────


@pytest.fixture
async def env():
    api = FakeApi()
    server = TestServer(api.app())
    await server.start_server()
    async with aiohttp.ClientSession() as session:
        client = WeheatMonitorClient(session, str(server.make_url("")), KEY)
        yield api, SyncEngine(client)
    await server.close()


async def tick(engine, api, minutes, rooms):
    return await engine.tick(api.now + timedelta(minutes=minutes), {"room_temp": 20.0}, rooms=rooms)


async def test_rooms_go_along_with_the_sync_and_leave_the_queue(env):
    api, engine = env
    await tick(engine, api, 0, [snap(minutes=0, humidity=63, floor="1")])
    sent = api.calls[-1]["rooms"]
    assert sent == [{"key": "aaaaaaaaaaaaaaaa", "name": "Slaapkamer", "floor": "1",
                     "readings": [{"measured_at": iso(NOW), "temp": 17.2, "humidity": 63}]}]
    assert engine.state["room_queue"] == []
    assert engine.state["room_result"] == {"stored": 1, "duplicates": 0, "skipped": 0}
    assert engine.state["rooms_supported"] is True
    # Next minute without a new value: the sensor still goes along (name, still there), no readings.
    await tick(engine, api, 1, [snap(minutes=0, humidity=63, floor="1")])
    assert api.calls[-1]["rooms"][0]["readings"] == []
    assert len(api.room_rows) == 1


async def test_no_rooms_chosen_sends_no_rooms_field(env):
    api, engine = env
    await tick(engine, api, 0, None)
    assert "rooms" not in api.calls[-1]


async def test_switch_off_sends_nothing_and_empties_the_queue(env):
    api, engine = env
    api.mode = "down"
    await tick(engine, api, 0, [snap(minutes=0)])
    assert len(engine.state["room_queue"]) == 1
    engine.set_send_rooms(False)
    assert engine.state["room_queue"] == []
    api.mode = "ok"
    await tick(engine, api, 2, [snap(minutes=2)])
    assert "rooms" not in api.calls[-1]
    assert api.room_rows == {}


async def test_an_older_server_keeps_the_readings_until_it_knows_rooms(env):
    api, engine = env
    api.rooms_supported = False
    await tick(engine, api, 0, [snap(minutes=0)])
    await tick(engine, api, 1, [snap(minutes=1, temp=17.3)])
    assert engine.state["rooms_supported"] is False
    assert len(engine.state["room_queue"]) == 2
    assert engine.state["queue"] == []  # the minute measurements did go through
    api.rooms_supported = True
    await tick(engine, api, 2, [snap(minutes=1, temp=17.3)])
    assert engine.state["room_queue"] == []
    assert len(api.room_rows) == 2


async def test_backlog_drains_in_calls_of_at_most_100_readings(env):
    api, engine = env
    api.mode = "down"
    rooms = None
    for m in range(0, 250):
        rooms = [snap(minutes=m, temp=17 + (m % 10) / 10)]
        engine.state["retry_at"] = None  # force a try every minute, like after a short outage
        await tick(engine, api, m, rooms)
    assert len(engine.state["room_queue"]) == 250
    api.mode = "ok"
    engine.state["retry_at"] = None
    await tick(engine, api, 250, rooms)
    sizes = [sum(len(r["readings"]) for r in c.get("rooms") or []) for c in api.calls]
    assert max(sizes) <= MAX_ROOM_READINGS_PER_CALL
    assert engine.state["room_queue"] == []
    assert len(api.room_rows) == 250


async def test_a_new_name_goes_along_with_the_same_key(env):
    api, engine = env
    await tick(engine, api, 0, [snap(minutes=0, name="Kinderkamer")])
    await tick(engine, api, 1, [snap(minutes=0, name="Logeerkamer")])
    assert api.room_names == {"aaaaaaaaaaaaaaaa": "Logeerkamer"}
