"""First-run auto-classification (no human review gate).

Maps the entity types emitted by the NER layer to the pseudonym kinds used
by the mapping file on a fresh run. See files/redaction-policy.md →
First-Run Defaults.
"""

from __future__ import annotations

# Presidio (and our own NER wrapper) emits these entity types. The first-run
# defaults assign each to a pseudonym kind so every detected entity gets a
# stable alias without a blocking human-review step.
NER_TYPE_TO_KIND: dict[str, str] = {
    "PERSON": "PERSON",
    "LOCATION": "LOCATION",
    "ORGANIZATION": "ORG",
    "EMAIL_ADDRESS": "EMAIL",
    "PHONE_NUMBER": "PHONE",
    "US_SSN": "__REDACT_SSN__",  # handled by rules layer, not pseudonymized
    "CREDIT_CARD": "__REDACT_CARD__",
    "IP_ADDRESS": "__REDACT_IP__",
}


def kind_for_ner_type(ner_type: str) -> str | None:
    """Return the pseudonym kind for an NER entity type, or None if unhandled."""
    return NER_TYPE_TO_KIND.get(ner_type)
