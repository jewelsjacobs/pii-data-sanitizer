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
import pwd
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


def build_probes(profile: str, cfg: LocalConfig, *, user_home: Path) -> list[Probe]:
    """Build the probe list for ``profile`` (``"user"`` or ``"agent"``).

    ``user_home`` is the **human user's** home directory (the macOS
    account that runs extract / sanitize / audit / promote).
    Library/Mail and Library/Messages live there and must be probed at
    that exact path — using ``Path.home()`` would silently rewrite the
    probe to the *agent's* own mailbox under the agent profile, which
    is irrelevant to the boundary check.
    """
    sensitive = [
        (cfg.raw_staging_dir, "raw-staging/"),
        (cfg.mapping_path.parent, "mapping/"),
        (cfg.mapping_path, "mapping/mapping.json"),
        (Path("config/local.json"), "config/local.json"),
        (Path(".env"), ".env"),
        (user_home / "Library" / "Mail", f"{user_home}/Library/Mail/"),
        (user_home / "Library" / "Messages", f"{user_home}/Library/Messages/"),
        (
            user_home / "Library" / "Messages" / "chat.db",
            f"{user_home}/Library/Messages/chat.db",
        ),
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


def render_report(
    profile: str,
    who: str,
    results: list[ProbeResult],
    *,
    config_note: str | None = None,
) -> str:
    lines = [
        "Verifying account-split boundary",
        f"  profile: {profile}",
        f"  running as: {who} (uid={os.geteuid()})",
    ]
    if config_note:
        lines.append(f"  note: {config_note}")
    lines.extend(
        [
            "",
            f"  {'status':<48}  {'readable?':<10}  path",
            f"  {'-' * 48}  {'-' * 10}  ----",
        ]
    )
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
    parser.add_argument(
        "--user-home",
        type=Path,
        default=None,
        help=(
            "Path to the human user's home directory (the account that runs "
            "extract/sanitize). Required when running the agent profile under "
            "sudo -u openclaw, since Path.home() then refers to the agent. "
            "When omitted, auto-detected from the current working directory if "
            "it lives under /Users/<name>/..., otherwise falls back to "
            "Path.home()."
        ),
    )
    args = parser.parse_args(argv)

    cfg, config_note = _load_config_or_default(args.config)
    user_home = _resolve_user_home(args.user_home)
    probes = build_probes(args.profile, cfg, user_home=user_home)
    results = run_probes(probes)

    print(render_report(args.profile, _whoami(), results, config_note=config_note))
    return 0 if all(r.passed for r in results) else 1


def _resolve_user_home(arg_user_home: Path | None) -> Path:
    """Pick the human user's home directory.

    Priority order:
    1. ``--user-home`` if explicitly passed.
    2. cwd if it sits under ``/Users/<name>/...`` — covers the
       common case of running the script from inside the project tree.
    3. ``Path.home()`` of the caller, which is correct for the user
       profile and wrong for the agent profile (caller flagged via
       the auto-detection path above to dodge that case).
    """
    if arg_user_home is not None:
        return arg_user_home.expanduser().resolve()
    cwd_parts = Path.cwd().resolve().parts
    if len(cwd_parts) >= 3 and cwd_parts[0] == "/" and cwd_parts[1] == "Users":
        return Path("/") / cwd_parts[1] / cwd_parts[2]
    return Path.home()


def _whoami() -> str:
    """Return the *effective* username — what the kernel sees, not what the
    login session looks like.

    ``os.getlogin()`` returns the user that originated the session, so
    under ``sudo -u openclaw -i`` it still says the original login
    user even though everything else runs as openclaw. ``getpwuid`` of
    the effective uid gives the right answer.
    """
    try:
        return pwd.getpwuid(os.geteuid()).pw_name
    except KeyError:
        return os.environ.get("USER", "?")


def _load_config_or_default(path: Path) -> tuple[LocalConfig, str | None]:
    """Load ``path``; on read denial, fall back to LocalConfig defaults.

    The agent profile is *expected* to fail loading ``config/local.json``
    — that's literally one of the things we want denied. Crashing on
    that denial would make the verification script useless from the
    agent side, so we degrade gracefully and probe the default paths
    instead. The return tuple's second element is a one-line note for
    the report so the user sees that the fallback kicked in.
    """
    try:
        return load_config(path), None
    except PermissionError:
        return (
            LocalConfig(),
            f"config not readable ({path}) — using project defaults; "
            "for the agent profile this is expected and the boundary is working.",
        )
    except FileNotFoundError:
        return (
            LocalConfig(),
            f"config not found ({path}) — using project defaults.",
        )
    except SystemExit:
        # load_config exits(2) when the file is missing. Same outcome.
        return (
            LocalConfig(),
            f"config not found ({path}) — using project defaults.",
        )


if __name__ == "__main__":
    sys.exit(main())
