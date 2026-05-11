"""Smoke tests for the audit module.

Mirrors v1's tests narrowly — just enough to catch a port mistake.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent import audit


def _base_entry() -> dict:
    return {
        "ts": datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc).isoformat(),
        "op": "tool.Read",
        "actor": "agent",
        "outcome": "ok",
        "duration_ms": 42,
    }


def test_writes_one_line_to_daily_file(tmp_path: Path) -> None:
    entry = audit.write_audit_entry(_base_entry(), audit_root=tmp_path)

    daily = tmp_path / "2026-05-11.jsonl"
    assert daily.exists()
    lines = daily.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1

    record = json.loads(lines[0])
    assert record["id"] == entry.id
    assert record["op"] == "tool.Read"
    assert len(record["id"]) == 64  # sha256 hex


def test_missing_required_field_raises(tmp_path: Path) -> None:
    bad = _base_entry()
    del bad["op"]
    with pytest.raises(ValueError, match="missing required fields"):
        audit.write_audit_entry(bad, audit_root=tmp_path)


def test_same_payload_same_id(tmp_path: Path) -> None:
    a = audit.write_audit_entry(_base_entry(), audit_root=tmp_path)
    b = audit.write_audit_entry(_base_entry(), audit_root=tmp_path)
    assert a.id == b.id  # both appended (audit is append-only), same id


def test_extra_fields_preserved(tmp_path: Path) -> None:
    entry = _base_entry()
    entry["chat_id"] = "12345"
    entry["tool_name"] = "Read"
    audit.write_audit_entry(entry, audit_root=tmp_path)
    record = json.loads((tmp_path / "2026-05-11.jsonl").read_text().splitlines()[0])
    assert record["chat_id"] == "12345"
    assert record["tool_name"] == "Read"
