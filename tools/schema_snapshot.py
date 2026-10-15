"""Snapshot and diff the *shape* of the controller responses this integration reads.

Records only field paths and value types - never values - so snapshots are safe
to keep and share. Run before and after a controller upgrade, then diff:

    UNIFI_HOST=... UNIFI_USER=... UNIFI_PASS=... python3 tools/schema_snapshot.py snapshot > before.json
    python3 tools/schema_snapshot.py diff before.json after.json
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import sys
import types
from pathlib import Path
from typing import Any

ENDPOINTS = ["stat/sta", "stat/device", "stat/health", "rest/user", "stat/sysinfo"]
REPORTS = ["5minutes.gw", "hourly.gw", "daily.gw"]


def _load_api():
    path = Path(__file__).resolve().parent.parent / "custom_components" / "unifi_network_plus"
    # Stub package so api.py's relative imports work without importing the
    # integration's __init__ (which needs Home Assistant).
    package = types.ModuleType("unifi_network_plus_pkg")
    package.__path__ = [str(path)]
    sys.modules["unifi_network_plus_pkg"] = package
    return importlib.import_module("unifi_network_plus_pkg.api")


# Keys like "port_14-rx_bytes" only exist while that port has traffic, so the
# raw key set flaps between snapshots. Normalise the port number.
_VOLATILE_KEY = re.compile(r"(?<=port_)\d+")


def _walk(value: Any, prefix: str, out: dict[str, set[str]]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            key = _VOLATILE_KEY.sub("N", str(key))
            _walk(item, f"{prefix}.{key}" if prefix else key, out)
    elif isinstance(value, list):
        out.setdefault(prefix + "[]", set()).add("list")
        for item in value:
            _walk(item, prefix + "[]", out)
    else:
        out.setdefault(prefix, set()).add(type(value).__name__)


def shape(records: list[dict[str, Any]]) -> dict[str, list[str]]:
    out: dict[str, set[str]] = {}
    for record in records:
        _walk(record, "", out)
    return {path: sorted(types) for path, types in sorted(out.items())}


async def take_snapshot() -> dict[str, Any]:
    import aiohttp

    api = _load_api()
    jar = aiohttp.CookieJar(unsafe=True)
    async with aiohttp.ClientSession(cookie_jar=jar, connector=aiohttp.TCPConnector(ssl=False)) as session:
        client = api.UniFiClient(
            session,
            os.environ["UNIFI_HOST"],
            os.environ["UNIFI_USER"],
            os.environ["UNIFI_PASS"],
            site=os.environ.get("UNIFI_SITE", "default"),
        )
        await client.login()
        snapshot: dict[str, Any] = {}
        try:
            info = (await client._get("stat/sysinfo"))[0]
            snapshot["__meta__"] = {"controller_version": [str(info.get("version"))], "build": [str(info.get("build"))]}
        except Exception:  # noqa: BLE001
            snapshot["__meta__"] = {"controller_version": ["unknown"]}
        for endpoint in ENDPOINTS:
            try:
                snapshot[endpoint] = shape(await client._get(endpoint))
            except Exception as err:  # noqa: BLE001 - record the failure, keep going
                snapshot[endpoint] = {"__error__": [type(err).__name__ + ": " + str(err)[:120]]}
        for kind in REPORTS:
            try:
                snapshot[f"stat/report/{kind}"] = shape(await client._get_report(kind))
            except Exception as err:  # noqa: BLE001
                snapshot[f"stat/report/{kind}"] = {"__error__": [type(err).__name__ + ": " + str(err)[:120]]}
        return snapshot


def diff(before: dict[str, Any], after: dict[str, Any]) -> int:
    changes = 0
    for endpoint in sorted(set(before) | set(after)):
        old, new = before.get(endpoint, {}), after.get(endpoint, {})
        removed = sorted(set(old) - set(new))
        added = sorted(set(new) - set(old))
        retyped = sorted(p for p in set(old) & set(new) if old[p] != new[p])
        if not (removed or added or retyped):
            continue
        changes += len(removed) + len(added) + len(retyped)
        print(f"== {endpoint}")
        for path in removed:
            print(f"  REMOVED  {path}")
        for path in retyped:
            print(f"  RETYPED  {path}: {old[path]} -> {new[path]}")
        for path in added:
            print(f"  added    {path}")
    print(f"{changes} change(s)")
    return 1 if changes else 0


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == "snapshot":
        json.dump(asyncio.run(take_snapshot()), sys.stdout, indent=1, sort_keys=True)
        return 0
    if len(sys.argv) == 4 and sys.argv[1] == "diff":
        return diff(json.load(open(sys.argv[2])), json.load(open(sys.argv[3])))
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
