"""Tests for sanitizer.state — per-source processing state file."""

from __future__ import annotations

import json
from pathlib import Path

from sanitizer.state import STATE_FILENAME, read_state, write_state


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def test_write_state_finds_high_water_mark(tmp_path: Path) -> None:
    clean_dir = tmp_path / "clean" / "ical"
    _write_jsonl(
        clean_dir / "events.jsonl",
        [
            {"source_id": "a", "timestamp": "2026-01-15T10:00:00+00:00"},
            {"source_id": "b", "timestamp": "2026-04-25T10:00:00+00:00"},
            {"source_id": "c", "timestamp": "2026-03-30T10:00:00+00:00"},
        ],
    )
    state = write_state(clean_dir, run_window=None)
    assert state["latest_record_date"] == "2026-04-25"
    assert state["suggested_next_since"] == "2026-04-25"
    assert state["record_count"] == 3
    assert state["last_run_window"] is None


def test_write_state_records_run_window(tmp_path: Path) -> None:
    clean_dir = tmp_path / "clean" / "apple-mail"
    _write_jsonl(clean_dir / "events.jsonl", [])
    state = write_state(clean_dir, run_window="2026-04-01")
    assert state["last_run_window"] == "2026-04-01"


def test_write_state_handles_empty_clean_file(tmp_path: Path) -> None:
    clean_dir = tmp_path / "clean" / "ical"
    _write_jsonl(clean_dir / "events.jsonl", [])
    state = write_state(clean_dir, run_window=None)
    assert state["record_count"] == 0
    assert state["latest_record_date"] is None
    assert state["suggested_next_since"] is None


def test_write_state_handles_missing_clean_file(tmp_path: Path) -> None:
    """Sanitize never ran but state still gets written — record_count == 0."""
    clean_dir = tmp_path / "clean" / "imessage"
    state = write_state(clean_dir, run_window=None)
    assert state["record_count"] == 0
    assert state["latest_record_date"] is None
    assert (clean_dir / STATE_FILENAME).exists()


def test_write_state_handles_date_only_timestamps(tmp_path: Path) -> None:
    """All-day ical events have date-only timestamps — still comparable."""
    clean_dir = tmp_path / "clean" / "ical"
    _write_jsonl(
        clean_dir / "events.jsonl",
        [
            {"source_id": "a", "timestamp": "2026-07-04"},
            {"source_id": "b", "timestamp": "2026-08-15"},
        ],
    )
    state = write_state(clean_dir, run_window=None)
    assert state["latest_record_date"] == "2026-08-15"


def test_write_state_skips_records_with_bad_timestamp(tmp_path: Path) -> None:
    clean_dir = tmp_path / "clean" / "ical"
    _write_jsonl(
        clean_dir / "events.jsonl",
        [
            {"source_id": "good", "timestamp": "2026-04-25T10:00:00+00:00"},
            {"source_id": "bad-1", "timestamp": None},
            {"source_id": "bad-2"},
            {"source_id": "bad-3", "timestamp": "abc"},
        ],
    )
    state = write_state(clean_dir, run_window=None)
    # All four records are counted even if their timestamp is unusable.
    assert state["record_count"] == 4
    # Only the "good" record contributes to the high-water mark.
    assert state["latest_record_date"] == "2026-04-25"


def test_write_state_skips_blank_lines_and_bad_json(tmp_path: Path) -> None:
    clean_dir = tmp_path / "clean" / "ical"
    clean_dir.mkdir(parents=True)
    (clean_dir / "events.jsonl").write_text(
        '\n\n{"source_id": "a", "timestamp": "2026-04-25T10:00:00+00:00"}\nnot-json\n\n'
    )
    state = write_state(clean_dir, run_window=None)
    assert state["record_count"] == 1
    assert state["latest_record_date"] == "2026-04-25"


def test_read_state_round_trips(tmp_path: Path) -> None:
    clean_dir = tmp_path / "clean" / "ical"
    _write_jsonl(
        clean_dir / "events.jsonl",
        [{"source_id": "a", "timestamp": "2026-04-25T10:00:00+00:00"}],
    )
    written = write_state(clean_dir, run_window="2026-04-01")
    loaded = read_state(clean_dir)
    assert loaded == written


def test_read_state_returns_none_when_missing(tmp_path: Path) -> None:
    assert read_state(tmp_path / "no-such-dir") is None


def test_read_state_returns_none_on_corrupt_json(tmp_path: Path) -> None:
    clean_dir = tmp_path / "clean" / "ical"
    clean_dir.mkdir(parents=True)
    (clean_dir / STATE_FILENAME).write_text("{ not valid json")
    assert read_state(clean_dir) is None
