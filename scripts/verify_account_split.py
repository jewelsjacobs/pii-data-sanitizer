"""CLI: verify the M7 account-split boundary holds.

Probes the filesystem from whatever account is running this script and
reports whether the access pattern matches the expected profile:

* ``--profile user``  — your macOS user. Should be able to read
  everything in this project, including ``raw-staging/``, ``mapping/``,
  and the source mailboxes.
* ``--profile agent`` — the unprivileged agent account
  (e.g. ``openclaw``). Should be **denied** read on
  ``raw-staging/``, ``mapping/``, ``~/Library/Mail/``,
  ``~/Library/Messages/``, ``.env``, and ``config/local.json``;
  should be able to read ``clean-staging/`` and the vault.

Always read-only. Never modifies the filesystem. Exits 0 if every
probe matched expectations, 1 otherwise.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from scripts._config import LocalConfig, load_config

# /Users/Shared/The Brain Vault path comes from CLAUDE.md (the user's
# vault location). It's hardcoded here intentionally — the verification
# script is a sanity check, not a generic security tool, and the vault
# location is project-policy, not configuration.
_VAULT_PATH = Path("/Users/Shared/The Brain Vault")


@dataclass(frozen=True)
class Probe:
    """One filesystem path to test for read access."""

    path: Path
    label: str
    # Whether the current profile is *expected* to be able to read it.
    expected_readable: bool


@dataclass
class ProbeResult:
    probe: Probe
    actual_readable: bool

    @property
    def passed(self) -> bool:
        return self.actual_readable == self.probe.expected_readable

    @property
    def status(self) -> str:
        if self.passed:
            return "PASS"
        if self.probe.expected_readable:
            return "FAIL (expected readable, got denied)"
        return "FAIL (expected denied, got readable)"


def is_readable(path: Path) -> bool:
    """Return True iff the calling process can read ``path``.

    A directory is "readable" if we can list its contents; a file is
    readable if we can ``open(...).read(1)``. Missing paths count as
    readable=False — same observable effect as a denial.
    """
    try:
        if path.is_dir():
            for _ in path.iterdir():
                break
            return True
        if path.is_file():
            with path.open("rb") as f:
                f.read(1)
            return True
    except (PermissionError, FileNotFoundError, OSError):
        return False
    # Path exists but is neither a regular file nor a directory
    # (symlink to nothing, socket, device). Treat as unreadable so we
    # don't accidentally pass a probe on a broken symlink.
    return False


def build_probes(profile: str, cfg: LocalConfig) -> list[Probe]:
    """Build the probe list for ``profile`` (``"user"`` or ``"agent"``)."""
    home = Path.home()

    sensitive = [
        (cfg.raw_staging_dir, "raw-staging/"),
        (cfg.mapping_path.parent, "mapping/"),
        (cfg.mapping_path, "mapping/mapping.json"),
        (Path("config/local.json"), "config/local.json"),
        (Path(".env"), ".env"),
        (home / "Library" / "Mail", "~/Library/Mail/"),
        (home / "Library" / "Messages", "~/Library/Messages/"),
        (home / "Library" / "Messages" / "chat.db", "~/Library/Messages/chat.db"),
    ]

    accessible = [
        (cfg.clean_staging_dir, "clean-staging/"),
        (_VAULT_PATH, "/Users/Shared/The Brain Vault"),
    ]

    if profile == "user":
        return [Probe(p, label, expected_readable=True) for p, label in sensitive] + [
            Probe(p, label, expected_readable=True) for p, label in accessible
        ]
    if profile == "agent":
        return [Probe(p, label, expected_readable=False) for p, label in sensitive] + [
            Probe(p, label, expected_readable=True) for p, label in accessible
        ]
    raise ValueError(f"unknown profile: {profile!r}")


def run_probes(probes: list[Probe]) -> list[ProbeResult]:
    return [ProbeResult(p, is_readable(p.path)) for p in probes]


def render_report(profile: str, who: str, results: list[ProbeResult]) -> str:
    lines = [
        "Verifying account-split boundary",
        f"  profile: {profile}",
        f"  running as: {who} (uid={os.geteuid()})",
        "",
        f"  {'status':<48}  {'readable?':<10}  path",
        f"  {'-' * 48}  {'-' * 10}  ----",
    ]
    for r in results:
        readable_str = "yes" if r.actual_readable else "no"
        lines.append(f"  {r.status:<48}  {readable_str:<10}  {r.probe.label}")
    lines.append("")
    failed = [r for r in results if not r.passed]
    if failed:
        lines.append(
            f"FAILED: {len(failed)}/{len(results)} probe(s) did not match the {profile} profile."
        )
        lines.append("Do NOT run the agents until every probe passes — see docs/account-split.md.")
    else:
        lines.append(f"OK: all {len(results)} probe(s) match the {profile} profile.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile",
        choices=("user", "agent"),
        required=True,
        help="Which side of the boundary to check.",
    )
    parser.add_argument(
        "--config", type=Path, default=Path("config/local.json"), help="Local config path."
    )
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    probes = build_probes(args.profile, cfg)
    results = run_probes(probes)

    try:
        who = os.getlogin()
    except OSError:
        who = os.environ.get("USER", "?")

    print(render_report(args.profile, who, results))
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
