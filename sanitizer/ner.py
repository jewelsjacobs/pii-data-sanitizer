"""NER layer — Stage 2b of the sanitize pipeline.

Wraps spaCy's English pipeline (``en_core_web_sm``) and surfaces only the
entity labels the sanitizer cares about: ``PERSON``, ``ORG``, and ``GPE``.
Everything else spaCy recognizes (``DATE``, ``TIME``, ``MONEY``, ``ORDINAL``,
etc.) is passed through by the caller per the redaction policy's KEEP list.

spaCy is loaded lazily on first call so importing this module does not pay
the model-load cost during test collection.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from spacy.language import Language

_MODEL_NAME = "en_core_web_lg"
_nlp: Language | None = None

# spaCy emits many entity labels; these are the ones we act on.
_LABELS_OF_INTEREST = frozenset({"PERSON", "ORG", "GPE", "LOC", "FAC"})


@dataclass(frozen=True)
class EntitySpan:
    entity_type: str
    start: int
    end: int
    text: str


def _get_nlp() -> Language:
    global _nlp
    if _nlp is None:
        import spacy

        _nlp = spacy.load(_MODEL_NAME)
    return _nlp


def analyze(text: str) -> list[EntitySpan]:
    """Return non-overlapping spans for PERSON / ORG / GPE / LOC / FAC entities.

    Spans are returned in document order (ascending ``start``). The caller
    is responsible for applying replacements in reverse order so earlier
    offsets stay valid.
    """
    if not text:
        return []
    doc = _get_nlp()(text)
    return [
        EntitySpan(entity_type=ent.label_, start=ent.start_char, end=ent.end_char, text=ent.text)
        for ent in doc.ents
        if ent.label_ in _LABELS_OF_INTEREST
    ]
