# Ground-truth fixtures

Hand-written synthetic snippets that encode the expected behavior of the sanitizer.
No real PII. Every name, email, phone number, and address here is made up.

## Hard rules for fixture content

- **No real filesystem information.** Any `metadata.path` (or any other
  filesystem-shaped field) must be a fully-synthetic absolute path under
  `/synthetic/<source>/<case>.<ext>`. No real home directories, macOS
  usernames, or real directory structures — even though the tree is
  gitignored, we keep the habit to avoid leaking via logs, error messages,
  or accidental sharing.
- **No real identifiers.** Fictional names, `example.invalid` domains,
  reserved phone ranges (`555-0100`–`555-0199`), documented test
  credentials (`AKIAIOSFODNN7EXAMPLE`, `4111 1111 1111 1111`). Anything
  that could be mistaken for a real account, person, or address is
  replaced before the fixture lands on disk.

## Layout

```
tests/fixtures/ground-truth/
├── local-files/<case>/{input.json, expected.json}
├── apple-mail/<case>/{input.json, expected.json}
├── imessage/<case>/{input.json, expected.json}
└── ical/<case>/{input.json, expected.json}
```

## Fixture shape

`input.json` and `expected.json` both hold a **normalized record** matching the
Stage 1 extract contract (the project's pipeline architecture doc, kept local).
The sanitizer (M2+) reads `input.json`, runs its layered redaction, and should
produce exactly `expected.json`.

```json
{
  "source": "<source-name>",
  "source_id": "<stable id>",
  "timestamp": "<ISO 8601>",
  "content": "<text>",
  "metadata": { ... }
}
```

## Pseudonym conventions used here

Each fixture is evaluated against a **fresh, empty mapping file**. That means:

- `N` counters start at 1 inside each fixture.
- Entities are numbered in order of first appearance, scanning `content` first,
  then metadata fields in the order they appear in the JSON.
- The same underlying entity always resolves to the same pseudonym within a
  fixture (this is the property M2 must verify).

Tokens used (from the project's redaction policy → First-Run Defaults):

| Kind                | Pseudonym            |
|---------------------|----------------------|
| Person              | `[PERSON_N]`         |
| Email (not mine)    | `[EMAIL_N]`          |
| Phone (not mine)    | `[PHONE_N]`          |
| Organization        | `[ORG_N]`            |
| Location < city     | `[LOCATION_N]`       |
| Non-home address    | `[ADDRESS_N]`        |

Hard-redact tokens (from the entity catalog):

`[REDACTED_CREDENTIAL]`, `[REDACTED_API_KEY]`, `[REDACTED_TOKEN]`,
`[REDACTED_PRIVATE_KEY]`, `[REDACTED_CARD]`, `[REDACTED_ACCOUNT]`,
`[REDACTED_ROUTING]`, `[REDACTED_SSN]`, `[REDACTED_IP]`,
`[REDACTED_COORDS]`, `[REDACTED_HEADER]`, `[REDACTED_LINK]`,
`[HOME_ADDRESS]`, `[ATTACHMENT_SKIPPED]`.

`[REDACTED_LINK]` is used for **every** URL-shaped thing: markdown
`[text](url)` collapses to a single token (display text and URL both
go), bare `http(s)://...` URLs likewise. This deliberately wipes
tracking numbers, order IDs, session tokens, UTM params, and
conference auth codes — they carry pattern-of-life signal and the
vault doesn't need them. `[REDACTED_CONFERENCE_URL]` is retired; if
you see it in an older fixture it should be rewritten.

## What is kept

Per policy, these stay untouched in `expected.json`:

- The user's own first name (per the KEEP allow-list in the redaction policy)
- Mental-health narrative content
- Public figures / authors / fictional characters
- City-level geography (e.g. `Seattle`, `Portland`)
- Named pets (per the KEEP allow-list in the redaction policy)

## Tricky cases to keep covered

If adding or replacing fixtures, make sure these categories remain represented:

- Credential-looking-but-isn't (metaphorical uses of "password", "key").
- Common-word names (`April`, `May`, `Frank`).
- Address inside a signature block.
- Unformatted phone (`5551234567`, no separators).

## Known NER limitations (accepted in current fixtures)

The M2 sanitizer uses spaCy (`en_core_web_lg`) for PERSON / ORG / GPE
detection. A few shapes are genuinely hard for NER and the fixtures
encode the sanitizer's actual behavior rather than an idealized one:

- **Common-word first names in ambiguous context.** `April said ...`
  stays as `April` because spaCy can't reliably tell the first `April`
  from the date in `April 15th`. The headers in the same fixture still
  pseudonymize `April Chen` correctly (the last-name disambiguates).
- **Fictional / invented org names.** `Cafe Luna` as a one-off venue
  name is not detected. Real brands (`Netflix`) and names with strong
  org signals (`Oak & Vine LLC`) are caught.
- **Coreference across surface forms.** `Linda` ↔ `Linda Hartwell`
  collapses to the same pseudonym via a first-token heuristic, but
  pronominal coref (`she` → Linda) is out of scope for M2.

These limitations are tracked in the redaction policy's "Known Gaps"
section. Future work: add a coreference pass and manual org-name hints.
