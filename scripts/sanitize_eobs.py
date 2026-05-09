"""CLI: sanitize Explanation-of-Benefits records for SSA disability use.

The standard sanitizer pseudonymizes provider names, doctor names, and
facility addresses (NER ORG/PERSON, ``_sub_addresses`` regex). EOBs
used as proof of medical events for SSA disability forms must keep
that information verbatim — the form requires real provider names and
real dates of service. This script applies a tailored subset of the
redaction layers:

  * Hard-redact user PII via ``scrub_strings`` (name, home address,
    account last-4s) plus the user's phone digits (added to the scrub
    set since ``_sub_phones`` is bypassed below to keep 10-digit NPIs
    intact).
  * Run the regex layer (``apply_rules``) minus ``zip_code``. Catches
    SSN, credit card, credentials, ``labeled_gov_id`` (Member /
    Subscriber / Patient / Group / Plan / Policy), ``labeled_reference``
    (Ref# / Conf# / Auth# / Invoice#), ``labeled_dob`` (added),
    ``labeled_claim`` (added).
  * Redact the user's email; pseudonymize any other email.
  * Skip ``_sub_phones`` (NPIs are 10 digits and would be hit).
  * Skip ``_sub_addresses`` (provider/facility addresses preserved).
  * Skip ``_sub_ner`` (provider/doctor/facility names preserved).

Reads ``raw-staging/eobs/events.jsonl`` (override with ``--input``) and
writes ``clean-staging/eobs/events.jsonl`` plus a manifest.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from sanitizer.manifest import write_manifest
from sanitizer.mapping import Mapping
from sanitizer.pipeline import (  # noqa: PLC2701  -- one-off script reuses internals
    SelfIdentity,
    _scrub_personal_strings,
    _sub_emails,
)
from sanitizer.rules import RULES, Rule, apply_rules
from scripts._config import LocalConfig, load_config

# Standard rules to skip for EOB output:
#   ``zip_code`` strips ZIPs from "WA 98101" patterns; provider /
#   facility addresses including ZIP must be preserved for SSA proof.
_SKIP_RULES = frozenset({"zip_code"})

# EOB-specific labeled redactors. Slot in alongside ``labeled_gov_id``
# and ``labeled_reference`` so all PII shapes from the patient block
# get caught.
_EOB_RULES: list[Rule] = [
    Rule(
        name="labeled_dob",
        pattern=re.compile(
            r"(?im)(?P<label>\b(?:DOB|D\.O\.B\.?|Date\s+of\s+Birth|Birth\s*Date|Birthdate))"
            r"(?P<sep>\s*[:#=]?\s*)"
            r"(?P<value>\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{2}-\d{2}|"
            r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+\d{4})\b"
        ),
        replacement=r"\g<label>\g<sep>[REDACTED_DOB]",
    ),
    Rule(
        name="labeled_claim",
        # ``Claim Number`` / ``Claim ID`` / ``Claim #`` / bare ``Claim``
        # followed by an alphanumeric identifier of 5+ chars. Trailing
        # ``\b`` anchors so ``claim`` inside ``claims processed`` is not
        # matched.
        pattern=re.compile(
            r"(?im)(?P<label>\bClaim(?:\s+(?:Number|No\.?|#|ID|Reference|Ref\.?))?)\b"
            r"(?P<sep>\s*[:#=]?\s*)"
            r"(?P<value>[A-Z0-9][A-Z0-9-]{4,})\b"
        ),
        replacement=r"\g<label>\g<sep>[REDACTED_CLAIM]",
    ),
]


def _build_eob_ruleset() -> list[Rule]:
    base = [r for r in RULES if r.name not in _SKIP_RULES]
    return base + _EOB_RULES


def _sanitize_text_eob(
    text: str,
    eob_rules: list[Rule],
    scrub_strings: frozenset[str],
    identity: SelfIdentity,
    mapping: Mapping,
) -> str:
    if not text:
        return text
    if scrub_strings:
        text = _scrub_personal_strings(text, scrub_strings)
    text = apply_rules(text, eob_rules)
    text = _sub_emails(text, mapping, identity)
    return text


def _sanitize_record(
    record: dict[str, Any],
    *,
    eob_rules: list[Rule],
    scrub_strings: frozenset[str],
    identity: SelfIdentity,
    mapping: Mapping,
) -> dict[str, Any]:
    out = copy.deepcopy(record)
    if isinstance(out.get("content"), str):
        out["content"] = _sanitize_text_eob(
            out["content"], eob_rules, scrub_strings, identity, mapping
        )
    if isinstance(out.get("metadata"), dict):
        new_meta: dict[str, Any] = {}
        for k, v in out["metadata"].items():
            if isinstance(v, str):
                new_meta[k] = _sanitize_text_eob(v, eob_rules, scrub_strings, identity, mapping)
            else:
                new_meta[k] = v
        out["metadata"] = new_meta
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=Path("config/local.json"), help="Local config path."
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=None,
        help="Override raw input path. Default: <raw_staging_dir>/eobs/events.jsonl.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Override clean output dir. Default: <clean_staging_dir>/eobs.",
    )
    args = parser.parse_args(argv)

    cfg: LocalConfig = load_config(args.config)

    raw_file = args.input or (cfg.raw_staging_dir / "eobs" / "events.jsonl")
    if not raw_file.exists():
        print(
            f"error: no raw input at {raw_file}. Run extract first.",
            file=sys.stderr,
        )
        return 2

    # User phone digits get added to the scrub set so they are
    # literally redacted — we skip ``_sub_phones`` to keep 10-digit
    # NPIs intact, so the user's phone needs another redaction path.
    # ``cfg.self_phones`` is already digit-only after _config normalization.
    scrub_set: set[str] = set(cfg.scrub_strings)
    for phone_digits in cfg.self_phones:
        if phone_digits:
            scrub_set.add(phone_digits)
    scrub_strings = frozenset(scrub_set)

    # ``_sub_emails`` consults identity.emails to send the user's email
    # to ``[MY_EMAIL]``. Identity's own scrub_strings is left empty
    # because we apply the merged scrub set ourselves.
    identity = SelfIdentity(
        emails=cfg.self_emails,
        phones=cfg.self_phones,
        scrub_strings=frozenset(),
    )

    mapping = Mapping.load(cfg.mapping_path)

    eob_rules = _build_eob_ruleset()

    out_dir = args.output_dir or (cfg.clean_staging_dir / "eobs")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "events.jsonl"

    records_in = 0
    records_out = 0
    counts: Counter[str] = Counter()
    before_email = len(mapping._by_kind.get("EMAIL", {}))  # noqa: SLF001
    with raw_file.open() as fin, out_file.open("w") as fout:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            records_in += 1
            raw = json.loads(line)
            sanitized = _sanitize_record(
                raw,
                eob_rules=eob_rules,
                scrub_strings=scrub_strings,
                identity=identity,
                mapping=mapping,
            )
            fout.write(json.dumps(sanitized) + "\n")
            records_out += 1
    after_email = len(mapping._by_kind.get("EMAIL", {}))  # noqa: SLF001
    new_email_assignments = after_email - before_email
    if new_email_assignments > 0:
        counts["EMAIL"] = new_email_assignments

    cfg.mapping_path.parent.mkdir(parents=True, exist_ok=True)
    mapping.save(cfg.mapping_path)

    manifest_path = write_manifest(
        out_dir,
        records_in=records_in,
        records_out=records_out,
        entities_redacted=dict(counts),
        new_entities_auto_classified=new_email_assignments,
        mapping_file_path=cfg.mapping_path,
    )

    print(
        f"Sanitized {records_out}/{records_in} EOB record(s) → {out_file}\n"
        f"Manifest: {manifest_path}\n"
        f"New EMAIL pseudonyms: {new_email_assignments}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
