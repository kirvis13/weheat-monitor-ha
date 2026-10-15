"""Client for POST /api/v1/devices/sync on Weheat Monitor.

Only aiohttp, which Home Assistant ships. The API key goes into the
Authorization header and nowhere else: never into a log line, an exception
message or an entity attribute.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import aiohttp

from .const import REQUEST_TIMEOUT_SECONDS, SYNC_PATH

# Same shape as SLEUTEL_VORM on the server: whm_ + 64 hex (a bare 64 hex is the
# older Toon-bridge form, which the server still accepts).
KEY_PATTERN = re.compile(r"^(whm_)?[0-9a-f]{64}$")


def valid_key_format(key: str) -> bool:
    return bool(KEY_PATTERN.match(key.strip()))


class ApiError(Exception):
    """Network problem, timeout, 429 or 5xx: try again later."""


class AuthError(ApiError):
    """401: the key is unknown or revoked."""


class BadRequestError(ApiError):
    """400: the server will never accept this body; retrying does not help."""


class WeheatMonitorClient:
    def __init__(self, session: aiohttp.ClientSession, base_url: str, api_key: str) -> None:
        self._session = session
        self._url = base_url.rstrip("/") + SYNC_PATH
        self._headers = {
            "Authorization": f"Bearer {api_key.strip()}",
            "Content-Type": "application/json",
        }

    async def sync(
        self,
        measurements: list[dict[str, Any]] | None = None,
        applied: list[int] | None = None,
        thermostat: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """One sync call. Returns the parsed answer or raises an ApiError."""
        body: dict[str, Any] = {}
        if measurements:
            body["measurements"] = measurements
        if applied:
            body["applied"] = applied
        if thermostat:
            body["thermostat"] = thermostat
        try:
            async with self._session.post(
                self._url,
                json=body,
                headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS),
            ) as resp:
                try:
                    data = await resp.json(content_type=None)
                except (aiohttp.ContentTypeError, ValueError):
                    data = None
                error = data.get("error") if isinstance(data, dict) else None
                if resp.status == 401:
                    raise AuthError("unknown key")
                if resp.status == 400:
                    raise BadRequestError(f"HTTP 400: {error or 'bad request'}")
                if resp.status != 200 or not isinstance(data, dict):
                    raise ApiError(f"HTTP {resp.status}" + (f": {error}" if error else ""))
                return data
        except asyncio.TimeoutError as err:
            raise ApiError("timeout") from err
        except aiohttp.ClientError as err:
            raise ApiError(type(err).__name__) from err
