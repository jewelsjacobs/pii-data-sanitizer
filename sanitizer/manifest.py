"""Manifest writer — the gate the CC-agent pipeline reads first.

Shape defined in files/pipeline-architecture.md → Stage 2. Every
sanitize run produces a ``manifest.json`` in ``/clean-staging/<source>/``.
The CC-agent ingest step refuses to read the clean tree unless this file
exists and reports ``status == "ready_for_ingest"``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

SANITIZER_VERSION = "0.3.1"


def compute_file_hash(path: Path | None) -> str | None:
    """Return ``"sha256:<hex>"`` for ``path``, or ``None`` if the file is
    missing / no path given. The manifest caller treats ``None`` as a
    hint that the hash was not available this run (e.g. first run before
    the mapping file exists)."""
    if path is None or not path.exists():
        return None
    return "sha256:" + sha256(path.read_bytes()).hexdigest()


def write_manifest(
    clean_dir: Path,
    *,
    records_in: int,
    records_out: int,
    entities_redacted: dict[str, int],
    new_entities_auto_classified: int,
    mapping_file_path: Path | None = None,
    policy_file_path: Path | None = None,
    auditor_flags: int = 0,
    status: str = "ready_for_ingest",
    sanitizer_version: str = SANITIZER_VERSION,
) -> Path:
    """Write ``manifest.json`` to ``clean_dir`` and return its path."""
    manifest: dict[str, Any] = {
        "run_id": datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%S"),
        "sanitizer_version": sanitizer_version,
        "mapping_file_hash": compute_file_hash(mapping_file_path),
        "policy_hash": compute_file_hash(policy_file_path),
        "records_in": records_in,
        "records_out": records_out,
        "entities_redacted": dict(entities_redacted),
        "new_entities_auto_classified": new_entities_auto_classified,
        "auditor_flags": auditor_flags,
        "status": status,
    }
    clean_dir.mkdir(parents=True, exist_ok=True)
    path = clean_dir / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    return path
