"""Unit tests for sanitizer.mapping."""

from __future__ import annotations

from pathlib import Path

from sanitizer.mapping import Mapping


def test_get_or_assign_is_stable_for_same_key() -> None:
    m = Mapping()
    first = m.get_or_assign("PERSON", "Linda Hartwell")
    second = m.get_or_assign("PERSON", "Linda Hartwell")
    assert first == second == "[PERSON_1]"


def test_counter_is_per_kind() -> None:
    m = Mapping()
    assert m.get_or_assign("PERSON", "a") == "[PERSON_1]"
    assert m.get_or_assign("EMAIL", "a@example.invalid") == "[EMAIL_1]"
    assert m.get_or_assign("PERSON", "b") == "[PERSON_2]"
    assert m.get_or_assign("EMAIL", "b@example.invalid") == "[EMAIL_2]"


def test_different_keys_get_different_pseudonyms() -> None:
    m = Mapping()
    first = m.get_or_assign("ORG", "Acme")
    second = m.get_or_assign("ORG", "Beta")
    assert first != second
    assert first == "[ORG_1]"
    assert second == "[ORG_2]"


def test_round_trip_through_json(tmp_path: Path) -> None:
    m = Mapping()
    m.get_or_assign("PERSON", "a")
    m.get_or_assign("EMAIL", "a@example.invalid")
    path = tmp_path / "mapping.json"
    m.save(path)
    reloaded = Mapping.load(path)
    assert reloaded.get_or_assign("PERSON", "a") == "[PERSON_1]"
    assert reloaded.get_or_assign("EMAIL", "a@example.invalid") == "[EMAIL_1]"
    # A new key increments the right counter.
    assert reloaded.get_or_assign("PERSON", "b") == "[PERSON_2]"


def test_load_missing_file_returns_empty(tmp_path: Path) -> None:
    m = Mapping.load(tmp_path / "does-not-exist.json")
    assert m.get_or_assign("PERSON", "a") == "[PERSON_1]"
