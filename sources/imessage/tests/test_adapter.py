"""Tests for the imessage adapter.

Each test builds a synthetic SQLite database in ``tmp_path`` with the
same table shape as Apple's ``chat.db``, then runs the adapter against
it. No real chat.db is ever touched.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sources.imessage.adapter import (
    AdapterError,
    _apple_epoch_to_iso,
    _decode_attributed_body,
    extract_messages,
    snapshot_chat_db,
)

# ---------------------------------------------------------------------------
# Database fixture builder
# ---------------------------------------------------------------------------

# Minimal schema — only the columns the adapter queries. Matches chat.db's
# column names exactly so the real SQL works unchanged.
_SCHEMA = """
CREATE TABLE message (
    ROWID INTEGER PRIMARY KEY,
    guid TEXT,
    text TEXT,
    attributedBody BLOB,
    date INTEGER,
    is_from_me INTEGER DEFAULT 0,
    cache_has_attachments INTEGER DEFAULT 0,
    service TEXT,
    handle_id INTEGER,
    associated_message_type INTEGER DEFAULT 0,
    date_retracted INTEGER DEFAULT 0,
    date_edited INTEGER DEFAULT 0,
    thread_originator_guid TEXT
);
CREATE TABLE handle (
    ROWID INTEGER PRIMARY KEY,
    id TEXT
);
CREATE TABLE chat (
    ROWID INTEGER PRIMARY KEY,
    chat_identifier TEXT,
    style INTEGER,
    guid TEXT
);
CREATE TABLE chat_message_join (
    chat_id INTEGER,
    message_id INTEGER
);
CREATE TABLE message_attachment_join (
    message_id INTEGER,
    attachment_id INTEGER
);
"""


def _apple_ns(seconds_since_2001: float) -> int:
    """Helper: seconds since 2001-01-01 UTC → Apple-ns integer."""
    return int(seconds_since_2001 * 1_000_000_000)


def _build_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "chat.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()
    return db_path


def _insert_handle(conn: sqlite3.Connection, rowid: int, handle: str) -> None:
    conn.execute("INSERT INTO handle (ROWID, id) VALUES (?, ?)", (rowid, handle))


def _insert_chat(conn: sqlite3.Connection, rowid: int, identifier: str, style: int = 43) -> None:
    conn.execute(
        "INSERT INTO chat (ROWID, chat_identifier, style, guid) VALUES (?, ?, ?, ?)",
        (rowid, identifier, style, f"chat-{rowid}-guid"),
    )


def _insert_message(
    conn: sqlite3.Connection,
    *,
    rowid: int,
    text: str | None = None,
    attributed_body: bytes | None = None,
    date_apple: int = 0,
    from_me: int = 0,
    handle_id: int | None = None,
    chat_id: int | None = None,
    service: str = "iMessage",
    associated_type: int = 0,
    date_retracted: int = 0,
    date_edited: int = 0,
    thread_originator_guid: str | None = None,
    cache_has_attachments: int = 0,
    attachment_ids: list[int] | None = None,
) -> None:
    conn.execute(
        """INSERT INTO message (
            ROWID, guid, text, attributedBody, date, is_from_me,
            cache_has_attachments, service, handle_id,
            associated_message_type, date_retracted, date_edited,
            thread_originator_guid
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            rowid,
            f"msg-{rowid}-guid",
            text,
            attributed_body,
            date_apple,
            from_me,
            cache_has_attachments,
            service,
            handle_id,
            associated_type,
            date_retracted,
            date_edited,
            thread_originator_guid,
        ),
    )
    if chat_id is not None:
        conn.execute(
            "INSERT INTO chat_message_join (chat_id, message_id) VALUES (?, ?)",
            (chat_id, rowid),
        )
    for att_id in attachment_ids or []:
        conn.execute(
            "INSERT INTO message_attachment_join (message_id, attachment_id) VALUES (?, ?)",
            (rowid, att_id),
        )


# ---------------------------------------------------------------------------
# Apple epoch conversion
# ---------------------------------------------------------------------------


def test_apple_epoch_seconds_form() -> None:
    """Pre-Mojave dates are seconds since 2001-01-01."""
    # 100 seconds after Apple epoch → 2001-01-01T00:01:40+00:00
    assert _apple_epoch_to_iso(100) == "2001-01-01T00:01:40+00:00"


def test_apple_epoch_nanoseconds_form() -> None:
    """Post-Mojave dates are nanoseconds."""
    # 60 * 10^9 nanoseconds = 60 seconds after epoch.
    assert _apple_epoch_to_iso(60 * 1_000_000_000) == "2001-01-01T00:01:00+00:00"


def test_apple_epoch_zero_returns_epoch() -> None:
    assert _apple_epoch_to_iso(0) == "2001-01-01T00:00:00+00:00"


# ---------------------------------------------------------------------------
# attributedBody decoder
# ---------------------------------------------------------------------------


def test_decode_attributed_body_finds_text() -> None:
    """A realistic typedstream-ish blob yields the message body."""
    blob = (
        b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84"
        b"NSMutableAttributedString\x00\x84\x84NSAttributedString\x00"
        b"\x84\x84NSObject\x00\x85\x92\x84\x84\x84NSString\x01\x84"
        b"\x07+\x0cHello world!\x86\x84"
    )
    assert _decode_attributed_body(blob) == "Hello world!"


def test_decode_attributed_body_empty_returns_empty() -> None:
    assert _decode_attributed_body(b"") == ""
    assert _decode_attributed_body(b"\x00\x00\x00") == ""


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


def test_snapshot_chat_db_copies_file(tmp_path: Path) -> None:
    src = tmp_path / "chat.db"
    src.write_bytes(b"fake db bytes")
    dst = tmp_path / "snap" / "chat.db"
    snapshot_chat_db(src, dst)
    assert dst.exists()
    assert dst.read_bytes() == b"fake db bytes"


def test_snapshot_includes_wal_and_shm_sidecars(tmp_path: Path) -> None:
    src = tmp_path / "chat.db"
    src.write_bytes(b"db")
    (tmp_path / "chat.db-wal").write_bytes(b"wal")
    (tmp_path / "chat.db-shm").write_bytes(b"shm")
    dst = tmp_path / "snap" / "chat.db"
    snapshot_chat_db(src, dst)
    assert dst.with_name("chat.db-wal").read_bytes() == b"wal"
    assert dst.with_name("chat.db-shm").read_bytes() == b"shm"


def test_snapshot_missing_source_raises(tmp_path: Path) -> None:
    with pytest.raises(AdapterError, match="not found"):
        snapshot_chat_db(tmp_path / "nope.db", tmp_path / "out.db")


# ---------------------------------------------------------------------------
# Full adapter runs
# ---------------------------------------------------------------------------


def test_simple_one_to_one(tmp_path: Path) -> None:
    db_path = _build_db(tmp_path)
    conn = sqlite3.connect(str(db_path))
    try:
        _insert_handle(conn, 1, "+15551234567")
        _insert_chat(conn, 1, "+15551234567", style=43)
        _insert_message(
            conn,
            rowid=101,
            text="Running 10 min late.",
            date_apple=_apple_ns(60),  # 60 seconds after Apple epoch
            from_me=0,
            handle_id=1,
            chat_id=1,
        )
        conn.commit()
    finally:
        conn.close()

    records = list(extract_messages(db_path))
    assert len(records) == 1
    r = records[0]
    assert r["source"] == "imessage"
    assert r["source_id"] == "101"
    assert r["timestamp"].startswith("2001-01-01T00:01:00")
    assert r["content"] == "Running 10 min late."
    assert r["metadata"]["chat_identifier"] == "+15551234567"
    assert r["metadata"]["chat_is_group"] is False
    assert r["metadata"]["handle_id"] == "+15551234567"
    assert r["metadata"]["from_me"] is False
    assert r["metadata"]["service"] == "iMessage"
    assert r["metadata"]["has_attachments"] is False
    assert r["metadata"]["attachment_count"] == 0


def test_group_chat_with_hash_identifier(tmp_path: Path) -> None:
    db_path = _build_db(tmp_path)
    conn = sqlite3.connect(str(db_path))
    try:
        _insert_handle(conn, 2, "marcus@example.invalid")
        _insert_chat(conn, 2, "chat000000012345", style=45)
        _insert_message(
            conn,
            rowid=201,
            text="bringing wine",
            date_apple=_apple_ns(120),
            from_me=0,
            handle_id=2,
            chat_id=2,
        )
        conn.commit()
    finally:
        conn.close()

    [record] = list(extract_messages(db_path))
    assert record["metadata"]["chat_identifier"] == "chat000000012345"
    assert record["metadata"]["chat_is_group"] is True
    assert record["metadata"]["handle_id"] == "marcus@example.invalid"


def test_from_me_handle_becomes_me(tmp_path: Path) -> None:
    db_path = _build_db(tmp_path)
    conn = sqlite3.connect(str(db_path))
    try:
        _insert_chat(conn, 1, "+15551234567", style=43)
        _insert_message(
            conn,
            rowid=301,
            text="Sent from me",
            date_apple=_apple_ns(100),
            from_me=1,
            chat_id=1,
        )
        conn.commit()
    finally:
        conn.close()

    [record] = list(extract_messages(db_path))
    assert record["metadata"]["from_me"] is True
    assert record["metadata"]["handle_id"] == "me"


def test_tapbacks_are_skipped(tmp_path: Path) -> None:
    db_path = _build_db(tmp_path)
    conn = sqlite3.connect(str(db_path))
    try:
        _insert_chat(conn, 1, "+15551234567", style=43)
        _insert_message(
            conn,
            rowid=400,
            text="real message",
            date_apple=_apple_ns(50),
            chat_id=1,
        )
        _insert_message(
            conn,
            rowid=401,
            text="Liked 'real message'",
            date_apple=_apple_ns(55),
            associated_type=2000,  # tapback
            chat_id=1,
        )
        conn.commit()
    finally:
        conn.close()

    records = list(extract_messages(db_path))
    assert [r["source_id"] for r in records] == ["400"]


def test_unsent_messages_are_skipped(tmp_path: Path) -> None:
    db_path = _build_db(tmp_path)
    conn = sqlite3.connect(str(db_path))
    try:
        _insert_chat(conn, 1, "+15551234567", style=43)
        _insert_message(
            conn,
            rowid=500,
            text="Please ignore",
            date_apple=_apple_ns(40),
            date_retracted=_apple_ns(45),  # unsent at 45 s
            chat_id=1,
        )
        _insert_message(
            conn,
            rowid=501,
            text="Kept this one",
            date_apple=_apple_ns(45),
            chat_id=1,
        )
        conn.commit()
    finally:
        conn.close()

    records = list(extract_messages(db_path))
    assert [r["source_id"] for r in records] == ["501"]


def test_edited_message_flag(tmp_path: Path) -> None:
    db_path = _build_db(tmp_path)
    conn = sqlite3.connect(str(db_path))
    try:
        _insert_chat(conn, 1, "+15551234567", style=43)
        _insert_message(
            conn,
            rowid=600,
            text="final edited text",
            date_apple=_apple_ns(10),
            date_edited=_apple_ns(20),
            chat_id=1,
        )
        conn.commit()
    finally:
        conn.close()

    [record] = list(extract_messages(db_path))
    assert record["content"] == "final edited text"
    assert record["metadata"]["was_edited"] is True


def test_attachments_counted_not_extracted(tmp_path: Path) -> None:
    db_path = _build_db(tmp_path)
    conn = sqlite3.connect(str(db_path))
    try:
        _insert_chat(conn, 1, "+15551234567", style=43)
        _insert_message(
            conn,
            rowid=700,
            text="look at these",
            date_apple=_apple_ns(30),
            cache_has_attachments=1,
            chat_id=1,
            attachment_ids=[9001, 9002, 9003],
        )
        conn.commit()
    finally:
        conn.close()

    [record] = list(extract_messages(db_path))
    assert record["metadata"]["has_attachments"] is True
    assert record["metadata"]["attachment_count"] == 3
    # Content field never carries attachment bytes.
    assert record["content"] == "look at these"


def test_null_text_falls_back_to_attributed_body(tmp_path: Path) -> None:
    db_path = _build_db(tmp_path)
    blob = (
        b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84"
        b"NSMutableAttributedString\x00\x84\x84NSAttributedString\x00"
        b"\x84\x84NSObject\x00\x85\x92\x84\x84\x84NSString\x01\x84"
        b"\x07+\x0cFrom the blob\x86\x84"
    )
    conn = sqlite3.connect(str(db_path))
    try:
        _insert_chat(conn, 1, "+15551234567", style=43)
        _insert_message(
            conn,
            rowid=800,
            text=None,  # NULL — fall back to attributedBody
            attributed_body=blob,
            date_apple=_apple_ns(40),
            chat_id=1,
        )
        conn.commit()
    finally:
        conn.close()

    [record] = list(extract_messages(db_path))
    assert "From the blob" in record["content"]


def test_is_reply_flag(tmp_path: Path) -> None:
    db_path = _build_db(tmp_path)
    conn = sqlite3.connect(str(db_path))
    try:
        _insert_chat(conn, 1, "+15551234567", style=43)
        _insert_message(
            conn,
            rowid=900,
            text="reply",
            date_apple=_apple_ns(10),
            thread_originator_guid="parent-guid",
            chat_id=1,
        )
        conn.commit()
    finally:
        conn.close()

    [record] = list(extract_messages(db_path))
    assert record["metadata"]["is_reply"] is True
    assert record["metadata"]["thread_originator_guid"] == "parent-guid"


def test_idempotent_on_repeat_run(tmp_path: Path) -> None:
    db_path = _build_db(tmp_path)
    conn = sqlite3.connect(str(db_path))
    try:
        _insert_chat(conn, 1, "+15551234567", style=43)
        _insert_message(conn, rowid=1001, text="stable", date_apple=_apple_ns(1), chat_id=1)
        conn.commit()
    finally:
        conn.close()

    first = list(extract_messages(db_path))
    second = list(extract_messages(db_path))
    assert first == second


def test_missing_snapshot_raises(tmp_path: Path) -> None:
    with pytest.raises(AdapterError, match="not found"):
        list(extract_messages(tmp_path / "does-not-exist.db"))


def test_sqlite_is_opened_read_only(tmp_path: Path) -> None:
    """Opening via ``file:...?mode=ro`` must fail any write attempt."""
    db_path = _build_db(tmp_path)
    # Force-populate one message so there's something to query.
    conn = sqlite3.connect(str(db_path))
    try:
        _insert_chat(conn, 1, "+15551234567", style=43)
        _insert_message(conn, rowid=1, text="hi", date_apple=_apple_ns(1), chat_id=1)
        conn.commit()
    finally:
        conn.close()

    # Open via the adapter's URI and attempt a write — should fail.
    uri = f"file:{db_path}?mode=ro"
    ro_conn = sqlite3.connect(uri, uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            ro_conn.execute("INSERT INTO message (ROWID, text, date) VALUES (2, 'x', 0)")
    finally:
        ro_conn.close()


# ---------------------------------------------------------------------------
# Sanity: datetime helper used above behaves as expected.
# ---------------------------------------------------------------------------


def test_helper_apple_ns_round_trip() -> None:
    """Round-trip the test helper so fixture timestamps are meaningful."""
    ns = _apple_ns(60)
    iso = _apple_epoch_to_iso(ns)
    dt = datetime.fromisoformat(iso)
    apple_epoch = datetime(2001, 1, 1, tzinfo=UTC)
    assert dt - apple_epoch == timedelta(seconds=60)
