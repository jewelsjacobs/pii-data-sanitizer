"""CLI: bulk-convert legacy ``.doc`` files to ``.docx`` using macOS textutil.

The local-files adapter doesn't support legacy ``.doc`` (Word 97-2003)
because the existing Python tooling for that format is fragile and
heavy. macOS ships with ``textutil``, which handles ``.doc`` reliably
via the same Cocoa text classes that TextEdit uses.

This script walks every directory listed in ``local_files_source_dirs``
in your config (or one or more ``--source-dir`` overrides) and converts
each ``.doc`` to a sibling ``.docx`` file. Originals are preserved by
default so you can verify the converted output before deleting them
(``--delete-originals`` removes them after a successful conversion).

Default mode is **dry-run**: counts only, no conversions. Pass
``--apply`` to actually run textutil.

Skipped: files where the ``.docx`` sibling already exists (idempotent
re-runs). Reports both the conversion count and the skip count.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from scripts._config import load_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-dir",
        action="append",
        type=Path,
        default=None,
        help="Directory to walk for .doc files. Repeatable. Overrides config.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/local.json"),
        help="Local config path (used when --source-dir isn't given).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually convert. Default is dry-run.",
    )
    parser.add_argument(
        "--delete-originals",
        action="store_true",
        help=(
            "After successful conversion, delete the original .doc file. "
            "Only takes effect with --apply."
        ),
    )
    args = parser.parse_args(argv)

    if shutil.which("textutil") is None:
        print(
            "error: 'textutil' not found on PATH. This script is macOS-only.",
            file=sys.stderr,
        )
        return 2

    if args.source_dir:
        source_dirs = args.source_dir
    else:
        cfg = load_config(args.config)
        source_dirs = list(cfg.local_files_source_dirs)

    if not source_dirs:
        print("error: no source dirs configured. Pass --source-dir or set "
              "local_files_source_dirs in config.", file=sys.stderr)
        return 2

    mode = "APPLY (converting)" if args.apply else "DRY-RUN (no changes)"
    delete = "yes" if (args.apply and args.delete_originals) else "no"
    print(f"Mode:                {mode}")
    print(f"Delete originals:    {delete}")
    print()

    found = 0
    converted = 0
    skipped_existing = 0
    failed: list[Path] = []

    for src in source_dirs:
        src = Path(src).expanduser()
        if not src.exists():
            print(f"  warn: source dir does not exist: {src}", file=sys.stderr)
            continue

        for doc_path in sorted(src.rglob("*.doc")):
            if not doc_path.is_file():
                continue
            # Skip resource forks / hidden synthetic files
            if doc_path.name.startswith("._"):
                continue

            found += 1
            docx_path = doc_path.with_suffix(".docx")

            if docx_path.exists():
                skipped_existing += 1
                continue

            if not args.apply:
                # Dry-run: just count; don't print every path (could be 100s).
                continue

            try:
                # textutil writes the .docx alongside the .doc.
                subprocess.run(
                    ["textutil", "-convert", "docx", str(doc_path)],
                    check=True,
                    capture_output=True,
                )
                converted += 1
                if args.delete_originals:
                    doc_path.unlink()
                if converted % 25 == 0:
                    print(f"  ...converted {converted} files", flush=True)
            except subprocess.CalledProcessError as exc:
                failed.append(doc_path)
                stderr = (exc.stderr or b"").decode("utf-8", errors="replace").strip()
                print(f"  fail: {doc_path}: {stderr or 'textutil error'}",
                      file=sys.stderr)

    print()
    print(f"Found .doc files:       {found:,}")
    print(f"Converted:              {converted:,}")
    print(f"Skipped (.docx exists): {skipped_existing:,}")
    print(f"Failed:                 {len(failed):,}")

    if not args.apply and found > 0:
        print()
        print(f"This was a DRY-RUN. {found - skipped_existing} files would "
              f"be converted (skipping {skipped_existing} already-converted).")
        print("Re-run with --apply to actually convert.")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
