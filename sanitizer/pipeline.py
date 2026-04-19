"""Sanitize orchestrator — rules → emails/phones → addresses → NER → mapping.

Operates on one normalized record at a time (see files/pipeline-architecture.md
→ Stage 1 for the shape). Does not touch the filesystem, does not write
manifests, does not know about sources — that is the caller's job.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any

from sanitizer.mapping import Mapping
from sanitizer.rules import apply_rules

# Metadata keys that pass through untouched. Structural identifiers needed
# for threading / calendar semantics, plus booleans and integer metadata
# where redaction would corrupt the shape.
_STRUCTURAL_KEEP_KEYS = frozenset(
    {
        "message_id",
        "in_reply_to",
        "references",
        "source_id",
        "source",
        "timestamp",
        "dtstart",
        "dtend",
        "rrule",
        "format",
        "extraction_method",
        "ocr_low_confidence",
        "page_count",
        "size_bytes",
        "calendar_name",
        "chat_is_group",
        "from_me",
        "service",
        "has_attachments",
        "attachment_count",
        "is_reply",
        "thread_originator_guid",
    }
)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# Common US/international phone shapes: paren'd area code, dotted, dashed,
# spaced, E.164, and bare 10-digit.
_PHONE_RE = re.compile(
    r"(?<!\d)"
    r"(?:\+\d{1,3}[-. ]?)?"
    r"(?:\(\d{3}\)[-. ]?|\d{3}[-. ])"
    r"\d{3}[-. ]?\d{4}"
    r"(?!\d)"
    r"|(?<!\d)\+?\d{10,15}(?!\d)"
)

# Generic street-address detector (non-home). Requires a leading number, at
# least one word component, and a known street suffix; optional unit/suite
# and optional N/S/E/W direction marker.
_ADDRESS_RE = re.compile(
    r"\b\d+\s+(?:[\w.]+\s+)+"
    r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Drive|Dr|Way|"
    r"Lane|Ln|Terrace|Ter|Parkway|Pkwy|Court|Ct|Place|Pl)"
    r"(?:[,.]?\s*(?:Suite|Apt|Unit|Ste|#)\s*\w+)?"
    r"(?:\s+[NSEW]\b)?",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SelfIdentity:
    """The user's own contact identifiers → `[MY_EMAIL]` / `[MY_PHONE]`."""

    emails: frozenset[str] = field(default_factory=frozenset)
    phones: frozenset[str] = field(default_factory=frozenset)

    @staticmethod
    def normalize_email(raw: str) -> str:
        return raw.strip().lower()

    @staticmethod
    def normalize_phone(raw: str) -> str:
        return re.sub(r"\D", "", raw)


@dataclass(frozen=True)
class KeepList:
    """Entity surface forms that bypass pseudonymization.

    ``names`` is checked case-sensitively against the NER span text. Intended
    for the user's own first name and any allow-listed pet names (per the
    redaction policy → KEEP list). ``gpes`` handles city-level geography
    that should stay as-is.
    """

    names: frozenset[str] = field(default_factory=frozenset)
    gpes: frozenset[str] = field(default_factory=frozenset)


def sanitize(
    record: dict[str, Any],
    *,
    mapping: Mapping | None = None,
    self_identity: SelfIdentity | None = None,
    keep: KeepList | None = None,
) -> dict[str, Any]:
    """Run the layered sanitizer over ``record`` and return a new record."""
    mapping = mapping if mapping is not None else Mapping()
    identity = self_identity if self_identity is not None else SelfIdentity()
    keep = keep if keep is not None else KeepList()
    out = copy.deepcopy(record)

    if "content" in out and isinstance(out["content"], str):
        out["content"] = _sanitize_text(out["content"], mapping, identity, keep)

    if "metadata" in out and isinstance(out["metadata"], dict):
        out["metadata"] = _sanitize_metadata(out["metadata"], mapping, identity, keep)

    return out


def _sanitize_text(
    text: str,
    mapping: Mapping,
    identity: SelfIdentity,
    keep: KeepList,
) -> str:
    text = apply_rules(text)
    text = _sub_emails(text, mapping, identity)
    text = _sub_phones(text, mapping, identity)
    text = _sub_addresses(text, mapping)
    text = _sub_ner(text, mapping, keep)
    return text


def _sub_emails(text: str, mapping: Mapping, identity: SelfIdentity) -> str:
    def repl(m: re.Match[str]) -> str:
        key = SelfIdentity.normalize_email(m.group(0))
        if key in identity.emails:
            return "[MY_EMAIL]"
        return mapping.get_or_assign("EMAIL", key)

    return _EMAIL_RE.sub(repl, text)


def _sub_phones(text: str, mapping: Mapping, identity: SelfIdentity) -> str:
    def repl(m: re.Match[str]) -> str:
        raw = m.group(0)
        key = SelfIdentity.normalize_phone(raw)
        if not 10 <= len(key) <= 15:
            return raw
        if key in identity.phones:
            return "[MY_PHONE]"
        # US 10-digit → normalize to 11-digit with leading "1" so
        # "(503) 555-0142" and "+15035550142" share the same pseudonym.
        if len(key) == 10:
            alt = "1" + key
            if alt in identity.phones:
                return "[MY_PHONE]"
        return mapping.get_or_assign("PHONE", key)

    return _PHONE_RE.sub(repl, text)


def _sub_addresses(text: str, mapping: Mapping) -> str:
    def repl(m: re.Match[str]) -> str:
        return _get_or_assign_with_coref(mapping, "ADDRESS", m.group(0))

    return _ADDRESS_RE.sub(repl, text)


def _sub_ner(text: str, mapping: Mapping, keep: KeepList) -> str:
    """Run NER and replace recognized spans with mapped pseudonyms."""
    if not text:
        return text
    from sanitizer.ner import analyze

    spans = analyze(text)
    if not spans:
        return text

    # Resolve each span to a pseudonym (or KEEP). Assign in forward order
    # so [PERSON_1] really is the first person encountered.
    resolved: list[tuple[int, int, str]] = []
    for span in spans:
        surface = span.text
        # Ignore spans that sit inside our own redaction tokens (`[MY_PHONE]`,
        # `[HOME_ADDRESS]`, etc.). spaCy sometimes tags the inner text as ORG.
        if (
            span.start > 0
            and span.end < len(text)
            and (text[span.start - 1] == "[" and text[span.end] == "]")
        ):
            continue
        # Name / GPE keep-list applies regardless of entity type. spaCy routinely
        # mis-tags short tokens (a personal name as ORG, a city as FAC, a
        # branded product as LOC); if the surface form is on the KEEP list we
        # bypass pseudonymization for all of them.
        if surface in keep.names or surface in keep.gpes:
            continue
        # Short all-caps acronyms (SSN, API, WA, ...) are almost always
        # abbreviations or document labels that spaCy mislabels as ORG.
        if surface.isupper() and len(surface) <= 5:
            continue
        if span.entity_type == "PERSON":
            pseudonym = _get_or_assign_with_coref(mapping, "PERSON", surface)
        elif span.entity_type == "ORG":
            pseudonym = _get_or_assign_with_coref(mapping, "ORG", surface)
        elif span.entity_type in {"GPE", "LOC", "FAC"}:
            # Before assigning a LOCATION pseudonym, check whether the surface
            # form matches an existing PERSON (common spaCy mislabel for
            # single-token names like "Marcus"). Reuse the PERSON pseudonym if
            # so — this is the same first-token coref heuristic, cross-kind.
            person_pseudonym = _find_cross_kind_coref(mapping, "PERSON", surface)
            if person_pseudonym is not None:
                pseudonym = person_pseudonym
            else:
                pseudonym = _get_or_assign_with_coref(mapping, "LOCATION", surface)
        else:
            continue
        resolved.append((span.start, span.end, pseudonym))

    # Apply in reverse order so offsets stay valid.
    resolved.sort(key=lambda s: s[0], reverse=True)
    chars = list(text)
    for start, end, pseudonym in resolved:
        chars[start:end] = pseudonym
    return "".join(chars)


def _find_cross_kind_coref(mapping: Mapping, kind: str, key: str) -> str | None:
    """If ``key`` shares a first token with an existing ``kind`` entry, reuse
    that entry's pseudonym. Used when spaCy mislabels a known PERSON as an
    ORG/LOC/GPE because the surface token is too short for it to disambiguate.
    Returns ``None`` if no match.
    """
    slot = mapping._by_kind.get(kind, {})  # noqa: SLF001
    if not slot:
        return None
    key_first = key.split()[0] if key.strip() else key
    for existing_key, pseudonym in slot.items():
        existing_first = existing_key.split()[0] if existing_key.strip() else existing_key
        if existing_first.lower() == key_first.lower():
            return pseudonym
    return None


def _get_or_assign_with_coref(mapping: Mapping, kind: str, key: str) -> str:
    """Like ``mapping.get_or_assign`` but with first-token coreference.

    If an existing key in the same ``kind`` shares a first whitespace-split
    token with the new key, reuse its pseudonym (and record the new key as
    an alias). This is the simple-coref path called out as a known gap in
    the redaction policy — good enough for "Linda" and "Linda Hartwell" to
    collapse to the same pseudonym, insufficient for pronominal coref.
    """
    slot = mapping._by_kind.setdefault(kind, {})  # noqa: SLF001
    if key in slot:
        return slot[key]
    key_first = key.split()[0] if key.strip() else key
    for existing_key, pseudonym in slot.items():
        existing_first = existing_key.split()[0] if existing_key.strip() else existing_key
        if existing_first.lower() == key_first.lower():
            slot[key] = pseudonym
            return pseudonym
    return mapping.get_or_assign(kind, key)


def _sanitize_metadata(
    meta: dict[str, Any],
    mapping: Mapping,
    identity: SelfIdentity,
    keep: KeepList,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in meta.items():
        if key == "chat_identifier" and isinstance(value, str):
            # Opaque group hashes (`chat000000012345`) pass through; phone /
            # email identifiers get the usual pseudonymization.
            if _EMAIL_RE.fullmatch(value) or _PHONE_RE.fullmatch(value):
                out[key] = _sanitize_text(value, mapping, identity, keep)
            else:
                out[key] = value
            continue
        if key in _STRUCTURAL_KEEP_KEYS:
            out[key] = value
            continue
        if isinstance(value, str):
            out[key] = _sanitize_text(value, mapping, identity, keep)
        elif isinstance(value, list):
            out[key] = [
                _sanitize_text(v, mapping, identity, keep) if isinstance(v, str) else v
                for v in value
            ]
        elif isinstance(value, dict):
            out[key] = _sanitize_nested_dict(value, mapping, identity, keep)
        else:
            out[key] = value
    return out


def _sanitize_nested_dict(
    d: dict[str, Any],
    mapping: Mapping,
    identity: SelfIdentity,
    keep: KeepList,
) -> dict[str, Any]:
    """Handle nested shapes like ical's structured_location."""
    result: dict[str, Any] = {}
    for k, v in d.items():
        if k in {"lat", "lon"}:
            # Any coordinate pair is a location leak per policy.
            result[k] = None
        elif isinstance(v, str):
            result[k] = _sanitize_text(v, mapping, identity, keep)
        else:
            result[k] = v
    return result
