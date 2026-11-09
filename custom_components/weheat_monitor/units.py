"""Unit conversion to the units the API expects: °C, W, kWh, %, ppm and hPa.

No Home Assistant imports: the tests load this module on its own.
"""

from __future__ import annotations

_POWER = {"W": 1.0, "kW": 1000.0, "MW": 1_000_000.0, "mW": 0.001}
_ENERGY = {"Wh": 0.001, "kWh": 1.0, "MWh": 1000.0, "GWh": 1_000_000.0}
# Air pressure to hPa, for every unit Home Assistant offers for atmospheric_pressure.
_PRESSURE = {
    "hPa": 1.0, "mbar": 1.0, "Pa": 0.01, "kPa": 10.0, "bar": 1000.0, "cbar": 10.0,
    "mmHg": 1.333224, "inHg": 33.86389, "psi": 68.94757,
}

UNAVAILABLE = {"unavailable", "unknown", "none", ""}


def parse_number(state: str | None) -> float | None:
    """A state string as a number; unavailable or unknown is None, never 0."""
    if state is None or str(state).strip().lower() in UNAVAILABLE:
        return None
    try:
        value = float(state)
    except (TypeError, ValueError):
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return value


def fahrenheit_to_celsius(value: float) -> float:
    return (value - 32.0) * 5.0 / 9.0


def celsius_to_fahrenheit(value: float) -> float:
    return value * 9.0 / 5.0 + 32.0


def convert(value: float | None, kind: str, unit: str | None) -> float | None:
    """Convert a value in `unit` to the API unit for `kind`.

    An unknown unit is passed through for temperature and percentage (both
    usually carry °C and %), but dropped for power, energy, CO2 and pressure,
    where a wrong factor would poison the data.
    """
    if value is None:
        return None
    unit = (unit or "").strip()
    if kind == "temperature":
        if unit in ("°F", "F"):
            return round(fahrenheit_to_celsius(value), 2)
        if unit == "K":
            return round(value - 273.15, 2)
        return value
    if kind == "power":
        factor = _POWER.get(unit)
        return None if factor is None else value * factor
    if kind == "energy":
        factor = _ENERGY.get(unit)
        return None if factor is None else value * factor
    if kind == "pressure":
        factor = _PRESSURE.get(unit)
        return None if factor is None else round(value * factor, 1)
    if kind == "co2":
        return value if unit == "ppm" else None
    return value
