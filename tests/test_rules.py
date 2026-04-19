"""Unit tests for sanitizer.rules.

Covers the credential / financial / location recall surface called out in
PLAN.md (M2 Done-when) and a few corner cases that have historically
tripped this rule set (URL-param `pwd=`, trailing sentence punctuation,
idempotency when a rule would otherwise re-enter its own replacement).
"""

from __future__ import annotations

import pytest

from sanitizer.rules import apply_rules


@pytest.mark.parametrize(
    "text, expected_token",
    [
        ("AKIAIOSFODNN7EXAMPLE", "[REDACTED_API_KEY]"),
        ("ghp_aB3cD4eF5gH6iJ7kL8mN9oP0qR1sT2uV3wXy", "[REDACTED_API_KEY]"),
        ("sk-abcdefghijklmnop0123456789", "[REDACTED_API_KEY]"),
        ("AWS secret: wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "[REDACTED_API_KEY]"),
        ("password: hunter2", "[REDACTED_CREDENTIAL]"),
        ("PIN 998877", "[REDACTED_CREDENTIAL]"),
        ("SSN: 123-45-6789", "[REDACTED_SSN]"),
        ("card: 4111 1111 1111 1111", "[REDACTED_CARD]"),
        ("Routing: 021000021", "[REDACTED_ROUTING]"),
        ("Checking account: 1234567890", "[REDACTED_ACCOUNT]"),
        ("Brokerage (Fidelity): Z12345678", "[REDACTED_BROKERAGE]"),
        ("server at 192.168.1.15", "[REDACTED_IP]"),
        ("gateway: 2001:db8::1", "[REDACTED_IP]"),
        (
            "https://zoom.us/j/1234567890?pwd=abcdefghijklmnopqr",
            "[REDACTED_CONFERENCE_URL]",
        ),
    ],
)
def test_hard_redacts(text: str, expected_token: str) -> None:
    out = apply_rules(text)
    assert expected_token in out
    # The raw secret / address / value must not survive.
    assert "hunter2" not in out


def test_credential_lookalike_is_not_redacted() -> None:
    """Metaphorical uses of "password" / "key" without the `:` label stay."""
    text = "the password to a good life is forgiveness. The real key is letting go."
    assert apply_rules(text) == text


def test_trailing_sentence_punctuation_stays_outside_token() -> None:
    """'password: hunter2.' ends with a period — the period stays."""
    out = apply_rules("password: hunter2. Next sentence.")
    assert out == "password: [REDACTED_CREDENTIAL]. Next sentence."


def test_apply_rules_is_idempotent() -> None:
    """Running apply_rules twice produces no extra `]`s or duplicate tokens."""
    text = "AWS access key: AKIAIOSFODNN7EXAMPLE\npassword: hunter2"
    once = apply_rules(text)
    twice = apply_rules(once)
    assert once == twice


def test_conference_url_in_query_string_does_not_leak_bracket() -> None:
    """URL `?pwd=...` redacts cleanly even though the pwd label also matches."""
    out = apply_rules("Join: https://zoom.us/j/1?pwd=xYz123")
    assert out == "Join: [REDACTED_CONFERENCE_URL]"
