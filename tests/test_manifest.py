"""Unit tests for sanitizer.manifest."""

from __future__ import annotations

import json
from pathlib import Path

from sanitizer.manifest import compute_file_hash, write_manifest


def test_manifest_shape(tmp_path: Path) -> None:
    mapping_path = tmp_path / "mapping.json"
    mapping_path.write_text("{}")
    out = write_manifest(
        tmp_path / "clean",
        records_in=10,
        records_out=10,
        entities_redacted={"PERSON": 3, "EMAIL": 2},
        new_entities_auto_classified=5,
        mapping_file_path=mapping_path,
    )
    manifest = json.loads(out.read_text())
    assert manifest["records_in"] == 10
    assert manifest["records_out"] == 10
    assert manifest["entities_redacted"] == {"PERSON": 3, "EMAIL": 2}
    assert manifest["new_entities_auto_classified"] == 5
    assert manifest["status"] == "ready_for_ingest"
    assert manifest["mapping_file_hash"].startswith("sha256:")
    assert manifest["policy_hash"] is None  # no policy path provided
    assert manifest["auditor_flags"] == 0


def test_missing_file_hash_is_none(tmp_path: Path) -> None:
    assert compute_file_hash(None) is None
    assert compute_file_hash(tmp_path / "does-not-exist.json") is None


def test_file_hash_is_stable(tmp_path: Path) -> None:
    p = tmp_path / "a.txt"
    p.write_text("hello")
    first = compute_file_hash(p)
    second = compute_file_hash(p)
    assert first == second


def test_status_can_be_overridden(tmp_path: Path) -> None:
    out = write_manifest(
        tmp_path / "clean",
        records_in=0,
        records_out=0,
        entities_redacted={},
        new_entities_auto_classified=0,
        status="blocked_on_error",
    )
    manifest = json.loads(out.read_text())
    assert manifest["status"] == "blocked_on_error"
