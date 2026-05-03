"""Deterministic regex layer — Stage 2a of the sanitize pipeline.

High-confidence structured patterns: credentials, cards, SSNs, IPs, conference
URLs with auth tokens. Every match becomes an opaque REDACT token — never a
pseudonym. Pseudonymization (stable aliases for people/emails/phones/orgs)
happens later in the pipeline via the NER layer + mapping file.

Rule order matters. More specific / multi-line patterns come first.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from re import Pattern


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: Pattern[str]
    replacement: str


# Value terminator used by labeled-secret rules: match non-whitespace that
# does not end with sentence punctuation. Prevents consuming the trailing
# "." in "password: hunter2." so the output reads "password: [REDACTED]."
#
# Intentionally omits `]` and `}` so that a second pass over an already-
# redacted line (e.g. "password: [REDACTED_CREDENTIAL]") treats the whole
# bracketed token as the value — the replacement is identical, so the
# operation is idempotent instead of producing "[REDACTED_CREDENTIAL]]".
_VALUE = r"(?P<value>\S+?)(?=[\s.,;!?)\"']|$)"

RULES: list[Rule] = [
    # --- multi-line / high-distinctiveness first -----------------------------
    Rule(
        name="private_key_block",
        pattern=re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
        ),
        replacement="[REDACTED_PRIVATE_KEY]",
    ),
    Rule(
        name="certificate_block",
        # PEM-encoded X.509 certificates surface in dev exports / .pem files
        # / config dumps. Body is base64 with potential identifying metadata
        # (CN, serial). Strip the whole envelope.
        pattern=re.compile(
            r"-----BEGIN [A-Z ]*CERTIFICATE-----[\s\S]*?-----END [A-Z ]*CERTIFICATE-----"
        ),
        replacement="[REDACTED_CERTIFICATE]",
    ),
    Rule(
        name="jwt",
        pattern=re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
        replacement="[REDACTED_TOKEN]",
    ),
    Rule(
        name="apple_id_license_key",
        # Apple/game-style license keys: prefix block + 4+ hyphen-separated
        # alphanumeric blocks of 4+ chars each, all uppercase + digits.
        # Examples: A3-YZNB6S-R9G3NJ-547SC-9AGZT-3C82N-JQ3N2,
        # A3-JWGPRG-YBAYNC-EHWHW-6V7SR-6ZC7N-D5VPR. Distinct from recovery
        # codes (which are comma-separated) and AWS keys (specific prefix).
        pattern=re.compile(r"\b[A-Z0-9]{2,}(?:-[A-Z0-9]{4,}){3,}\b"),
        replacement="[REDACTED_API_KEY]",
    ),
    # --- Links + URLs -----------------------------------------------------
    # URL handling runs before everything else that could chew on URL
    # contents (labeled_password chasing `?pwd=`, NER mis-tagging
    # `utm_source=outfordelivery` as an ORG, etc.). Every form collapses
    # to a single `[REDACTED_LINK]` token — tracking numbers, session
    # IDs, UTM params, and conference auth tokens all disappear uniformly.
    #
    # Order matters: markdown_link first so `[text](url)` is consumed
    # atomically, then bare_url for the rest.
    Rule(
        name="markdown_link",
        # `!?[...](...)` including the image variant. re.DOTALL lets the
        # URL span html2text's wrapped lines. Lazy `.*?` stops at the
        # first `](` / `)` so adjacent links don't glom together.
        pattern=re.compile(r"!?\[.*?\]\(.*?\)", re.DOTALL),
        replacement="[REDACTED_LINK]",
    ),
    Rule(
        name="bare_url",
        # Any remaining http/https URL. Same non-greedy trailing-punct
        # trick used elsewhere so `visit https://x.com.` keeps the `.`.
        pattern=re.compile(r"https?://\S*?(?=[.,;!?)\]\"']?(?:\s|$))"),
        replacement="[REDACTED_LINK]",
    ),
    # --- known prefix tokens -------------------------------------------------
    Rule(
        name="aws_access_key",
        pattern=re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
        replacement="[REDACTED_API_KEY]",
    ),
    Rule(
        name="github_pat",
        pattern=re.compile(r"\bghp_[A-Za-z0-9]{30,}\b"),
        replacement="[REDACTED_API_KEY]",
    ),
    Rule(
        name="generic_sk_key",
        pattern=re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
        replacement="[REDACTED_API_KEY]",
    ),
    # --- labeled secrets -----------------------------------------------------
    Rule(
        name="labeled_secret",
        pattern=re.compile(
            r"(?im)(?P<label>\b(?:AWS\s+(?:access\s+key|secret(?:\s+access\s+key)?)"
            r"|(?:API|Personal\s+access|Access|OAuth|Bearer|Slack|GitLab|OpenAI|Anthropic)"
            r"\s+token|API\s+key|Token|Secret))"
            r"(?P<sep>\s*[:=]\s*)" + _VALUE
        ),
        replacement=r"\g<label>\g<sep>[REDACTED_API_KEY]",
    ),
    Rule(
        name="recovery_codes",
        pattern=re.compile(
            r"(?im)(?P<label>\b(?:recovery|backup)\s+codes?)"
            r"(?P<sep>\s*[:=]\s*)"
            r"(?P<value>[A-Z0-9][A-Z0-9-]+(?:\s*,\s*[A-Z0-9][A-Z0-9-]+)*)"
        ),
        replacement=r"\g<label>\g<sep>[REDACTED_RECOVERY_CODE]",
    ),
    # 2FA / verification / one-time codes. Must run BEFORE labeled_password
    # so that "one-time passcode" matches the longer label here instead of
    # the bare "passcode" inside labeled_password (which would mis-redact
    # as a generic credential rather than a 2FA code).
    Rule(
        name="labeled_2fa_code",
        pattern=re.compile(
            r"(?im)(?P<label>(?:verification|authentication|2FA|MFA|security|"
            r"login|sign-?in|sign-?up|access)\s+code"
            r"|your\s+(?:verification\s+)?code(?:\s+is)?"
            r"|one-?time\s+(?:passcode|code|password|pin)"
            r"|OTP)"
            r"(?P<sep>\s*[:#=]?\s*[\"']?)"
            r"(?P<value>\d{4,8})\b"
        ),
        replacement=r"\g<label>\g<sep>[REDACTED_2FA]",
    ),
    Rule(
        name="labeled_password",
        # Negative lookahead `(?!\[)` skips values that are already a
        # sanitizer token — otherwise a "one-time passcode: [REDACTED_2FA]"
        # form (matched first by labeled_2fa_code) would be re-redacted
        # here as [REDACTED_CREDENTIAL].
        pattern=re.compile(
            r"(?im)(?P<label>\b(?:password|passphrase|pw|pwd|passcode))"
            r"(?P<sep>\s*[:=]\s*)"
            r"(?!\[)" + _VALUE
        ),
        replacement=r"\g<label>\g<sep>[REDACTED_CREDENTIAL]",
    ),
    # "login is USERNAME / PASSWORD" — common shorthand for shared creds.
    # Terminator deliberately permissive so passwords containing `!`, `?`, or
    # `,` (common special chars) get fully redacted rather than chopped at
    # the first special character.
    Rule(
        name="login_pair",
        pattern=re.compile(
            r"(?im)(?P<prefix>\blogin\s+is\s+)(?P<u>\S+?)(?P<sep>\s*/\s*)(?P<p>\S+?)"
            r"(?=[\s.]|$)"
        ),
        replacement=r"\g<prefix>[REDACTED_CREDENTIAL]\g<sep>[REDACTED_CREDENTIAL]",
    ),
    Rule(
        name="labeled_pin",
        pattern=re.compile(r"(?i)\b(?P<label>PIN)\s+(?P<value>\d{4,})\b"),
        replacement=r"\g<label> [REDACTED_CREDENTIAL]",
    ),
    # --- financial -----------------------------------------------------------
    Rule(
        name="ssn",
        # Allow optional whitespace around the hyphens — OCR'd documents
        # commonly produce "148 - 56 - 1678" instead of "148-56-1678".
        pattern=re.compile(r"(?<!\d)\d{3}\s*-\s*\d{2}\s*-\s*\d{4}(?!\d)"),
        replacement="[REDACTED_SSN]",
    ),
    Rule(
        name="ssn_spaced_digits",
        # OCR can produce "1 4 8 - 5 6 - 1 6 7 8" — every digit separated
        # by 1-3 chars of whitespace, dot, or dash. Exactly 9 digits.
        #
        # The lookbehind ``(?<!\d[\s.\-])`` ensures we don't match the
        # tail of a longer OCR-spaced run: for a 10-digit phone like
        # "5 5 5 - 0 1 2 3 - 4 5 6", positions 2+ would otherwise match a
        # 9-digit subsequence. The trailing ``(?![\s.\-]{1,3}\d)`` covers
        # the symmetric case (digit sequence continues to the right).
        # Result: only complete 9-digit OCR-spaced runs match here;
        # 10-12-digit runs fall through to _PHONE_RE in pipeline.py.
        pattern=re.compile(
            r"(?<!\d[\s.\-])(?<![\d.])"
            r"\d(?:[\s.\-]{1,3}\d){8}"
            r"(?![\s.\-]{1,3}\d)(?![\d.])",
        ),
        replacement="[REDACTED_SSN]",
    ),
    # NOTE: OCR-spaced phones ("5 1 3 - 6 0 8 - 1 4 4 9") are intentionally
    # NOT handled in this rules layer. Phones must be pseudonymized via the
    # mapping (so the same number is [PHONE_1] every time it appears, not
    # [REDACTED_PHONE] uniformly), and pseudonymization happens in
    # pipeline.py's _sub_phones. _PHONE_RE there has been extended to
    # match the spaced-digit variant.
    Rule(
        name="credit_card_masked_last_four",
        # Patterns like "Credit Card ****7255" or "card ending ****1234".
        # Existing labeled_account / labeled_card rules expect a full
        # number; this catches the masked-last-four OCR/print form.
        pattern=re.compile(r"(?im)\b(?:credit\s+card|card|acct|account)\s+\*{2,}\s*\d{4}\b"),
        replacement="[REDACTED_CARD]",
    ),
    Rule(
        name="credit_card_spaced",
        pattern=re.compile(r"\b(?:\d{4}[- ]){3}\d{4}\b"),
        replacement="[REDACTED_CARD]",
    ),
    Rule(
        name="credit_card_contiguous",
        pattern=re.compile(r"\b\d{16}\b"),
        replacement="[REDACTED_CARD]",
    ),
    Rule(
        name="cvv",
        pattern=re.compile(r"(?i)\b(?P<label>cvv|cvc|cvn)\s+(?P<value>\d{3,4})\b"),
        replacement=r"\g<label> [REDACTED_CARD]",
    ),
    Rule(
        name="labeled_routing",
        pattern=re.compile(
            r"(?im)(?P<label>\b(?:routing|aba)(?:\s+(?:number|no|#))?)"
            r"(?P<sep>\s*[:=]\s*)"
            r"(?P<value>\d{9})\b"
        ),
        replacement=r"\g<label>\g<sep>[REDACTED_ROUTING]",
    ),
    Rule(
        name="labeled_account",
        pattern=re.compile(
            r"(?im)(?P<label>\b(?:checking|savings)?\s*account(?:\s+(?:number|no|#))?)"
            r"(?P<sep>\s*[:=]\s*)"
            r"(?P<value>\d{6,})\b"
        ),
        replacement=r"\g<label>\g<sep>[REDACTED_ACCOUNT]",
    ),
    Rule(
        name="labeled_brokerage",
        pattern=re.compile(
            r"(?im)(?P<label>\bbrokerage[^\n:]*)"
            r"(?P<sep>\s*[:=]\s*)"
            r"(?P<value>[A-Z]?\d{6,})\b"
        ),
        replacement=r"\g<label>\g<sep>[REDACTED_BROKERAGE]",
    ),
    # SSN last-4 patterns: "last 4 of SSN: NNNN", "SSN ending in NNNN".
    # Audit caught these slipping through because the bare-SSN rule
    # requires the full XXX-XX-NNNN shape.
    Rule(
        name="ssn_last_four",
        pattern=re.compile(
            r"(?im)(?P<label>(?:last\s+(?:4|four)\s+(?:of|digits?\s+of)?\s*SSN"
            r"|SSN(?:'s)?\s+(?:last\s+(?:4|four)|ending(?:\s+in)?)))"
            r"(?P<sep>\s*[:#=]?\s*)"
            r"(?P<value>\d{4})\b"
        ),
        replacement=r"\g<label>\g<sep>[REDACTED_SSN]",
    ),
    # Account-ending patterns: "account ending in NNNN", "ending in NNNN".
    # Common in bank / credit-card receipts.
    Rule(
        name="account_ending",
        pattern=re.compile(
            r"(?im)(?P<label>(?:account(?:\s+(?:number|#))?\s+)?ending(?:\s+(?:in|with))?)"
            r"(?P<sep>\s*[:#=]?\s*)"
            r"(?P<value>\d{4})\b"
        ),
        replacement=r"\g<label>\g<sep>[REDACTED_ACCOUNT]",
    ),
    # Government / state-program / health-insurance IDs. State client IDs
    # (DSHS, EBT, SNAP) and health IDs (Medicare, Medicaid, member /
    # subscriber numbers) are high-severity per the audit and have no
    # uniform shape — match the labeled form only.
    #
    # Label structure: <agency-or-role-word> (optional second-role-word) +
    # ID/Number/#. Catches "DSHS ID", "Client ID", "DSHS Client ID",
    # "Medicare Member Number", etc.
    #
    # Separator allows an optional parenthetical between the label and
    # the colon — e.g. "DSHS Client ID (if you have one): NNNN" was the
    # exact shape that slipped through the first iteration.
    Rule(
        name="labeled_gov_id",
        pattern=re.compile(
            r"(?im)(?P<label>"
            r"\b(?:DSHS|EBT|SNAP|TANF|Medicaid|Medicare|Insurance|Health|"
            r"Member|Patient|Policy|Case|Client|Subscriber|Plan)"
            r"(?:\s+(?:Client|Case|Member|Patient|Subscriber|Account|Group|Plan|Policy))?"
            r"\s+(?:ID|Number|#|No\.?)"
            r")"
            r"(?P<sep>\s*(?:\([^)]*\))?\s*[:#=]?\s*)"
            r"(?P<value>[A-Z]?\d{6,}|[A-Z0-9-]{8,})\b"
        ),
        replacement=r"\g<label>\g<sep>[REDACTED_GOV_ID]",
    ),
    # Reference / confirmation / order / tracking numbers from automated
    # messages (banking, shipping, healthcare). Same pattern-of-life
    # threat-model as URL tracking numbers — per-event identifiers that
    # correlate to specific transactions when leaked. Matches the labeled
    # form only ("Ref# 12345", "Confirmation Number: ABC123") so generic
    # numbers in prose don't get redacted.
    #
    # Audit shape that prompted this: "Ref# NNNNNN/ NNNNNNN" — the value
    # group allows alphanumerics, dashes, and slash-separated continuations.
    Rule(
        name="labeled_reference",
        # Trailing \b after the label alternation so e.g. "Conf" doesn't
        # match inside "Confirmed". Without it, "Card ****1234 confirmed"
        # would have "conf" matched as a label and "irmed" as the value.
        pattern=re.compile(
            r"(?im)(?P<label>\b(?:Ref(?:erence)?|Conf(?:irmation)?|Order|Tracking|"
            r"Transaction|Trans|Receipt|Auth(?:orization)?|Invoice|Ticket)\b"
            r"\s*#?\s*(?:Number|No\.?|#)?)"
            r"(?P<sep>\s*[:#=]?\s*)"
            r"(?P<value>[A-Z0-9][A-Z0-9-]{4,}(?:\s*/\s*[A-Z0-9][A-Z0-9-]+)?)\b"
        ),
        replacement=r"\g<label>\g<sep>[REDACTED_REF]",
    ),
    # --- URLs + IPs ----------------------------------------------------------
    # --- addresses -----------------------------------------------------------
    # A "home address" label + street pattern → [HOME_ADDRESS] (non-reversible).
    # Runs BEFORE the generic street-address detector so the home case is
    # claimed first and doesn't resolve to a mapped [ADDRESS_N].
    Rule(
        name="home_address",
        pattern=re.compile(
            r"(?im)(?P<prefix>\b(?:my\s+)?home\s+address\s*:?\s*)"
            # Bounded {1,8} on the inner word-and-space repetition so this
            # rule can't ReDoS-explode on a wall of word-shaped text without
            # a street suffix. Real addresses are 2-5 words between the
            # number and the suffix; 8 is safe headroom.
            r"(?P<street>\d+\s+(?:[\w.]+\s+){1,8}"
            r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Drive|Dr|Way|Lane|Ln|"
            r"Terrace|Ter|Parkway|Pkwy|Court|Ct|Place|Pl|Highway|Hwy|HWY)"
            r"(?:[,.]?\s*(?:Suite|Apt|Unit|Ste|#)\s*\w+)?)"
        ),
        replacement=r"\g<prefix>[HOME_ADDRESS]",
    ),
    # ZIP code cleanup. Runs AFTER address detection and is intentionally
    # narrow: only matches a ZIP that immediately follows a comma-state
    # pattern ("Portland, OR 97212") or is preceded by whitespace at end of
    # a line. Avoids accidentally eating 5-digit numbers that aren't ZIPs.
    Rule(
        name="zip_code",
        pattern=re.compile(r"(?<=[A-Z]{2})\s+\d{5}(?:-\d{4})?\b"),
        replacement="",
    ),
    Rule(
        name="ipv4",
        pattern=re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
        replacement="[REDACTED_IP]",
    ),
    # IPv6: full 8-group form OR any form with :: compression.
    Rule(
        name="ipv6",
        pattern=re.compile(
            r"(?i)\b(?:[0-9a-f]{1,4}:){7}[0-9a-f]{1,4}\b"
            r"|\b[0-9a-f]{0,4}(?::[0-9a-f]{0,4}){2,7}\b"
        ),
        replacement="[REDACTED_IP]",
    ),
]


def apply_rules(text: str, rules: list[Rule] | None = None) -> str:
    """Apply every rule in order, returning the redacted text."""
    active = RULES if rules is None else rules
    for rule in active:
        text = rule.pattern.sub(rule.replacement, text)
    return text
