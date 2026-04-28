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
            "[REDACTED_LINK]",
        ),
        ("[click here](https://example.com/path?id=abc)", "[REDACTED_LINK]"),
        ("visit https://tools.usps.com/go/TrackConfirm", "[REDACTED_LINK]"),
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


def test_url_with_pwd_query_string_collapses_to_single_token() -> None:
    """URL `?pwd=...` redacts cleanly — the whole URL becomes one
    [REDACTED_LINK] token rather than leaking the query value."""
    out = apply_rules("Join: https://zoom.us/j/1?pwd=xYz123")
    assert out == "Join: [REDACTED_LINK]"


def test_markdown_link_redacted_atomically() -> None:
    """`[text](url)` collapses to [REDACTED_LINK] — neither the display
    text nor the URL leaks through."""
    out = apply_rules(
        "See [Tracking 9200190327101300024246](https://tools.usps.com/go/Track?tLabels=9200190327101300024246)"
    )
    assert out == "See [REDACTED_LINK]"


def test_markdown_image_link_also_redacted() -> None:
    """Image-link shape `![alt](url)` is still a URL reference."""
    out = apply_rules("![chart](https://example.com/img.png)")
    assert out == "[REDACTED_LINK]"


def test_multiline_markdown_link() -> None:
    """html2text wraps long URLs across lines; the redactor must still match."""
    wrapped = "[Tracking](https://tools.usps.com/go/TrackConfirm?tLabels=abc\n&utm_source=delivery)"
    out = apply_rules(wrapped)
    assert out == "[REDACTED_LINK]"


def test_bare_url_keeps_trailing_sentence_punctuation() -> None:
    """A period or comma after a URL stays outside the redaction."""
    out = apply_rules("visit https://example.com. Next sentence.")
    assert out == "visit [REDACTED_LINK]. Next sentence."


def test_non_url_brackets_left_alone() -> None:
    """`[n]` without `(url)` next to it is not a markdown link."""
    text = "Footnote [1] in the paper; array index arr[2]."
    assert apply_rules(text) == text


# ---------------------------------------------------------------------------
# Rule fixes from the M6 first audit pass
# ---------------------------------------------------------------------------


def test_ssn_last_four_labeled() -> None:
    """'last 4 of SSN: 1234' shapes get [REDACTED_SSN]."""
    out = apply_rules("Please confirm: last 4 of SSN: 1234.")
    assert "[REDACTED_SSN]" in out
    assert "1234" not in out


def test_ssn_ending_in_labeled() -> None:
    out = apply_rules("Verify SSN ending in 5678 to continue.")
    assert "[REDACTED_SSN]" in out
    assert "5678" not in out


def test_account_ending_in_labeled() -> None:
    """'account ending in 5083' → [REDACTED_ACCOUNT]."""
    out = apply_rules("Charged to account ending in 5083 today.")
    assert "[REDACTED_ACCOUNT]" in out
    assert "5083" not in out


def test_bare_ending_in_labeled() -> None:
    """'ending with 9012' alone (no 'account' prefix) is also caught."""
    out = apply_rules("Card ending with 9012 on file.")
    assert "[REDACTED_ACCOUNT]" in out


def test_dshs_client_id_labeled() -> None:
    """State client IDs like DSHS are high-priority gov IDs."""
    out = apply_rules("DSHS Client ID: 1234567890")
    assert "[REDACTED_GOV_ID]" in out
    assert "1234567890" not in out


def test_medicare_member_id_labeled() -> None:
    out = apply_rules("Medicare ID: ABC123456789")
    assert "[REDACTED_GOV_ID]" in out
    assert "ABC123456789" not in out


def test_2fa_verification_code_labeled() -> None:
    out = apply_rules("Your verification code is 482917.")
    assert "[REDACTED_2FA]" in out
    assert "482917" not in out


def test_2fa_one_time_passcode() -> None:
    out = apply_rules("One-time passcode: 5293")
    assert "[REDACTED_2FA]" in out
    assert "5293" not in out


def test_2fa_otp_label() -> None:
    out = apply_rules("OTP: 824673")
    assert "[REDACTED_2FA]" in out
    assert "824673" not in out
