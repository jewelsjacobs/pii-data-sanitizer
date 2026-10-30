"""CLI: scan a directory tree for suspected unredacted credentials.

Walks every ``.md`` file under ``--root`` and flags tokens that look
like they could be unredacted secrets. Output is a TSV with one row
per hit: relative path, line number, masked token, masked context
window. All alphanumerics in the output are replaced with ``X``, so
the file is safe to paste into chat or share for triage without
re-leaking anything.

Heuristics (high recall, moderate precision — every hit needs human
review against the actual file):

* Token length 10-60, no whitespace.
* Contains at least one digit AND at least one letter.
* Mixed case OR contains a non-alphanumeric symbol.
* Shannon entropy >= 3.0 bits / char.
* Not inside ``[SANITIZER_TOKEN_N]`` brackets.
* Not part of an ``http(s)://`` URL.
* Not inside the YAML frontmatter block.

Designed to surface *unlabeled* credentials — the labeled cases are
already covered by ``sanitizer/rules.py``. Tune thresholds via flags
if the output volume is unmanageable.

Example:
    uv run python -m scripts.scan_for_credentials \\
        --root "/Users/Shared/The Brain Vault" \\
        --output /tmp/credential-scan.tsv
"""

from __future__ import annotations

import argparse
import math
import re
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

# Symbols that strongly indicate a password / API key value rather than
# an order number, reference ID, or filename-derived heading. Their
# presence in a token is enough to consider it suspect on its own.
#
# Intentionally excludes ``@`` (email / user@host shapes), ``=`` (code
# assignment, CLI flags), and ``%`` (URL-encoded query strings). Those
# three caused the dominant FP class in the first scrub dry-run. The
# tighter ``--all-suspect`` mode still picks up high-entropy strings
# containing those symbols when the non-symbol heuristic kicks in.
_PASSWORD_SYMBOLS = set("!#$^&*+?<>~|")

_URL_ENCODED_RE = re.compile(r"%[0-9A-Fa-f]{2}")

# Directories that hold project config / agent prompts / git internals,
# not user-authored notes. Skipped entirely.
_DEFAULT_EXCLUDE_DIRS = (".claude", ".obsidian", ".git", ".trash")

_SEPARATOR_RE = re.compile(r"[._\-]")
_WORD_SEGMENT_RE = re.compile(r"^[A-Za-z]{3,}$")
_FRONTMATTER_SOURCE_RE = re.compile(r"^\s*source\s*:\s*([^\s]+)\s*$")


def _shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    counts = Counter(s)
    total = len(s)
    return -sum((c / total) * math.log2(c / total) for c in counts.values())


def _looks_suspect(tok: str, *, min_entropy: float) -> bool:
    if not (10 <= len(tok) <= 60):
        return False
    # Path-shaped tokens (folder/file references) are the dominant FP
    # class in a note vault. Anything containing a forward or back slash
    # is treated as a path, not a credential.
    if "/" in tok or "\\" in tok:
        return False
    if "{{" in tok or "}}" in tok or "[[" in tok or "]]" in tok:
        return False
    # Shell / Terraform variable interpolation, e.g. ``${Environment}``.
    if "${" in tok:
        return False
    # Email / user@host / social-handle shapes: any token containing ``@``
    # is far more likely to be one of those than a credential.
    if "@" in tok:
        return False
    # URL-encoded fragments like ``%2C%23`` show up after a redacted link
    # leaves stranded query-string content behind.
    if _URL_ENCODED_RE.search(tok):
        return False
    has_digit = any(c.isdigit() for c in tok)
    has_letter = any(c.isalpha() for c in tok)
    if not (has_digit and has_letter):
        return False

    # Strong signal: a password-class symbol in the token. These rarely
    # appear in order numbers, filenames, or PDF-extraction artifacts.
    # If present we accept on length + entropy alone.
    has_password_symbol = any(c in _PASSWORD_SYMBOLS for c in tok)
    if has_password_symbol:
        return _shannon_entropy(tok) >= min_entropy

    # No password symbol — apply the stricter "looks like an opaque
    # secret" rules to weed out reference / order / SKU strings and
    # filename-derived headings.
    has_upper = any(c.isupper() for c in tok)
    has_lower = any(c.islower() for c in tok)
    if not (has_upper and has_lower):
        return False
    if _shannon_entropy(tok) < min_entropy:
        return False

    # Word-segment heuristic: split on . _ -. If most segments are
    # plain dictionary-shape words (>=3 letters, no digits), the token
    # is almost certainly a filename or document title, not a secret.
    segments = [s for s in _SEPARATOR_RE.split(tok) if s]
    if len(segments) >= 2:
        word_segments = sum(1 for s in segments if _WORD_SEGMENT_RE.match(s))
        if word_segments / len(segments) >= 0.5:
            return False

    return True


def _mask(s: str) -> str:
    return "".join("X" if c.isalnum() else c for c in s)


def _scan_file(
    path: Path,
    *,
    window: int,
    min_entropy: float,
    require_source: str | None = None,
) -> list[tuple[int, str, str]]:
    findings: list[tuple[int, str, str]] = []
    in_frontmatter = False
    in_code_fence = False
    frontmatter_source: str | None = None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return findings

    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.rstrip()

        if lineno == 1 and stripped == "---":
            in_frontmatter = True
            continue
        if in_frontmatter:
            if stripped == "---":
                in_frontmatter = False
                if require_source is not None and frontmatter_source != require_source:
                    return findings
                continue
            m = _FRONTMATTER_SOURCE_RE.match(stripped)
            if m is not None:
                frontmatter_source = m.group(1).strip().strip("\"'")
            continue

        if require_source is not None and frontmatter_source != require_source:
            # No frontmatter or wrong source — skip body entirely.
            return findings

        # Skip heading lines: in promote output, heading is auto-derived
        # from the source's title/filename and is a major false-positive
        # source (PDF filenames, invoice numbers, etc.).
        if stripped.lstrip().startswith("#"):
            continue

        if stripped.lstrip().startswith("```"):
            in_code_fence = not in_code_fence
            continue
        if in_code_fence:
            continue

        cleaned = _SANITIZER_TOKEN_RE.sub(" ", stripped)
        cleaned = _URL_RE.sub(" ", cleaned)
        cleaned = _WIKILINK_RE.sub(" ", cleaned)
        cleaned = _TEMPLATE_VAR_RE.sub(" ", cleaned)
        cleaned = _INLINE_CODE_RE.sub(" ", cleaned)

        for m in _TOKEN_RE.finditer(cleaned):
            tok = m.group(0).strip(_TRIM_PUNCT)
            if not _looks_suspect(tok, min_entropy=min_entropy):
                continue
            start = max(0, m.start() - window)
            end = min(len(cleaned), m.end() + window)
            ctx = cleaned[start:end]
            findings.append((lineno, _mask(tok), _mask(ctx)))

    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        required=True,
        help="Directory to scan recursively (.md files only).",
    )
    parser.add_argument(
        "--window",
        type=int,
        default=30,
        help="Characters of masked context to show on each side (default 30).",
    )
    parser.add_argument(
        "--min-entropy",
        type=float,
        default=3.0,
        help="Minimum Shannon entropy bits/char for a token to be flagged (default 3.0).",
    )
    parser.add_argument(
        "--max-per-file",
        type=int,
        default=20,
        help="Cap reported hits per file so one chatty file can't dominate output.",
    )
    parser.add_argument(
        "--exclude-dir",
        action="append",
        default=None,
        help=(
            "Directory name to skip (matched against any path component). "
            "Repeatable. Defaults to .claude / .obsidian / .git / .trash if not set."
        ),
    )
    parser.add_argument(
        "--source",
        default=None,
        help=(
            "Only scan files whose YAML frontmatter has 'source: <value>'. "
            "Useful for narrowing to one adapter (e.g. apple-notes)."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Write TSV results here. Default: stdout.",
    )
    args = parser.parse_args(argv)
    exclude_dirs = frozenset(args.exclude_dir or _DEFAULT_EXCLUDE_DIRS)

    if not args.root.exists():
        print(f"error: root not found: {args.root}", file=sys.stderr)
        return 2

    out = args.output.open("w", encoding="utf-8") if args.output else sys.stdout

    files_scanned = 0
    files_with_hits = 0
    total_hits = 0

    out.write("path\tline\tmasked_token\tmasked_context\n")
    for path in sorted(args.root.rglob("*.md")):
        if any(part in exclude_dirs for part in path.parts):
            continue
        files_scanned += 1
        hits = _scan_file(
            path,
            window=args.window,
            min_entropy=args.min_entropy,
            require_source=args.source,
        )
        if not hits:
            continue
        files_with_hits += 1
        try:
            rel = path.relative_to(args.root)
        except ValueError:
            rel = path
        for lineno, masked_tok, masked_ctx in hits[: args.max_per_file]:
            out.write(f"{rel}\t{lineno}\t{masked_tok}\t{masked_ctx}\n")
            total_hits += 1

    if args.output:
        out.close()

    print(f"files scanned: {files_scanned}", file=sys.stderr)
    print(f"files with suspect tokens: {files_with_hits}", file=sys.stderr)
    print(f"total suspect tokens reported: {total_hits}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
