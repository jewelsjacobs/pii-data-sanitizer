"""Ground-truth sanitizer tests.

Parametrizes over every `tests/fixtures/ground-truth/<source>/<case>/` directory,
loads its `input.json`, runs it through the sanitizer, and diffs the result
against `expected.json`.

Until M2 (sanitizer core) lands, `sanitizer.pipeline.sanitize` does not exist.
Every parametrized case will fail uniformly with a clear message about the
missing implementation — this is the intended M1 state per PLAN.md.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "ground-truth"


def _discover_fixtures() -> list[tuple[str, Path]]:
    """Walk the fixture tree and collect (id, case_dir) for every valid case.

    A valid case directory contains both `input.json` and `expected.json`.
    """
    cases: list[tuple[str, Path]] = []
    if not FIXTURE_ROOT.is_dir():
        return cases
    for source_dir in sorted(FIXTURE_ROOT.iterdir()):
        if not source_dir.is_dir():
            continue
        for case_dir in sorted(source_dir.iterdir()):
            if not case_dir.is_dir():
                continue
            if not (case_dir / "input.json").is_file():
                continue
            if not (case_dir / "expected.json").is_file():
                continue
            case_id = f"{source_dir.name}/{case_dir.name}"
            cases.append((case_id, case_dir))
    return cases


_FIXTURES = _discover_fixtures()


# Synthetic "self" identifiers used by the ground-truth fixtures.
# These are test-only stand-ins for the real user; fixtures encode them
# as `[MY_EMAIL]` / `[MY_PHONE]` in expected.json.
_TEST_SELF_EMAILS = frozenset({"testuser@example.invalid"})
_TEST_SELF_PHONES = frozenset({"5035550142", "15035550142"})

# Keep-list for NER bypass: a synthetic first name and two synthetic pet
# names (stand-ins for the real user's KEEP allow-list), plus city-level
# geography that should pass through instead of being pseudonymized.
_TEST_KEEP_NAMES = frozenset({"Lea", "Mochi", "Kip"})
_TEST_KEEP_GPES = frozenset({"Seattle", "Portland"})


@pytest.mark.parametrize(
    "case_dir",
    [pytest.param(path, id=case_id) for case_id, path in _FIXTURES],
)
def test_sanitizer_matches_ground_truth(case_dir: Path) -> None:
    """Feed input.json through the sanitizer; diff against expected.json."""
    try:
        from sanitizer.pipeline import KeepList, SelfIdentity, sanitize
    except ImportError as exc:
        pytest.fail(
            f"sanitizer.pipeline.sanitize is not implemented yet "
            f"(expected until M2 — see PLAN.md). Import error: {exc}"
        )

    input_record = json.loads((case_dir / "input.json").read_text())
    expected_record = json.loads((case_dir / "expected.json").read_text())

    self_identity = SelfIdentity(
        emails=_TEST_SELF_EMAILS,
        phones=_TEST_SELF_PHONES,
    )
    keep = KeepList(names=_TEST_KEEP_NAMES, gpes=_TEST_KEEP_GPES)
    actual_record = sanitize(input_record, self_identity=self_identity, keep=keep)

    assert actual_record == expected_record, (
        f"Sanitizer output diverged from {case_dir.name}/expected.json"
    )


def test_fixture_tree_is_populated() -> None:
    """M1 sanity check: the ground-truth tree is not empty.

    Fails fast if someone deletes the local fixtures (they are gitignored, so
    this is easy to miss) — the rest of the parametrized suite would silently
    collect zero cases otherwise.
    """
    assert _FIXTURES, (
        "No ground-truth fixtures found under tests/fixtures/ground-truth/. "
        "The tree is gitignored; rebuild it from the README in that directory."
    )
