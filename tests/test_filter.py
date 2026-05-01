"""Tests for scripts._filter — the shared --since / --until helpers."""

from __future__ import annotations

import argparse

import pytest

from scripts._filter import is_in_window, validate_since, validate_until


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


# --- --until --------------------------------------------------------------


def test_validate_until_returns_none_when_unset() -> None:
    assert validate_until(None) is None


def test_validate_until_accepts_iso_date() -> None:
    assert validate_until("2026-03-30") == "2026-03-30"


@pytest.mark.parametrize("value", ["2026/03/30", "03-30-2026", "yesterday"])
def test_validate_until_rejects_bad_shape(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        validate_until(value)


def test_validate_until_error_mentions_until_flag() -> None:
    with pytest.raises(argparse.ArgumentTypeError, match=r"--until"):
        validate_until("nope")


def test_is_in_window_until_is_exclusive_upper_bound() -> None:
    """A record dated exactly the --until day must be EXCLUDED so back-to-back
    windows don't double-count the boundary."""
    rec = {"timestamp": "2026-04-15T00:00:00+00:00"}
    assert is_in_window(rec, since=None, until="2026-04-15") is False
    assert is_in_window(rec, since=None, until="2026-04-16") is True
    assert is_in_window(rec, since=None, until="2026-04-14") is False


def test_is_in_window_since_and_until_combined() -> None:
    """Both bounds active: record must be >= since AND < until."""
    rec = {"timestamp": "2026-02-15T12:00:00+00:00"}
    # In window
    assert is_in_window(rec, since="2026-01-01", until="2026-03-01") is True
    # Below since
    assert is_in_window(rec, since="2026-03-01", until="2026-04-01") is False
    # At/above until
    assert is_in_window(rec, since="2026-01-01", until="2026-02-15") is False


def test_back_to_back_windows_partition_records_cleanly() -> None:
    """Three records on three boundary days; two windows should split them
    exactly without overlap or gap."""
    a = {"timestamp": "2026-01-31T23:00:00+00:00"}
    b = {"timestamp": "2026-02-01T00:00:00+00:00"}
    c = {"timestamp": "2026-02-15T00:00:00+00:00"}
    # Window 1: [2026-01-01, 2026-02-01)
    assert is_in_window(a, "2026-01-01", "2026-02-01") is True
    assert is_in_window(b, "2026-01-01", "2026-02-01") is False
    assert is_in_window(c, "2026-01-01", "2026-02-01") is False
    # Window 2: [2026-02-01, 2026-03-01)
    assert is_in_window(a, "2026-02-01", "2026-03-01") is False
    assert is_in_window(b, "2026-02-01", "2026-03-01") is True
    assert is_in_window(c, "2026-02-01", "2026-03-01") is True


def test_is_in_window_excludes_records_with_no_timestamp_when_only_until_set() -> None:
    assert is_in_window({}, since=None, until="2026-04-15") is False
    assert is_in_window({"timestamp": ""}, since=None, until="2026-04-15") is False


def test_is_in_window_no_bounds_passes_records_with_no_timestamp() -> None:
    """Without any bound active, even timestamp-less records pass."""
    assert is_in_window({}, None, None) is True
    assert is_in_window({"timestamp": None}, None, None) is True
