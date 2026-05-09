"""CLI: promote sanitized EOB records → Obsidian-friendly markdown notes.

EOB records share the local-files normalized shape (the EOB extract
ran through the local-files adapter), so much of the rendering follows
``promote_local_files``. The differences:

  * ``type: eob`` (vs ``reference``) so the vault's Crew agents can
    route EOB notes to the SSA-evidence workflow rather than the
    generic reference template.
  * Filename type label is ``EOB`` (vs ``Note``).
  * Tags include ``medical``, ``eob``, ``ssa-evidence``.
  * Frontmatter exposes ``original-filename`` and ``mtime`` as the EOB
    document's date — the actual date(s) of service live in the body
    where downstream agents can parse them out as needed.

Output goes to ``<promote_staging_dir>/eobs/``. As with every other
promoter, the staging folder is OUTSIDE the vault by design — the user
reviews and moves approved notes into ``00-Inbox/``.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import Any

from sanitizer.promote import _build_filename, _now_iso, _render_note, _split_iso, _str
from scripts._config import LocalConfig, load_config


def promote_eob(record: dict[str, Any]) -> tuple[str, str]:
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

    title = PurePosixPath(raw_path).stem if raw_path else "Untitled EOB"

    fm: dict[str, Any] = {
        "type": "eob",
        "date": date_str,
        "tags": ["reference", "medical", "eob", "ssa-evidence", "imported"],
        "status": "inbox",
        "source": "eobs",
        "source-path": raw_path,
        "source-record-id": _str(record.get("source_id")),
        "original-filename": title,
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
    body_lines.append(
        "**Document type**: Explanation of Benefits (EOB) — sanitized for SSA "
        "disability documentation."
    )
    body_lines.append(
        "**PII handling**: patient identifiers (name, address, member/subscriber/"
        "policy IDs, account numbers, claim reference numbers) have been redacted. "
        "Provider names, facility addresses, dates of service, diagnosis codes, "
        "procedure codes, and amounts are preserved verbatim for SSA proof."
    )
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
    body_lines.append("*Imported from sanitized EOB export.*")

    markdown = _render_note(fm, "\n".join(body_lines))
    filename = _build_filename(date_str, "EOB", title)
    return filename, markdown


def _iter_records(path: Path) -> Iterator[dict[str, Any]]:
    with path.open() as fin:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


def _disambiguate(filename: str, n: int) -> str:
    if filename.endswith(".md"):
        return f"{filename[:-3]} ({n}).md"
    return f"{filename} ({n})"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=Path("config/local.json"), help="Local config path."
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing notes in the staging folder. Default is to skip.",
    )
    args = parser.parse_args(argv)

    cfg: LocalConfig = load_config(args.config)
    clean_file = cfg.clean_staging_dir / "eobs" / "events.jsonl"
    if not clean_file.exists():
        print(
            f"error: no sanitized input at {clean_file}. Run sanitize_eobs first.",
            file=sys.stderr,
        )
        return 2

    out_dir = cfg.promote_staging_dir / "eobs"
    out_dir.mkdir(parents=True, exist_ok=True)

    print(
        f"Promoting eobs → {out_dir}\n"
        f"   from: {clean_file}\n"
        f"   note: this is OUTSIDE the vault — review and move manually.",
        file=sys.stderr,
        flush=True,
    )

    written = 0
    skipped = 0
    duplicates = 0
    seen: dict[str, int] = {}

    for record in _iter_records(clean_file):
        filename, markdown = promote_eob(record)
        unique_name = filename
        if filename in seen:
            seen[filename] += 1
            duplicates += 1
            unique_name = _disambiguate(filename, seen[filename])
        else:
            seen[filename] = 1
        out_path = out_dir / unique_name
        if out_path.exists() and not args.overwrite:
            skipped += 1
            continue
        out_path.write_text(markdown)
        written += 1

    print(
        f"Done. wrote={written} skipped={skipped} disambiguated={duplicates}\n"
        f"Review and move approved notes into the vault's 00-Inbox/.",
        file=sys.stderr,
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
