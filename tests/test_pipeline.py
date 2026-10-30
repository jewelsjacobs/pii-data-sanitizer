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
from sanitizer.pipeline import KeepList, SelfIdentity, _sub_known_entities, sanitize


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


def test_calendar_name_is_sanitized_not_preserved() -> None:
    """calendar_name gets the full entity catalog, like metadata.folder.

    Calendar.app derives calendar_name from X-WR-CALNAME or the .ics
    filename — which for subscribed Google calendars is an email address.
    Regression: calendar_name used to sit in _STRUCTURAL_KEEP_KEYS and
    leaked raw. Generic names must still pass through unchanged.
    """
    identity = SelfIdentity(emails=frozenset({"me@example.invalid"}))
    rec = {
        "source": "ical",
        "source_id": "cal-name-1",
        "timestamp": "2026-05-01T10:00:00+00:00",
        "content": "Dentist",
        "metadata": {"calendar_name": "me@example.invalid", "rrule": "FREQ=WEEKLY"},
    }
    out = sanitize(rec, self_identity=identity)
    assert out["metadata"]["calendar_name"] == "[MY_EMAIL]"
    # rrule stays structural.
    assert out["metadata"]["rrule"] == "FREQ=WEEKLY"

    generic = dict(rec, source_id="cal-name-2", metadata={"calendar_name": "Work"})
    assert sanitize(generic, self_identity=identity)["metadata"]["calendar_name"] == "Work"


def test_known_person_substituted_without_ner_context() -> None:
    """A person already in the mapping is replaced even in text too short
    or context-free for NER to detect — the calendar-title leak."""
    m = Mapping()
    # Simulate a prior run having mapped this person via NER.
    token = m.get_or_assign("PERSON", "Linda Hartwell")
    rec = _record("Linda Hartwell")  # bare title, no sentence context
    out = sanitize(rec, mapping=m)
    assert out["content"] == token
    assert m.get_or_assign("PERSON", "Linda Hartwell") == token  # stable


def test_known_person_sweep_is_case_sensitive_and_word_bounded() -> None:
    """A mapped name that collides with a common word must not consume it.

    Tests _sub_known_entities directly — the full sanitize() path would let
    NER independently tag lookalike words and muddy the assertion.
    """
    m = Mapping()
    m.get_or_assign("PERSON", "Will")
    out = _sub_known_entities("Coffee with Will — he will bring the Willows book.", m, KeepList())
    assert out == "Coffee with [PERSON_1] — he will bring the Willows book."


def test_known_sweep_never_rewrites_existing_tokens() -> None:
    """A mapped surface that collides with token vocabulary must not
    rewrite the inside of an already-substituted token.

    NER once mapped the literal word "LOCATION" as an ORG; with
    underscore-as-boundary matching, the sweep then corrupted
    "[LOCATION_490]" into "[[ORG_1]_490]" (audit-found). Tokens must pass
    through whole; stoplisted junk surfaces must not be swept in prose
    either.
    """
    m = Mapping()
    m.get_or_assign("ORG", "LOCATION")
    m.get_or_assign("ORG", "Phone")
    text = "Meet at [LOCATION_490] tomorrow. Phone: [PHONE_12]. See [REDACTED_PERSONAL]."
    out = _sub_known_entities(text, m, KeepList())
    assert out == text


def test_known_person_sweep_crosses_underscore_boundaries() -> None:
    """A mapped surname fused to snake_case must still be swept.

    Regex \\b treats "_" as a word character, so the old \\b-bounded sweep
    missed "smith_Hartwell.docx" (empirical leak: a surname inside a
    filename in metadata.path). Underscores must count as boundaries;
    mid-word matches ("Hartwells") must still be refused.
    """
    m = Mapping()
    token = m.get_or_assign("PERSON", "Hartwell")
    out = _sub_known_entities("see smith_Hartwell.docx and Hartwells Ferry", m, KeepList())
    assert out == f"see smith_{token}.docx and Hartwells Ferry"


def test_known_org_substituted_without_ner_context() -> None:
    """A mapped ORG is swept even where NER misses it — keeps content and
    metadata consistent (empirical leak: an event title's '@ <org>' suffix
    caught in metadata.summary but missed in the concatenated content)."""
    m = Mapping()
    token = m.get_or_assign("ORG", "Northstar Investments")
    out = _sub_known_entities("Q3 review @ Northstar Investments", m, KeepList())
    assert out == f"Q3 review @ {token}"


def test_known_sweep_person_wins_over_org_on_same_surface() -> None:
    """A surface mapped under both kinds resolves to the PERSON token."""
    m = Mapping()
    org = m.get_or_assign("ORG", "Hartwell")
    person = m.get_or_assign("PERSON", "Hartwell")
    out = _sub_known_entities("Meet Hartwell at nine", m, KeepList())
    assert person in out
    assert org not in out


def test_known_sweep_respects_gpe_keep_list() -> None:
    """A keep-listed GPE (e.g. the user's home city) is never swept even
    if some run mis-mapped it as an ORG."""
    m = Mapping()
    m.get_or_assign("ORG", "Springfield")
    keep = KeepList(gpes=frozenset({"Springfield"}))
    out = _sub_known_entities("Flight back to Springfield", m, keep)
    assert out == "Flight back to Springfield"


def test_known_person_sweep_respects_keep_list() -> None:
    """A keep-listed name is never substituted even if mapped previously."""
    m = Mapping()
    m.get_or_assign("PERSON", "Mochi")
    keep = KeepList(names=frozenset({"Mochi"}))
    out = sanitize(_record("Walk Mochi at noon"), mapping=m, keep=keep)
    assert "Mochi" in out["content"]


def test_known_person_sweep_prefers_longest_surface() -> None:
    """'Linda Hartwell' must win over a bare 'Linda' mapping entry."""
    m = Mapping()
    first = m.get_or_assign("PERSON", "Linda")
    full = m.get_or_assign("PERSON", "Linda Hartwell")
    out = sanitize(_record("Dinner w/ Linda Hartwell"), mapping=m)
    assert full in out["content"]
    assert first not in out["content"]


def test_phone_with_multi_char_separators() -> None:
    """Audit caught (NNN)-NNN- NNNN format (extra hyphen + space) leaking
    through. The phone regex must allow runs of separators between groups."""
    rec = _record("Call (732)-930- 0987 to confirm your appointment.")
    out = sanitize(rec)
    assert "(732)" not in out["content"]
    assert "0987" not in out["content"]
    assert "[PHONE_" in out["content"]


def test_person_token_trailing_initial_collapsed() -> None:
    """'[PERSON_N] S.' leaks a last-name hint — the initial folds into the
    token (audit-found on a swept first name + surviving initial)."""
    m = Mapping()
    m.get_or_assign("PERSON", "Zed")
    out = sanitize(_record("Massage w/ Zed S. at noon"), mapping=m)
    assert "[PERSON_1]" in out["content"]
    assert " S." not in out["content"]
    # A capitalized word without a period after a token must survive.
    m2 = Mapping()
    m2.get_or_assign("PERSON", "Zed")
    out2 = sanitize(_record("Zed OK'd the plan"), mapping=m2)
    assert "OK'd" in out2["content"]


def test_phone_international_three_group_shape() -> None:
    """3-4-4 spaced grouping (audit-found international shape) is caught."""
    rec = _record("Front desk: 020 7946 0958, ask for reception.")
    out = sanitize(rec)
    assert "7946" not in out["content"]
    assert "[PHONE_" in out["content"]


def test_phone_dotted_then_spaced_format() -> None:
    """'732.930. 0987' (dot + dot-space) — multi-char separators."""
    rec = _record("Reach me at 732.930. 0987 anytime.")
    out = sanitize(rec)
    assert "0987" not in out["content"]
    assert "[PHONE_" in out["content"]


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


# --------------------------------------------------------------------------- #
# Chunked NER for very long content (multi-megabyte PDFs / OCR scans)
# --------------------------------------------------------------------------- #


def test_chunk_text_for_ner_short_input_single_chunk() -> None:
    """Text under the limit returns a single chunk at offset 0."""
    from sanitizer.pipeline import _chunk_text_for_ner

    text = "Hello world.\n\nA short doc."
    chunks = _chunk_text_for_ner(text, 1000)
    assert chunks == [(0, text)]


def test_chunk_text_for_ner_breaks_on_paragraph_boundary() -> None:
    """Splits prefer ``\\n\\n`` at or after the half-chunk mark."""
    from sanitizer.pipeline import _chunk_text_for_ner

    # 200 chars + paragraph break + 200 chars; max=300 forces a split
    para_a = "A" * 200
    para_b = "B" * 200
    text = para_a + "\n\n" + para_b
    chunks = _chunk_text_for_ner(text, 300)
    # First chunk should end at or after the paragraph break
    assert len(chunks) == 2
    assert chunks[0][1].endswith("\n\n")
    assert chunks[0][0] == 0
    assert chunks[1][1] == para_b
    assert chunks[1][0] == len(chunks[0][1])


def test_chunk_text_for_ner_offsets_reconstruct_original() -> None:
    """Concatenating chunks (in order) reproduces the original text."""
    from sanitizer.pipeline import _chunk_text_for_ner

    parts = ["paragraph " + str(i) + "\n\n" for i in range(50)]
    text = "".join(parts)
    chunks = _chunk_text_for_ner(text, 100)
    rebuilt = "".join(chunk for _, chunk in chunks)
    assert rebuilt == text


def test_chunk_text_for_ner_falls_back_to_hard_cutoff() -> None:
    """When a single line is longer than the chunk size, hard-cut it."""
    from sanitizer.pipeline import _chunk_text_for_ner

    text = "X" * 1000  # one giant line, no breaks
    chunks = _chunk_text_for_ner(text, 200)
    rebuilt = "".join(chunk for _, chunk in chunks)
    assert rebuilt == text
    # No chunk exceeds the limit
    assert all(len(c) <= 200 for _, c in chunks)


def test_long_content_still_pseudonymizes_entities_in_each_chunk() -> None:
    """Across-chunk consistency: 'Linda' in chunk 1 and chunk 2 share an alias."""
    paragraph = "Linda met someone else at the cafe.\n\n"
    # Build content larger than the NER chunk size so chunking kicks in.
    content = paragraph * 10000  # ~360k chars, > 200k threshold
    rec = _record(content)
    out = sanitize(rec)
    # Linda should be pseudonymized to a single consistent token
    assert "Linda" not in out["content"]
    assert "[PERSON_1]" in out["content"]
    # And the count of pseudonym occurrences should be substantial — proves
    # the chunked path actually ran NER on the later chunks.
    assert out["content"].count("[PERSON_1]") >= 100


def test_long_content_does_not_hang() -> None:
    """A pathological 1MB input must complete in a sane wall-clock time.

    No assertion on time; pytest's default timeout would have already
    fired if the chunking didn't fix the hang. The test passing at all
    is the assertion.
    """
    rec = _record("Alpha. " * 200_000)  # ~1.4 MB
    sanitize(rec)


# --------------------------------------------------------------------------- #
# scrub_strings (literal-string hard-redaction)
# --------------------------------------------------------------------------- #


def test_scrub_strings_redacts_all_occurrences() -> None:
    """Configured strings get hard-redacted everywhere, regardless of context."""
    identity = SelfIdentity(scrub_strings=frozenset({"janedoe"}))
    rec = _record(
        "/Users/janedoe/Documents/note.md and ~janedoe/.zshrc and JaneDoe again."
    )
    out = sanitize(rec, self_identity=identity)
    assert "janedoe" not in out["content"].lower()
    # All three occurrences (mixed casing) get the same token
    assert out["content"].count("[REDACTED_PERSONAL]") == 3


def test_scrub_strings_case_insensitive() -> None:
    identity = SelfIdentity(scrub_strings=frozenset({"secret"}))
    rec = _record("the secret was Secret which is SECRET.")
    out = sanitize(rec, self_identity=identity)
    assert out["content"].count("[REDACTED_PERSONAL]") == 3


def test_scrub_strings_longest_first_match() -> None:
    """If both 'janedoe' and 'julia' are configured, the longer wins."""
    identity = SelfIdentity(scrub_strings=frozenset({"jane", "janedoe"}))
    rec = _record("janedoe is the username, but jane is the name.")
    out = sanitize(rec, self_identity=identity)
    # "janedoe" should not be split as [REDACTED_PERSONAL]doe
    assert "[REDACTED_PERSONAL]doe" not in out["content"]
    assert out["content"].count("[REDACTED_PERSONAL]") == 2


def test_scrub_strings_runs_before_other_rules() -> None:
    """A string in scrub_strings is redacted before NER could pseudonymize it."""
    identity = SelfIdentity(scrub_strings=frozenset({"Linda"}))
    rec = _record("Linda met John at the cafe.")
    out = sanitize(rec, self_identity=identity)
    # Linda should be [REDACTED_PERSONAL], not [PERSON_N] (from NER).
    assert "Linda" not in out["content"]
    assert "[REDACTED_PERSONAL]" in out["content"]


def test_scrub_strings_empty_set_is_passthrough() -> None:
    """Default empty scrub_strings doesn't change behavior."""
    identity = SelfIdentity()
    rec = _record("nothing to scrub here.")
    out = sanitize(rec, self_identity=identity)
    assert out["content"] == "nothing to scrub here."


def test_scrub_strings_substring_matches_too() -> None:
    """A short scrub string matches inside larger words. Documented trade-off
    to avoid leaking digits-as-substring-of-other-numbers cases."""
    identity = SelfIdentity(scrub_strings=frozenset({"5421"}))
    rec = _record("ZIP 95421 contains my home number 5421.")
    out = sanitize(rec, self_identity=identity)
    # Both "5421" instances (the standalone and the suffix of 95421) get redacted
    assert out["content"].count("[REDACTED_PERSONAL]") == 2
