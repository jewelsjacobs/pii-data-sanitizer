"""CLI: manually add entities to the pseudonym mapping.

For entities the auditor finds but NER has never caught anywhere — e.g.
a person who only ever appears in short calendar titles. Adding them here
assigns a stable pseudonym; the pipeline's known-surface sweep then
substitutes them everywhere on the next sanitize run.

Intentionally prints only the assigned tokens, never the input surfaces,
so the output is safe to share with an agent that must not see PII.

Example:
    uv run python -m scripts.add_mapping_entity "Ada Lovelace" "Zed"
    uv run python -m scripts.add_mapping_entity --kind ORG "Initech"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sanitizer.mapping import Mapping
from scripts._config import LocalConfig, load_config

_ALLOWED_KINDS = ("PERSON", "ORG", "LOCATION")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("surfaces", nargs="+", help="Entity surface forms to map.")
    parser.add_argument(
        "--kind",
        choices=_ALLOWED_KINDS,
        default="PERSON",
        help="Entity kind (default: PERSON).",
    )
    parser.add_argument(
        "--config", type=Path, default=Path("config/local.json"), help="Local config path."
    )
    args = parser.parse_args(argv)

    cfg: LocalConfig = load_config(args.config)
    mapping_path = cfg.mapping_path
    mapping = Mapping.load(mapping_path)

    added = 0
    already = 0
    for surface in args.surfaces:
        surface = surface.strip()
        if len(surface) < 3:
            print(
                "error: surfaces shorter than 3 chars are too collision-prone "
                "for the known-surface sweep; refusing.",
                file=sys.stderr,
            )
            return 2
        if mapping.contains(args.kind, surface):
            already += 1
            token = mapping.get_or_assign(args.kind, surface)
        else:
            token = mapping.get_or_assign(args.kind, surface)
            added += 1
        print(f"  {args.kind} → {token}")

    if added:
        mapping.save(mapping_path)
    print(f"{added} added, {already} already mapped → {mapping_path}")
    if added:
        print("Re-run sanitize for affected sources so the sweep applies them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
