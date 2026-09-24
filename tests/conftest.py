"""Test helpers: load the integration's leaf modules without Home Assistant.

api.py / const.py have no Home Assistant dependency (only aiohttp), but
they live inside a regular package whose __init__.py *does* import
homeassistant. To unit test them in isolation (Home Assistant is not
installed in this dev environment - a real HA instance would be needed for
a full integration test), we register a stand-in empty package module in
sys.modules before loading const.py/api.py via importlib, so their
relative imports resolve without ever executing the real __init__.py.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

PKG_PATH = Path(__file__).resolve().parents[1] / "custom_components" / "unifi_network_plus"


def _load_isolated(module_name: str):
    full_name = f"unifi_network_plus.{module_name}"
    if full_name in sys.modules:
        return sys.modules[full_name]

    if "unifi_network_plus" not in sys.modules:
        pkg = types.ModuleType("unifi_network_plus")
        pkg.__path__ = [str(PKG_PATH)]
        sys.modules["unifi_network_plus"] = pkg

    spec = importlib.util.spec_from_file_location(full_name, PKG_PATH / f"{module_name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def api_module():
    """Return the isolated api module (also loads const as a dependency)."""
    _load_isolated("const")
    return _load_isolated("api")


@pytest.fixture()
def websocket_module():
    """Return the isolated websocket module (also loads its dependencies)."""
    _load_isolated("const")
    _load_isolated("api")
    return _load_isolated("websocket")
