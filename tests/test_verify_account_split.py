"""Tests for scripts.verify_account_split — probe + report logic.

The actual chmod / ACL behavior must be tested by the user running the
script from each account on their real machine. These tests cover:

* ``is_readable`` returns the right thing for files, directories,
  missing paths, and unreadable paths (best-effort — relies on
  ``chmod 0`` working on the test runner's filesystem).
* ``build_probes`` produces the expected probe list per profile.
* ``ProbeResult.passed`` and ``status`` classify outcomes correctly.
* ``render_report`` produces a human-readable PASS / FAIL summary.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from scripts._config import LocalConfig
from scripts.verify_account_split import (
    Probe,
    ProbeResult,
    _load_config_or_default,
    build_probes,
    is_readable,
    render_report,
)


@pytest.fixture
def fake_cfg(tmp_path: Path) -> LocalConfig:
    return LocalConfig(
        mapping_path=tmp_path / "mapping" / "mapping.json",
        raw_staging_dir=tmp_path / "raw-staging",
        clean_staging_dir=tmp_path / "clean-staging",
        promote_staging_dir=tmp_path / "promote-staging",
    )


# --- is_readable -----------------------------------------------------------


def test_is_readable_true_for_existing_file(tmp_path: Path) -> None:
    p = tmp_path / "a.txt"
    p.write_text("hi")
    assert is_readable(p) is True


def test_is_readable_true_for_existing_dir(tmp_path: Path) -> None:
    assert is_readable(tmp_path) is True


def test_is_readable_false_for_missing_path(tmp_path: Path) -> None:
    assert is_readable(tmp_path / "no-such") is False


def test_is_readable_false_for_unreadable_file(tmp_path: Path) -> None:
    """chmod 000 a file and confirm we report it as unreadable.

    Skipped when running as root (root bypasses POSIX read perms).
    """
    if os.geteuid() == 0:
        pytest.skip("root bypasses POSIX permissions")
    p = tmp_path / "secret.txt"
    p.write_text("don't look")
    p.chmod(0)
    try:
        assert is_readable(p) is False
    finally:
        p.chmod(stat.S_IRUSR | stat.S_IWUSR)


def test_is_readable_false_for_unreadable_dir(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root bypasses POSIX permissions")
    d = tmp_path / "locked"
    d.mkdir()
    (d / "inside").write_text("x")
    d.chmod(0)
    try:
        assert is_readable(d) is False
    finally:
        d.chmod(stat.S_IRWXU)


# --- build_probes ----------------------------------------------------------


def test_user_profile_expects_everything_readable(fake_cfg: LocalConfig) -> None:
    probes = build_probes("user", fake_cfg)
    # Every probe in the user profile expects readable=True.
    assert all(p.expected_readable for p in probes)
    # Sensitive paths AND accessible paths both appear.
    labels = {p.label for p in probes}
    assert "raw-staging/" in labels
    assert "mapping/mapping.json" in labels
    assert "clean-staging/" in labels
    assert "/Users/Shared/The Brain Vault" in labels


def test_agent_profile_expects_sensitive_denied_accessible_readable(fake_cfg: LocalConfig) -> None:
    probes = build_probes("agent", fake_cfg)
    by_label = {p.label: p for p in probes}
    # Sensitive paths must be expected-denied for the agent.
    for label in (
        "raw-staging/",
        "mapping/",
        "mapping/mapping.json",
        "config/local.json",
        ".env",
        "~/Library/Mail/",
        "~/Library/Messages/",
        "~/Library/Messages/chat.db",
    ):
        assert by_label[label].expected_readable is False, label
    # Accessible paths must be expected-readable.
    assert by_label["clean-staging/"].expected_readable is True
    assert by_label["/Users/Shared/The Brain Vault"].expected_readable is True


def test_unknown_profile_raises(fake_cfg: LocalConfig) -> None:
    with pytest.raises(ValueError):
        build_probes("admin", fake_cfg)


# --- ProbeResult ----------------------------------------------------------


def test_probe_result_passed_when_actual_matches_expected() -> None:
    probe = Probe(Path("/tmp"), "label", expected_readable=True)
    assert ProbeResult(probe, actual_readable=True).passed is True
    assert ProbeResult(probe, actual_readable=False).passed is False


def test_probe_result_status_fail_messages_distinguish_direction() -> None:
    expect_readable = Probe(Path("/tmp"), "label", expected_readable=True)
    expect_denied = Probe(Path("/tmp"), "label", expected_readable=False)

    r_missing_read = ProbeResult(expect_readable, actual_readable=False)
    assert r_missing_read.status == "FAIL (expected readable, got denied)"

    r_leaky = ProbeResult(expect_denied, actual_readable=True)
    assert r_leaky.status == "FAIL (expected denied, got readable)"

    r_pass_readable = ProbeResult(expect_readable, actual_readable=True)
    assert r_pass_readable.status == "PASS"

    r_pass_denied = ProbeResult(expect_denied, actual_readable=False)
    assert r_pass_denied.status == "PASS"


# --- render_report --------------------------------------------------------


def test_render_report_shows_ok_when_all_pass() -> None:
    probe = Probe(Path("/tmp"), "tmp", expected_readable=True)
    results = [ProbeResult(probe, actual_readable=True)]
    out = render_report("user", "alice", results)
    assert "OK:" in out
    assert "FAILED:" not in out


def test_render_report_shows_failure_count_and_pointer_to_doc() -> None:
    p1 = Probe(Path("/raw"), "raw-staging/", expected_readable=False)
    p2 = Probe(Path("/clean"), "clean-staging/", expected_readable=True)
    results = [
        ProbeResult(p1, actual_readable=True),  # leak
        ProbeResult(p2, actual_readable=True),  # ok
    ]
    out = render_report("agent", "openclaw", results)
    assert "FAILED: 1/2" in out
    assert "docs/account-split.md" in out
    assert "raw-staging/" in out
    assert "FAIL (expected denied, got readable)" in out


def test_render_report_includes_config_note_when_provided() -> None:
    probe = Probe(Path("/clean"), "clean-staging/", expected_readable=True)
    results = [ProbeResult(probe, actual_readable=True)]
    out = render_report(
        "agent",
        "openclaw",
        results,
        config_note="config not readable — boundary working.",
    )
    assert "note: config not readable — boundary working." in out


# --- _load_config_or_default ---------------------------------------------


def test_load_config_or_default_returns_config_when_readable(tmp_path: Path) -> None:
    cfg_path = tmp_path / "local.json"
    cfg_path.write_text('{"raw_staging_dir": "raw-staging"}')
    cfg, note = _load_config_or_default(cfg_path)
    assert note is None
    assert cfg.raw_staging_dir == Path("raw-staging")


def test_load_config_or_default_falls_back_on_permission_error(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root bypasses POSIX permissions")
    cfg_path = tmp_path / "local.json"
    cfg_path.write_text("{}")
    cfg_path.chmod(0)
    try:
        cfg, note = _load_config_or_default(cfg_path)
        assert note is not None
        assert "boundary is working" in note
        # Falls back to LocalConfig defaults.
        assert cfg.raw_staging_dir == Path("raw-staging")
    finally:
        cfg_path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def test_load_config_or_default_falls_back_on_missing_file(tmp_path: Path) -> None:
    cfg, note = _load_config_or_default(tmp_path / "no-such.json")
    assert note is not None
    assert "not found" in note
    assert cfg.raw_staging_dir == Path("raw-staging")
