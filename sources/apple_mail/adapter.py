"""Apple Mail (iCloud IMAP) source adapter — Stage 1 extract.

Connects to iCloud IMAP over implicit TLS (port 993, no STARTTLS fallback,
cert verification required) and emits normalized records per
files/pipeline-architecture.md → Stage 1. Attachments are never
downloaded; their count is preserved in metadata so the downstream
vault knows the message had media context.

Headers are preserved raw in metadata — the sanitize stage is
responsible for pseudonymizing From/To/Cc and applying the folder-name
redaction policy.

Folder handling is a **deny-list**: read every folder the server
reports, minus the usual throwaway ones (Junk/Trash/Drafts/etc.).
Per files/source-adapters.md, user-created folders sit as siblings of
INBOX in IMAP, and an allow-list would silently miss them.

Credentials live in memory only for the lifetime of the connection.
The adapter never logs the password and never includes it in raised
exception messages.
"""

from __future__ import annotations

import email
import email.utils
import imaplib
import re
import ssl
import sys
from collections.abc import Iterator
from datetime import UTC
from email.message import Message
from hashlib import sha256
from typing import Any

IMAP_SERVER = "imap.mail.me.com"
IMAP_PORT = 993

# Case-insensitive short-name deny-list per files/source-adapters.md →
# apple-mail → Folder Exclusion List.
EXCLUDED_FOLDER_SHORT_NAMES = frozenset(
    {
        "junk",
        "junk mail",
        "trash",
        "deleted items",
        "deleted messages",
        "recently deleted",
        "drafts",
    }
)

# IMAP LIST line shape: (\HasChildren) "/" "INBOX.Family" — pull the last
# quoted string as the mailbox name. `\\` in flags is fine to ignore.
_FOLDER_LINE_RE = re.compile(r'"([^"]*)"\s*$')


class AdapterError(RuntimeError):
    """Raised when the adapter refuses to continue (decode failure,
    malformed headers, IMAP protocol-level error)."""


def extract_mail(
    email_address: str,
    app_password: str,
    *,
    server: str = IMAP_SERVER,
    port: int = IMAP_PORT,
    excluded_folder_short_names: frozenset[str] = EXCLUDED_FOLDER_SHORT_NAMES,
    since_uid: dict[str, int] | None = None,
) -> Iterator[dict[str, Any]]:
    """Connect, walk non-excluded folders, and yield one normalized record
    per message.

    ``since_uid`` maps folder name → last-seen UID. When supplied, the
    search becomes ``UID <N>:*`` so only messages newer than the last
    run are fetched. Missing folders are treated as "fetch everything".
    """
    since_uid = since_uid or {}
    context = ssl.create_default_context()  # verifies certs + hostname
    client = imaplib.IMAP4_SSL(server, port, ssl_context=context)
    try:
        try:
            client.login(email_address, app_password)
        except imaplib.IMAP4.error as exc:
            raise AdapterError(f"IMAP login failed for {email_address}") from exc

        status, folders_raw = client.list()
        if status != "OK":
            raise AdapterError(f"IMAP LIST failed: status={status}")

        for raw_line in folders_raw or []:
            folder_name = _parse_folder_line(raw_line)
            if folder_name is None:
                continue
            short_name = folder_name.rsplit("/", 1)[-1].rsplit(".", 1)[-1]
            if short_name.lower() in excluded_folder_short_names:
                continue
            yield from _fetch_folder(
                client,
                folder_name=folder_name,
                since_uid=since_uid.get(folder_name),
            )
    finally:
        try:
            client.logout()
        except Exception:
            pass


def _parse_folder_line(raw_line: bytes | tuple[bytes, ...]) -> str | None:
    """Pull the mailbox name out of an IMAP LIST response line."""
    if isinstance(raw_line, tuple):
        raw_line = b" ".join(raw_line)
    decoded = raw_line.decode("utf-8", errors="replace")
    match = _FOLDER_LINE_RE.search(decoded)
    if match is None:
        return None
    return match.group(1)


def _fetch_folder(
    client: imaplib.IMAP4_SSL,
    *,
    folder_name: str,
    since_uid: int | None,
) -> Iterator[dict[str, Any]]:
    """Select ``folder_name`` read-only and yield every message matched
    by the UID-search criterion."""
    quoted = f'"{folder_name}"'
    status, select_data = client.select(quoted, readonly=True)
    if status != "OK":
        print(f"  [{folder_name}] unreadable, skipping", file=sys.stderr, flush=True)
        return

    # EXISTS count from SELECT — just for progress visibility.
    total: int | None = None
    try:
        if select_data and isinstance(select_data[0], bytes | bytearray):
            total = int(select_data[0].decode())
    except (ValueError, AttributeError):
        total = None

    criterion = f"UID {since_uid + 1}:*" if since_uid else "ALL"
    status, search_data = client.uid("SEARCH", None, criterion)
    if status != "OK" or not search_data or not search_data[0]:
        print(
            f"  [{folder_name}] 0 messages matched (total={total if total is not None else '?'})",
            file=sys.stderr,
            flush=True,
        )
        return

    uids = search_data[0].split()
    print(
        f"  [{folder_name}] fetching {len(uids)} message(s) "
        f"(total={total if total is not None else '?'})",
        file=sys.stderr,
        flush=True,
    )

    for i, uid in enumerate(uids, start=1):
        status, fetch_data = client.uid("FETCH", uid, "(RFC822)")
        if status != "OK" or not fetch_data:
            continue
        body = _extract_rfc822_bytes(fetch_data)
        if body is None:
            continue
        yield _parse_message(body, folder_name=folder_name)
        if i % 100 == 0:
            print(f"    [{folder_name}] {i}/{len(uids)}", file=sys.stderr, flush=True)


def _extract_rfc822_bytes(fetch_data: list[Any]) -> bytes | None:
    """IMAP FETCH returns a list of tuples / bytes. Pull out the first
    tuple's second element, which is the raw RFC822 message."""
    for item in fetch_data:
        if isinstance(item, tuple) and len(item) >= 2:
            return item[1]
    return None


def _parse_message(raw: bytes, *, folder_name: str) -> dict[str, Any]:
    """Parse raw RFC822 bytes into a normalized record."""
    msg = email.message_from_bytes(raw)

    message_id = (msg.get("Message-ID") or "").strip()
    source_id = message_id or "sha256:" + sha256(raw).hexdigest()

    timestamp = _parse_date_header(msg.get("Date", ""))
    content = _extract_body(msg)
    has_attachments, attachment_count = _count_attachments(msg)

    return {
        "source": "apple-mail",
        "source_id": source_id,
        "timestamp": timestamp,
        "content": content,
        "metadata": {
            "folder": folder_name,
            "from": msg.get("From", "") or "",
            "to": _split_address_list(msg.get("To", "")),
            "cc": _split_address_list(msg.get("Cc", "")),
            "subject": msg.get("Subject", "") or "",
            "message_id": message_id,
            "in_reply_to": (msg.get("In-Reply-To") or "").strip(),
            "references": _split_references(msg.get("References", "")),
            "has_attachments": has_attachments,
            "attachment_count": attachment_count,
        },
    }


def _parse_date_header(raw: str) -> str:
    """``email.utils.parsedate_to_datetime`` handles the usual RFC 2822
    shapes. Halt on failure rather than guess; malformed timestamps are
    a sign of something weirder happening."""
    if not raw:
        raise AdapterError("Missing Date header")
    try:
        dt = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError) as exc:
        raise AdapterError(f"Unparseable Date header {raw!r}: {exc}") from exc
    if dt is None:
        raise AdapterError(f"Unparseable Date header {raw!r}")
    if dt.tzinfo is None:
        # Rare but valid — per-RFC 2822 a missing zone means "local".
        # Treat as UTC to produce a canonical form rather than a floating time.
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def _extract_body(msg: Message) -> str:
    """Prefer ``text/plain`` parts; fall back to ``text/html`` → html2text."""
    if msg.is_multipart():
        text_plain: Message | None = None
        text_html: Message | None = None
        for part in msg.walk():
            if _is_attachment(part):
                continue
            ctype = part.get_content_type()
            if ctype == "text/plain" and text_plain is None:
                text_plain = part
            elif ctype == "text/html" and text_html is None:
                text_html = part
        chosen = text_plain if text_plain is not None else text_html
        if chosen is None:
            return ""
        return _render(chosen)
    return _render(msg)


def _render(part: Message) -> str:
    decoded = _decode_payload(part)
    if part.get_content_type() == "text/html":
        import html2text

        return html2text.html2text(decoded)
    return decoded


def _decode_payload(part: Message) -> str:
    """``get_payload(decode=True)`` handles quoted-printable and base64
    transfer encodings. Charset comes from the Content-Type header; fall
    back to UTF-8 on unknown charsets, replacing undecodable bytes."""
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _is_attachment(part: Message) -> bool:
    cd = (part.get("Content-Disposition") or "").lower()
    return cd.startswith("attachment")


def _count_attachments(msg: Message) -> tuple[bool, int]:
    count = 0
    if msg.is_multipart():
        for part in msg.walk():
            if _is_attachment(part):
                count += 1
    return count > 0, count


def _split_address_list(raw: str) -> list[str]:
    """Header values like "Alex <alex@x.invalid>, Sam <sam@x.invalid>" →
    the individual entries. Uses ``email.utils.getaddresses`` which
    correctly handles quoted display names containing commas."""
    if not raw:
        return []
    entries: list[str] = []
    for display, email_addr in email.utils.getaddresses([raw]):
        if display and email_addr:
            entries.append(f"{display} <{email_addr}>")
        elif email_addr:
            entries.append(email_addr)
        elif display:
            entries.append(display)
    return entries


def _split_references(raw: str) -> list[str]:
    if not raw:
        return []
    return [r for r in raw.split() if r.strip()]
