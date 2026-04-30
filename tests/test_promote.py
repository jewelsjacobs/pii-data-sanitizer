"""Tests for sanitizer.promote — converting sanitized records to markdown.

Each promote function takes a sanitized record (or list of records, for
imessage) and returns ``(filename, markdown)``. These tests verify:

* the filename matches the Crew convention ``YYYY-MM-DD — Type — Title.md``
* the YAML frontmatter has the keys downstream agents expect
* sanitized tokens like ``[PERSON_1]`` pass through verbatim
* edge cases (empty content, missing fields, unsafe filename chars,
  collisions across multiple records) don't crash and don't produce
  invalid filenames or YAML

No real PII anywhere — every fixture uses synthetic ``[TOKEN_N]``-style
content as if the sanitizer already ran.
"""

from __future__ import annotations

import re

from sanitizer.promote import (
    promote_apple_mail,
    promote_ical,
    promote_imessage,
    promote_local_files,
)

_FILENAME_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}|0000-00-00) \u2014 [^/\\<>:\"|?*]+ \u2014 [^/\\<>:\"|?*]+\.md$"
)


def _frontmatter(markdown: str) -> str:
    assert markdown.startswith("---\n")
    end = markdown.index("\n---\n", 4)
    return markdown[4:end]


def _has_yaml_key(markdown: str, key: str) -> bool:
    fm = _frontmatter(markdown)
    return any(line.startswith(f"{key}:") for line in fm.splitlines())


def _yaml_value(markdown: str, key: str) -> str:
    """Return the raw text after ``key: `` on the first matching line."""
    fm = _frontmatter(markdown)
    for line in fm.splitlines():
        if line.startswith(f"{key}:"):
            return line[len(key) + 1 :].strip()
    raise KeyError(key)


# --------------------------------------------------------------------------- #
# ical
# --------------------------------------------------------------------------- #


def test_promote_ical_basic_meeting() -> None:
    record = {
        "source": "ical",
        "source_id": "evt-12345",
        "timestamp": "2026-04-15T14:00:00+00:00",
        "content": "Quarterly review with [PERSON_1]",
        "metadata": {
            "calendar_name": "Work",
            "summary": "Quarterly review",
            "description": "Discuss roadmap with [PERSON_1]",
            "location": "Conference Room B",
            "dtstart": "2026-04-15T14:00:00+00:00",
            "dtend": "2026-04-15T15:00:00+00:00",
            "rrule": None,
            "organizer": "[PERSON_1]",
            "attendees": ["[PERSON_2]", "[PERSON_3]"],
            "url": None,
            "structured_location": None,
        },
    }
    filename, markdown = promote_ical(record)

    assert _FILENAME_RE.match(filename), filename
    assert filename.startswith("2026-04-15 \u2014 Meeting \u2014 ")
    assert "Quarterly review" in filename
    assert filename.endswith(".md")

    assert _yaml_value(markdown, "type") == '"meeting"'
    assert _yaml_value(markdown, "date") == '"2026-04-15"'
    assert _yaml_value(markdown, "time") == '"14:00\u201315:00"'
    assert _yaml_value(markdown, "location") == '"Conference Room B"'
    assert _yaml_value(markdown, "calendar-event-id") == '"evt-12345"'
    assert _yaml_value(markdown, "source-record-id") == '"evt-12345"'
    assert _yaml_value(markdown, "recurring") == "false"
    # Sanitized tokens pass through into the body verbatim.
    assert "[PERSON_1]" in markdown
    assert "[PERSON_2]" in markdown
    assert "[[05-People/[PERSON_1]]]" in markdown


def test_promote_ical_recurring_with_rrule() -> None:
    record = {
        "source": "ical",
        "source_id": "evt-recur",
        "timestamp": "2026-04-15T09:00:00+00:00",
        "content": "Standup",
        "metadata": {
            "summary": "Daily standup",
            "rrule": "FREQ=DAILY;COUNT=20",
            "dtstart": "2026-04-15T09:00:00+00:00",
            "dtend": "2026-04-15T09:15:00+00:00",
        },
    }
    _, markdown = promote_ical(record)
    assert _yaml_value(markdown, "recurring") == "true"
    assert "FREQ=DAILY;COUNT=20" in markdown


def test_promote_ical_all_day_event_has_no_time() -> None:
    record = {
        "source": "ical",
        "source_id": "evt-allday",
        "timestamp": "2026-07-04",
        "content": "Holiday",
        "metadata": {
            "summary": "Holiday",
            "dtstart": "2026-07-04",
            "dtend": "2026-07-05",
        },
    }
    filename, markdown = promote_ical(record)
    assert filename.startswith("2026-07-04 \u2014 Meeting \u2014 ")
    # All-day → date-only, time field is empty string in frontmatter
    assert _yaml_value(markdown, "date") == '"2026-07-04"'
    assert _yaml_value(markdown, "time") == '""'


def test_promote_ical_empty_summary_yields_untitled() -> None:
    record = {
        "source": "ical",
        "source_id": "evt-empty",
        "timestamp": "2026-04-15T00:00:00+00:00",
        "content": "",
        "metadata": {"summary": "", "dtstart": "2026-04-15T00:00:00+00:00"},
    }
    filename, _ = promote_ical(record)
    assert "Untitled event" in filename


# --------------------------------------------------------------------------- #
# apple-mail
# --------------------------------------------------------------------------- #


def test_promote_apple_mail_basic() -> None:
    record = {
        "source": "apple-mail",
        "source_id": "<msg-id-001@example.invalid>",
        "timestamp": "2026-04-10T08:00:00+00:00",
        "content": "Hi [PERSON_1], here's the doc.",
        "metadata": {
            "folder": "INBOX",
            "from": "[PERSON_1] <[EMAIL_1]>",
            "to": ["[MY_EMAIL]"],
            "cc": [],
            "subject": "Quarterly numbers",
            "message_id": "<msg-id-001@example.invalid>",
            "in_reply_to": "",
            "references": [],
            "has_attachments": False,
            "attachment_count": 0,
            "date_synthesized": False,
        },
    }
    filename, markdown = promote_apple_mail(record)
    assert _FILENAME_RE.match(filename), filename
    assert filename.startswith("2026-04-10 \u2014 Email \u2014 Quarterly numbers")
    assert _yaml_value(markdown, "type") == '"email-info"'
    assert _yaml_value(markdown, "subject") == '"Quarterly numbers"'
    assert _yaml_value(markdown, "from") == '"[PERSON_1] <[EMAIL_1]>"'
    assert _yaml_value(markdown, "source-email-id") == '"<msg-id-001@example.invalid>"'
    assert _yaml_value(markdown, "has-attachments") == "false"
    assert "[PERSON_1]" in markdown
    assert "[MY_EMAIL]" in markdown


def test_promote_apple_mail_synthesized_date_is_flagged() -> None:
    record = {
        "source": "apple-mail",
        "source_id": "msg-002",
        "timestamp": "2026-04-10T08:00:00+00:00",
        "content": "Body",
        "metadata": {
            "subject": "No date header",
            "from": "[PERSON_1] <[EMAIL_1]>",
            "date_synthesized": True,
        },
    }
    _, markdown = promote_apple_mail(record)
    assert _yaml_value(markdown, "date-synthesized") == "true"
    assert "*(synthesized)*" in markdown


def test_promote_apple_mail_subject_with_unsafe_chars() -> None:
    record = {
        "source": "apple-mail",
        "source_id": "msg-003",
        "timestamp": "2026-04-10T08:00:00+00:00",
        "content": "",
        "metadata": {
            "subject": "Re: Q3/Q4 report — what's the status?",
            "from": "[PERSON_1] <[EMAIL_1]>",
        },
    }
    filename, _ = promote_apple_mail(record)
    # No raw "/" in the filename, even though the subject had one.
    assert "/" not in filename
    assert filename.endswith(".md")
    assert _FILENAME_RE.match(filename), filename


def test_promote_apple_mail_empty_subject() -> None:
    record = {
        "source": "apple-mail",
        "source_id": "msg-004",
        "timestamp": "2026-04-10T08:00:00+00:00",
        "content": "",
        "metadata": {"subject": "", "from": ""},
    }
    filename, markdown = promote_apple_mail(record)
    assert "(no subject)" in filename
    assert "*(empty body)*" in markdown


def test_promote_apple_mail_attachment_count_in_body() -> None:
    record = {
        "source": "apple-mail",
        "source_id": "msg-005",
        "timestamp": "2026-04-10T08:00:00+00:00",
        "content": "see attached",
        "metadata": {
            "subject": "Slides",
            "from": "[PERSON_1] <[EMAIL_1]>",
            "has_attachments": True,
            "attachment_count": 3,
        },
    }
    _, markdown = promote_apple_mail(record)
    assert _yaml_value(markdown, "has-attachments") == "true"
    assert _yaml_value(markdown, "attachment-count") == "3"
    assert "3 (skipped by sanitizer)" in markdown


# --------------------------------------------------------------------------- #
# local-files
# --------------------------------------------------------------------------- #


def test_promote_local_files_pdf_with_ocr() -> None:
    record = {
        "source": "local-files",
        "source_id": "sha256:abc123",
        "timestamp": "2026-03-01T00:00:00+00:00",
        "content": "Scanned page text [PERSON_1]",
        "metadata": {
            "path": "/synthetic/local-files/notes/2026-03-01-scan.pdf",
            "format": "pdf",
            "extraction_method": "ocr",
            "ocr_low_confidence": True,
            "page_count": 5,
            "size_bytes": 12345,
        },
    }
    filename, markdown = promote_local_files(record)
    assert _FILENAME_RE.match(filename), filename
    assert "2026-03-01-scan" in filename
    assert _yaml_value(markdown, "type") == '"reference"'
    assert _yaml_value(markdown, "format") == '"pdf"'
    assert _yaml_value(markdown, "extraction-method") == '"ocr"'
    assert _yaml_value(markdown, "ocr-low-confidence") == "true"
    assert _yaml_value(markdown, "page-count") == "5"
    assert "below threshold" in markdown
    assert "[PERSON_1]" in markdown


def test_promote_local_files_empty_content() -> None:
    record = {
        "source": "local-files",
        "source_id": "sha256:empty",
        "timestamp": "2026-03-01T00:00:00+00:00",
        "content": "",
        "metadata": {
            "path": "/some/place/empty.txt",
            "format": "txt",
            "extraction_method": "native",
            "ocr_low_confidence": False,
            "page_count": 1,
            "size_bytes": 0,
        },
    }
    _, markdown = promote_local_files(record)
    assert "*(empty document)*" in markdown


def test_promote_local_files_missing_path_falls_back() -> None:
    record = {
        "source": "local-files",
        "source_id": "sha256:nopath",
        "timestamp": "2026-03-01T00:00:00+00:00",
        "content": "stuff",
        "metadata": {"format": "md"},
    }
    filename, markdown = promote_local_files(record)
    assert "Untitled document" in filename
    assert "Untitled document" in markdown


# --------------------------------------------------------------------------- #
# imessage (multi-record grouping)
# --------------------------------------------------------------------------- #


def _imsg(
    rowid: str,
    ts: str,
    chat: str,
    text: str,
    *,
    from_me: bool = False,
    handle: str = "[PHONE_1]",
) -> dict:
    return {
        "source": "imessage",
        "source_id": rowid,
        "timestamp": ts,
        "content": text,
        "metadata": {
            "chat_identifier": chat,
            "chat_is_group": False,
            "handle_id": "me" if from_me else handle,
            "from_me": from_me,
            "service": "iMessage",
            "has_attachments": False,
            "attachment_count": 0,
            "is_reply": False,
            "thread_originator_guid": None,
            "was_edited": False,
        },
    }


def test_promote_imessage_groups_by_chat_and_day() -> None:
    records = [
        _imsg("1", "2026-04-10T14:00:00+00:00", "[PHONE_1]", "hi", handle="[PHONE_1]"),
        _imsg("2", "2026-04-10T14:01:00+00:00", "[PHONE_1]", "hello", from_me=True),
        _imsg("3", "2026-04-11T09:00:00+00:00", "[PHONE_1]", "morning", handle="[PHONE_1]"),
        _imsg("4", "2026-04-10T14:05:00+00:00", "[PHONE_2]", "yo", handle="[PHONE_2]"),
    ]
    notes = list(promote_imessage(records))
    # 3 groups: ([PHONE_1], 2026-04-10), ([PHONE_1], 2026-04-11), ([PHONE_2], 2026-04-10)
    assert len(notes) == 3
    filenames = [n[0] for n in notes]
    assert all(f.endswith(".md") for f in filenames)
    assert all(_FILENAME_RE.match(f) for f in filenames), filenames

    # The 2026-04-10 [PHONE_1] note should contain both messages and
    # record both source-record-ids in frontmatter.
    target = [m for f, m in notes if "2026-04-10" in f and "[PHONE_1]" in f][0]
    assert _yaml_value(target, "type") == '"chat"'
    assert _yaml_value(target, "message-count") == "2"
    assert "hi" in target
    assert "hello" in target
    # source-record-ids is a YAML list — both ids must appear
    assert '"1"' in target and '"2"' in target


def test_promote_imessage_group_chat_label() -> None:
    records = [
        {
            "source": "imessage",
            "source_id": "g1",
            "timestamp": "2026-04-10T14:00:00+00:00",
            "content": "team msg",
            "metadata": {
                "chat_identifier": "chat000000012345",
                "chat_is_group": True,
                "handle_id": "[PHONE_1]",
                "from_me": False,
                "service": "iMessage",
                "has_attachments": False,
                "attachment_count": 0,
                "was_edited": False,
            },
        },
    ]
    notes = list(promote_imessage(records))
    assert len(notes) == 1
    filename, markdown = notes[0]
    assert "Group" in filename
    assert _yaml_value(markdown, "chat-is-group") == "true"
    assert _yaml_value(markdown, "chat-identifier") == '"chat000000012345"'


def test_promote_imessage_attachment_marker_in_transcript() -> None:
    records = [
        {
            "source": "imessage",
            "source_id": "a1",
            "timestamp": "2026-04-10T14:00:00+00:00",
            "content": "",
            "metadata": {
                "chat_identifier": "[PHONE_1]",
                "chat_is_group": False,
                "handle_id": "[PHONE_1]",
                "from_me": False,
                "service": "iMessage",
                "has_attachments": True,
                "attachment_count": 2,
                "was_edited": False,
            },
        },
    ]
    _, markdown = next(iter(promote_imessage(records)))
    assert "2 attachment(s) skipped" in markdown


def test_promote_imessage_edited_marker() -> None:
    records = [
        {
            "source": "imessage",
            "source_id": "e1",
            "timestamp": "2026-04-10T14:00:00+00:00",
            "content": "fixed typo",
            "metadata": {
                "chat_identifier": "[PHONE_1]",
                "chat_is_group": False,
                "handle_id": "[PHONE_1]",
                "from_me": False,
                "service": "iMessage",
                "has_attachments": False,
                "attachment_count": 0,
                "was_edited": True,
            },
        },
    ]
    _, markdown = next(iter(promote_imessage(records)))
    assert "*(edited)*" in markdown


def test_promote_imessage_empty_iterable_yields_nothing() -> None:
    assert list(promote_imessage([])) == []


# --------------------------------------------------------------------------- #
# round-trip stability
# --------------------------------------------------------------------------- #


def test_promote_ical_is_deterministic() -> None:
    record = {
        "source": "ical",
        "source_id": "evt-x",
        "timestamp": "2026-04-15T14:00:00+00:00",
        "content": "x",
        "metadata": {"summary": "x", "dtstart": "2026-04-15T14:00:00+00:00"},
    }
    a = promote_ical(record)
    b = promote_ical(record)

    # Body is deterministic up to the `created` timestamp; strip that
    # one line and compare the rest.
    def _strip_created(m: str) -> str:
        return "\n".join(line for line in m.splitlines() if not line.startswith("created:"))

    assert a[0] == b[0]
    assert _strip_created(a[1]) == _strip_created(b[1])


def test_promote_yaml_double_quotes_internal_quotes() -> None:
    """Double quotes inside a string are escaped, so YAML stays valid."""
    record = {
        "source": "ical",
        "source_id": "evt-q",
        "timestamp": "2026-04-15T14:00:00+00:00",
        "content": "x",
        "metadata": {
            "summary": "Quarterly all-hands",
            "location": 'Room "B"',
            "dtstart": "2026-04-15T14:00:00+00:00",
        },
    }
    _, markdown = promote_ical(record)
    fm = _frontmatter(markdown)
    # The location string contains a double quote; the YAML serializer
    # must escape it so the file stays parseable.
    assert '\\"B\\"' in fm
