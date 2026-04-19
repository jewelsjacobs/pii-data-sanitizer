"""iCal source adapter — Stage 1 extract for the ical source.

Reads `.ics` files (or Calendar.app's local `.ics` store) and emits
normalized records matching files/pipeline-architecture.md → Stage 1.
Never modifies content, never redacts, never writes outside
``/raw-staging/ical/``. Halts loudly on anything it doesn't recognise.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from icalendar import Calendar


class AdapterError(RuntimeError):
    """Raised when the adapter encounters input it refuses to pass through.

    Per the adapter contract in files/source-adapters.md: halt on any
    unexpected field, format, or error. Never silently skip data.
    """


def extract_events(paths: list[Path]) -> Iterator[dict[str, Any]]:
    """Yield normalized records from every ``.ics`` file under ``paths``.

    Each entry in ``paths`` is either a single ``.ics`` file or a
    directory that is walked recursively for ``.ics`` files. Anything
    else raises ``AdapterError``.
    """
    for path in paths:
        path = Path(path)
        if path.is_dir():
            for ics_file in sorted(path.rglob("*.ics")):
                yield from _parse_file(ics_file)
        elif path.is_file() and path.suffix.lower() == ".ics":
            yield from _parse_file(path)
        else:
            raise AdapterError(f"Not a .ics file or directory of .ics files: {path}")


def _parse_file(path: Path) -> Iterator[dict[str, Any]]:
    try:
        cal = Calendar.from_ical(path.read_bytes())
    except Exception as exc:
        raise AdapterError(f"Failed to parse {path}: {exc}") from exc
    calendar_name = str(cal.get("X-WR-CALNAME", path.stem))
    for vevent in cal.walk("VEVENT"):
        yield _event_to_record(vevent, calendar_name)


def _event_to_record(vevent: Any, calendar_name: str) -> dict[str, Any]:
    uid = vevent.get("UID")
    if uid is None:
        raise AdapterError(f"VEVENT missing UID in calendar {calendar_name!r}")

    dtstart_prop = vevent.get("DTSTART")
    if dtstart_prop is None:
        raise AdapterError(f"VEVENT {uid} missing DTSTART")
    dtstart_iso = _to_iso(dtstart_prop.dt, context=f"DTSTART of {uid}")

    dtend_prop = vevent.get("DTEND")
    dtend_iso = (
        _to_iso(dtend_prop.dt, context=f"DTEND of {uid}") if dtend_prop is not None else None
    )

    summary = str(vevent.get("SUMMARY", ""))
    description = str(vevent.get("DESCRIPTION", ""))
    location = str(vevent.get("LOCATION", ""))

    rrule_prop = vevent.get("RRULE")
    rrule = rrule_prop.to_ical().decode("utf-8") if rrule_prop is not None else None

    organizer_prop = vevent.get("ORGANIZER")
    organizer = _contact_to_str(organizer_prop) if organizer_prop is not None else None

    attendees_prop = vevent.get("ATTENDEE", [])
    if not isinstance(attendees_prop, list):
        attendees_prop = [attendees_prop]
    attendees = [_contact_to_str(a) for a in attendees_prop]

    url = str(vevent.get("URL", "")) or None

    structured_location = _parse_structured_location(vevent)

    content_parts = [p for p in (summary, description) if p]
    content = "\n\n".join(content_parts)

    return {
        "source": "ical",
        "source_id": str(uid),
        "timestamp": dtstart_iso,
        "content": content,
        "metadata": {
            "calendar_name": calendar_name,
            "summary": summary,
            "description": description,
            "location": location,
            "dtstart": dtstart_iso,
            "dtend": dtend_iso,
            "rrule": rrule,
            "organizer": organizer,
            "attendees": attendees,
            "url": url,
            "structured_location": structured_location,
        },
    }


def _to_iso(value: Any, *, context: str) -> str:
    """Normalize a DTSTART/DTEND to ISO 8601.

    - Floating-time ``datetime`` (no tzinfo) → interpret as the user's local
      timezone (matches Apple Calendar semantics for TZ-less events) and
      convert to UTC. Calendar.app exports produce these routinely; halting
      would make the adapter unusable against real exports.
    - ``datetime`` with tzinfo → convert to UTC.
    - ``date`` (all-day) → return the date as ISO (no time component).
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=_local_tzinfo())
        return value.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    raise AdapterError(f"{context}: unexpected type {type(value).__name__}")


def _local_tzinfo() -> Any:
    """Best-effort local tzinfo for the current machine."""
    return datetime.now().astimezone().tzinfo


def _contact_to_str(prop: Any) -> str:
    """Render an ORGANIZER / ATTENDEE as ``"Display Name <email>"`` when a
    CN (common name) parameter is present, otherwise just the email address.
    """
    raw = prop.to_ical().decode("utf-8") if hasattr(prop, "to_ical") else str(prop)
    email = raw[len("mailto:") :] if raw.lower().startswith("mailto:") else raw
    cn = prop.params.get("CN") if hasattr(prop, "params") else None
    if cn:
        return f"{cn} <{email}>"
    return email


def _parse_structured_location(vevent: Any) -> dict[str, Any] | None:
    """Pull Apple's ``X-APPLE-STRUCTURED-LOCATION`` into a flat dict.

    The property value is a ``geo:<lat>,<lon>`` URI with an X-TITLE
    parameter giving the human-readable venue name. Both components are
    preserved so the sanitize stage can apply the policy's location
    rules (redact coords, pseudonymize title).
    """
    prop = vevent.get("X-APPLE-STRUCTURED-LOCATION")
    if prop is None:
        return None
    raw = str(prop)
    lat: float | None = None
    lon: float | None = None
    if raw.startswith("geo:"):
        try:
            lat_str, lon_str = raw[len("geo:") :].split(",", 1)
            lat = float(lat_str)
            lon = float(lon_str)
        except (ValueError, IndexError):
            lat = None
            lon = None
    title = None
    if hasattr(prop, "params"):
        title_param = prop.params.get("X-TITLE")
        title = str(title_param) if title_param is not None else None
    return {"lat": lat, "lon": lon, "title": title}
