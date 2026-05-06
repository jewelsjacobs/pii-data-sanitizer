"""Shared config loader for the extract/sanitize scripts.

Reads ``config/local.json`` (gitignored) and returns a typed object.
Fails fast with a pointer to ``config/local.example.json`` if the file
is missing — the scripts refuse to operate on the real filesystem
without an explicit local config.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = Path("config/local.json")
EXAMPLE_CONFIG_PATH = Path("config/local.example.json")


@dataclass(frozen=True)
class LocalConfig:
    ical_source_dirs: list[Path] = field(default_factory=list)
    local_files_source_dirs: list[Path] = field(default_factory=list)
    imessage_chat_db_path: Path | None = None
    apple_notes_db_path: Path | None = None
    self_emails: frozenset[str] = field(default_factory=frozenset)
    self_phones: frozenset[str] = field(default_factory=frozenset)
    keep_names: frozenset[str] = field(default_factory=frozenset)
    keep_gpes: frozenset[str] = field(default_factory=frozenset)
    # Literal strings to hard-redact everywhere they appear, case-insensitively.
    # For sensitive tokens that lack pattern context (bare last-4 digits of an
    # SSN / bank account, a macOS username appearing in file paths, a home
    # address number that NER won't recognize). Hard-redacted to
    # ``[REDACTED_PERSONAL]`` regardless of surrounding text. Stored
    # case-folded internally so both casings of "Juliajacobs" / "juliajacobs"
    # match. Some over-redaction (a "1234" that isn't your SSN-last-4) is
    # acceptable — much better than a real leak.
    scrub_strings: frozenset[str] = field(default_factory=frozenset)
    mapping_path: Path = Path("mapping/mapping.json")
    raw_staging_dir: Path = Path("raw-staging")
    clean_staging_dir: Path = Path("clean-staging")
    promote_staging_dir: Path = Path("promote-staging")


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> LocalConfig:
    if not path.exists():
        print(
            f"error: config not found at {path}.\n"
            f"Copy {EXAMPLE_CONFIG_PATH} to {path} and fill in real values.",
            file=sys.stderr,
        )
        sys.exit(2)
    data = json.loads(path.read_text())
    imessage_raw = data.get("imessage_chat_db_path")
    imessage_path = Path(imessage_raw).expanduser() if imessage_raw else None
    notes_raw = data.get("apple_notes_db_path")
    notes_path = Path(notes_raw).expanduser() if notes_raw else None
    return LocalConfig(
        ical_source_dirs=[Path(p) for p in data.get("ical_source_dirs", [])],
        local_files_source_dirs=[Path(p) for p in data.get("local_files_source_dirs", [])],
        imessage_chat_db_path=imessage_path,
        apple_notes_db_path=notes_path,
        self_emails=frozenset(e.strip().lower() for e in data.get("self_emails", [])),
        self_phones=frozenset(_normalize_phone(p) for p in data.get("self_phones", [])),
        keep_names=frozenset(data.get("keep_names", [])),
        keep_gpes=frozenset(data.get("keep_gpes", [])),
        scrub_strings=frozenset(s.casefold() for s in data.get("scrub_strings", []) if s),
        mapping_path=Path(data.get("mapping_path", "mapping/mapping.json")),
        raw_staging_dir=Path(data.get("raw_staging_dir", "raw-staging")),
        clean_staging_dir=Path(data.get("clean_staging_dir", "clean-staging")),
        promote_staging_dir=Path(data.get("promote_staging_dir", "promote-staging")).expanduser(),
    )


def _normalize_phone(raw: str) -> str:
    import re

    return re.sub(r"\D", "", raw)
