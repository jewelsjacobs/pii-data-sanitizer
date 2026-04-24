"""Unit tests for sanitizer.auditor.

Ollama is never called — we hand ``audit_record`` / ``audit_source`` a
stub client whose ``.chat()`` and ``.list()`` return canned responses.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from sanitizer.auditor import (
    AuditConfig,
    AuditorError,
    _parse_findings,
    audit_record,
    audit_source,
    build_user_prompt,
    resolve_model,
)

# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------


def test_build_user_prompt_includes_content_and_metadata() -> None:
    record = {
        "source": "apple-mail",
        "source_id": "<abc@example.invalid>",
        "content": "Hi Jahna, let's sync tomorrow.",
        "metadata": {
            "folder": "INBOX",
            "subject": "Sync",
            "message_id": "<abc@example.invalid>",  # structural, should be skipped
            "to": ["[PERSON_1] <[EMAIL_1]>"],
        },
    }
    prompt = build_user_prompt(record)
    assert "Hi Jahna" in prompt
    assert "folder: INBOX" in prompt
    assert "subject: Sync" in prompt
    assert "to: [PERSON_1]" in prompt  # list items unpacked
    assert "message_id" not in prompt  # skipped


def test_build_user_prompt_trims_long_content() -> None:
    record = {
        "source": "apple-mail",
        "source_id": "x",
        "content": "A" * 20000,
        "metadata": {},
    }
    prompt = build_user_prompt(record)
    # Trim elides the middle but keeps head + tail.
    assert "elided" in prompt
    assert len(prompt) < 20000


# ---------------------------------------------------------------------------
# Finding parsing
# ---------------------------------------------------------------------------


def _rec() -> dict[str, Any]:
    return {"source": "apple-mail", "source_id": "<abc@example.invalid>"}


def test_parse_findings_strict_json() -> None:
    text = json.dumps(
        {
            "findings": [
                {
                    "severity": "high",
                    "category": "name",
                    "excerpt": "Hi Jahna",
                    "reasoning": "proper name in greeting",
                }
            ]
        }
    )
    findings = list(_parse_findings(text, _rec()))
    assert len(findings) == 1
    assert findings[0].excerpt == "Hi Jahna"
    assert findings[0].category == "name"
    assert findings[0].severity == "high"


def test_parse_findings_tolerates_surrounding_prose() -> None:
    """Some models prepend/suffix prose around the JSON object."""
    text = (
        "Here is what I found:\n"
        '{"findings": [{"severity": "medium", "category": "phone", '
        '"excerpt": "555-1234", "reasoning": "looks like a phone"}]}\n'
        "Done."
    )
    findings = list(_parse_findings(text, _rec()))
    assert len(findings) == 1
    assert findings[0].category == "phone"


def test_parse_findings_empty_list_is_empty() -> None:
    assert list(_parse_findings('{"findings": []}', _rec())) == []


def test_parse_findings_malformed_json_yields_nothing() -> None:
    assert list(_parse_findings("not json at all", _rec())) == []


def test_parse_findings_normalizes_unknown_enum_values() -> None:
    text = json.dumps(
        {"findings": [{"severity": "CRITICAL", "category": "blood type", "excerpt": "x"}]}
    )
    [finding] = list(_parse_findings(text, _rec()))
    assert finding.severity == "low"  # unknown → low
    assert finding.category == "other"  # unknown → other


def test_parse_findings_drops_empty_excerpts() -> None:
    text = json.dumps({"findings": [{"severity": "high", "category": "name", "excerpt": ""}]})
    assert list(_parse_findings(text, _rec())) == []


# ---------------------------------------------------------------------------
# audit_record + keep-list injection
# ---------------------------------------------------------------------------


def _fake_client(response_text: str) -> MagicMock:
    """Client stub whose .chat() returns the given JSON text."""
    client = MagicMock()
    client.chat.return_value = {"message": {"content": response_text}}
    return client


def test_audit_record_returns_parsed_findings() -> None:
    client = _fake_client(
        json.dumps(
            {
                "findings": [
                    {
                        "severity": "high",
                        "category": "name",
                        "excerpt": "Jahna",
                        "reasoning": "unrecognised first name",
                    }
                ]
            }
        )
    )
    record = {
        "source": "apple-mail",
        "source_id": "<abc@x.invalid>",
        "content": "Hi Jahna",  # excerpt is a literal substring → survives verifier
        "metadata": {},
    }
    findings = audit_record(record, client=client, model_name="test-model")
    assert len(findings) == 1
    assert findings[0].source == "apple-mail"
    assert findings[0].source_id == "<abc@x.invalid>"
    assert findings[0].excerpt == "Jahna"
    client.chat.assert_called_once()


# ---------------------------------------------------------------------------
# Verifier (hallucination / keep-list / severity guards)
# ---------------------------------------------------------------------------


def _finding_response(*findings: dict[str, Any]) -> str:
    return json.dumps({"findings": list(findings)})


def test_verifier_drops_hallucinated_excerpt_not_in_source() -> None:
    """LLM invented a name that's not in the record — must be dropped."""
    client = _fake_client(
        _finding_response(
            {
                "severity": "medium",
                "category": "name",
                "excerpt": "Familiar",  # NOT in content
                "reasoning": "looks like a name",
            }
        )
    )
    record = {
        "source": "apple-mail",
        "source_id": "1",
        "content": "I'm a 15 year old kid",
        "metadata": {},
    }
    assert audit_record(record, client=client, model_name="m") == []


def test_verifier_drops_finding_that_equals_a_keep_list_name() -> None:
    """LLM flags 'Lea' in 'Dear Lea'; Lea is on keep-list → drop."""
    client = _fake_client(
        _finding_response(
            {
                "severity": "low",
                "category": "name",
                "excerpt": "Lea",
                "reasoning": "first name in personal context",
            }
        )
    )
    record = {
        "source": "apple-mail",
        "source_id": "1",
        "content": "Dear Lea, hope you're well.",
        "metadata": {},
    }
    config = AuditConfig(keep_names=frozenset({"Lea"}))
    assert audit_record(record, client=client, model_name="m", config=config) == []


def test_verifier_drops_compound_name_containing_keep_list_entry() -> None:
    """'Lea Chen' contains 'Lea' which is on the keep-list → drop."""
    client = _fake_client(
        _finding_response(
            {
                "severity": "medium",
                "category": "name",
                "excerpt": "Lea Chen",
                "reasoning": "full name",
            }
        )
    )
    record = {
        "source": "apple-mail",
        "source_id": "1",
        "content": "Sincerely, Lea Chen",
        "metadata": {},
    }
    config = AuditConfig(keep_names=frozenset({"Lea"}))
    assert audit_record(record, client=client, model_name="m", config=config) == []


def test_verifier_drops_bracketed_token_excerpt() -> None:
    """LLM mis-flags [PERSON_1] as a leak — must be dropped."""
    client = _fake_client(
        _finding_response(
            {
                "severity": "medium",
                "category": "name",
                "excerpt": "[PERSON_1]",
                "reasoning": "a name",
            }
        )
    )
    record = {
        "source": "apple-mail",
        "source_id": "1",
        "content": "Hi [PERSON_1]",
        "metadata": {},
    }
    assert audit_record(record, client=client, model_name="m") == []


def test_verifier_forces_high_severity_for_credential_category() -> None:
    """Model returned credential at 'medium'; verifier must upgrade to 'high'."""
    client = _fake_client(
        _finding_response(
            {
                "severity": "medium",
                "category": "credential",
                "excerpt": "hunter2xyz",
                "reasoning": "looks like a password",
            }
        )
    )
    record = {
        "source": "apple-mail",
        "source_id": "1",
        "content": "password: hunter2xyz",
        "metadata": {},
    }
    [finding] = audit_record(record, client=client, model_name="m")
    assert finding.severity == "high"
    assert finding.category == "credential"


def test_verifier_preserves_legitimate_high_severity_finding() -> None:
    """A well-formed real leak survives every filter intact."""
    client = _fake_client(
        _finding_response(
            {
                "severity": "high",
                "category": "phone",
                "excerpt": "555-123-4567",
                "reasoning": "phone number in plain text",
            }
        )
    )
    record = {
        "source": "apple-mail",
        "source_id": "1",
        "content": "Call me at 555-123-4567",
        "metadata": {},
    }
    [finding] = audit_record(record, client=client, model_name="m")
    assert finding.excerpt == "555-123-4567"
    assert finding.severity == "high"


def test_verifier_keep_list_uses_word_boundaries_not_substring() -> None:
    """Keep-listed 'Lea' must not silently drop a finding whose excerpt
    happens to contain the letters 'lea' inside another word (e.g.
    'Please'). Previously the substring check had this bug."""
    client = _fake_client(
        _finding_response(
            {
                "severity": "medium",
                "category": "name",
                "excerpt": "Please",  # contains 'lea' as a substring, not a word
                "reasoning": "seen as name (hypothetical)",
            }
        )
    )
    record = {
        "source": "apple-mail",
        "source_id": "1",
        "content": "Please review the attached file.",
        "metadata": {},
    }
    config = AuditConfig(keep_names=frozenset({"Lea"}))
    findings = audit_record(record, client=client, model_name="m", config=config)
    assert len(findings) == 1
    assert findings[0].excerpt == "Please"


def test_verifier_keep_list_does_not_drop_different_name_with_shared_prefix() -> None:
    """Keep-listed 'Lea' does not cancel a finding of 'Leander' — they
    are different names, not compound forms of the keep-listed one."""
    client = _fake_client(
        _finding_response(
            {
                "severity": "medium",
                "category": "name",
                "excerpt": "Leander",
                "reasoning": "proper name",
            }
        )
    )
    record = {
        "source": "apple-mail",
        "source_id": "1",
        "content": "Hi Leander, thanks for the heads up.",
        "metadata": {},
    }
    config = AuditConfig(keep_names=frozenset({"Lea"}))
    findings = audit_record(record, client=client, model_name="m", config=config)
    assert len(findings) == 1
    assert findings[0].excerpt == "Leander"


def test_verifier_checks_metadata_not_just_content() -> None:
    """Excerpt found only in a metadata field still counts as present."""
    client = _fake_client(
        _finding_response(
            {
                "severity": "medium",
                "category": "email",
                "excerpt": "boss@example.invalid",
                "reasoning": "email in From header",
            }
        )
    )
    record = {
        "source": "apple-mail",
        "source_id": "1",
        "content": "",
        "metadata": {"from": "Boss <boss@example.invalid>"},
    }
    [finding] = audit_record(record, client=client, model_name="m")
    assert finding.excerpt == "boss@example.invalid"


def test_audit_record_keep_list_is_injected_into_system_prompt() -> None:
    client = _fake_client('{"findings": []}')
    record = {"source": "x", "source_id": "y", "content": "hi", "metadata": {}}
    config = AuditConfig(
        keep_names=frozenset({"Lea", "Mochi"}),
        keep_gpes=frozenset({"Seattle"}),
    )
    audit_record(record, client=client, model_name="m", config=config)
    system_msg = client.chat.call_args.kwargs["messages"][0]
    assert system_msg["role"] == "system"
    assert "Lea" in system_msg["content"]
    assert "Mochi" in system_msg["content"]
    assert "Seattle" in system_msg["content"]


def test_audit_record_no_keep_list_keeps_prompt_generic() -> None:
    """With an empty config the committed SYSTEM_PROMPT is used verbatim."""
    client = _fake_client('{"findings": []}')
    record = {"source": "x", "source_id": "y", "content": "hi", "metadata": {}}
    audit_record(record, client=client, model_name="m", config=AuditConfig())
    system_msg = client.chat.call_args.kwargs["messages"][0]
    # No user-specific names land in the prompt when keep-list is empty.
    assert "KEEP-LIST" not in system_msg["content"] or "ADDITIONAL" not in system_msg["content"]


def test_audit_record_wraps_ollama_errors() -> None:
    client = MagicMock()
    client.chat.side_effect = RuntimeError("connection refused")
    record = {"source": "x", "source_id": "y", "content": "hi", "metadata": {}}
    with pytest.raises(AuditorError, match="Ollama chat failed"):
        audit_record(record, client=client, model_name="m")


# ---------------------------------------------------------------------------
# audit_source end-to-end (filesystem, mocked client)
# ---------------------------------------------------------------------------


def test_audit_source_writes_jsonl_findings(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        json.dumps(
            {"source": "apple-mail", "source_id": "1", "content": "Hi Jahna", "metadata": {}}
        )
        + "\n"
        + json.dumps(
            {"source": "apple-mail", "source_id": "2", "content": "No leaks", "metadata": {}}
        )
        + "\n"
    )

    def _chat_for(messages: list[dict[str, str]], **kwargs: Any) -> dict[str, Any]:
        user_text = messages[-1]["content"]
        if "Jahna" in user_text:
            return {
                "message": {
                    "content": json.dumps(
                        {
                            "findings": [
                                {
                                    "severity": "high",
                                    "category": "name",
                                    "excerpt": "Jahna",
                                    "reasoning": "real first name",
                                }
                            ]
                        }
                    )
                }
            }
        return {"message": {"content": '{"findings": []}'}}

    client = MagicMock()
    client.chat.side_effect = lambda model, messages, **kw: _chat_for(messages, **kw)

    log_path = tmp_path / "logs" / "audit.jsonl"
    total = audit_source(
        events_path,
        log_path,
        model_name="test-model",
        client=client,
    )
    assert total == 1
    lines = log_path.read_text().strip().splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["source_id"] == "1"
    assert parsed["excerpt"] == "Jahna"


def test_audit_source_missing_input_raises(tmp_path: Path) -> None:
    with pytest.raises(AuditorError, match="No sanitized events"):
        audit_source(
            tmp_path / "nope.jsonl",
            tmp_path / "log.jsonl",
            model_name="test-model",
            client=MagicMock(),
        )


def test_audit_source_sample_subset(tmp_path: Path) -> None:
    """sample=N audits a random subset; deterministic via seed."""
    events_path = tmp_path / "events.jsonl"
    with events_path.open("w") as f:
        for i in range(20):
            f.write(
                json.dumps(
                    {
                        "source": "x",
                        "source_id": str(i),
                        "content": "",
                        "metadata": {},
                    }
                )
                + "\n"
            )

    client = MagicMock()
    client.chat.return_value = {"message": {"content": '{"findings": []}'}}

    log_path = tmp_path / "logs" / "audit.jsonl"
    audit_source(
        events_path,
        log_path,
        model_name="test-model",
        client=client,
        sample=5,
        seed=42,
    )
    # 5 records audited, not 20.
    assert client.chat.call_count == 5


# ---------------------------------------------------------------------------
# Model resolution + fallback
# ---------------------------------------------------------------------------


def _list_client(names: list[str]) -> MagicMock:
    client = MagicMock()
    client.list.return_value = {"models": [{"name": n} for n in names]}
    return client


def test_resolve_model_returns_preferred_when_available() -> None:
    client = _list_client(["qwen2.5:14b-instruct", "llama3"])
    assert resolve_model(client=client) == "qwen2.5:14b-instruct"


def test_resolve_model_falls_back_when_preferred_missing() -> None:
    client = _list_client(["qwen2.5:7b-instruct", "llama3"])
    assert resolve_model(client=client) == "qwen2.5:7b-instruct"


def test_resolve_model_raises_when_nothing_available() -> None:
    client = _list_client(["llama3"])
    with pytest.raises(AuditorError, match="None of the requested models"):
        resolve_model(client=client)


def test_resolve_model_env_var_overrides_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUDIT_MODEL", "custom-model-xyz")
    client = _list_client(["custom-model-xyz"])
    assert resolve_model(client=client) == "custom-model-xyz"


def test_resolve_model_unreachable_server_raises_with_install_hint() -> None:
    client = MagicMock()
    client.list.side_effect = ConnectionError("connection refused")
    with pytest.raises(AuditorError, match="brew install ollama"):
        resolve_model(client=client)


# ---------------------------------------------------------------------------
# Finding dataclass round-trip (manifest shape)
# ---------------------------------------------------------------------------


def test_finding_serializes_to_simple_dict() -> None:
    from sanitizer.auditor import Finding

    finding = Finding(
        source="apple-mail",
        source_id="<abc>",
        severity="high",
        category="name",
        excerpt="Jahna",
        reasoning="real first name",
    )
    as_dict = asdict(finding)
    assert set(as_dict.keys()) == {
        "source",
        "source_id",
        "severity",
        "category",
        "excerpt",
        "reasoning",
    }
    assert json.loads(json.dumps(as_dict)) == as_dict
