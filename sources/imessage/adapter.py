"""iMessage source adapter — Stage 1 extract.

Reads ``~/Library/Messages/chat.db`` (SQLite) and emits normalized
records per files/pipeline-architecture.md → Stage 1. Attachments are
never extracted, only counted. Tapbacks/reactions and unsent messages
are skipped entirely. Edited messages use their current text with an
``was_edited`` metadata flag.

Access requirements:

* Terminal (or the Python process running the adapter) must have Full
  Disk Access in System Settings → Privacy & Security.
* The database is copied to a snapshot first, then opened read-only
  via ``sqlite:`` URI with ``mode=ro``. Never writes to chat.db.

Content extraction:

* The ``message.text`` column holds plain text for most messages.
* When it's NULL, fall back to a heuristic decoder on the
  ``attributedBody`` typedstream blob — works for most single-run
  messages, returns empty string on shapes we don't recognise.
* Handle normalisation (one person as a phone in some rows, an email
  in others) is deferred to the sanitize stage's coref pass per
  source-adapters.md.
"""

from __future__ import annotations

import re
import shutil
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# Apple Cocoa reference date: 2001-01-01 00:00:00 UTC.
_APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=UTC)

# iMessage's ``chat.style`` field: 43 = 1-on-1, 45 = group.
_CHAT_STYLE_GROUP = 45

# associated_message_type classification per Apple's iMessage extension
# numbering. 0 is a regular message; 2000-series and 3000-series are
# tapbacks (added / removed). 1000-series is edited message metadata.
_TAPBACK_TYPES = frozenset(range(2000, 2010)) | frozenset(range(3000, 3010))


class AdapterError(RuntimeError):
    """Raised when the adapter refuses to continue."""


def snapshot_chat_db(src_path: Path, snapshot_path: Path) -> Path:
    """Copy ``chat.db`` (+ WAL + SHM sidecars when present) to a snapshot
    location the adapter can read without racing the live Messages app.

    Returns the snapshot path. Raises ``AdapterError`` if the source
    isn't readable.
    """
    src_path = Path(src_path)
    snapshot_path = Path(snapshot_path)
    if not src_path.exists():
        raise AdapterError(
            f"{src_path} not found. Ensure Terminal has Full Disk Access and chat.db is on disk."
        )
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src_path, snapshot_path)
    # SQLite WAL + shared memory sidecars: copy when present so the
    # snapshot reflects the very latest writes.
    for suffix in ("-wal", "-shm"):
        sidecar = src_path.with_name(src_path.name + suffix)
        if sidecar.exists():
            shutil.copy2(sidecar, snapshot_path.with_name(snapshot_path.name + suffix))
    return snapshot_path


def extract_messages(snapshot_path: Path) -> Iterator[dict[str, Any]]:
    """Query the snapshot and yield one normalized record per message.

    Tapbacks, reactions, and unsent messages are skipped silently; they
    are protocol noise and have no useful content for the vault.
    """
    snapshot_path = Path(snapshot_path)
    if not snapshot_path.exists():
        raise AdapterError(f"{snapshot_path} not found")
    uri = f"file:{snapshot_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        for row in conn.execute(_QUERY):
            record = _row_to_record(row)
            if record is not None:
                yield record
    finally:
        conn.close()


_QUERY = """
SELECT
    m.ROWID                         AS rowid,
    m.guid                          AS guid,
    m.text                          AS text,
    m.attributedBody                AS attributed_body,
    m.date                          AS date_apple,
    m.is_from_me                    AS from_me,
    m.cache_has_attachments         AS has_attachments,
    m.service                       AS service,
    m.associated_message_type       AS associated_type,
    m.date_retracted                AS date_retracted,
    m.date_edited                   AS date_edited,
    m.thread_originator_guid        AS thread_originator_guid,
    h.id                            AS handle_id,
    c.chat_identifier               AS chat_identifier,
    c.style                         AS chat_style,
    (
        SELECT COUNT(*) FROM message_attachment_join maj
        WHERE maj.message_id = m.ROWID
    )                               AS attachment_count
FROM message m
LEFT JOIN handle h               ON h.ROWID = m.handle_id
LEFT JOIN chat_message_join cmj  ON cmj.message_id = m.ROWID
LEFT JOIN chat c                 ON c.ROWID = cmj.chat_id
ORDER BY m.date ASC
"""


def _row_to_record(row: sqlite3.Row) -> dict[str, Any] | None:
    # Skip unsent messages — date_retracted is non-zero when the user
    # tapped "Undo Send" (iOS 16+).
    if row["date_retracted"]:
        return None
    # Skip tapbacks and reactions; their associated_message_type lives in
    # the 2000s or 3000s range. Regular messages are 0.
    associated = row["associated_type"] or 0
    if associated in _TAPBACK_TYPES:
        return None

    timestamp = _apple_epoch_to_iso(row["date_apple"])

    content = row["text"] or ""
    if not content and row["attributed_body"]:
        content = _decode_attributed_body(row["attributed_body"])

    chat_identifier = row["chat_identifier"] or ""
    chat_is_group = (row["chat_style"] or 0) == _CHAT_STYLE_GROUP
    from_me = bool(row["from_me"])
    handle_id = "me" if from_me else (row["handle_id"] or "")
    has_attachments = bool(row["has_attachments"]) or (row["attachment_count"] or 0) > 0

    return {
        "source": "imessage",
        "source_id": str(row["rowid"]),
        "timestamp": timestamp,
        "content": content,
        "metadata": {
            "chat_identifier": chat_identifier,
            "chat_is_group": chat_is_group,
            "handle_id": handle_id,
            "from_me": from_me,
            "service": row["service"] or "",
            "has_attachments": has_attachments,
            "attachment_count": row["attachment_count"] or 0,
            "is_reply": bool(row["thread_originator_guid"]),
            "thread_originator_guid": row["thread_originator_guid"],
            "was_edited": bool(row["date_edited"]),
        },
    }


def _apple_epoch_to_iso(value: int | None) -> str:
    """Convert Apple Cocoa-reference timestamp to ISO 8601 UTC.

    Pre-Mojave chat.db stored ``date`` as seconds since 2001-01-01 UTC;
    post-Mojave it's nanoseconds. Any value above ~10^10 is
    nanoseconds (would otherwise land in the year 2318). Treat smaller
    values as seconds.
    """
    if value is None or value == 0:
        return _APPLE_EPOCH.isoformat()
    if value > 10**10:
        seconds = value / 1_000_000_000
    else:
        seconds = float(value)
    return (_APPLE_EPOCH + timedelta(seconds=seconds)).isoformat()


# Typedstream class-name markers that show up inside an attributedBody
# blob. Stripped from the extracted text runs below.
_TYPEDSTREAM_MARKERS = frozenset(
    {
        "streamtyped",
        "NSString",
        "NSMutableString",
        "NSAttributedString",
        "NSMutableAttributedString",
        "NSObject",
        "NSDictionary",
        "NSMutableDictionary",
        "NSArray",
        "NSMutableArray",
        "NSNumber",
        "NSDate",
        "NSData",
        "NSValue",
        "__kIMMessagePartAttributeName",
        "__kIMFileTransferGUIDAttributeName",
        "__kIMDataDetectedAttributeName",
        "__kIMMentionConfirmedMention",
    }
)

_PRINTABLE_RUN_RE = re.compile(r"[\x20-\x7e\u00a0-\uffff\n\t]+")


def _decode_attributed_body(blob: bytes) -> str:
    """Heuristic: find the longest printable UTF-8 run in an
    ``NSAttributedString`` typedstream blob, after blanking out
    typedstream class-name markers. Handles most single-run iMessage
    bodies; returns the empty string on unrecognized formats.

    A full typedstream parser would be nicer but introduces a heavy
    dependency for a cosmetic improvement — the sanitize-stage LLM
    auditor (M5) is the safety net for anything the heuristic misses.
    """
    if not blob:
        return ""
    try:
        text = blob.decode("utf-8", errors="ignore")
    except Exception:
        return ""
    # Replace each marker with a non-printable sentinel so adjacent
    # printable noise (like a lone "@" byte) gets broken up instead of
    # glommed into a single run with the class name.
    for marker in _TYPEDSTREAM_MARKERS:
        text = text.replace(marker, "\x00")
    candidates = [run.strip() for run in _PRINTABLE_RUN_RE.findall(text) if run.strip()]
    if not candidates:
        return ""
    # Message body is typically the longest remaining printable run.
    return max(candidates, key=len)
