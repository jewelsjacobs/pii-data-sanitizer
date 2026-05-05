"""CLI: prune junk directories / files from a Dropbox restore tree.

Walks a target directory and identifies junk dirs (node_modules, .git,
build/dist artifacts, virtualenvs, caches) and junk files (*.lock,
.DS_Store) — content with zero life-history value that bloats subsequent
extract / sanitize / index runs.

Default mode is **dry-run**: counts and reports what would be deleted
without touching anything. Pass ``--apply`` to actually delete.

Reports both directory count and total bytes per pattern so you can
see how much space each pattern reclaims before committing.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

# Directory names that are pure noise — entire subtree gets deleted.
_JUNK_DIRS: tuple[str, ...] = (
    "node_modules",
    ".git",
    "__pycache__",
    ".cache",
    "vendor",
    "dist",
    "build",
    ".next",
    ".venv",
    "venv",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".nuxt",
    ".turbo",
    "bower_components",
    "target",  # rust / java
)

# File patterns that match individual file names.
_JUNK_FILE_GLOBS: tuple[str, ...] = (
    "*.lock",
    ".DS_Store",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "Pipfile.lock",
    "poetry.lock",
    "Cargo.lock",
    "*.pyc",
)


def _dir_size(path: Path) -> int:
    total = 0
    for sub in path.rglob("*"):
        try:
            if sub.is_file():
                total += sub.stat().st_size
        except OSError:
            pass
    return total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target",
        type=Path,
        required=True,
        help="The directory tree to clean up (e.g. ~/Downloads/dropbox-restore).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete. Default is dry-run (counts only, no deletion).",
    )
    args = parser.parse_args(argv)

    target = args.target.expanduser().resolve()
    if not target.exists():
        print(f"error: target does not exist: {target}", file=sys.stderr)
        return 2
    if not target.is_dir():
        print(f"error: target is not a directory: {target}", file=sys.stderr)
        return 2

    mode = "APPLY (deleting)" if args.apply else "DRY-RUN (no changes)"
    print(f"Target: {target}")
    print(f"Mode:   {mode}")
    print()

    grand_dirs = 0
    grand_files = 0
    grand_bytes = 0

    # ----- Directories ------------------------------------------------------
    print(f"{'pattern':<24} {'dirs':>10} {'bytes':>16}  {'sample path'}")
    print("-" * 90)
    for dir_name in _JUNK_DIRS:
        matches: list[Path] = []
        # rglob doesn't descend into deleted-ancestor — but we're not deleting
        # mid-walk in dry-run. For the apply path, we delete after collecting
        # all matches.
        for p in target.rglob(dir_name):
            if p.is_dir() and not p.is_symlink():
                # Skip nested-junk-inside-junk (we'll delete the outer one).
                if any(parent.name in _JUNK_DIRS for parent in p.parents if parent != target):
                    continue
                matches.append(p)
        if not matches:
            continue
        size = sum(_dir_size(m) for m in matches)
        sample = str(matches[0]).replace(str(target), ".")
        if len(sample) > 40:
            sample = sample[:37] + "..."
        print(f"{dir_name:<24} {len(matches):>10} {size:>16,}  {sample}")
        grand_dirs += len(matches)
        grand_bytes += size
        if args.apply:
            for m in matches:
                try:
                    shutil.rmtree(m)
                except OSError as exc:
                    print(f"  warn: could not remove {m}: {exc}", file=sys.stderr)

    # ----- Files ------------------------------------------------------------
    print()
    print(f"{'pattern':<24} {'files':>10} {'bytes':>16}  {'sample path'}")
    print("-" * 90)
    for glob_pat in _JUNK_FILE_GLOBS:
        matches = []
        for p in target.rglob(glob_pat):
            if not p.is_file() or p.is_symlink():
                continue
            # Skip files inside already-pruned junk dirs (avoids double-counting
            # the bytes we already attributed to the parent dir).
            if any(parent.name in _JUNK_DIRS for parent in p.parents if parent != target):
                continue
            matches.append(p)
        if not matches:
            continue
        size = sum(p.stat().st_size for p in matches if p.exists())
        sample = str(matches[0]).replace(str(target), ".")
        if len(sample) > 40:
            sample = sample[:37] + "..."
        print(f"{glob_pat:<24} {len(matches):>10} {size:>16,}  {sample}")
        grand_files += len(matches)
        grand_bytes += size
        if args.apply:
            for m in matches:
                try:
                    m.unlink()
                except OSError as exc:
                    print(f"  warn: could not remove {m}: {exc}", file=sys.stderr)

    print()
    print(f"Total directories: {grand_dirs:,}")
    print(f"Total files:       {grand_files:,}")
    print(f"Total bytes:       {grand_bytes:,} ({grand_bytes / 1024 / 1024 / 1024:.2f} GiB)")

    if not args.apply:
        print()
        print("This was a DRY-RUN. To actually delete, re-run with --apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
