"""Promote sanitized records into Obsidian-friendly markdown notes.

This module is the bridge between the sanitizer (which writes JSONL to
``clean-staging/<source>/events.jsonl``) and the downstream Crew agents
(which expect markdown notes in the vault's ``00-Inbox/``).

Per the trust-boundary design, this code never writes into the vault
itself — the caller writes the rendered notes to a staging folder
outside the vault, and the user manually moves approved notes into
``00-Inbox/``. This matches the Option-B pattern the vault already
uses for Apple Health exports.

Each ``promote_*`` function takes one or more sanitized records and
returns ``(filename, markdown)`` pairs. Filenames follow the Crew
convention ``YYYY-MM-DD — Type — Title.md``. Frontmatter matches the
Crew templates so the Sorter and downstream agents can route each note
without retraining:

* ``promote_ical`` → Postman's meeting template (``type: meeting``)
* ``promote_apple_mail`` → Postman's informational email template
  (``type: email-info``); the Sorter / Postman can re-classify into
  ``email-action`` / ``email-deadline`` / etc. after the note lands.
* ``promote_imessage`` → ``type: chat`` (no Crew template precedent;
  groups records by chat identifier + day so each note is a transcript)
* ``promote_local_files`` → Scribe's ``type: reference`` for files,
  with the original filename in the title.

A ``source-record-id`` frontmatter field on every note carries the
sanitizer's ``source_id`` so a user can trace any note back to its
JSONL row.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Iterator
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any

# Filesystem-unsafe chars on macOS / Linux + the Windows-reserved set.
# Stripping them keeps the filename portable even though we target macOS;
# the user occasionally ships notes between machines.
_FORBIDDEN_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Collapse runs of whitespace inside titles so filenames don't carry
# invisible double-spaces from sanitized content.
_WHITESPACE_RUN = re.compile(r"\s+")

# Maximum characters of a derived title to embed in a filename. Keeps
# paths short for Finder / git clients without truncating meaningful
# detail. Anything longer is suffixed with `…` so the user can tell.
_TITLE_MAX_LEN = 80

# Sanitized records may carry tokens like [PERSON_1] in any field. We
# pass them through verbatim — they are exactly what the Crew sees.


def promote_ical(record: dict[str, Any]) -> tuple[str, str]:
    """Render one ical event record as a meeting note.

    Returns ``(filename, markdown)``. Frontmatter matches Postman's
    Mode 2 (Calendar Import) template so the Sorter routes it like any
    other calendar-imported meeting.
    """
    metadata = record.get("metadata", {}) or {}
    summary = _str(metadata.get("summary")) or "Untitled event"
    description = _str(metadata.get("description"))
    location = _str(metadata.get("location"))
    dtstart = _str(metadata.get("dtstart")) or _str(record.get("timestamp"))
    dtend = _str(metadata.get("dtend"))
    rrule = _str(metadata.get("rrule"))
    organizer = _str(metadata.get("organizer"))
    attendees = _str_list(metadata.get("attendees"))
    structured = metadata.get("structured_location") or {}
    venue_title = _str(structured.get("title")) if isinstance(structured, dict) else ""

    date_str, time_str = _split_iso(dtstart)
    _, end_time_str = _split_iso(dtend)
    time_field = _format_time_range(time_str, end_time_str)

    participants_yaml = _yaml_list_of_wikilinks("05-People", [organizer, *attendees])

    fm: dict[str, Any] = {
        "type": "meeting",
        "date": date_str,
        "time": time_field,
        "location": location or venue_title or "",
        "participants": [],  # rendered via participants_override below
        "tags": ["meeting", "imported", "ical"],
        "status": "inbox",
        "calendar-event-id": _str(record.get("source_id")),
        "calendar-name": _str(metadata.get("calendar_name")),
        "recurring": bool(rrule),
        "source": "ical",
        "source-record-id": _str(record.get("source_id")),
        "created": _now_iso(),
    }

    body_lines: list[str] = []
    body_lines.append(f"# {summary}")
    body_lines.append("")
    body_lines.append(f"**Date**: {date_str}" + (f" at {time_field}" if time_field else ""))
    if location or venue_title:
        body_lines.append(f"**Location**: {location or venue_title}")
    if organizer:
        body_lines.append(f"**Organizer**: {organizer}")
    if attendees:
        body_lines.append("")
        body_lines.append("## Participants")
        for a in attendees:
            body_lines.append(f"- {a}")
    if rrule:
        body_lines.append("")
        body_lines.append(f"**Recurrence**: `{rrule}`")
    if description:
        body_lines.append("")
        body_lines.append("## Description")
        body_lines.append(description)
    body_lines.append("")
    body_lines.append("---")
    body_lines.append("*Imported from sanitized iCal export.*")

    markdown = _render_note(fm, "\n".join(body_lines), participants_override=participants_yaml)
    filename = _build_filename(date_str, "Meeting", summary)
    return filename, markdown


def promote_apple_mail(record: dict[str, Any]) -> tuple[str, str]:
    """Render one apple-mail message record as an informational email note.

    The promote step does not classify mail into action/deadline/etc. —
    that is the Sorter's job. We use Postman's ``email-info`` template
    so every email lands in a known shape; the Sorter / Postman can
    re-tag after triage.
    """
    metadata = record.get("metadata", {}) or {}
    subject = _str(metadata.get("subject")) or "(no subject)"
    sender = _str(metadata.get("from"))
    folder = _str(metadata.get("folder"))
    timestamp = _str(record.get("timestamp"))
    date_str, _ = _split_iso(timestamp)
    content = _str(record.get("content"))
    to_list = _str_list(metadata.get("to"))
    cc_list = _str_list(metadata.get("cc"))
    has_attachments = bool(metadata.get("has_attachments"))
    attachment_count = int(metadata.get("attachment_count") or 0)
    date_synthesized = bool(metadata.get("date_synthesized"))

    fm: dict[str, Any] = {
        "type": "email-info",
        "date": date_str,
        "from": sender,
        "subject": subject,
        "tags": ["email", "imported", "apple-mail"],
        "status": "inbox",
        "folder": folder,
        "source": "apple-mail",
        "source-email-id": _str(metadata.get("message_id")) or _str(record.get("source_id")),
        "source-record-id": _str(record.get("source_id")),
        "has-attachments": has_attachments,
        "attachment-count": attachment_count,
        "date-synthesized": date_synthesized,
        "created": _now_iso(),
    }

    body_lines: list[str] = []
    body_lines.append(f"# {subject}")
    body_lines.append("")
    if sender:
        body_lines.append(f"**From**: {sender}")
    if to_list:
        body_lines.append(f"**To**: {', '.join(to_list)}")
    if cc_list:
        body_lines.append(f"**Cc**: {', '.join(cc_list)}")
    body_lines.append(f"**Date**: {date_str}" + (" *(synthesized)*" if date_synthesized else ""))
    if folder:
        body_lines.append(f"**Folder**: {folder}")
    if has_attachments:
        body_lines.append(f"**Attachments**: {attachment_count} (skipped by sanitizer)")
    body_lines.append("")
    body_lines.append("## Body")
    body_lines.append("")
    body_lines.append(content if content else "*(empty body)*")
    body_lines.append("")
    body_lines.append("---")
    body_lines.append("*Imported from sanitized Apple Mail export.*")

    markdown = _render_note(fm, "\n".join(body_lines))
    filename = _build_filename(date_str, "Email", subject)
    return filename, markdown


def promote_local_files(record: dict[str, Any]) -> tuple[str, str]:
    """Render one local-files record as a reference note.

    Uses Scribe's ``type: reference`` shape — the closest existing Crew
    template for "user dropped a document into the vault". The original
    filename (without extension) becomes the title.
    """
    metadata = record.get("metadata", {}) or {}
    raw_path = _str(metadata.get("path"))
    file_format = _str(metadata.get("format"))
    extraction = _str(metadata.get("extraction_method"))
    ocr_low = bool(metadata.get("ocr_low_confidence"))
    page_count = int(metadata.get("page_count") or 0)
    size_bytes = int(metadata.get("size_bytes") or 0)
    timestamp = _str(record.get("timestamp"))
    date_str, _ = _split_iso(timestamp)
    content = _str(record.get("content"))

    title = _filename_stem(raw_path) or "Untitled document"

    fm: dict[str, Any] = {
        "type": "reference",
        "date": date_str,
        "tags": ["reference", "imported", "local-file"],
        "status": "inbox",
        "source": "local-files",
        "source-path": raw_path,
        "source-record-id": _str(record.get("source_id")),
        "format": file_format,
        "extraction-method": extraction,
        "ocr-low-confidence": ocr_low,
        "page-count": page_count,
        "size-bytes": size_bytes,
        "created": _now_iso(),
    }

    body_lines: list[str] = []
    body_lines.append(f"# {title}")
    body_lines.append("")
    if raw_path:
        body_lines.append(f"**Original path**: `{raw_path}`")
    body_lines.append(f"**Format**: {file_format or 'unknown'}")
    if extraction:
        body_lines.append(f"**Extraction**: {extraction}")
    if ocr_low:
        body_lines.append("**OCR confidence**: below threshold — verify content")
    if page_count:
        body_lines.append(f"**Pages**: {page_count}")
    body_lines.append("")
    body_lines.append("## Content")
    body_lines.append("")
    body_lines.append(content if content else "*(empty document)*")
    body_lines.append("")
    body_lines.append("---")
    body_lines.append("*Imported from sanitized local-files export.*")

    markdown = _render_note(fm, "\n".join(body_lines))
    filename = _build_filename(date_str, "Note", title)
    return filename, markdown


def promote_imessage(records: Iterable[dict[str, Any]]) -> Iterator[tuple[str, str]]:
    """Group iMessage records by ``(chat_identifier, day)`` and yield one
    transcript note per group.

    A single message-per-note approach would flood the inbox; grouping
    by chat-and-day matches how a person reads back a conversation and
    keeps the inbox count manageable. Each note's frontmatter records
    every ``source-record-id`` in the group so traceback to the JSONL
    is still exact.
    """
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for rec in records:
        metadata = rec.get("metadata", {}) or {}
        chat_id = _str(metadata.get("chat_identifier")) or "unknown-chat"
        timestamp = _str(rec.get("timestamp"))
        day, _ = _split_iso(timestamp)
        groups[(chat_id, day)].append(rec)

    for (chat_id, day), msgs in sorted(groups.items()):
        msgs.sort(key=lambda m: _str(m.get("timestamp")))
        first = msgs[0]
        first_meta = first.get("metadata", {}) or {}
        is_group = bool(first_meta.get("chat_is_group"))
        service = _str(first_meta.get("service"))
        participants = sorted(
            {
                _str((m.get("metadata") or {}).get("handle_id"))
                for m in msgs
                if _str((m.get("metadata") or {}).get("handle_id"))
                and not (m.get("metadata") or {}).get("from_me")
            }
        )

        record_ids = [_str(m.get("source_id")) for m in msgs if _str(m.get("source_id"))]

        title_chat_label = _chat_title_label(chat_id, participants, is_group)

        fm: dict[str, Any] = {
            "type": "chat",
            "date": day,
            "tags": ["chat", "imported", "imessage"],
            "status": "inbox",
            "source": "imessage",
            "chat-identifier": chat_id,
            "chat-is-group": is_group,
            "service": service,
            "participants": participants,
            "message-count": len(msgs),
            "source-record-ids": record_ids,
            "created": _now_iso(),
        }

        body_lines: list[str] = []
        body_lines.append(f"# Chat — {title_chat_label} — {day}")
        body_lines.append("")
        body_lines.append(f"**Chat**: `{chat_id}`")
        body_lines.append(f"**Type**: {'group' if is_group else 'direct'}")
        if service:
            body_lines.append(f"**Service**: {service}")
        if participants:
            body_lines.append(f"**Participants**: {', '.join(participants)}")
        body_lines.append(f"**Messages**: {len(msgs)}")
        body_lines.append("")
        body_lines.append("## Transcript")
        body_lines.append("")
        for m in msgs:
            meta = m.get("metadata") or {}
            ts = _str(m.get("timestamp"))
            _, time_str = _split_iso(ts)
            speaker = "me" if meta.get("from_me") else (_str(meta.get("handle_id")) or "?")
            text = _str(m.get("content"))
            edited = " *(edited)*" if meta.get("was_edited") else ""
            attach = ""
            if meta.get("has_attachments"):
                count = int(meta.get("attachment_count") or 0)
                if count:
                    attach = f" *[{count} attachment(s) skipped]*"
                else:
                    attach = " *[attachment skipped]*"
            stamp = time_str or ts
            line_text = text if text else "*(empty)*"
            body_lines.append(f"- **{stamp}** — `{speaker}`: {line_text}{edited}{attach}")
        body_lines.append("")
        body_lines.append("---")
        body_lines.append("*Imported from sanitized iMessage export.*")

        markdown = _render_note(fm, "\n".join(body_lines))
        filename = _build_filename(day, "Chat", title_chat_label)
        yield filename, markdown


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _render_note(
    frontmatter: dict[str, Any],
    body: str,
    *,
    participants_override: str | None = None,
) -> str:
    """Render a note as ``--- frontmatter --- body``.

    ``participants_override`` injects a pre-rendered YAML block for the
    ``participants`` key — used by the meeting template to emit
    ``[[05-People/Name]]`` wikilinks the way the Crew expects.
    """
    yaml_lines: list[str] = ["---"]
    for key, value in frontmatter.items():
        if key == "participants" and participants_override is not None:
            yaml_lines.append("participants:")
            yaml_lines.append(participants_override)
            continue
        yaml_lines.append(_yaml_line(key, value))
    yaml_lines.append("---")
    return "\n".join(yaml_lines) + "\n\n" + body + "\n"


def _yaml_line(key: str, value: Any) -> str:
    if isinstance(value, bool):
        return f"{key}: {'true' if value else 'false'}"
    if isinstance(value, int) and not isinstance(value, bool):
        return f"{key}: {value}"
    if value is None:
        return f'{key}: ""'
    if isinstance(value, list):
        if not value:
            return f"{key}: []"
        items = "\n".join(f"  - {_yaml_scalar(v)}" for v in value)
        return f"{key}:\n{items}"
    return f"{key}: {_yaml_scalar(value)}"


def _yaml_scalar(value: Any) -> str:
    """Quote a scalar so it survives YAML round-trip.

    We always double-quote strings to dodge the long list of YAML 1.1
    edge cases (``yes``/``no``/``on``/``off`` booleans, leading zeros
    parsed as octal, colons inside values, etc.). Internal double quotes
    and backslashes are escaped.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    s = str(value)
    escaped = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _yaml_list_of_wikilinks(folder: str, names: list[str]) -> str:
    """Render a list of names as YAML ``[[folder/Name]]`` wikilink items."""
    cleaned = [n.strip() for n in names if n and n.strip()]
    if not cleaned:
        return "  []"
    return "\n".join(f'  - "[[{folder}/{n}]]"' for n in cleaned)


def _build_filename(date_str: str, type_label: str, title: str) -> str:
    """Produce a Crew-convention filename: ``YYYY-MM-DD — Type — Title.md``."""
    safe_title = _safe_title(title)
    safe_type = _safe_title(type_label) or "Note"
    safe_date = date_str if date_str else "0000-00-00"
    return f"{safe_date} \u2014 {safe_type} \u2014 {safe_title}.md"


def _safe_title(raw: str) -> str:
    """Strip filesystem-unsafe chars and collapse whitespace."""
    if not raw:
        return "Untitled"
    cleaned = _FORBIDDEN_FILENAME_CHARS.sub(" ", raw)
    cleaned = _WHITESPACE_RUN.sub(" ", cleaned).strip(" .-")
    if not cleaned:
        return "Untitled"
    if len(cleaned) > _TITLE_MAX_LEN:
        cleaned = cleaned[: _TITLE_MAX_LEN - 1].rstrip() + "\u2026"
    return cleaned


def _filename_stem(path: str) -> str:
    if not path:
        return ""
    return PurePosixPath(path).stem


def _split_iso(value: str) -> tuple[str, str]:
    """Split an ISO 8601 timestamp into ``(date, time)`` strings.

    Tolerates date-only values (all-day events) and trailing ``Z``. The
    time portion is HH:MM (seconds dropped — meeting templates display
    only minute precision). Returns ``("", "")`` on parse failure.
    """
    if not value:
        return "", ""
    s = value.strip()
    if "T" not in s and len(s) == 10:
        return s, ""
    s_norm = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s_norm)
    except ValueError:
        if "T" in s:
            date_part, time_part = s.split("T", 1)
            time_part = time_part.split("+", 1)[0].split("-", 1)[0]
            return date_part, time_part[:5]
        return s[:10], ""
    return dt.date().isoformat(), dt.strftime("%H:%M")


def _format_time_range(start: str, end: str) -> str:
    if start and end:
        return f"{start}\u2013{end}"
    return start or end


def _chat_title_label(chat_id: str, participants: list[str], is_group: bool) -> str:
    """Pick a human-readable label for the chat note's filename / title.

    Sanitizer output sometimes leaves ``chat000000012345``-style group
    hashes intact (opaque IDs, not PII). For 1:1 chats it carries the
    pseudonymized handle. We prefer the participant list when it's
    short and the chat id when it isn't useful as a label.
    """
    if is_group:
        if participants and len(participants) <= 3:
            return f"Group ({', '.join(participants)})"
        return "Group chat"
    if participants:
        return participants[0]
    return chat_id


def _str(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _str_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [_str(v) for v in value if _str(v)]
    return [_str(value)]


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
