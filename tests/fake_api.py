"""A stand-in for POST /api/v1/devices/sync, served by aiohttp on localhost.

Behaves like the server: Bearer key, at most 100
measurements and 20 applied per call, one row per minute (a repeat counts as
`duplicates`), measurements older than 24 h are `skipped`. With plan_live off
only the blocks of a thermostat test (reason `test:...`) are sent, and `test`
describes the open test (None; "omit" leaves the field out like an old server).

`rooms`: at most 20 sensors and 100 readings per call (else 400), one reading
per sensor per time (a repeat counts as `duplicates`), readings older than
29 days `skipped`. With `rooms_supported = False` the field is ignored and
left out of the answer, like a server from before November 2026.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from aiohttp import web

KEY = "whm_" + "ab" * 32


class FakeApi:
    def __init__(self) -> None:
        self.mode = "ok"  # ok | down (500) | unauthorized
        self.rows: dict[str, dict[str, Any]] = {}
        self.calls: list[dict[str, Any]] = []
        self.applied: list[int] = []
        self.thermostats: list[dict[str, Any]] = []
        self.plan_live = True
        self.base_setpoint = 19.0
        self.blocks: list[dict[str, Any]] = []
        self.test: Any = None
        self.rooms_supported = True
        self.room_rows: dict[tuple[str, str], dict[str, Any]] = {}
        self.room_names: dict[str, str] = {}
        self.now = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)

    async def handle(self, request: web.Request) -> web.Response:
        if request.headers.get("Authorization") != f"Bearer {KEY}" or self.mode == "unauthorized":
            return web.json_response({"error": "unknown key"}, status=401)
        if self.mode == "down":
            return web.json_response({"error": "internal error"}, status=500)
        body = await request.json()
        self.calls.append(body)
        measurements = body.get("measurements") or []
        applied = body.get("applied") or []
        if len(measurements) > 100:
            return web.json_response({"error": "at most 100 measurements per call"}, status=400)
        if len(applied) > 20:
            return web.json_response({"error": "at most 20 plan_ids per call"}, status=400)
        stored = duplicates = skipped = 0
        for m in measurements:
            t = datetime.fromisoformat(m["measured_at"].replace("Z", "+00:00"))
            if t < self.now - timedelta(hours=24):
                skipped += 1
            elif m["measured_at"] in self.rows:
                duplicates += 1
            else:
                self.rows[m["measured_at"]] = m
                stored += 1
        room_result = None
        rooms = body.get("rooms")
        if rooms is not None and self.rooms_supported:
            if not isinstance(rooms, list):
                return web.json_response({"error": "rooms is not a list"}, status=400)
            if len(rooms) > 20:
                return web.json_response({"error": "at most 20 rooms per call"}, status=400)
            if sum(len(r.get("readings") or []) for r in rooms) > 100:
                return web.json_response({"error": "at most 100 room readings per call"}, status=400)
            room_result = {"stored": 0, "duplicates": 0, "skipped": 0}
            for r in rooms:
                self.room_names[r["key"]] = r["name"]
                for reading in r.get("readings") or []:
                    t = datetime.fromisoformat(reading["measured_at"].replace("Z", "+00:00"))
                    if t < self.now - timedelta(days=29) or not isinstance(reading.get("temp"), (int, float)):
                        room_result["skipped"] += 1
                    elif (r["key"], reading["measured_at"]) in self.room_rows:
                        room_result["duplicates"] += 1
                    else:
                        self.room_rows[(r["key"], reading["measured_at"])] = reading
                        room_result["stored"] += 1
        self.applied.extend(applied)
        if body.get("thermostat"):
            self.thermostats.append(body["thermostat"])
        blocks = self.blocks if self.plan_live else [
            b for b in self.blocks if str(b.get("reason", "")).startswith("test:")
        ]
        answer = {
            "stored": stored, "duplicates": duplicates, "skipped": skipped,
            "plan_live": self.plan_live, "base_setpoint": self.base_setpoint,
            "blocks": blocks,
        }
        if self.test != "omit":
            answer["test"] = self.test
        if room_result is not None:
            answer["rooms"] = room_result
        return web.json_response(answer)

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/api/v1/devices/sync", self.handle)
        return app
