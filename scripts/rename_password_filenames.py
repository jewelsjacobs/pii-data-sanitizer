"""CLI: rename vault notes whose filenames leak credentials.

Companion to ``scrub_suspect_credentials.py``. The scrub pass only
modifies note bodies — if the leaked password also appears in the
filename (Apple Notes routinely uses the first line of a note as its
title, which means a "password manager" entry's title IS the password),
the basename remains a leak even after the body is clean.

For each ``.md`` file under ``--root`` that matches the source filter,
this script:

1. Splits the basename on the standard ``YYYY-MM-DD — Note — `` prefix.
2. Checks whether the remaining stem contains a password-class symbol
   (``! # $ ^ & * + ? < > ~ |``) and is at least 8 chars.
3. If so, replaces the stem with
   ``[REDACTED_PASSWORD]-<8-char hash of post-scrub content>``.
4. Renames the ``.md`` file. Also renames any sibling
   ``.before-scrub.bak`` so the recovery file stops leaking via its
   own filename.

The hash is derived from the file's CURRENT (post-scrub) content,
not the original — so it doesn't reveal the password the filename
used to contain, and it stays stable across repeated runs.

Frontmatter is not modified. ``filed-path:`` refers to the directory,
not the basename, so the rename doesn't break it.

Inbound wikilinks pointing at the old basename WILL break — these
notes are freshly imported with no expected backlinks, so this is
accepted. If broken links become a problem, add a follow-up pass.

Default mode is dry-run. Pass ``--apply`` to commit renames.

Example:
    uv run python -m scripts.rename_password_filenames \\
        --root "/Users/Shared/The Brain Vault" \\
        --source apple-notes
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

_DATE_NOTE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} — Note — )(.+)$")
_PASSWORD_SYMBOLS = set("!#$^&*+?<>~|")
_DEFAULT_EXCLUDE_DIRS = (".claude", ".obsidian", ".git", ".trash")
_FRONTMATTER_SOURCE_RE = re.compile(r"^\s*source\s*:\s*([^\s]+)\s*$")

# Sanitizer tokens like ``[PERSON_42]`` or ``[REDACTED_LINK]`` show up
# inside basenames (the promote pass uses them as-is). Strip them
# before the suspect heuristic looks at the basename — otherwise a
# ``Hi [PERSON_413]!`` title gets a free pass on letter+digit checks
# from the token contents.
_SANITIZER_BASENAME_TOKEN_RE = re.compile(r"\[[A-Z][A-Z0-9_]*(?:_\d+)?\]")
# Patterns that signal "this is a programming / shell / template
# expression, not a password" and should disqualify the basename.
_DISQUALIFYING_RE = re.compile(r"\$\{|\$\(|\[~\]|=")
# Currency: ``$320``. Anchored to start-of-string or whitespace so an
# embedded ``$8`` inside a random string (e.g. ``ab#JQE...$86fcLAf``)
# isn't mistaken for currency.
_CURRENCY_RE = re.compile(r"(?:^|\s)\$\d")


def _read_frontmatter_source(path: Path) -> str | None:
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            first = f.readline()
            if first.strip() != "---":
                return None
            for line in f:
                if line.strip() == "---":
                    return None
                m = _FRONTMATTER_SOURCE_RE.match(line.strip())
                if m is not None:
                    return m.group(1).strip().strip("\"'")
    except OSError:
        return None
    return None


def _has_password_symbol(s: str) -> bool:
    return any(c in _PASSWORD_SYMBOLS for c in s)


def _short_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:8]


def _looks_like_password_basename(suspect_part: str) -> bool:
    """Return True when the post-prefix portion of a basename looks
    opaque enough to be a leaked password rather than a normal title.

    Filters tuned against the empirical FP set seen on the apple-notes
    import: titles that happen to contain a `!` (greetings, headlines),
    a `$` (currency), `${...}` / `$(...)` (templates and shell), `=`
    (equations / assignments), `[~]` (shell home dir), or that are
    prose with multiple words. None of those are passwords; opaque
    random keystrokes are.
    """
    # Drop sanitizer tokens — their contents shouldn't count toward the
    # "looks opaque" judgement.
    text = _SANITIZER_BASENAME_TOKEN_RE.sub("", suspect_part).strip()

    if _DISQUALIFYING_RE.search(text):
        return False
    if _CURRENCY_RE.search(text):
        return False
    if len(text) < 10:
        return False
    # Real passwords have at most one space (some password managers
    # emit ``<short-id> <password>`` titles); prose has many.
    if sum(1 for c in text if c.isspace()) > 1:
        return False
    if not any(c.isdigit() for c in text):
        return False
    if not any(c.isalpha() for c in text):
        return False
    if not _has_password_symbol(text):
        return False
    return True


def _mask_basename(suspect_part: str) -> str:
    """Mask a suspect basename for safe display in dry-run output —
    alphanumerics become ``X``, symbols/spaces survive. Same convention
    as scan_for_credentials.py and scrub_suspect_credentials.py.
    """
    return "".join("X" if c.isalnum() else c for c in suspect_part)


def _new_basename(old_basename: str, post_scrub_content: str) -> str | None:
    if not old_basename.endswith(".md"):
        return None
    stem = old_basename[: -len(".md")]
    m = _DATE_NOTE_PREFIX_RE.match(stem)
    if m is None:
        return None
    prefix, suspect_part = m.group(1), m.group(2)
    if not _looks_like_password_basename(suspect_part):
        return None
    return f"{prefix}[REDACTED_PASSWORD]-{_short_hash(post_scrub_content)}.md"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        required=True,
        help="Directory to scan recursively (.md files only).",
    )
    parser.add_argument(
        "--source",
        default=None,
        help="Only act on files with frontmatter 'source: <value>'.",
    )
    parser.add_argument(
        "--exclude-dir",
        action="append",
        default=None,
        help="Directory name to skip (matched against any path component). Repeatable.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Commit renames. Default is dry-run.",
    )
    args = parser.parse_args(argv)

    if not args.root.exists():
        print(f"error: root not found: {args.root}", file=sys.stderr)
        return 2

    exclude_dirs = frozenset(args.exclude_dir or _DEFAULT_EXCLUDE_DIRS)

    print("masked_old_path\tnew_basename\taction")
    renamed = 0
    skipped_collision = 0
    for path in sorted(args.root.rglob("*.md")):
        if any(part in exclude_dirs for part in path.parts):
            continue
        if args.source is not None:
            src = _read_frontmatter_source(path)
            if src != args.source:
                continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        new_basename = _new_basename(path.name, content)
        if new_basename is None:
            continue

        try:
            rel = path.relative_to(args.root)
        except ValueError:
            rel = path
        new_path = path.parent / new_basename

        action = "would-rename" if not args.apply else "renamed"
        if args.apply and new_path.exists():
            action = "skipped-collision"
            skipped_collision += 1

        # Mask only the suspect portion of the basename; keep dirs +
        # the date-Note prefix legible so the user can tell which file
        # is which without re-leaking the password.
        m = _DATE_NOTE_PREFIX_RE.match(path.stem)
        if m is not None:
            masked_basename = f"{m.group(1)}{_mask_basename(m.group(2))}.md"
        else:
            masked_basename = _mask_basename(path.name)
        masked_rel = (
            str(rel.parent / masked_basename) if rel.parent != Path(".") else masked_basename
        )
        print(f"{masked_rel}\t{new_basename}\t{action}")

        if args.apply and action == "renamed":
            path.rename(new_path)
            bak_path = path.parent / (path.name + ".before-scrub.bak")
            if bak_path.exists():
                new_bak = new_path.parent / (new_path.name + ".before-scrub.bak")
                if not new_bak.exists():
                    bak_path.rename(new_bak)
        if action != "skipped-collision":
            renamed += 1

    mode = "renamed" if args.apply else "would rename (dry-run)"
    print(f"\nfiles {mode}: {renamed}", file=sys.stderr)
    if skipped_collision:
        print(f"skipped due to target collision: {skipped_collision}", file=sys.stderr)
    if not args.apply:
        print("(dry-run — re-run with --apply to commit)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
