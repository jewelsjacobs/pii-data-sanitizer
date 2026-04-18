# Ground-truth fixtures

Hand-written synthetic snippets that encode the expected behavior of the sanitizer.
No real PII. Every name, email, phone number, and address here is made up.

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
`[REDACTED_COORDS]`, `[REDACTED_HEADER]`, `[REDACTED_CONFERENCE_URL]`,
`[HOME_ADDRESS]`, `[ATTACHMENT_SKIPPED]`.

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
