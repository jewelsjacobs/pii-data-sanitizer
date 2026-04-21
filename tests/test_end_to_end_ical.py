"""Extract → sanitize → manifest smoke test for the ical source.

Drives the same path the user's ``make extract-ical`` + ``make sanitize``
flow takes, but through in-memory APIs rather than the CLI scripts so
the test doesn't depend on a config file.
"""

from __future__ import annotations

import json
from pathlib import Path

from sanitizer.manifest import write_manifest
from sanitizer.mapping import Mapping
from sanitizer.pipeline import KeepList, SelfIdentity, sanitize
from sources.ical.adapter import extract_events

ICS = """\
BEGIN:VCALENDAR
VERSION:2.0
X-WR-CALNAME:Work
BEGIN:VEVENT
UID:evt-smoke-001@example.invalid
SUMMARY:Quarterly review with Alex
DESCRIPTION:Join via the usual link
DTSTART:20260815T170000Z
DTEND:20260815T180000Z
URL:https://zoom.us/j/1234567890?pwd=aBcDefGhIjKlMnOpQr
ORGANIZER;CN=Sam Park:mailto:sam@example.invalid
ATTENDEE;CN=Alex Jones:mailto:alex@example.invalid
END:VEVENT
END:VCALENDAR
"""


def test_extract_sanitize_manifest_roundtrip(tmp_path: Path) -> None:
    # --- Stage 1: extract ------------------------------------------------
    ics_file = tmp_path / "work.ics"
    ics_file.write_text(ICS)
    raw_dir = tmp_path / "raw-staging" / "ical"
    raw_dir.mkdir(parents=True)
    raw_file = raw_dir / "events.jsonl"

    with raw_file.open("w") as fout:
        for record in extract_events([ics_file]):
            fout.write(json.dumps(record) + "\n")

    raw_records = [json.loads(line) for line in raw_file.read_text().splitlines()]
    assert len(raw_records) == 1
    assert raw_records[0]["source"] == "ical"
    assert raw_records[0]["metadata"]["organizer"] == "Sam Park <sam@example.invalid>"
    # Adapter never redacts — the raw URL with auth token passes through.
    assert "pwd=" in raw_records[0]["metadata"]["url"]

    # --- Stage 2: sanitize ----------------------------------------------
    clean_dir = tmp_path / "clean-staging" / "ical"
    clean_dir.mkdir(parents=True)
    clean_file = clean_dir / "events.jsonl"

    mapping = Mapping()
    identity = SelfIdentity(emails=frozenset({"me@example.invalid"}))
    keep = KeepList()

    with clean_file.open("w") as fout:
        for record in raw_records:
            sanitized = sanitize(record, mapping=mapping, self_identity=identity, keep=keep)
            fout.write(json.dumps(sanitized) + "\n")

    clean_records = [json.loads(line) for line in clean_file.read_text().splitlines()]
    assert len(clean_records) == 1
    # Any URL — conference or otherwise — collapses to [REDACTED_LINK].
    assert clean_records[0]["metadata"]["url"] == "[REDACTED_LINK]"
    # Raw attendee email gets pseudonymized.
    assert "alex@example.invalid" not in json.dumps(clean_records[0])

    # --- Stage 3: manifest ----------------------------------------------
    manifest_path = write_manifest(
        clean_dir,
        records_in=len(raw_records),
        records_out=len(clean_records),
        entities_redacted={"PERSON": 2, "EMAIL": 2},
        new_entities_auto_classified=4,
    )
    manifest = json.loads(manifest_path.read_text())
    assert manifest["records_in"] == 1
    assert manifest["records_out"] == 1
    assert manifest["status"] == "ready_for_ingest"
    assert manifest["run_id"]
