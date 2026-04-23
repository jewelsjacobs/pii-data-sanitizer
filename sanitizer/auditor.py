"""LLM auditor — post-sanitize sweep for missed PII.

Stage 2c of the pipeline: after the deterministic sanitizer has run,
feed each clean record to a local Ollama model and ask it to flag
anything that still looks like a real name, email, phone, address, or
credential. The auditor **never modifies files** — findings go to a
JSONL log for human review, nothing more.

Design constraints (files/pipeline-architecture.md → Principles):

* Local-only. Ollama runs on localhost; no cloud endpoint, no outbound
  network call from this module.
* Model is configurable. Default is ``qwen2.5:14b-instruct`` with a
  ``qwen2.5:7b-instruct`` fallback when the larger one isn't pulled;
  both can be overridden via ``AUDIT_MODEL`` env var or the
  ``model_name`` parameter.
* Keep-list (user's first name, pet names, allow-listed cities) is
  injected at runtime — not baked into the prompt — so the committed
  source stays generic and no real identifiers ship.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_MODEL = "qwen2.5:14b-instruct"
FALLBACK_MODEL = "qwen2.5:7b-instruct"


@dataclass(frozen=True)
class Finding:
    """One piece of PII the LLM thinks the sanitizer missed."""

    source: str
    source_id: str
    severity: str  # "high" | "medium" | "low"
    category: str  # "name" | "email" | "phone" | "address" | "credential" | "other"
    excerpt: str
    reasoning: str


@dataclass(frozen=True)
class AuditConfig:
    """Runtime configuration injected at audit time.

    ``keep_names`` and ``keep_gpes`` are the same lists the sanitizer
    passes to the NER bypass; keeping them out of the source means the
    user's real first/pet names never appear in committed code.
    """

    keep_names: frozenset[str] = field(default_factory=frozenset)
    keep_gpes: frozenset[str] = field(default_factory=frozenset)


class AuditorError(RuntimeError):
    """Raised when Ollama / the model is unavailable or misbehaves."""


# Fields whose values are structural (IDs, booleans, timestamps) rather
# than free text. Skipped when building the auditor prompt — the LLM
# doesn't need to scan them.
_SKIP_METADATA_KEYS = frozenset(
    {
        "message_id",
        "in_reply_to",
        "references",
        "source_id",
        "timestamp",
        "rrule",
        "dtstart",
        "dtend",
        "from_me",
        "service",
        "has_attachments",
        "attachment_count",
        "is_reply",
        "thread_originator_guid",
        "page_count",
        "size_bytes",
        "format",
        "extraction_method",
        "ocr_low_confidence",
        "chat_is_group",
        "was_edited",
        "date_synthesized",
    }
)

SYSTEM_PROMPT = "\n".join(
    [
        "You are a privacy auditor.",
        "",
        "Your job: review records that a sanitizer has already pseudonymized,",
        "and flag any PII that leaked through.",
        "",
        "The sanitizer replaces real identifiers with bracketed tokens like",
        "[PERSON_N], [EMAIL_N], [PHONE_N], [ORG_N], [LOCATION_N],",
        "[ADDRESS_N], [HOME_ADDRESS], [MY_EMAIL], [MY_PHONE], [REDACTED_LINK],",
        "[REDACTED_CREDENTIAL], [REDACTED_API_KEY], [REDACTED_CARD],",
        "[REDACTED_SSN], and similar. Anything in square brackets is already",
        "redacted.",
        "",
        "FLAG anything that could identify a specific real person, place, or",
        "account:",
        "- First or last names in plain text (not replaced with [PERSON_N])",
        "- Email addresses in plain text (anything with @ that's not already a token)",
        "- Phone numbers — formatted like (555) 555-1234, 555-555-1234,",
        "  or bare digit runs of 10+ characters",
        "- Street addresses (number + street suffix) in plain text",
        "- Credit card numbers, SSNs, routing numbers, bank account numbers",
        "- API keys, passwords, OAuth tokens in plain text",
        "- Specific employer / school / healthcare-provider names in personal context",
        "",
        "DO NOT FLAG:",
        "- Any string already enclosed in square brackets — those are sanitizer tokens",
        "- Widely known public entities (Apple, Google, Amazon, the IRS, large banks)",
        "- Generic marketing language, legal boilerplate, or product names",
        '- Dates, times, zip-less city references (e.g. "Seattle, WA")',
        "- Punctuation, HTML artifacts, tracking beacons",
        "",
        "Output strictly valid JSON, no prose outside JSON, matching:",
        "",
        '{"findings": [{"severity": "high|medium|low", "category": '
        '"name|email|phone|address|credential|other", "excerpt": "...", '
        '"reasoning": "..."}]}',
        "",
        'If nothing is suspect, return {"findings": []}.',
    ]
)


def _keep_list_prompt(config: AuditConfig) -> str:
    """Render the per-run keep-list as a prompt block. Returns empty
    string when nothing to add so the system prompt stays minimal."""
    parts: list[str] = []
    if config.keep_names:
        parts.append(
            "ADDITIONAL KEEP-LIST (do not flag these names — they are intentionally kept):"
        )
        for name in sorted(config.keep_names):
            parts.append(f"- {name}")
    if config.keep_gpes:
        parts.append(
            "ADDITIONAL KEEP-LIST (do not flag these places — they are intentionally kept):"
        )
        for gpe in sorted(config.keep_gpes):
            parts.append(f"- {gpe}")
    return "\n".join(parts)


def build_user_prompt(record: dict[str, Any]) -> str:
    """Render the per-record user message. Source-agnostic shape."""
    source = record.get("source", "unknown")
    source_id = record.get("source_id", "unknown")
    content = record.get("content", "") or ""
    metadata = record.get("metadata", {}) or {}

    meta_lines: list[str] = []
    for key, value in metadata.items():
        if key in _SKIP_METADATA_KEYS:
            continue
        if isinstance(value, str) and value:
            meta_lines.append(f"  {key}: {value}")
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str) and item:
                    meta_lines.append(f"  {key}: {item}")

    metadata_block = "\n".join(meta_lines) if meta_lines else "  (none)"

    # Cap the content so we don't blow the model's context budget on a
    # 50kB promotional HTML email. Most real PII lives near the start
    # and end — head+tail with a visible elision in between.
    content_excerpt = _trim_content(content, max_chars=6000)

    return (
        f"SOURCE: {source}\n"
        f"SOURCE_ID: {source_id}\n"
        f"\n"
        f"CONTENT:\n"
        f"---\n"
        f"{content_excerpt}\n"
        f"---\n"
        f"\n"
        f"METADATA (selected string fields):\n"
        f"{metadata_block}\n"
        f"\n"
        f"Return JSON only."
    )


def _trim_content(text: str, *, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    head = text[:half].rstrip()
    tail = text[-half:].lstrip()
    return f"{head}\n\n[... {len(text) - max_chars} chars elided ...]\n\n{tail}"


def audit_record(
    record: dict[str, Any],
    *,
    model_name: str = DEFAULT_MODEL,
    config: AuditConfig | None = None,
    client: Any = None,
) -> list[Finding]:
    """Run one sanitized record through the LLM auditor.

    Returns zero or more ``Finding``s. Passing ``client`` is for tests
    — production callers let the default ``ollama.Client()`` get built.
    """
    if client is None:
        import ollama

        client = ollama.Client()
    config = config if config is not None else AuditConfig()

    system = SYSTEM_PROMPT
    keep = _keep_list_prompt(config)
    if keep:
        system = system + "\n\n" + keep

    try:
        response = client.chat(
            model=model_name,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": build_user_prompt(record)},
            ],
            format="json",
            options={"temperature": 0.0},
        )
    except Exception as exc:
        raise AuditorError(f"Ollama chat failed with model {model_name}: {exc}") from exc

    text = _extract_message_content(response)
    return list(_parse_findings(text, record))


def _extract_message_content(response: Any) -> str:
    """Pull the assistant message text out of an Ollama response object.

    The SDK returns either a mapping or an object with attribute access.
    Handle both so tests can stub with plain dicts."""
    message = (
        response.get("message")
        if isinstance(response, dict)
        else getattr(response, "message", None)
    )
    if message is None:
        return ""
    if isinstance(message, dict):
        return str(message.get("content", ""))
    return str(getattr(message, "content", ""))


def _parse_findings(text: str, record: dict[str, Any]) -> Iterator[Finding]:
    """Parse the model's response into ``Finding`` objects.

    Tolerates fenced code blocks and trailing prose — the strict-JSON
    attempt runs first, then a bracket-search fallback."""
    data: Any = None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = _extract_json_block(text)
    if not isinstance(data, dict):
        return
    findings = data.get("findings", [])
    if not isinstance(findings, list):
        return

    source = str(record.get("source", "unknown"))
    source_id = str(record.get("source_id", "unknown"))
    for item in findings:
        if not isinstance(item, dict):
            continue
        excerpt = str(item.get("excerpt", "")).strip()
        if not excerpt:
            continue
        yield Finding(
            source=source,
            source_id=source_id,
            severity=_normalize_severity(item.get("severity")),
            category=_normalize_category(item.get("category")),
            excerpt=excerpt,
            reasoning=str(item.get("reasoning", "")).strip(),
        )


_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json_block(text: str) -> Any:
    """Fallback: grab the first top-level ``{...}`` block and parse."""
    match = _JSON_OBJECT_RE.search(text)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def _normalize_severity(value: Any) -> str:
    s = str(value or "").lower().strip()
    return s if s in {"high", "medium", "low"} else "low"


def _normalize_category(value: Any) -> str:
    c = str(value or "").lower().strip()
    return c if c in {"name", "email", "phone", "address", "credential", "other"} else "other"


def resolve_model(
    preferred: str = DEFAULT_MODEL,
    fallback: str = FALLBACK_MODEL,
    client: Any = None,
) -> str:
    """Return the first Ollama model that resolves on the local server.

    Preferred → fallback → halt with an install hint. Environment
    variable ``AUDIT_MODEL`` overrides ``preferred`` if set.
    """
    env_override = os.environ.get("AUDIT_MODEL", "").strip()
    if env_override:
        preferred = env_override
        fallback = ""

    if client is None:
        import ollama

        client = ollama.Client()

    candidates = [m for m in (preferred, fallback) if m]
    try:
        available = _list_available_models(client)
    except Exception as exc:
        raise AuditorError(
            f"Could not contact Ollama: {exc}. "
            f"Install via `brew install ollama`, start it, and pull a model with "
            f"`ollama pull {preferred}`."
        ) from exc

    for candidate in candidates:
        if candidate in available:
            return candidate
    raise AuditorError(
        f"None of the requested models are available locally: {candidates}. "
        f"Pull one with `ollama pull {candidates[0]}`."
    )


def _list_available_models(client: Any) -> set[str]:
    """Return the set of model names known to the local Ollama server."""
    response = client.list()
    models: Iterable[Any]
    if isinstance(response, dict):
        models = response.get("models", [])
    else:
        models = getattr(response, "models", [])
    names: set[str] = set()
    for model in models or []:
        name = None
        if isinstance(model, dict):
            name = model.get("name") or model.get("model")
        else:
            name = getattr(model, "name", None) or getattr(model, "model", None)
        if name:
            names.add(str(name))
    return names


def audit_source(
    events_path: Path,
    log_path: Path,
    *,
    model_name: str | None = None,
    config: AuditConfig | None = None,
    sample: int | None = None,
    seed: int | None = None,
    client: Any = None,
    progress_callback: Any = None,
) -> int:
    """Audit every record (or a random ``sample``) in ``events_path``
    and append findings to ``log_path`` as JSONL.

    Returns the total number of findings written. Never modifies the
    input file. Writes one JSON line per finding; records with no
    findings produce no output.
    """
    events_path = Path(events_path)
    if not events_path.exists():
        raise AuditorError(f"No sanitized events file at {events_path}")

    if client is None:
        import ollama

        client = ollama.Client()

    chosen_model = model_name or resolve_model(client=client)
    config = config if config is not None else AuditConfig()

    records = _load_records(events_path)
    if sample is not None and sample > 0 and sample < len(records):
        import random as _random

        rng = _random.Random(seed)
        records = rng.sample(records, sample)

    log_path.parent.mkdir(parents=True, exist_ok=True)
    total_findings = 0
    with log_path.open("w") as fout:
        for i, record in enumerate(records, start=1):
            findings = audit_record(record, model_name=chosen_model, config=config, client=client)
            for finding in findings:
                fout.write(json.dumps(asdict(finding)) + "\n")
                total_findings += 1
            fout.flush()
            if progress_callback is not None:
                progress_callback(i, len(records), total_findings)
    return total_findings


def _load_records(events_path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with events_path.open() as fin:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records
