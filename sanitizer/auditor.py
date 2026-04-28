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


@dataclass
class DropStats:
    """Counters for verifier rejections — surfaced after an audit run so
    the user can see how much the LLM over-generated vs. how much the
    verifier filtered out. A healthy run has most raw findings surviving
    verification; a run with ``hallucinated`` > ``kept`` means the model
    is making things up.
    """

    raw: int = 0
    kept: int = 0
    empty_excerpt: int = 0
    bracketed_token: int = 0
    hallucinated: int = 0
    keep_list: int = 0
    severity_upgraded: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "raw": self.raw,
            "kept": self.kept,
            "empty_excerpt": self.empty_excerpt,
            "bracketed_token": self.bracketed_token,
            "hallucinated": self.hallucinated,
            "keep_list": self.keep_list,
            "severity_upgraded": self.severity_upgraded,
        }


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
        "CRITICAL RULES for your output:",
        "- `excerpt` MUST be the literal PII string you're flagging (e.g. a",
        '  name like "Alex", an email like "alex@x.invalid", a number like',
        '  "555-1234"). Not the surrounding sentence. Just the PII itself.',
        "- `excerpt` MUST be a literal substring of the CONTENT or METADATA",
        "  shown in the user message. Never summarize, paraphrase, or invent.",
        "  A downstream verifier will reject any finding whose excerpt is",
        "  not found verbatim in the source.",
        "- If you would report context or a whole sentence, put only the",
        "  offending substring in `excerpt` and explain in `reasoning`.",
        "",
        "FLAG anything that could identify a specific real person, place, or",
        "account:",
        "- First or last names in plain text (not replaced with [PERSON_N])",
        "- Email addresses in plain text (anything with @ that's not already a token)",
        "- Phone numbers — formatted like (555) 555-1234, 555-555-1234,",
        "  or bare digit runs of 10+ characters",
        "- Street addresses (number + street suffix) in plain text",
        "- Credit card numbers, SSNs, routing numbers, bank account numbers",
        "- Government / health IDs: DSHS or other state client IDs, Medicare",
        "  numbers, Medicaid IDs, driver license numbers, passport numbers,",
        "  health-insurance member IDs, policy numbers, case numbers",
        "- API keys, passwords, OAuth tokens in plain text",
        "- Specific employer / school / healthcare-provider names in personal context",
        "",
        "DO NOT FLAG:",
        "- Any string already enclosed in square brackets — those are sanitizer tokens",
        "- Widely known public entities (Apple, Google, Amazon, the IRS, large banks)",
        "- Generic marketing language, legal boilerplate, or product names",
        '- Dates, times, zip-less city references (e.g. "Seattle, WA")',
        "- Punctuation, HTML artifacts, tracking beacons",
        "- **Any compound name that contains a KEEP-LIST first name**. If",
        "  a first name on the keep-list also appears alongside a surname or",
        "  initial, the full-name form is ALSO kept — treat the surname as",
        "  part of the user's own identity, not a separate leak.",
        "",
        "SEVERITY GUIDE (use consistently):",
        '- "high": credentials and secrets (passwords, API keys, OAuth tokens,',
        "  2FA codes, private keys), financial identifiers (SSN, credit card,",
        "  bank account, routing number), government / health IDs, home street",
        "  address, real-time location.",
        '- "medium": phone numbers, email addresses, full personal names,',
        "  non-home street addresses, medical diagnoses linked to a person.",
        '- "low": first-name-only references, employer / school names without',
        "  personal context, approximate location (city/state only), other",
        "  context-sensitive but non-identifying fragments.",
        "",
        "When unsure about credentials, financial data, or government /",
        'health IDs, always err toward "high" — those are categories the',
        "downstream vault cannot afford to leak.",
        "",
        "Output strictly valid JSON, no prose outside JSON, matching:",
        "",
        '{"findings": [{"severity": "high|medium|low", "category": '
        '"name|email|phone|address|credential|other", "excerpt": "...", '
        '"reasoning": "..."}]}',
        "",
        'If nothing is suspect, return {"findings": []}. Prefer returning an',
        "empty list over inventing a finding.",
    ]
)


# Severity overrides: if the category says credential or financial, force
# severity to "high" regardless of what the model picked. The rubric in the
# prompt asks for this but models don't follow it consistently.
_HIGH_SEVERITY_CATEGORIES = frozenset({"credential"})

# Regex for a bare sanitizer token — used to drop findings that "flag"
# the very token the sanitizer produced. Matches both fully-bracketed
# (`[PERSON_42]`) and the common LLM mistake of stripping the brackets
# but keeping the inner shape (`PERSON_42`). Only the known token
# stems are matched so generic acronyms like `USA` or `API` aren't
# accidentally treated as tokens.
_KNOWN_TOKEN_STEMS = (
    "PERSON",
    "EMAIL",
    "PHONE",
    "ADDRESS",
    "LOCATION",
    "ORG",
    "ENTITY",
    "FAMILY_PARENT",
    "FAMILY_SIBLING",
    "FRIEND",
    "PARTNER",
    "PROVIDER",
    "COLLEAGUE",
    "CLIENT",
    "SCHOOL",
    "EMPLOYER",
    "FREQUENT_LOCATION",
    "HOME_ADDRESS",
    "MY_EMAIL",
    "MY_PHONE",
    "ATTACHMENT_SKIPPED",
    "DATE_REDACTED",
    "IMAGE_REDACTED",
    "MEDICAL_INFO_REDACTED",
)
_BRACKETED_TOKEN_RE = re.compile(
    r"^\s*\[?(?:" + "|".join(_KNOWN_TOKEN_STEMS) + r"|REDACTED_[A-Z_]+)" + r"(?:_\d+)?\]?\s*$"
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

    Returns zero or more ``Finding``s. Every raw LLM finding is passed
    through ``_verify_finding`` to drop hallucinations, keep-list
    matches, and bracketed-token self-flags; severity is forced to
    ``"high"`` for credential / financial / government-ID categories.

    Passing ``client`` is for tests — production callers let the default
    ``ollama.Client()`` get built.
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
    haystack = _searchable_text(record)
    verified: list[Finding] = []
    stats = DropStats()
    for raw in _parse_findings(text, record):
        stats.raw += 1
        finding = _verify_finding(raw, haystack=haystack, config=config, stats=stats)
        if finding is not None:
            stats.kept += 1
            verified.append(finding)
    # Stash stats on the function call for optional inspection (tests /
    # the CLI). Not returned from this function to keep the public
    # signature stable.
    audit_record.last_stats = stats  # type: ignore[attr-defined]
    return verified


def _searchable_text(record: dict[str, Any]) -> str:
    """Concatenate everything the auditor was allowed to see so the
    verifier can confirm excerpts are literal substrings of the source.
    """
    parts: list[str] = [str(record.get("content", "") or "")]
    metadata = record.get("metadata", {}) or {}
    for value in metadata.values():
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    parts.append(item)
    return "\n".join(parts)


def _verify_finding(
    finding: Finding,
    *,
    haystack: str,
    config: AuditConfig,
    stats: DropStats | None = None,
) -> Finding | None:
    """Deterministic post-filter. Returns ``None`` to drop, else a (possibly
    severity-upgraded) ``Finding``.

    Filters in order:
    1. Drop if ``excerpt`` is empty.
    2. Drop if ``excerpt`` is just one of our sanitizer tokens (``[FOO_N]``).
    3. Drop if ``excerpt`` is not a literal substring of ``haystack`` —
       catches hallucinated names and made-up context.
    4. Drop if ``excerpt`` contains (or equals) a keep-list entry
       (word-boundary match, case-insensitive).
    5. Upgrade severity to ``"high"`` for credential / financial categories.
    """
    excerpt = finding.excerpt.strip()
    if not excerpt:
        if stats is not None:
            stats.empty_excerpt += 1
        return None

    if _BRACKETED_TOKEN_RE.match(excerpt):
        if stats is not None:
            stats.bracketed_token += 1
        return None

    if excerpt not in haystack:
        if stats is not None:
            stats.hallucinated += 1
        return None

    # Keep-list checks use word-boundary matching so a keep-listed short
    # name (e.g. "Lea") doesn't accidentally cancel a finding whose
    # excerpt happens to contain the same letters inside another word
    # (e.g. "Please"), and a keep-listed short name doesn't cancel a
    # different name that happens to share a prefix. A compound
    # full-name form like "<first-name> <surname>" still matches on the
    # first token and is correctly dropped.
    for name in config.keep_names:
        if _contains_whole_word(excerpt, name):
            if stats is not None:
                stats.keep_list += 1
            return None
    for gpe in config.keep_gpes:
        if _contains_whole_word(excerpt, gpe):
            if stats is not None:
                stats.keep_list += 1
            return None

    severity = finding.severity
    if finding.category in _HIGH_SEVERITY_CATEGORIES:
        severity = "high"

    if severity == finding.severity:
        return finding
    if stats is not None:
        stats.severity_upgraded += 1
    return Finding(
        source=finding.source,
        source_id=finding.source_id,
        severity=severity,
        category=finding.category,
        excerpt=finding.excerpt,
        reasoning=finding.reasoning,
    )


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


def _contains_whole_word(haystack: str, needle: str) -> bool:
    """True if ``needle`` appears inside ``haystack`` at word boundaries.

    Case-insensitive. ``re.escape`` on ``needle`` so names with
    punctuation don't blow up the regex. Empty needle never matches.
    """
    if not needle.strip():
        return False
    pattern = re.compile(rf"\b{re.escape(needle)}\b", re.IGNORECASE)
    return pattern.search(haystack) is not None


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
    aggregate = DropStats()
    with log_path.open("w") as fout:
        for i, record in enumerate(records, start=1):
            findings = audit_record(record, model_name=chosen_model, config=config, client=client)
            per_record: DropStats = getattr(audit_record, "last_stats", DropStats())
            aggregate.raw += per_record.raw
            aggregate.kept += per_record.kept
            aggregate.empty_excerpt += per_record.empty_excerpt
            aggregate.bracketed_token += per_record.bracketed_token
            aggregate.hallucinated += per_record.hallucinated
            aggregate.keep_list += per_record.keep_list
            aggregate.severity_upgraded += per_record.severity_upgraded
            for finding in findings:
                fout.write(json.dumps(asdict(finding)) + "\n")
                total_findings += 1
            fout.flush()
            if progress_callback is not None:
                progress_callback(i, len(records), total_findings)
    # Expose aggregate stats for the CLI to print (not returned from the
    # function so the public signature stays stable).
    audit_source.last_stats = aggregate  # type: ignore[attr-defined]
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
