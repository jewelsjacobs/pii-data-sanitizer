"""CLI: backfill correct creation/modification dates on apple-notes vault files.

The apple-notes adapter at extract time only read
``ZICCLOUDSYNCINGOBJECT.ZCREATIONDATE1`` and silently emitted
``2001-01-01`` (the Apple Cocoa epoch) when that column was NULL.
Apple's NoteStore schema has migrated multiple times — for many notes
the real date lives in ``ZCREATIONDATE2`` / ``ZCREATIONDATE3`` or in
the ``ZMODIFICATIONDATE*`` family. This script repairs already-promoted
vault files in place, without re-running the full
extract → sanitize → promote pipeline.

For each ``.md`` file under ``--root`` whose frontmatter has
``source: apple-notes``:

1. Read the note's ``source-record-id`` (the Apple ``ZIDENTIFIER`` UUID).
2. Re-query the snapshot SQLite, ``COALESCE``-ing across all available
   creation columns then all modification columns.
3. If the resolved date differs from the basename's date prefix or the
   frontmatter ``date:`` field, rename the file and rewrite the
   frontmatter.
4. Sibling ``.before-scrub.bak`` files are renamed alongside.

Notes whose date is genuinely NULL across every column become
``unknown-date — Note — <title>.md`` with frontmatter ``date: unknown``,
so they're visible as undated rather than silently impersonating real
2001 entries.

Default is dry-run. Pass ``--apply`` to commit.

Example:
    uv run python -m scripts.backfill_apple_notes_dates \\
        --root "/Users/Shared/The Brain Vault" \\
        --snapshot raw-staging/apple-notes/snapshot.sqlite
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

_APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=UTC)
_DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}|unknown-date)( — Note — .+)$")
_DEFAULT_EXCLUDE_DIRS = (".claude", ".obsidian", ".git", ".trash")
_FRONTMATTER_SOURCE_RE = re.compile(r"^\s*source\s*:\s*([^\s]+)\s*$")
_FRONTMATTER_RECORD_ID_RE = re.compile(r"^\s*source-record-id\s*:\s*[\"']?([^\s\"']+)[\"']?\s*$")
_FRONTMATTER_DATE_LINE_RE = re.compile(
    r"^\s*(date|modified-date)\s*:\s*[\"']?([^\s\"']+)[\"']?\s*$"
)


def _apple_epoch_to_date_str(value: float | int | None) -> str | None:
    """Convert Apple Cocoa-reference timestamp to ``YYYY-MM-DD``.

    Returns ``None`` when the value is missing, zero, unparseable, or
    yields a date outside the representable range.

    Some rows store the timestamp in nanoseconds rather than seconds —
    a migration artifact, mirroring the chat.db post-Mojave behavior
    documented in source-adapters.md → imessage. We detect nanoseconds
    by magnitude (anything beyond ~year 2317 in seconds since 2001 is
    plausibly nanoseconds and certainly not seconds).
    """
    if value is None or value == 0:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if abs(seconds) > 1e10:
        seconds = seconds / 1e9
    try:
        return (_APPLE_EPOCH + timedelta(seconds=seconds)).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _detect_date_columns(conn: sqlite3.Connection) -> tuple[list[str], list[str]]:
    cur = conn.execute("PRAGMA table_info(ZICCLOUDSYNCINGOBJECT)")
    cols = {row[1] for row in cur.fetchall()}
    # Prefer the highest-numbered column first because Apple migrations
    # have generally moved the canonical creation date "rightward" (1 → 2 → 3).
    creation_order = ("ZCREATIONDATE3", "ZCREATIONDATE2", "ZCREATIONDATE1", "ZCREATIONDATE")
    modification_order = (
        "ZMODIFICATIONDATE3",
        "ZMODIFICATIONDATE2",
        "ZMODIFICATIONDATE1",
        "ZMODIFICATIONDATE",
    )
    creation = [c for c in creation_order if c in cols]
    modification = [c for c in modification_order if c in cols]
    return creation, modification


def _build_date_map(snapshot: Path) -> dict[str, tuple[str | None, str | None]]:
    """Return ``ZIDENTIFIER -> (creation_date_str, modification_date_str)``."""
    uri = f"file:{snapshot}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        creation_cols, modification_cols = _detect_date_columns(conn)
        if not creation_cols and not modification_cols:
            raise RuntimeError(
                "No date columns found in ZICCLOUDSYNCINGOBJECT — schema unexpected."
            )
        creation_list = ", ".join(creation_cols) if creation_cols else ""
        modification_list = ", ".join(modification_cols) if modification_cols else ""
        # When both families exist, each falls back to the other.
        if creation_cols and modification_cols:
            creation_expr = f"COALESCE({creation_list}, {modification_list})"
            modification_expr = f"COALESCE({modification_list}, {creation_list})"
        elif creation_cols:
            creation_expr = f"COALESCE({creation_list})"
            modification_expr = creation_expr
        else:
            modification_expr = f"COALESCE({modification_list})"
            creation_expr = modification_expr

        query = f"""
            SELECT ZIDENTIFIER,
                   {creation_expr} AS created,
                   {modification_expr} AS modified
            FROM ZICCLOUDSYNCINGOBJECT
            WHERE ZIDENTIFIER IS NOT NULL
        """
        result: dict[str, tuple[str | None, str | None]] = {}
        for row in conn.execute(query):
            ident = row[0]
            created = _apple_epoch_to_date_str(row[1])
            modified = _apple_epoch_to_date_str(row[2])
            result[str(ident)] = (created, modified)
        return result
    finally:
        conn.close()


def _read_frontmatter(
    path: Path,
) -> tuple[str | None, str | None, str | None, str | None]:
    """Return ``(source, record_id, date, modified_date)`` from the file's
    YAML frontmatter. Each is ``None`` when absent or unparseable.
    """
    source = record_id = date_val = modified_val = None
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            first = f.readline()
            if first.strip() != "---":
                return None, None, None, None
            for line in f:
                stripped = line.rstrip()
                if stripped == "---":
                    return source, record_id, date_val, modified_val
                m = _FRONTMATTER_SOURCE_RE.match(stripped)
                if m is not None:
                    source = m.group(1).strip().strip("\"'")
                    continue
                m = _FRONTMATTER_RECORD_ID_RE.match(stripped)
                if m is not None:
                    record_id = m.group(1)
                    continue
                m = _FRONTMATTER_DATE_LINE_RE.match(stripped)
                if m is not None:
                    if m.group(1) == "date":
                        date_val = m.group(2)
                    else:
                        modified_val = m.group(2)
                    continue
    except OSError:
        pass
    return source, record_id, date_val, modified_val


def _rewrite_frontmatter_dates(text: str, new_date: str | None, new_modified: str | None) -> str:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        return text
    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r\n") == "---":
            end_idx = i
            break
    if end_idx is None:
        return text
    for i in range(1, end_idx):
        stripped = lines[i].rstrip("\r\n")
        if re.match(r"^\s*date\s*:", stripped):
            value = new_date if new_date is not None else "unknown"
            lines[i] = f"date: {value}\n"
        elif re.match(r"^\s*modified-date\s*:", stripped):
            value = new_modified if new_modified is not None else "unknown"
            lines[i] = f"modified-date: {value}\n"
    return "".join(lines)


def _new_basename(old_basename: str, new_date_prefix: str) -> str | None:
    """Return new basename with the date prefix swapped, or ``None`` if
    the basename either doesn't have a recognizable prefix or already
    has the correct one."""
    if not old_basename.endswith(".md"):
        return None
    stem = old_basename[: -len(".md")]
    m = _DATE_PREFIX_RE.match(stem)
    if m is None:
        return None
    if m.group(1) == new_date_prefix:
        return None
    return f"{new_date_prefix}{m.group(2)}.md"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument(
        "--snapshot",
        type=Path,
        required=True,
        help="Path to the apple-notes snapshot SQLite.",
    )
    parser.add_argument("--source", default="apple-notes")
    parser.add_argument("--exclude-dir", action="append", default=None)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Commit changes. Default is dry-run.",
    )
    args = parser.parse_args(argv)

    if not args.root.exists():
        print(f"error: root not found: {args.root}", file=sys.stderr)
        return 2
    if not args.snapshot.exists():
        print(f"error: snapshot not found: {args.snapshot}", file=sys.stderr)
        return 2

    exclude_dirs = frozenset(args.exclude_dir or _DEFAULT_EXCLUDE_DIRS)

    print(f"Building date map from {args.snapshot}…", file=sys.stderr)
    date_map = _build_date_map(args.snapshot)
    print(f"  {len(date_map)} identifiers in snapshot", file=sys.stderr)

    print("path\told_date\tnew_date\taction")
    n_changed = 0
    n_no_change = 0
    n_skipped_no_record_id = 0
    n_record_id_not_in_snapshot = 0
    n_collision = 0

    for path in sorted(args.root.rglob("*.md")):
        if any(part in exclude_dirs for part in path.parts):
            continue
        source, record_id, current_date, current_modified = _read_frontmatter(path)
        if source != args.source:
            continue
        if not record_id:
            n_skipped_no_record_id += 1
            continue
        if record_id not in date_map:
            n_record_id_not_in_snapshot += 1
            continue

        new_date, new_modified = date_map[record_id]
        new_date_prefix = new_date or "unknown-date"
        new_basename = _new_basename(path.name, new_date_prefix)

        date_value_for_compare = new_date if new_date is not None else "unknown"
        modified_value_for_compare = new_modified if new_modified is not None else "unknown"
        date_changed = current_date != date_value_for_compare
        modified_changed = current_modified != modified_value_for_compare
        rename_needed = new_basename is not None

        if not (date_changed or modified_changed or rename_needed):
            n_no_change += 1
            continue

        try:
            rel = path.relative_to(args.root)
        except ValueError:
            rel = path

        action = "would-update" if not args.apply else "updated"
        target_path = path.parent / new_basename if new_basename else path

        if args.apply:
            if new_basename and target_path.exists():
                action = "skipped-collision"
                n_collision += 1
            else:
                content = path.read_text(encoding="utf-8", errors="replace")
                new_content = _rewrite_frontmatter_dates(content, new_date, new_modified)
                if new_content != content:
                    path.write_text(new_content, encoding="utf-8")
                if new_basename:
                    path.rename(target_path)
                    bak_path = path.parent / (path.name + ".before-scrub.bak")
                    if bak_path.exists():
                        new_bak = target_path.parent / (target_path.name + ".before-scrub.bak")
                        if not new_bak.exists():
                            bak_path.rename(new_bak)
                n_changed += 1
        else:
            n_changed += 1

        old_date_display = current_date or "(no date)"
        new_date_display = new_date or "unknown"
        print(f"{rel}\t{old_date_display}\t{new_date_display}\t{action}")

    print(f"\nfiles changed: {n_changed}", file=sys.stderr)
    print(f"files already correct: {n_no_change}", file=sys.stderr)
    print(f"files with no record-id in frontmatter: {n_skipped_no_record_id}", file=sys.stderr)
    print(
        f"files whose record-id wasn't in the snapshot: {n_record_id_not_in_snapshot}",
        file=sys.stderr,
    )
    if n_collision:
        print(f"skipped due to target collision: {n_collision}", file=sys.stderr)
    if not args.apply:
        print("(dry-run — re-run with --apply to commit)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
