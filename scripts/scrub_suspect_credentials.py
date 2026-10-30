"""CLI: scrub suspected unredacted credentials from vault notes.

Companion to ``scan_for_credentials.py``. Re-runs the same suspect-token
heuristic over every ``.md`` file under ``--root``, and replaces each
flagged token with ``[REDACTED_CREDENTIAL]``. By default the scope is
**symbol-only** (tokens containing one of ``! @ # $ % ^ & * + = ? < > ~ |``)
because that's the highest-confidence subset; pass ``--all-suspect`` to
also scrub no-symbol tokens that pass the entropy + word-segment rules.

Default mode is dry-run: it prints a TSV preview of what would change
(masked tokens, no real values) but writes nothing. Pass ``--apply`` to
actually mutate files. Each mutated file gets a sibling backup at
``<original>.before-scrub.bak`` (overwriting any earlier backup) so the
original is recoverable if a false positive slips in.

Frontmatter is never modified. Heading lines (``#`` prefixed) are
skipped. Code-fenced blocks are skipped. Same exclusions as the
scanner: ``.claude``, ``.obsidian``, ``.git``, ``.trash`` directories.

Example:
    # Preview
    uv run python -m scripts.scrub_suspect_credentials \\
        --root "/Users/Shared/The Brain Vault" \\
        --source apple-notes

    # Apply
    uv run python -m scripts.scrub_suspect_credentials \\
        --root "/Users/Shared/The Brain Vault" \\
        --source apple-notes \\
        --apply
"""

from __future__ import annotations

import argparse
import math
import re
import shutil
import sys
from collections import Counter
from pathlib import Path

_TOKEN_RE = re.compile(r"\S{10,60}")
_SANITIZER_TOKEN_RE = re.compile(r"\[[A-Z][A-Z0-9_]*(?:_\d+)?\]")
_URL_RE = re.compile(r"https?://\S+")
_WIKILINK_RE = re.compile(r"\[\[[^\]]*\]\]")
_TEMPLATE_VAR_RE = re.compile(r"\{\{[^}]*\}\}")
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_TRIM_PUNCT = ".,;:!?()[]{}\"'`*"

# See scan_for_credentials.py for the rationale on these excludes.
_PASSWORD_SYMBOLS = set("!#$^&*+?<>~|")
_URL_ENCODED_RE = re.compile(r"%[0-9A-Fa-f]{2}")
_DEFAULT_EXCLUDE_DIRS = (".claude", ".obsidian", ".git", ".trash")
_SEPARATOR_RE = re.compile(r"[._\-]")
_WORD_SEGMENT_RE = re.compile(r"^[A-Za-z]{3,}$")
_FRONTMATTER_SOURCE_RE = re.compile(r"^\s*source\s*:\s*([^\s]+)\s*$")

_REPLACEMENT = "[REDACTED_CREDENTIAL]"


def _shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    counts = Counter(s)
    total = len(s)
    return -sum((c / total) * math.log2(c / total) for c in counts.values())


def _looks_suspect(tok: str, *, min_entropy: float, symbol_only: bool) -> bool:
    if not (10 <= len(tok) <= 60):
        return False
    if "/" in tok or "\\" in tok:
        return False
    if "{{" in tok or "}}" in tok or "[[" in tok or "]]" in tok:
        return False
    if "${" in tok:
        return False
    if "@" in tok:
        return False
    if _URL_ENCODED_RE.search(tok):
        return False
    has_digit = any(c.isdigit() for c in tok)
    has_letter = any(c.isalpha() for c in tok)
    if not (has_digit and has_letter):
        return False

    has_password_symbol = any(c in _PASSWORD_SYMBOLS for c in tok)
    if has_password_symbol:
        return _shannon_entropy(tok) >= min_entropy

    if symbol_only:
        return False

    has_upper = any(c.isupper() for c in tok)
    has_lower = any(c.islower() for c in tok)
    if not (has_upper and has_lower):
        return False
    if _shannon_entropy(tok) < min_entropy:
        return False

    segments = [s for s in _SEPARATOR_RE.split(tok) if s]
    if len(segments) >= 2:
        word_segments = sum(1 for s in segments if _WORD_SEGMENT_RE.match(s))
        if word_segments / len(segments) >= 0.5:
            return False

    return True


def _mask(s: str) -> str:
    return "".join("X" if c.isalnum() else c for c in s)


def _process_line(
    line: str,
    *,
    min_entropy: float,
    symbol_only: bool,
) -> tuple[str, list[str]]:
    """Return (possibly-rewritten-line, list-of-masked-tokens-replaced).

    Strategy: find all candidate tokens against a masked copy of the
    line (where URLs / sanitizer tokens / wikilinks / templates / inline
    code are blanked), then for each match that ``_looks_suspect``, find
    the same span in the ORIGINAL line and replace it with the redaction
    token. We can't just substitute on the cleaned copy because that
    would lose surrounding context (links, code, etc.) on rewrite.
    """
    cleaned = _SANITIZER_TOKEN_RE.sub(lambda m: " " * len(m.group(0)), line)
    cleaned = _URL_RE.sub(lambda m: " " * len(m.group(0)), cleaned)
    cleaned = _WIKILINK_RE.sub(lambda m: " " * len(m.group(0)), cleaned)
    cleaned = _TEMPLATE_VAR_RE.sub(lambda m: " " * len(m.group(0)), cleaned)
    cleaned = _INLINE_CODE_RE.sub(lambda m: " " * len(m.group(0)), cleaned)

    spans: list[tuple[int, int, str]] = []
    for m in _TOKEN_RE.finditer(cleaned):
        raw_span_start, raw_span_end = m.start(), m.end()
        # Re-derive the trimmed span by removing leading/trailing punct.
        raw = m.group(0)
        lstripped = raw.lstrip(_TRIM_PUNCT)
        left_trim = len(raw) - len(lstripped)
        rstripped = lstripped.rstrip(_TRIM_PUNCT)
        right_trim = len(lstripped) - len(rstripped)
        tok = rstripped
        start = raw_span_start + left_trim
        end = raw_span_end - right_trim
        if not tok:
            continue
        if not _looks_suspect(tok, min_entropy=min_entropy, symbol_only=symbol_only):
            continue
        spans.append((start, end, tok))

    if not spans:
        return line, []

    # Rewrite right-to-left so earlier offsets stay valid.
    out = line
    masked_replacements: list[str] = []
    for start, end, tok in reversed(spans):
        out = out[:start] + _REPLACEMENT + out[end:]
        masked_replacements.append(_mask(tok))
    masked_replacements.reverse()
    return out, masked_replacements


def _process_file(
    path: Path,
    *,
    min_entropy: float,
    symbol_only: bool,
    require_source: str | None,
) -> tuple[str | None, list[tuple[int, str]]]:
    """Return (rewritten-text-or-None, [(lineno, masked_token), ...]).

    ``rewritten-text`` is None when no changes are needed. Frontmatter
    and headings and code fences are pass-through.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, []

    lines = text.splitlines(keepends=True)
    in_frontmatter = False
    in_code_fence = False
    frontmatter_source: str | None = None
    frontmatter_done = False

    output_lines: list[str] = []
    replacements: list[tuple[int, str]] = []
    any_changes = False

    for lineno, raw_line in enumerate(lines, start=1):
        # Reuse the same parsing rules as the scanner.
        stripped = raw_line.rstrip("\r\n")

        if not frontmatter_done:
            if lineno == 1 and stripped == "---":
                in_frontmatter = True
                output_lines.append(raw_line)
                continue
            if in_frontmatter:
                if stripped == "---":
                    in_frontmatter = False
                    frontmatter_done = True
                    output_lines.append(raw_line)
                    if require_source is not None and frontmatter_source != require_source:
                        # Wrong source — bail without rewriting.
                        return None, []
                    continue
                m = _FRONTMATTER_SOURCE_RE.match(stripped)
                if m is not None:
                    frontmatter_source = m.group(1).strip().strip("\"'")
                output_lines.append(raw_line)
                continue
            # No frontmatter at all and a source filter is set — skip.
            if require_source is not None:
                return None, []
            frontmatter_done = True

        if stripped.lstrip().startswith("#"):
            output_lines.append(raw_line)
            continue

        if stripped.lstrip().startswith("```"):
            in_code_fence = not in_code_fence
            output_lines.append(raw_line)
            continue
        if in_code_fence:
            output_lines.append(raw_line)
            continue

        new_line, masked_tokens = _process_line(
            raw_line,
            min_entropy=min_entropy,
            symbol_only=symbol_only,
        )
        output_lines.append(new_line)
        if masked_tokens:
            any_changes = True
            for masked in masked_tokens:
                replacements.append((lineno, masked))

    if not any_changes:
        return None, []
    return "".join(output_lines), replacements


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
        help=(
            "Only scrub files whose YAML frontmatter has 'source: <value>'. "
            "Useful for narrowing to one adapter (e.g. apple-notes)."
        ),
    )
    parser.add_argument(
        "--min-entropy",
        type=float,
        default=3.0,
        help="Minimum Shannon entropy bits/char for a token to qualify (default 3.0).",
    )
    parser.add_argument(
        "--exclude-dir",
        action="append",
        default=None,
        help="Directory name to skip (matched against any path component). Repeatable.",
    )
    parser.add_argument(
        "--all-suspect",
        action="store_true",
        help=(
            "Also scrub tokens with NO password-class symbols (relies on "
            "entropy + mixed case + word-segment ratio). Higher recall, "
            "more false positives."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Mutate files in place. Default is dry-run (preview only).",
    )
    args = parser.parse_args(argv)

    if not args.root.exists():
        print(f"error: root not found: {args.root}", file=sys.stderr)
        return 2

    exclude_dirs = frozenset(args.exclude_dir or _DEFAULT_EXCLUDE_DIRS)
    symbol_only = not args.all_suspect

    files_modified = 0
    total_replacements = 0
    print("path\tline\tmasked_token\taction")
    for path in sorted(args.root.rglob("*.md")):
        if any(part in exclude_dirs for part in path.parts):
            continue
        new_text, replacements = _process_file(
            path,
            min_entropy=args.min_entropy,
            symbol_only=symbol_only,
            require_source=args.source,
        )
        if new_text is None:
            continue

        try:
            rel = path.relative_to(args.root)
        except ValueError:
            rel = path

        action = "would-scrub" if not args.apply else "scrubbed"
        for lineno, masked in replacements:
            print(f"{rel}\t{lineno}\t{masked}\t{action}")
            total_replacements += 1

        if args.apply:
            backup = path.with_suffix(path.suffix + ".before-scrub.bak")
            shutil.copy2(path, backup)
            path.write_text(new_text, encoding="utf-8")

        files_modified += 1

    mode = "scrubbed" if args.apply else "would scrub (dry-run)"
    print(f"\nfiles {mode}: {files_modified}", file=sys.stderr)
    print(f"total token replacements: {total_replacements}", file=sys.stderr)
    if not args.apply:
        print("\n(dry-run — re-run with --apply to mutate files)", file=sys.stderr)
    else:
        print(
            "(originals saved as <file>.before-scrub.bak alongside each modified file)",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
