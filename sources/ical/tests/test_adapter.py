"""Tests for the ical adapter.

Fixtures are hand-written ``.ics`` strings embedded in the test module
(rather than on-disk files) so the test suite is self-contained and the
fixture tree doesn't need its own gitignore entry. Every value here is
synthetic — fake UIDs, `example.invalid` emails, made-up venue names.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sources.ical.adapter import AdapterError, extract_events

NORMAL_ICS = """\
BEGIN:VCALENDAR
VERSION:2.0
X-WR-CALNAME:Personal
BEGIN:VEVENT
UID:evt-normal-001@example.invalid
SUMMARY:Coffee with Alex
DESCRIPTION:Quick sync before the meeting
LOCATION:Blue Jay Cafe
DTSTART:20260605T153000Z
DTEND:20260605T163000Z
END:VEVENT
END:VCALENDAR
"""

ALL_DAY_ICS = """\
BEGIN:VCALENDAR
VERSION:2.0
X-WR-CALNAME:US Holidays
BEGIN:VEVENT
UID:evt-allday-001@example.invalid
SUMMARY:Thanksgiving
DTSTART;VALUE=DATE:20261126
DTEND;VALUE=DATE:20261127
END:VEVENT
END:VCALENDAR
"""

RECURRING_ICS = """\
BEGIN:VCALENDAR
VERSION:2.0
X-WR-CALNAME:Work
BEGIN:VEVENT
UID:evt-recurring-001@example.invalid
SUMMARY:Team standup
DTSTART:20260601T160000Z
DTEND:20260601T163000Z
RRULE:FREQ=WEEKLY;BYDAY=MO
ORGANIZER;CN=Taylor Reyes:mailto:taylor@example.invalid
ATTENDEE;CN=Sam Park:mailto:sam@example.invalid
ATTENDEE;CN=Jordan Bell:mailto:jordan@example.invalid
END:VEVENT
END:VCALENDAR
"""

STRUCTURED_LOCATION_ICS = """\
BEGIN:VCALENDAR
VERSION:2.0
X-WR-CALNAME:Personal
BEGIN:VEVENT
UID:evt-location-001@example.invalid
SUMMARY:Lunch meeting
DTSTART:20260710T190000Z
DTEND:20260710T200000Z
X-APPLE-STRUCTURED-LOCATION;VALUE=URI;X-TITLE=Blue Jay Cafe:geo:47.6194,-122.3129
LOCATION:Blue Jay Cafe\\, 411 15th Ave E\\, Seattle\\, WA 98112
END:VEVENT
END:VCALENDAR
"""

CONFERENCE_URL_ICS = """\
BEGIN:VCALENDAR
VERSION:2.0
X-WR-CALNAME:Work
BEGIN:VEVENT
UID:evt-conf-001@example.invalid
SUMMARY:Quarterly review
DTSTART:20260815T170000Z
DTEND:20260815T180000Z
URL:https://zoom.us/j/1234567890?pwd=aBcDefGhIjKlMnOpQr
LOCATION:https://zoom.us/j/1234567890?pwd=aBcDefGhIjKlMnOpQr
END:VEVENT
END:VCALENDAR
"""

MISSING_UID_ICS = """\
BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
SUMMARY:Orphan event
DTSTART:20260701T120000Z
END:VEVENT
END:VCALENDAR
"""

FLOATING_DTSTART_ICS = """\
BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:evt-floating-001@example.invalid
SUMMARY:Event with floating time
DTSTART:20260701T120000
DTEND:20260701T130000
END:VEVENT
END:VCALENDAR
"""


def _write(tmp_path: Path, name: str, ics: str) -> Path:
    p = tmp_path / name
    p.write_text(ics)
    return p


def test_normal_event(tmp_path: Path) -> None:
    ics = _write(tmp_path, "normal.ics", NORMAL_ICS)
    records = list(extract_events([ics]))
    assert len(records) == 1
    r = records[0]
    assert r["source"] == "ical"
    assert r["source_id"] == "evt-normal-001@example.invalid"
    assert r["timestamp"] == "2026-06-05T15:30:00+00:00"
    assert r["content"] == "Coffee with Alex\n\nQuick sync before the meeting"
    assert r["metadata"]["calendar_name"] == "Personal"
    assert r["metadata"]["location"] == "Blue Jay Cafe"
    assert r["metadata"]["dtend"] == "2026-06-05T16:30:00+00:00"
    assert r["metadata"]["rrule"] is None
    assert r["metadata"]["attendees"] == []


def test_all_day_event(tmp_path: Path) -> None:
    ics = _write(tmp_path, "allday.ics", ALL_DAY_ICS)
    records = list(extract_events([ics]))
    r = records[0]
    assert r["timestamp"] == "2026-11-26"  # date-only, no time component
    assert r["metadata"]["dtstart"] == "2026-11-26"
    assert r["metadata"]["dtend"] == "2026-11-27"


def test_recurring_event_with_attendees(tmp_path: Path) -> None:
    ics = _write(tmp_path, "recurring.ics", RECURRING_ICS)
    records = list(extract_events([ics]))
    r = records[0]
    assert r["metadata"]["rrule"] == "FREQ=WEEKLY;BYDAY=MO"
    assert r["metadata"]["organizer"] == "Taylor Reyes <taylor@example.invalid>"
    assert r["metadata"]["attendees"] == [
        "Sam Park <sam@example.invalid>",
        "Jordan Bell <jordan@example.invalid>",
    ]


def test_structured_location(tmp_path: Path) -> None:
    ics = _write(tmp_path, "location.ics", STRUCTURED_LOCATION_ICS)
    records = list(extract_events([ics]))
    sl = records[0]["metadata"]["structured_location"]
    assert sl == {"lat": 47.6194, "lon": -122.3129, "title": "Blue Jay Cafe"}


def test_conference_url_preserved_raw(tmp_path: Path) -> None:
    """The adapter never redacts. Auth-token-bearing URLs go through raw so
    the sanitize stage can match and redact them there.
    """
    ics = _write(tmp_path, "conf.ics", CONFERENCE_URL_ICS)
    records = list(extract_events([ics]))
    r = records[0]
    assert "pwd=aBcDefGhIjKlMnOpQr" in r["metadata"]["url"]
    assert "pwd=aBcDefGhIjKlMnOpQr" in r["metadata"]["location"]


def test_directory_walk_sorted(tmp_path: Path) -> None:
    _write(tmp_path, "z.ics", NORMAL_ICS)
    _write(tmp_path, "a.ics", ALL_DAY_ICS)
    records = list(extract_events([tmp_path]))
    # Files are walked in sorted order; "a.ics" (all-day) comes first.
    assert records[0]["source_id"] == "evt-allday-001@example.invalid"
    assert records[1]["source_id"] == "evt-normal-001@example.invalid"


def test_missing_uid_halts(tmp_path: Path) -> None:
    ics = _write(tmp_path, "no-uid.ics", MISSING_UID_ICS)
    with pytest.raises(AdapterError, match="missing UID"):
        list(extract_events([ics]))


def test_floating_datetime_interpreted_as_local(tmp_path: Path) -> None:
    """Apple Calendar exports often have floating times (events created
    without a specific TZ). The adapter interprets them as the machine's
    local tz and converts to UTC rather than halting.
    """
    ics = _write(tmp_path, "floating.ics", FLOATING_DTSTART_ICS)
    records = list(extract_events([ics]))
    assert len(records) == 1
    ts = records[0]["timestamp"]
    # Whatever local tz the test runner is in, the result must be a
    # tz-aware ISO string ending in a UTC offset.
    assert ts.endswith("+00:00") or ts.endswith("Z")
    assert "T" in ts  # not a date-only string


def test_unknown_path_halts(tmp_path: Path) -> None:
    junk = tmp_path / "not-a-calendar.txt"
    junk.write_text("hello")
    with pytest.raises(AdapterError, match="Not a .ics"):
        list(extract_events([junk]))


def test_idempotent_on_same_input(tmp_path: Path) -> None:
    ics = _write(tmp_path, "normal.ics", NORMAL_ICS)
    first = list(extract_events([ics]))
    second = list(extract_events([ics]))
    assert first == second
