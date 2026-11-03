"""Stable-pseudonym mapping for the sanitizer.

A `Mapping` holds, per entity kind, a dictionary from the raw entity key
(e.g. an email address or a person's canonical name) to a stable pseudonym
token (e.g. `[EMAIL_3]`). Once a key is assigned a pseudonym, that pseudonym
never changes — this is the property that lets downstream agents thread
conversations and link vault entries across runs.

Mappings are serializable to plain JSON. The real pipeline encrypts the
serialized form at rest (see files/redaction-policy.md); tests use an
ephemeral in-memory mapping or a temp-file JSON.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Self


class Mapping:
    """Per-kind pseudonym store."""

    def __init__(self) -> None:
        self._by_kind: dict[str, dict[str, str]] = {}
        self._counters: dict[str, int] = {}

    def get_or_assign(self, kind: str, key: str) -> str:
        """Return the stable pseudonym for ``(kind, key)``, assigning if new.

        ``kind`` is an uppercase category name (``PERSON``, ``EMAIL``, ``ORG``,
        ``PHONE``, ``LOCATION``, ``ADDRESS``). ``key`` is the canonical form
        of the entity (e.g. the lowercased email address, or the person's
        canonical display name).
        """
        slot = self._by_kind.setdefault(kind, {})
        if key not in slot:
            self._counters[kind] = self._counters.get(kind, 0) + 1
            slot[key] = f"[{kind}_{self._counters[kind]}]"
        return slot[key]

    def contains(self, kind: str, key: str) -> bool:
        return key in self._by_kind.get(kind, {})

    def entries(self, kind: str) -> dict[str, str]:
        """Return a copy of the ``key -> pseudonym`` dict for ``kind``.

        Used by the known-surface sweep in the pipeline: entities already
        in the mapping are substituted by literal match even where NER
        fails to detect them (short calendar titles, greetings, etc.).
        """
        return dict(self._by_kind.get(kind, {}))

    def size(self, kind: str) -> int:
        """Number of entries for ``kind`` — cheap cache-invalidation signal."""
        return len(self._by_kind.get(kind, {}))

    def to_dict(self) -> dict[str, object]:
        return {"by_kind": self._by_kind, "counters": self._counters}

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> Self:
        m = cls()
        m._by_kind = dict(data.get("by_kind", {}))  # type: ignore[arg-type]
        m._counters = dict(data.get("counters", {}))  # type: ignore[arg-type]
        return m

    def save(self, path: Path) -> None:
        """Write atomically: a kill mid-write must not truncate the JSON.

        Serialize to a sibling temp file, then ``os.replace`` it over the
        target so readers only ever see the old file or the complete new one.
        """
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True))
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: Path) -> Self:
        if not path.exists():
            return cls()
        return cls.from_dict(json.loads(path.read_text()))
