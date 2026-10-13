"""Tests for tools/schema_snapshot.py (shape extraction and diffing)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "schema_snapshot", Path(__file__).resolve().parent.parent / "tools" / "schema_snapshot.py"
)
schema_snapshot = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(schema_snapshot)


def test_shape_records_paths_and_types_but_not_values() -> None:
    result = schema_snapshot.shape(
        [{"mac": "aa:bb", "ports": [{"idx": 1, "up": True}], "n": 1}, {"mac": "cc:dd", "n": None}]
    )
    assert result["mac"] == ["str"]
    assert result["n"] == ["NoneType", "int"]
    assert result["ports[]"] == ["list"]
    assert result["ports[].idx"] == ["int"]
    assert "aa:bb" not in str(result)


def test_diff_reports_removed_added_and_retyped(capsys) -> None:
    before = {"stat/health": {"a": ["int"], "b": ["str"]}}
    after = {"stat/health": {"a": ["str"], "c": ["int"]}}
    assert schema_snapshot.diff(before, after) == 1
    out = capsys.readouterr().out
    assert "REMOVED  b" in out and "RETYPED  a" in out and "added    c" in out


def test_diff_identical_returns_zero() -> None:
    snap = {"stat/sta": {"mac": ["str"]}}
    assert schema_snapshot.diff(snap, snap) == 0
