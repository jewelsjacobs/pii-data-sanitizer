"""Unit tests for sanitizer.first_run_defaults."""

from __future__ import annotations

from sanitizer.first_run_defaults import kind_for_ner_type


def test_known_ner_types_map_to_pseudonym_kinds() -> None:
    assert kind_for_ner_type("PERSON") == "PERSON"
    assert kind_for_ner_type("LOCATION") == "LOCATION"
    assert kind_for_ner_type("ORGANIZATION") == "ORG"
    assert kind_for_ner_type("EMAIL_ADDRESS") == "EMAIL"
    assert kind_for_ner_type("PHONE_NUMBER") == "PHONE"


def test_unknown_ner_type_returns_none() -> None:
    assert kind_for_ner_type("DATE_TIME") is None
    assert kind_for_ner_type("NOT_A_REAL_LABEL") is None
