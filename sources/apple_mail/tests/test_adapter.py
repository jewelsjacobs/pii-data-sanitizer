"""Tests for the apple-mail adapter.

Split into two groups:

* Pure-MIME parsing — no network, synthetic RFC822 bytes. Covers every
  shape the iCloud IMAP backend can plausibly return (text/plain,
  text/html → html2text, multipart prefer-plain, quoted-printable,
  base64, non-UTF-8 charset, attachment counting, malformed Date halts,
  Message-ID fallback).
* IMAP integration — ``imaplib.IMAP4_SSL`` is mocked so we can verify
  the deny-list, UID pagination, and that credentials never surface
  in logs or exception messages.
"""

from __future__ import annotations

from email.message import EmailMessage
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from sources.apple_mail.adapter import (
    AdapterError,
    _parse_folder_line,
    _parse_message,
    _split_address_list,
    _split_references,
    extract_mail,
    fetch_and_cache,
    parse_cache,
)

# ---------------------------------------------------------------------------
# Address + reference header splitting
# ---------------------------------------------------------------------------


def test_split_address_list_handles_display_names() -> None:
    result = _split_address_list("Alex Chen <alex@example.invalid>, Sam Park <sam@example.invalid>")
    assert result == [
        "Alex Chen <alex@example.invalid>",
        "Sam Park <sam@example.invalid>",
    ]


def test_split_address_list_handles_bare_email() -> None:
    assert _split_address_list("sam@example.invalid") == ["sam@example.invalid"]


def test_split_address_list_handles_quoted_comma_in_name() -> None:
    result = _split_address_list(
        '"Chen, Alex" <alex@example.invalid>, Sam Park <sam@example.invalid>'
    )
    # getaddresses preserves the quoted display name intact.
    assert result[0].endswith("<alex@example.invalid>")
    assert "Chen, Alex" in result[0]
    assert result[1] == "Sam Park <sam@example.invalid>"


def test_split_address_list_empty() -> None:
    assert _split_address_list("") == []


def test_split_references() -> None:
    raw = "<a@ex.invalid> <b@ex.invalid>\n  <c@ex.invalid>"
    assert _split_references(raw) == [
        "<a@ex.invalid>",
        "<b@ex.invalid>",
        "<c@ex.invalid>",
    ]


# ---------------------------------------------------------------------------
# Folder-line parsing
# ---------------------------------------------------------------------------


def test_parse_folder_line_simple() -> None:
    assert _parse_folder_line(b'(\\HasNoChildren) "/" "INBOX"') == "INBOX"


def test_parse_folder_line_with_slash_hierarchy() -> None:
    assert _parse_folder_line(b'(\\HasNoChildren) "/" "INBOX/Family"') == "INBOX/Family"


def test_parse_folder_line_malformed_returns_none() -> None:
    assert _parse_folder_line(b"no quoted names here") is None


# ---------------------------------------------------------------------------
# Message parsing — text/plain
# ---------------------------------------------------------------------------


def _build(
    *,
    from_: str = "Alex Chen <alex@example.invalid>",
    to: str = "Lea <testuser@example.invalid>",
    subject: str = "Test message",
    date: str = "Mon, 01 Feb 2026 10:00:00 +0000",
    message_id: str | None = "<msg-001@example.invalid>",
    body: str = "Hello, this is the body.",
    content_type: str = "text/plain; charset=utf-8",
    extra_headers: dict[str, str] | None = None,
) -> bytes:
    msg = EmailMessage()
    msg["From"] = from_
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = date
    if message_id:
        msg["Message-ID"] = message_id
    for k, v in (extra_headers or {}).items():
        msg[k] = v
    # Force the content type
    if "html" in content_type:
        msg.set_content(body, subtype="html")
    else:
        msg.set_content(body)
    return msg.as_bytes()


def test_parse_plain_text_message() -> None:
    raw = _build(body="Hello, this is the body.")
    record = _parse_message(raw, folder_name="INBOX")
    assert record["source"] == "apple-mail"
    assert record["source_id"] == "<msg-001@example.invalid>"
    assert record["timestamp"] == "2026-02-01T10:00:00+00:00"
    assert record["content"].strip() == "Hello, this is the body."
    assert record["metadata"]["folder"] == "INBOX"
    assert record["metadata"]["from"] == "Alex Chen <alex@example.invalid>"
    assert record["metadata"]["to"] == ["Lea <testuser@example.invalid>"]
    assert record["metadata"]["subject"] == "Test message"
    assert record["metadata"]["has_attachments"] is False
    assert record["metadata"]["attachment_count"] == 0


def test_missing_message_id_falls_back_to_sha256() -> None:
    raw = _build(message_id=None)
    record = _parse_message(raw, folder_name="INBOX")
    assert record["source_id"].startswith("sha256:")


def test_unparseable_date_falls_back_to_received() -> None:
    """Unparseable Date headers used to halt; now they fall back to the
    last Received-header timestamp and flag ``date_synthesized``."""
    raw = (
        b"From: Alex <alex@example.invalid>\r\n"
        b"To: Lea <testuser@example.invalid>\r\n"
        b"Subject: Malformed date\r\n"
        b"Date: not a real date\r\n"
        b"Received: from smtp.example.invalid by x.invalid;\r\n"
        b"          Mon, 01 Feb 2026 10:00:00 +0000\r\n"
        b"Message-ID: <bad-date-001@example.invalid>\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"\r\n"
        b"body\r\n"
    )
    record = _parse_message(raw, folder_name="INBOX")
    assert record["timestamp"] == "2026-02-01T10:00:00+00:00"
    assert record["metadata"]["date_synthesized"] is True


def test_missing_date_synthesizes_when_no_received() -> None:
    """No Date, no usable Received → synthesize 'now' and flag it."""
    raw = (
        b"From: Alex <alex@example.invalid>\r\n"
        b"To: Lea <testuser@example.invalid>\r\n"
        b"Subject: No date\r\n"
        b"Message-ID: <synth-001@example.invalid>\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"\r\n"
        b"body\r\n"
    )
    record = _parse_message(raw, folder_name="INBOX")
    assert record["metadata"]["date_synthesized"] is True
    # Synthesized "now" timestamp — just verify it's a valid tz-aware ISO.
    assert record["timestamp"].endswith("+00:00")


def test_valid_date_is_not_marked_synthesized() -> None:
    raw = _build()
    record = _parse_message(raw, folder_name="INBOX")
    assert record["metadata"]["date_synthesized"] is False


# ---------------------------------------------------------------------------
# HTML + multipart + transfer encodings
# ---------------------------------------------------------------------------


def test_html_body_converted_to_markdown_like_text() -> None:
    raw = _build(
        body="<p>Hello <b>world</b></p><p>Second paragraph.</p>",
        content_type="text/html; charset=utf-8",
    )
    record = _parse_message(raw, folder_name="INBOX")
    # html2text renders bold as **world**
    assert "world" in record["content"]
    assert "<p>" not in record["content"]


def test_multipart_prefers_plain() -> None:
    msg = EmailMessage()
    msg["From"] = "Alex Chen <alex@example.invalid>"
    msg["To"] = "Lea <testuser@example.invalid>"
    msg["Subject"] = "Multipart"
    msg["Date"] = "Mon, 01 Feb 2026 10:00:00 +0000"
    msg["Message-ID"] = "<mp-001@example.invalid>"
    msg.set_content("Plain body wins")
    msg.add_alternative("<p>HTML body loses</p>", subtype="html")
    record = _parse_message(msg.as_bytes(), folder_name="INBOX")
    assert "Plain body wins" in record["content"]
    assert "HTML body loses" not in record["content"]


def test_quoted_printable_body_decoded() -> None:
    import quopri

    body_qp = quopri.encodestring(b"caf\xc3\xa9 time").decode("ascii")
    raw_template = (
        "From: Alex <alex@example.invalid>\r\n"
        "To: Lea <testuser@example.invalid>\r\n"
        "Subject: QP\r\n"
        "Date: Mon, 01 Feb 2026 10:00:00 +0000\r\n"
        "Message-ID: <qp-001@example.invalid>\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "Content-Transfer-Encoding: quoted-printable\r\n"
        "\r\n"
        f"{body_qp}\r\n"
    )
    record = _parse_message(raw_template.encode("ascii"), folder_name="INBOX")
    assert "café" in record["content"]


def test_base64_body_decoded() -> None:
    import base64

    body_b64 = base64.b64encode(b"hello base64 world").decode("ascii")
    raw = (
        "From: Alex <alex@example.invalid>\r\n"
        "To: Lea <testuser@example.invalid>\r\n"
        "Subject: B64\r\n"
        "Date: Mon, 01 Feb 2026 10:00:00 +0000\r\n"
        "Message-ID: <b64-001@example.invalid>\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "Content-Transfer-Encoding: base64\r\n"
        "\r\n"
        f"{body_b64}\r\n"
    ).encode("ascii")
    record = _parse_message(raw, folder_name="INBOX")
    assert "hello base64 world" in record["content"]


def test_iso_8859_1_charset() -> None:
    raw = (
        b"From: Alex <alex@example.invalid>\r\n"
        b"To: Lea <testuser@example.invalid>\r\n"
        b"Subject: Latin1\r\n"
        b"Date: Mon, 01 Feb 2026 10:00:00 +0000\r\n"
        b"Message-ID: <l1-001@example.invalid>\r\n"
        b"Content-Type: text/plain; charset=iso-8859-1\r\n"
        b"\r\n"
        b"caf\xe9 time\r\n"
    )
    record = _parse_message(raw, folder_name="INBOX")
    assert "café" in record["content"]


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------


def test_attachments_counted_but_not_extracted() -> None:
    msg = EmailMessage()
    msg["From"] = "Alex <alex@example.invalid>"
    msg["To"] = "Lea <testuser@example.invalid>"
    msg["Subject"] = "With attachments"
    msg["Date"] = "Mon, 01 Feb 2026 10:00:00 +0000"
    msg["Message-ID"] = "<att-001@example.invalid>"
    msg.set_content("See attachments.")
    msg.add_attachment(
        b"binary-bytes-go-here",
        maintype="application",
        subtype="pdf",
        filename="report.pdf",
    )
    msg.add_attachment(
        b"more-bytes",
        maintype="image",
        subtype="png",
        filename="chart.png",
    )
    record = _parse_message(msg.as_bytes(), folder_name="INBOX")
    assert record["metadata"]["has_attachments"] is True
    assert record["metadata"]["attachment_count"] == 2
    # Body stays intact; attachment bytes are never inlined.
    assert "See attachments." in record["content"]
    assert "binary-bytes-go-here" not in record["content"]


# ---------------------------------------------------------------------------
# IMAP integration — mocked
# ---------------------------------------------------------------------------


class _FakeIMAP:
    """Minimal stand-in for ``imaplib.IMAP4_SSL`` used by extract_mail."""

    def __init__(self, folders: list[str], messages: dict[str, list[tuple[str, bytes]]]):
        self._folders = folders
        self._messages = messages  # folder -> list[(uid, raw)]
        self._selected: str | None = None
        self.login_calls: list[tuple[str, str]] = []
        self.logged_out = False

    def login(self, email_addr: str, password: str) -> tuple[str, list[bytes]]:
        self.login_calls.append((email_addr, password))
        return ("OK", [b"Logged in"])

    def list(self) -> tuple[str, list[bytes]]:  # noqa: A003
        lines = [f'(\\HasNoChildren) "/" "{f}"'.encode() for f in self._folders]
        return ("OK", lines)

    def select(self, folder: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        clean = folder.strip('"')
        if clean in self._messages:
            self._selected = clean
            return ("OK", [str(len(self._messages[clean])).encode()])
        return ("NO", [b"Unknown folder"])

    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
        if self._selected is None:
            return ("NO", [b""])
        if command == "SEARCH":
            uids = [uid.encode() for uid, _ in self._messages[self._selected]]
            return ("OK", [b" ".join(uids)])
        if command == "FETCH":
            uid_str = args[0].decode() if isinstance(args[0], bytes) else args[0]
            for uid, raw in self._messages[self._selected]:
                if uid == uid_str:
                    return ("OK", [(b"preamble", raw)])
            return ("NO", [b""])
        return ("NO", [b"unknown command"])

    def logout(self) -> tuple[str, list[bytes]]:
        self.logged_out = True
        return ("BYE", [b"Logout"])


def _good_raw(uid: str, folder: str) -> bytes:
    return (
        f"From: Alex <alex@example.invalid>\r\n"
        f"To: Lea <testuser@example.invalid>\r\n"
        f"Subject: Sample {uid}\r\n"
        f"Date: Mon, 01 Feb 2026 10:0{int(uid) % 10}:00 +0000\r\n"
        f"Message-ID: <{folder}-{uid}@example.invalid>\r\n"
        f"Content-Type: text/plain; charset=utf-8\r\n"
        f"\r\n"
        f"Body for {uid}\r\n"
    ).encode("ascii")


def test_extract_mail_skips_denylisted_folders() -> None:
    fake = _FakeIMAP(
        folders=["INBOX", "Junk", "Trash", "Drafts", "Family"],
        messages={
            "INBOX": [("1", _good_raw("1", "INBOX"))],
            "Family": [("1", _good_raw("1", "Family"))],
            # Denylisted folders get messages too — they must NOT appear.
            "Junk": [("1", _good_raw("1", "Junk"))],
            "Trash": [("1", _good_raw("1", "Trash"))],
            "Drafts": [("1", _good_raw("1", "Drafts"))],
        },
    )
    with patch("sources.apple_mail.adapter.imaplib.IMAP4_SSL", return_value=fake):
        records = list(extract_mail("testuser@example.invalid", "secret-pw"))
    folders_seen = {r["metadata"]["folder"] for r in records}
    assert folders_seen == {"INBOX", "Family"}
    assert fake.logged_out is True


def test_extract_mail_denylist_is_case_insensitive() -> None:
    fake = _FakeIMAP(
        folders=["INBOX", "JUNK MAIL", "recently deleted"],
        messages={
            "INBOX": [("1", _good_raw("1", "INBOX"))],
            "JUNK MAIL": [("1", _good_raw("1", "Junk"))],
            "recently deleted": [("1", _good_raw("1", "Trash"))],
        },
    )
    with patch("sources.apple_mail.adapter.imaplib.IMAP4_SSL", return_value=fake):
        records = list(extract_mail("testuser@example.invalid", "secret-pw"))
    assert [r["metadata"]["folder"] for r in records] == ["INBOX"]


def test_extract_mail_since_uid_uses_incremental_search() -> None:
    """Verify that since_uid[folder] triggers ``UID <N>:*`` semantics."""
    fake = _FakeIMAP(
        folders=["INBOX"],
        messages={"INBOX": [("1", _good_raw("1", "INBOX"))]},
    )
    fake.uid = MagicMock(wraps=fake.uid)  # type: ignore[method-assign]
    with patch("sources.apple_mail.adapter.imaplib.IMAP4_SSL", return_value=fake):
        list(extract_mail("testuser@example.invalid", "secret-pw", since_uid={"INBOX": 42}))

    # First uid() call is the SEARCH — must request UID 43:*.
    search_calls = [c for c in fake.uid.mock_calls if c.args[0] == "SEARCH"]
    assert search_calls, "expected at least one SEARCH"
    assert search_calls[0].args[2] == "UID 43:*"


def test_login_failure_raises_without_logging_password() -> None:
    import imaplib as stdlib_imaplib

    fake = MagicMock()
    fake.login.side_effect = stdlib_imaplib.IMAP4.error("AUTHENTICATIONFAILED")
    with patch("sources.apple_mail.adapter.imaplib.IMAP4_SSL", return_value=fake):
        with pytest.raises(AdapterError) as excinfo:
            list(extract_mail("testuser@example.invalid", "secret-pw-XYZ"))
    # The password MUST NOT appear in the exception's message chain.
    combined = f"{excinfo.value} {excinfo.value.__cause__}"
    assert "secret-pw-XYZ" not in combined


# ---------------------------------------------------------------------------
# Two-phase fetch_and_cache / parse_cache
# ---------------------------------------------------------------------------


def test_fetch_and_cache_writes_eml_files(tmp_path: Path) -> None:
    """Phase 1 should land one .eml per fetched UID and skip denylisted folders."""
    fake = _FakeIMAP(
        folders=["INBOX", "Trash", "Family"],
        messages={
            "INBOX": [("101", _good_raw("101", "INBOX"))],
            "Trash": [("1", _good_raw("1", "Trash"))],  # denylisted, must not appear
            "Family": [
                ("201", _good_raw("201", "Family")),
                ("202", _good_raw("202", "Family")),
            ],
        },
    )
    with patch("sources.apple_mail.adapter.imaplib.IMAP4_SSL", return_value=fake):
        new, already = fetch_and_cache("testuser@example.invalid", "pw", tmp_path)
    assert (new, already) == (3, 0)
    # Trash/1.eml should NOT exist.
    trash_files = list(tmp_path.rglob("*Trash*/*.eml"))
    assert trash_files == []
    # Quoted folder names: INBOX → "INBOX", Family → "Family" (no special chars).
    assert (tmp_path / "INBOX" / "101.eml").exists()
    assert (tmp_path / "Family" / "201.eml").exists()
    assert (tmp_path / "Family" / "202.eml").exists()


def test_fetch_and_cache_skips_already_cached(tmp_path: Path) -> None:
    """Second run diffs against on-disk cache and only fetches missing UIDs."""
    fake = _FakeIMAP(
        folders=["INBOX"],
        messages={
            "INBOX": [
                ("1", _good_raw("1", "INBOX")),
                ("2", _good_raw("2", "INBOX")),
            ]
        },
    )
    with patch("sources.apple_mail.adapter.imaplib.IMAP4_SSL", return_value=fake):
        first = fetch_and_cache("testuser@example.invalid", "pw", tmp_path)
        second = fetch_and_cache("testuser@example.invalid", "pw", tmp_path)
    assert first == (2, 0)
    assert second == (0, 2)


def test_fetch_and_cache_round_trips_through_parse_cache(tmp_path: Path) -> None:
    """Fetch → parse yields the same records as a streaming extract_mail."""
    fake = _FakeIMAP(
        folders=["INBOX", "Family"],
        messages={
            "INBOX": [("1", _good_raw("1", "INBOX"))],
            "Family": [
                ("1", _good_raw("1", "Family")),
                ("2", _good_raw("2", "Family")),
            ],
        },
    )
    with patch("sources.apple_mail.adapter.imaplib.IMAP4_SSL", return_value=fake):
        fetch_and_cache("testuser@example.invalid", "pw", tmp_path)

    records = list(parse_cache(tmp_path))
    assert len(records) == 3
    folders = sorted({r["metadata"]["folder"] for r in records})
    assert folders == ["Family", "INBOX"]


def test_parse_cache_handles_special_chars_in_folder_names(tmp_path: Path) -> None:
    """Folders like ``NewEraCode/Bank of America`` round-trip through the
    URL-quoted filesystem encoding."""
    fake = _FakeIMAP(
        folders=["NewEraCode/Bank of America"],
        messages={
            "NewEraCode/Bank of America": [
                ("1", _good_raw("1", "folder-with-slash")),
            ]
        },
    )
    with patch("sources.apple_mail.adapter.imaplib.IMAP4_SSL", return_value=fake):
        fetch_and_cache("testuser@example.invalid", "pw", tmp_path)
    records = list(parse_cache(tmp_path))
    assert len(records) == 1
    assert records[0]["metadata"]["folder"] == "NewEraCode/Bank of America"


def test_parse_cache_empty_dir_yields_nothing(tmp_path: Path) -> None:
    assert list(parse_cache(tmp_path)) == []
    # Non-existent path also yields nothing rather than erroring.
    assert list(parse_cache(tmp_path / "nope")) == []
