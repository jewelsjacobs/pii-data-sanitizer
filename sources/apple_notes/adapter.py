"""Apple Notes source adapter — Stage 1 extract.

Reads ``~/Library/Group Containers/group.com.apple.notes/NoteStore.sqlite``
(SQLite, just like imessage's chat.db) and emits normalized records per
files/pipeline-architecture.md → Stage 1.

Note bodies are stored as gzip-compressed Apple-internal protobuf in the
``ZICNOTEDATA.ZDATA`` blob column. A full protobuf parser would need
Apple's CoreNote proto definitions; for the sanitizer's purposes we only
need the plain text content, which we extract via the same printable-
run heuristic used for iMessage's ``attributedBody`` blobs. Acceptable
loss: lists, attachments, hand-drawn sketches, table cells with rich
formatting are flattened to text.

Access requirements:

* Terminal (or the Python process running the adapter) must have Full
  Disk Access in System Settings → Privacy & Security.
* The database is copied to a snapshot first, then opened read-only via
  ``sqlite:`` URI with ``mode=ro``. Never writes to NoteStore.sqlite.

Skipped:

* Locked notes (``ZISPASSWORDPROTECTED == 1``) — body is encrypted
  with a key the adapter doesn't have access to. Title may still be
  readable but on its own without context isn't useful.
* Notes marked for deletion (``ZMARKEDFORDELETION == 1``) — already
  in the user's trash; not part of active vault content.
"""

from __future__ import annotations

import gzip
import re
import shutil
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# Apple Cocoa reference date: 2001-01-01 00:00:00 UTC. Same as iMessage.
_APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=UTC)


class AdapterError(RuntimeError):
    """Raised when the adapter refuses to continue."""


def snapshot_notes_db(src_path: Path, snapshot_path: Path) -> Path:
    """Copy ``NoteStore.sqlite`` (+ WAL + SHM sidecars when present) to a
    snapshot location the adapter can read without racing the live Notes
    app.

    Returns the snapshot path. Raises ``AdapterError`` if the source
    isn't readable.
    """
    src_path = Path(src_path)
    snapshot_path = Path(snapshot_path)
    if not src_path.exists():
        raise AdapterError(
            f"{src_path} not found. Ensure Terminal has Full Disk Access and "
            f"the Notes app has been opened at least once on this machine."
        )
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src_path, snapshot_path)
    for suffix in ("-wal", "-shm"):
        sidecar = src_path.with_name(src_path.name + suffix)
        if sidecar.exists():
            shutil.copy2(sidecar, snapshot_path.with_name(snapshot_path.name + suffix))
    return snapshot_path


def extract_notes(snapshot_path: Path) -> Iterator[dict[str, Any]]:
    """Query the snapshot and yield one normalized record per note.

    Locked / encrypted notes and notes marked for deletion are skipped
    silently — they aren't useful for life-history reflection.
    """
    snapshot_path = Path(snapshot_path)
    if not snapshot_path.exists():
        raise AdapterError(f"{snapshot_path} not found")
    uri = f"file:{snapshot_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        for row in conn.execute(_build_query(conn)):
            record = _row_to_record(row)
            if record is not None:
                yield record
    finally:
        conn.close()


# Schema notes:
# - ZICCLOUDSYNCINGOBJECT is a polymorphic table holding accounts, folders,
#   notes, attachments, etc. We filter to notes by joining ZNOTEDATA to
#   ZICNOTEDATA — only notes have a body blob.
# - ZTITLE1 is the note title (folders use ZTITLE2; we read both via the
#   join and pick the right one per record).
# - Creation/modification timestamps are Cocoa-reference. Apple's schema
#   has migrated the live creation column several times: on current
#   NoteStore versions ZCREATIONDATE1 is NULL for every note and the real
#   value lives in ZCREATIONDATE3 (audit-found: all 763 notes were emitted
#   as the 2001-01-01 epoch). Column availability varies by macOS version,
#   so _build_query COALESCEs across whichever generations exist in the
#   snapshot, newest first.
# - ZIDENTIFIER is a UUID-style identifier stable across syncs — used as
#   the source_id so re-runs are dedupe-friendly.
# - ZACCOUNT4 / ZFOLDER may be NULL on default-account local notes; we
#   tolerate either.
_QUERY_TEMPLATE = """
SELECT
    n.Z_PK                         AS rowid,
    n.ZIDENTIFIER                  AS identifier,
    n.ZTITLE1                      AS title,
    n.ZSNIPPET                     AS snippet,
    {created_expr}                 AS created_apple,
    {modified_expr}                AS modified_apple,
    n.ZISPASSWORDPROTECTED         AS is_locked,
    n.ZMARKEDFORDELETION           AS deleted,
    f.ZTITLE2                      AS folder_name,
    a.ZNAME                        AS account_name,
    d.ZDATA                        AS note_data
FROM ZICCLOUDSYNCINGOBJECT n
LEFT JOIN ZICCLOUDSYNCINGOBJECT f  ON n.ZFOLDER = f.Z_PK
LEFT JOIN ZICCLOUDSYNCINGOBJECT a  ON n.ZACCOUNT4 = a.Z_PK
LEFT JOIN ZICNOTEDATA d            ON n.ZNOTEDATA = d.Z_PK
WHERE n.ZNOTEDATA IS NOT NULL
ORDER BY created_apple ASC
"""

# Preference order: newest schema generation first.
_CREATED_COLUMNS = ("ZCREATIONDATE3", "ZCREATIONDATE2", "ZCREATIONDATE1", "ZCREATIONDATE")
_MODIFIED_COLUMNS = ("ZMODIFICATIONDATE1", "ZMODIFICATIONDATE")


def _coalesce_expr(available: set[str], preferred: tuple[str, ...]) -> str:
    cols = [f"n.{c}" for c in preferred if c in available]
    if not cols:
        return "NULL"
    if len(cols) == 1:
        return cols[0]
    return f"COALESCE({', '.join(cols)})"


def _build_query(conn: sqlite3.Connection) -> str:
    available = {str(row[1]) for row in conn.execute("PRAGMA table_info(ZICCLOUDSYNCINGOBJECT)")}
    return _QUERY_TEMPLATE.format(
        created_expr=_coalesce_expr(available, _CREATED_COLUMNS),
        modified_expr=_coalesce_expr(available, _MODIFIED_COLUMNS),
    )


def _row_to_record(row: sqlite3.Row) -> dict[str, Any] | None:
    if row["deleted"]:
        return None
    if row["is_locked"]:
        # Encrypted body. Title alone is unreliable.
        return None

    identifier = row["identifier"] or f"note-{row['rowid']}"
    title = (row["title"] or "").strip()
    snippet = (row["snippet"] or "").strip()
    folder = (row["folder_name"] or "").strip()
    account = (row["account_name"] or "").strip()

    body = _decode_note_body(row["note_data"]) if row["note_data"] else ""
    if not body and snippet:
        # Fallback: snippet field has plain-text preview when body decode
        # fails entirely. Better than dropping the note.
        body = snippet

    # Fall back to the modification date when creation is NULL across every
    # schema generation; emit None when both are missing so the note is
    # visibly undated (and excluded from windowed runs) instead of silently
    # impersonating a real 2001-01-01 entry.
    timestamp = _apple_epoch_to_iso(row["created_apple"])
    modified = _apple_epoch_to_iso(row["modified_apple"])
    if timestamp is None:
        timestamp = modified

    # Title isn't always populated — Apple Notes uses the first line of
    # the body as the title when the user hasn't named the note. Mirror
    # that behavior so we always have a title.
    if not title and body:
        first_line = body.split("\n", 1)[0].strip()
        title = first_line[:120] if first_line else ""

    # Compose content with title prepended so the title participates in
    # NER / scrub_strings sanitization in the same pipeline pass.
    content_parts = [p for p in (title, body) if p]
    content = "\n\n".join(content_parts)

    return {
        "source": "apple-notes",
        "source_id": str(identifier),
        "timestamp": timestamp,
        "content": content,
        "metadata": {
            "title": title,
            "folder": folder,
            "account": account,
            "modified": modified,
            "snippet": snippet,
        },
    }


def _apple_epoch_to_iso(value: float | int | None) -> str | None:
    """Convert Apple Cocoa-reference timestamp to ISO 8601 UTC.

    Notes' timestamps are seconds (float) since 2001-01-01 UTC. Unlike
    iMessage which uses nanoseconds post-Mojave, Notes consistently uses
    seconds.

    Returns ``None`` for NULL/zero/garbage values. Returning the epoch
    itself (the old behavior) made undated notes impersonate real
    2001-01-01 entries — audit-found on every note when the creation
    column migrated out from under the query.
    """
    if value is None or value == 0:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return (_APPLE_EPOCH + timedelta(seconds=seconds)).isoformat()


# Markers commonly found in an Apple Notes protobuf blob. Stripped from
# the extracted text runs so they don't leak into the redacted output.
_PROTOBUF_MARKERS = frozenset(
    {
        "AttributeRun",
        "AttachmentInfo",
        "ParagraphStyle",
        "Font",
        "Color",
        "TableObject",
        "GalleryObject",
        "Drawing",
        "ChecklistItemAttachment",
        "MergeableDataObjectMap",
        "MergeableDataObjectKeyMapEntry",
    }
)

_PRINTABLE_RUN_RE = re.compile(r"[\x20-\x7e\u00a0-\uffff\n\t]+")


def _decode_note_body(blob: bytes) -> str:
    """Heuristic text extraction from a gzipped Apple-internal protobuf.

    The body is stored as a CoreNote proto, gzip-compressed. A full
    parse requires Apple's proto definitions (which we don't ship). For
    sanitizer purposes we only need the plain text content, which is
    reliably the longest length-delimited UTF-8 string field in the
    decompressed protobuf payload.

    Approach: gunzip → blank out known protobuf type-name markers →
    extract every printable run → return the longest, on the assumption
    that user-typed note content dwarfs any single attribute / metadata
    field. Returns the empty string if the blob doesn't decompress or
    contains no usable text.
    """
    if not blob:
        return ""
    try:
        decompressed = gzip.decompress(blob)
    except (OSError, gzip.BadGzipFile, EOFError):
        # Not all blobs are gzipped (older notes, edge cases). Try the
        # raw bytes too.
        decompressed = blob

    try:
        text = decompressed.decode("utf-8", errors="ignore")
    except (UnicodeDecodeError, AttributeError):
        return ""

    # Knock out protobuf type-name markers so they don't get glommed
    # into a printable run.
    for marker in _PROTOBUF_MARKERS:
        text = text.replace(marker, "\x00")

    runs = [run.strip() for run in _PRINTABLE_RUN_RE.findall(text) if run.strip()]
    if not runs:
        return ""

    # The user-typed text is reliably the longest run. Multi-paragraph
    # notes often have *multiple* large runs (one per AttributeRun); we
    # pick the longest as a conservative MVP. Future improvement: a
    # proper protobuf parse to recover ordering across runs.
    return max(runs, key=len)
