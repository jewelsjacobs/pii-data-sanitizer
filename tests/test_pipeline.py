"""Integration + consistency tests for sanitizer.pipeline.

The ground-truth fixture suite (tests/test_sanitizer.py) exercises the
end-to-end output shape per record. These tests cover orthogonal
properties that PLAN.md's M2 "Done when" criteria call out explicitly:

* Pseudonym consistency across repeated runs (same mapping → same output).
* First-run default assignment of PERSON_N / EMAIL_N / PHONE_N counters.
* Structural metadata fields pass through untouched.
"""

from __future__ import annotations

from sanitizer.mapping import Mapping
from sanitizer.pipeline import KeepList, SelfIdentity, sanitize


def _record(content: str) -> dict:
    return {
        "source": "local-files",
        "source_id": "sha256:test",
        "timestamp": "2026-01-01T00:00:00Z",
        "content": content,
        "metadata": {"path": "/synthetic/t.md", "format": "md"},
    }


def test_same_input_twice_produces_same_output() -> None:
    """Running sanitize twice on the same record yields identical results."""
    rec = _record("Email Linda at linda@example.invalid, phone +15551234567.")
    first = sanitize(rec)
    second = sanitize(rec)
    assert first == second


def test_shared_mapping_gives_stable_pseudonyms_across_records() -> None:
    """Two records processed with the same Mapping reuse the same aliases."""
    m = Mapping()
    a = sanitize(_record("Email linda@example.invalid."), mapping=m)
    b = sanitize(_record("Also linda@example.invalid should match."), mapping=m)
    assert "[EMAIL_1]" in a["content"]
    assert "[EMAIL_1]" in b["content"]


def test_self_identity_wins_over_pseudonym() -> None:
    """Identifiers on the self list resolve to [MY_*], never [EMAIL_N]."""
    identity = SelfIdentity(
        emails=frozenset({"me@example.invalid"}),
        phones=frozenset({"15551234567"}),
    )
    rec = _record("Reach me at me@example.invalid or +1 555 123 4567.")
    out = sanitize(rec, self_identity=identity)
    assert "[MY_EMAIL]" in out["content"]
    assert "[MY_PHONE]" in out["content"]
    assert "me@example.invalid" not in out["content"]


def test_keep_list_names_bypass_pseudonymization() -> None:
    """An entity on the keep list passes through regardless of NER tag."""
    keep = KeepList(names=frozenset({"Mochi"}))
    rec = _record("Mochi curled up on the sofa next to Linda Hartwell.")
    out = sanitize(rec, keep=keep)
    assert "Mochi" in out["content"]
    # Linda Hartwell is NOT on the keep list — she should still be pseudonymized.
    assert "Linda" not in out["content"].replace("Linda's", "")


def test_structural_metadata_is_preserved() -> None:
    """message_id / rrule / timestamps / booleans pass through untouched."""
    rec = {
        "source": "apple-mail",
        "source_id": "<abc@example.invalid>",
        "timestamp": "2026-01-01T00:00:00Z",
        "content": "Hi",
        "metadata": {
            "message_id": "<abc@example.invalid>",
            "in_reply_to": "<parent@example.invalid>",
            "references": ["<r1@example.invalid>", "<r2@example.invalid>"],
            "has_attachments": False,
            "attachment_count": 0,
        },
    }
    out = sanitize(rec)
    assert out["metadata"]["message_id"] == "<abc@example.invalid>"
    assert out["metadata"]["in_reply_to"] == "<parent@example.invalid>"
    assert out["metadata"]["references"] == [
        "<r1@example.invalid>",
        "<r2@example.invalid>",
    ]
    assert out["metadata"]["has_attachments"] is False
    assert out["metadata"]["attachment_count"] == 0


def test_input_is_not_mutated() -> None:
    """sanitize returns a new record; the caller's dict is untouched."""
    rec = _record("Email linda@example.invalid.")
    snapshot = {
        "content": rec["content"],
        "metadata": dict(rec["metadata"]),
    }
    sanitize(rec)
    assert rec["content"] == snapshot["content"]
    assert rec["metadata"] == snapshot["metadata"]
