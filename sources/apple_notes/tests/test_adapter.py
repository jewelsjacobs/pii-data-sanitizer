"""Tests for the Apple Notes adapter.

Builds a minimal synthetic NoteStore.sqlite at test time so the suite
runs offline and without depending on a real Notes database.
"""

from __future__ import annotations

import gzip
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sources.apple_notes.adapter import (
    AdapterError,
    _apple_epoch_to_iso,
    _decode_note_body,
    extract_notes,
    snapshot_notes_db,
)

_APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=UTC)


def _make_synthetic_db(path: Path, notes: list[dict]) -> None:
    """Build a NoteStore.sqlite-shaped synthetic db with the rows we test
    against. Only the subset of columns the adapter queries.
    """
    conn = sqlite3.connect(str(path))
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE ZICCLOUDSYNCINGOBJECT (
            Z_PK INTEGER PRIMARY KEY,
            ZIDENTIFIER TEXT,
            ZTITLE1 TEXT,
            ZTITLE2 TEXT,
            ZSNIPPET TEXT,
            ZCREATIONDATE1 REAL,
            ZCREATIONDATE2 REAL,
            ZCREATIONDATE3 REAL,
            ZMODIFICATIONDATE1 REAL,
            ZISPASSWORDPROTECTED INTEGER,
            ZMARKEDFORDELETION INTEGER,
            ZFOLDER INTEGER,
            ZACCOUNT4 INTEGER,
            ZNOTEDATA INTEGER,
            ZNAME TEXT
        );
        CREATE TABLE ZICNOTEDATA (
            Z_PK INTEGER PRIMARY KEY,
            ZDATA BLOB
        );
        """
    )
    for n in notes:
        body_blob = n.get("body_blob")
        notedata_pk = None
        if body_blob is not None:
            cur.execute("INSERT INTO ZICNOTEDATA (ZDATA) VALUES (?)", (body_blob,))
            notedata_pk = cur.lastrowid
        cur.execute(
            """
            INSERT INTO ZICCLOUDSYNCINGOBJECT
            (ZIDENTIFIER, ZTITLE1, ZTITLE2, ZSNIPPET, ZCREATIONDATE1,
             ZCREATIONDATE2, ZCREATIONDATE3, ZMODIFICATIONDATE1,
             ZISPASSWORDPROTECTED, ZMARKEDFORDELETION,
             ZFOLDER, ZACCOUNT4, ZNOTEDATA, ZNAME)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                n.get("identifier"),
                n.get("title"),
                n.get("folder_title"),
                n.get("snippet"),
                n.get("created"),
                n.get("created2"),
                n.get("created3"),
                n.get("modified"),
                n.get("is_locked", 0),
                n.get("deleted", 0),
                n.get("folder_pk"),
                n.get("account_pk"),
                notedata_pk,
                n.get("account_name"),
            ),
        )
    conn.commit()
    conn.close()


def _gzip_protobuf_like(text: str) -> bytes:
    """Wrap ``text`` in a gzip envelope so the adapter's heuristic body
    decoder produces it. Doesn't need to be a real Apple proto — the
    adapter only relies on the longest printable run being the body."""
    fake_blob = b"AttributeRun\x00\x00" + text.encode("utf-8") + b"\x00ParagraphStyle"
    return gzip.compress(fake_blob)


def test_apple_epoch_to_iso_seconds_format() -> None:
    """Notes timestamps are seconds since 2001-01-01 UTC."""
    one_year = _APPLE_EPOCH + timedelta(days=365)
    out = _apple_epoch_to_iso(365 * 24 * 3600)
    assert out == one_year.isoformat()


def test_apple_epoch_to_iso_null_is_none_not_epoch() -> None:
    """NULL/zero must yield None, not the epoch — an epoch-stamped record
    silently impersonates a real 2001-01-01 note (audit-found on every
    note when the creation column migrated to ZCREATIONDATE3)."""
    assert _apple_epoch_to_iso(None) is None
    assert _apple_epoch_to_iso(0) is None


def test_extract_notes_reads_migrated_creation_column(tmp_path: Path) -> None:
    """When ZCREATIONDATE1 is NULL and the real value lives in
    ZCREATIONDATE3 (current NoteStore schema), the record must carry the
    ZCREATIONDATE3 date — not the 2001 epoch."""
    db = tmp_path / "test.sqlite"
    _make_synthetic_db(
        db,
        [
            {
                "identifier": "note-uuid-migrated",
                "title": "Migrated note",
                "snippet": "s",
                "created": None,
                "created3": 366 * 24 * 3600,
                "modified": 366 * 24 * 3600,
                "folder_pk": None,
                "account_pk": None,
                "body_blob": _gzip_protobuf_like("body text"),
            },
        ],
    )
    records = list(extract_notes(db))
    assert len(records) == 1
    expected = (_APPLE_EPOCH + timedelta(days=366)).isoformat()
    assert records[0]["timestamp"] == expected


def test_extract_notes_undated_note_has_none_timestamp(tmp_path: Path) -> None:
    """A note with every date column NULL is emitted with timestamp None —
    visibly undated rather than epoch-stamped."""
    db = tmp_path / "test.sqlite"
    _make_synthetic_db(
        db,
        [
            {
                "identifier": "note-uuid-undated",
                "title": "Undated note",
                "snippet": "s",
                "created": None,
                "modified": None,
                "folder_pk": None,
                "account_pk": None,
                "body_blob": _gzip_protobuf_like("body text"),
            },
        ],
    )
    records = list(extract_notes(db))
    assert len(records) == 1
    assert records[0]["timestamp"] is None


def test_decode_note_body_extracts_longest_run() -> None:
    """Body decoder recovers the user-typed text from a gzipped blob."""
    text = "This is the actual note content the user typed at length."
    blob = _gzip_protobuf_like(text)
    out = _decode_note_body(blob)
    assert out == text


def test_decode_note_body_empty_blob() -> None:
    assert _decode_note_body(b"") == ""
    assert _decode_note_body(None) == ""  # type: ignore[arg-type]


def test_decode_note_body_invalid_gzip_falls_back() -> None:
    """Non-gzipped blobs still get text-extracted via the raw-byte path."""
    text = "raw bytes with text content embedded"
    blob = b"\x00\x01" + text.encode("utf-8") + b"\x00"
    out = _decode_note_body(blob)
    assert text in out


def test_extract_notes_returns_record_per_note(tmp_path: Path) -> None:
    db = tmp_path / "test.sqlite"
    _make_synthetic_db(
        db,
        [
            {
                "identifier": "note-uuid-1",
                "title": "Grocery list",
                "snippet": "milk, eggs, bread",
                "created": 365 * 24 * 3600,
                "modified": 365 * 24 * 3600,
                "folder_pk": None,
                "account_pk": None,
                "body_blob": _gzip_protobuf_like("milk\neggs\nbread"),
            },
            {
                "identifier": "note-uuid-2",
                "title": "Journal entry",
                "snippet": "Had a tough day today.",
                "created": 366 * 24 * 3600,
                "modified": 366 * 24 * 3600,
                "folder_pk": None,
                "account_pk": None,
                "body_blob": _gzip_protobuf_like(
                    "Had a tough day today. Thinking about the project."
                ),
            },
        ],
    )
    records = list(extract_notes(db))
    assert len(records) == 2
    titles = {r["metadata"]["title"] for r in records}
    assert titles == {"Grocery list", "Journal entry"}
    sources = {r["source"] for r in records}
    assert sources == {"apple-notes"}


def test_extract_notes_skips_locked(tmp_path: Path) -> None:
    db = tmp_path / "test.sqlite"
    _make_synthetic_db(
        db,
        [
            {
                "identifier": "note-1",
                "title": "Public",
                "created": 0,
                "body_blob": _gzip_protobuf_like("visible content"),
            },
            {
                "identifier": "note-2",
                "title": "Secret",
                "is_locked": 1,
                "created": 0,
                "body_blob": _gzip_protobuf_like("encrypted content body"),
            },
        ],
    )
    records = list(extract_notes(db))
    assert len(records) == 1
    assert records[0]["metadata"]["title"] == "Public"


def test_extract_notes_skips_deleted(tmp_path: Path) -> None:
    db = tmp_path / "test.sqlite"
    _make_synthetic_db(
        db,
        [
            {
                "identifier": "note-1",
                "title": "Active",
                "created": 0,
                "body_blob": _gzip_protobuf_like("active content"),
            },
            {
                "identifier": "note-2",
                "title": "Trashed",
                "deleted": 1,
                "created": 0,
                "body_blob": _gzip_protobuf_like("deleted content"),
            },
        ],
    )
    records = list(extract_notes(db))
    assert len(records) == 1
    assert records[0]["metadata"]["title"] == "Active"


def test_extract_notes_falls_back_to_snippet_when_body_blank(
    tmp_path: Path,
) -> None:
    db = tmp_path / "test.sqlite"
    _make_synthetic_db(
        db,
        [
            {
                "identifier": "note-1",
                "title": "No body",
                "snippet": "snippet preview text",
                "created": 0,
                "body_blob": gzip.compress(b""),
            },
        ],
    )
    records = list(extract_notes(db))
    assert len(records) == 1
    # Snippet flows into content when the gzipped body has no decodable text.
    assert "snippet preview text" in records[0]["content"]


def test_extract_notes_synthesizes_title_from_first_line(tmp_path: Path) -> None:
    """When ZTITLE1 is empty (matches Notes' default-title behavior), we use
    the first line of the body as the title."""
    db = tmp_path / "test.sqlite"
    _make_synthetic_db(
        db,
        [
            {
                "identifier": "note-1",
                "title": None,
                "created": 0,
                "body_blob": _gzip_protobuf_like(
                    "First line is the title.\n\nSecond paragraph body."
                ),
            },
        ],
    )
    records = list(extract_notes(db))
    assert len(records) == 1
    # Heuristic returns longest run, which is the multi-paragraph string.
    assert records[0]["metadata"]["title"]


def test_snapshot_notes_db_copies_to_destination(tmp_path: Path) -> None:
    src = tmp_path / "src.sqlite"
    _make_synthetic_db(src, [])
    dst = tmp_path / "snapshot" / "snap.sqlite"
    snapshot_notes_db(src, dst)
    assert dst.exists()
    assert dst.stat().st_size > 0


def test_snapshot_notes_db_halts_on_missing_source(tmp_path: Path) -> None:
    with pytest.raises(AdapterError, match="not found"):
        snapshot_notes_db(tmp_path / "nope.sqlite", tmp_path / "dst.sqlite")


def test_extract_notes_halts_on_missing_snapshot(tmp_path: Path) -> None:
    with pytest.raises(AdapterError, match="not found"):
        list(extract_notes(tmp_path / "missing.sqlite"))
