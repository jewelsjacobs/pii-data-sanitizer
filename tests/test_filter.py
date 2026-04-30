"""Tests for scripts._filter — the shared --since helpers."""

from __future__ import annotations

import argparse

import pytest

from scripts._filter import is_in_window, validate_since


def test_validate_since_returns_none_when_unset() -> None:
    assert validate_since(None) is None


def test_validate_since_accepts_iso_date() -> None:
    assert validate_since("2026-03-30") == "2026-03-30"


@pytest.mark.parametrize(
    "value",
    ["2026/03/30", "03-30-2026", "2026-3-30", "yesterday", "2026-13-01"],
)
def test_validate_since_rejects_bad_shape(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        validate_since(value)


def test_is_in_window_no_filter_keeps_everything() -> None:
    assert is_in_window({"timestamp": "2020-01-01T00:00:00+00:00"}, None) is True
    assert is_in_window({}, None) is True


def test_is_in_window_keeps_records_on_or_after_threshold() -> None:
    rec = {"timestamp": "2026-04-15T12:00:00+00:00"}
    assert is_in_window(rec, "2026-04-15") is True
    assert is_in_window(rec, "2026-04-14") is True
    assert is_in_window(rec, "2026-04-16") is False


def test_is_in_window_handles_date_only_timestamp() -> None:
    """All-day ical events store the date with no time — still comparable."""
    rec = {"timestamp": "2026-07-04"}
    assert is_in_window(rec, "2026-07-04") is True
    assert is_in_window(rec, "2026-07-05") is False
    assert is_in_window(rec, "2026-06-01") is True


def test_is_in_window_excludes_records_with_no_timestamp() -> None:
    """A --since run with no timestamp can't be classified, so drop it."""
    assert is_in_window({}, "2026-04-15") is False
    assert is_in_window({"timestamp": None}, "2026-04-15") is False
    assert is_in_window({"timestamp": ""}, "2026-04-15") is False
    assert is_in_window({"timestamp": "abc"}, "2026-04-15") is False


def test_is_in_window_lexicographic_safety_across_year_boundary() -> None:
    """ISO 8601 dates sort lexicographically; this must survive year wrap."""
    rec = {"timestamp": "2025-12-31T23:59:59+00:00"}
    assert is_in_window(rec, "2026-01-01") is False
    assert is_in_window(rec, "2025-12-31") is True
