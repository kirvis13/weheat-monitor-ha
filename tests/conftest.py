"""Load the integration's Home Assistant-free modules without Home Assistant.

`custom_components/weheat_monitor/__init__.py` imports Home Assistant, so the
tests register the directory as a bare package `wm` and import the pure
modules (const, units, logic, api, engine) from there.
"""

import importlib
import sys
import types
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "weheat_monitor"

if "wm" not in sys.modules:
    pkg = types.ModuleType("wm")
    pkg.__path__ = [str(PACKAGE_DIR)]
    sys.modules["wm"] = pkg

for name in ("const", "units", "logic", "api", "engine"):
    importlib.import_module(f"wm.{name}")
