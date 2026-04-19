# Implementation Plan

Work through milestones in order. Each milestone is a stopping point — the project is in a valid state at the end of each one. Commit after each.

## M0: Scaffolding

Set up the repo so every later milestone has a place to land.

**Build:**
- `pyproject.toml` with `uv` and dependencies: `python-docx`, `pdfplumber`, `pytesseract`, `Pillow`, `striprtf`, `icalendar`, `html2text`, `presidio-analyzer`, `presidio-anonymizer`, `spacy`, `pytest`, `ruff`
- Directory skeleton: `sources/`, `sanitizer/`, `policy/`, `mapping/`, `tests/fixtures/`, `logs/`
- Empty `__init__.py` in each Python package directory
- `justfile` (or `Makefile`) with targets: `test`, `lint`, `extract`, `sanitize`, `audit`
- `README.md` pointing at `CLAUDE.md` and `PLAN.md`
- `.env.example` already exists — do not modify

**Done when:** `just test` runs (pytest finds zero tests, exits 0). `just lint` passes.

## M1: Ground-truth test set

Hand-write synthetic test data. No real PII.

**Build:**
- `tests/fixtures/ground-truth/` with subdirectories per source
- 20-30 total snippets across local-files, apple-mail, imessage, ical
- Include tricky cases: credential-looking-but-isn't, common-word names, address in signature, unformatted phone
- Each fixture has an `input.txt` (or format-appropriate) and an `expected.json` with the sanitized output
- `tests/test_sanitizer.py` loads fixtures and runs them through the sanitizer (stub for now — will fail until M2)

**Done when:** `just test` runs all fixtures and reports the expected failures (sanitizer does not exist yet).

## M2: Sanitizer core

Build the redaction engine. No adapters yet — operate on normalized records directly.

**Build:**
- `sanitizer/rules.py` — deterministic regex layer (credentials, cards, SSN, emails, phones, coordinates, private keys, JWTs). Must achieve 100% recall on credentials/financial/location in the ground-truth set.
- `sanitizer/ner.py` — Presidio wrapper with default recognizers for PERSON, LOCATION, ORGANIZATION
- `sanitizer/mapping.py` — load/save mapping file, stable pseudonym assignment, per-category counter. Mapping file path configurable; tests use an ephemeral temp file.
- `sanitizer/first_run_defaults.py` — auto-classification per `redaction-policy.md` → First-Run Defaults
- `sanitizer/pipeline.py` — orchestrates rules → NER → mapping lookup for one normalized record
- Unit tests for each module

**Done when:** `just test` passes against the ground-truth fixtures. Credential/financial/location recall is 100%. Pseudonym consistency verified across repeated runs.

## M3: First adapter (ical)

Wire one source end-to-end. ical first — smallest scope, cleanest format.

**Build:**
- `sources/ical/adapter.py` per `source-adapters.md` → ical section
- `tests/fixtures/ical/` with synthetic `.ics` files: normal event, all-day, recurring, with `X-APPLE-STRUCTURED-LOCATION`, with conference URL
- `sources/ical/tests/test_adapter.py`
- `scripts/run_pipeline.py` — orchestrates one source: extract → sanitize → write manifest → `/clean-staging`
- Manifest writer in `sanitizer/manifest.py` matching the shape in `pipeline-architecture.md`

**Done when:** the user can run `just extract-ical` and `just sanitize` against their real calendar and see sanitized output in `/clean-staging/ical/` plus a valid manifest. User runs this, not you.

## M4: Remaining adapters

One adapter per sub-milestone. Build in this order — complexity ramps up.

### M4a: local-files

**Build:**
- `sources/local-files/adapter.py` handling `.txt`, `.md`, `.docx` first
- Add `.pdf` text-layer extraction via `pdfplumber`
- Add OCR fallback via `pytesseract` at 300 DPI with confidence threshold 60
- `.rtf` via `striprtf`. `.doc` via `textract` or `antiword` — whichever installs cleanly
- Synthetic fixtures for each format
- `config.yml` schema for source directories

**Done when:** adapter processes a fixture directory containing one file per supported format and produces valid normalized records. User runs it against their real source directories.

### M4b: imessage

**Build:**
- `sources/imessage/adapter.py` per spec
- Snapshot `chat.db` to `/raw-staging/imessage/snapshot.db` at run start
- Read-only SQLite URI (`mode=ro`)
- Decode `attributedBody` blobs when `text` is null
- Apple epoch → ISO 8601 UTC
- Synthetic SQLite fixture with 1:1 and group chats, edited messages, tapbacks, attachment references

**Done when:** adapter processes the fixture database and produces valid normalized records. Attachments counted but skipped. User runs against their real `chat.db` snapshot.

### M4c: apple-mail

**Build:**
- `sources/apple-mail/adapter.py` per spec
- `imaplib.IMAP4_SSL` only. No STARTTLS fallback. Cert verification required.
- `.env` loading via `python-dotenv`. Password never logged.
- Folder exclusion list matching case-insensitively on short name
- Folder name preserved raw in `metadata.folder`
- UID-based pagination for incremental runs
- MIME parsing: prefer `text/plain`, fall back to `text/html` → `html2text`
- Synthetic fixture: local mock IMAP server or recorded responses

**Done when:** adapter runs against a test mailbox (either a throwaway iCloud account or a mock server) and produces valid normalized records. User runs against their real iCloud account.

## M5: Auditor pass

Post-hoc LLM sweep for missed PII.

**Build:**
- `sanitizer/auditor.py` calling a local Ollama model (default: `qwen2.5:14b-instruct`, fallback: `qwen2.5:7b-instruct` for faster iteration)
- Prompt: scan sanitized text, flag anything PII-shaped, return JSON findings
- Output: `logs/audit/<run-id>.jsonl`
- Auditor never modifies files
- `just audit` target
- Model name configurable via env var `AUDIT_MODEL` so the user can switch without code changes

**Done when:** a full pipeline run produces both sanitized output and an audit log. Audit log findings are human-readable.

## M6: First real full run

**Do (user runs, CC assists with iteration):**
- Run all four adapters against real sources
- Run sanitizer
- Run auditor
- Pull 50 random records from `/clean-staging`, spot-check for leaks
- Fix rules in `sanitizer/rules.py` as needed, re-run
- Expect 2-3 iterations

**Done when:** spot-check finds no PII leaks and auditor produces zero high-severity flags.

## M7: Account split (hard gate)

The filesystem-level gate between raw and clean staging.

**Build:**
- Document exact commands in `docs/account-split.md`: creating `openclaw`, ACLs on `/raw-staging` and `/clean-staging`, moving CC agent invocation under `openclaw`
- Verification script: attempts to read `/raw-staging` from `openclaw`, expects failure

**Done when:** running the verification script under `openclaw` fails to read raw data, and running it under `juliajacobs` succeeds.

---

## Where we are

Current milestone: **M4a complete.** Next up: **M4b — imessage adapter.**

M3 notes: `sources/ical/adapter.py`, `sanitizer/manifest.py`,
`scripts/extract_ical.py`, `scripts/sanitize_source.py`. Ical run
against real exports: 601 events extracted + sanitized, spot-check
clean. Real-run workflow for ical: export each calendar from
Calendar.app (File → Export → Export…) into a dedicated directory
and point `ical_source_dirs` at it — `~/Library/Calendars` on current
macOS is empty, events live in iCloud CalDAV + a SQLite cache this
project refuses to read. Floating-time datetimes are interpreted as
local-timezone (Apple Calendar convention) rather than halted.

M4a notes: `sources/local_files/adapter.py` handles `.txt`, `.md`,
`.rtf`, `.docx`, and `.pdf` (text-layer → Tesseract OCR fallback at
300 DPI with confidence threshold 60). Halts on UTF-8 decode errors,
corrupted / encrypted docx, corrupted / password-protected PDFs.
Legacy `.doc` halts with a "convert to .docx or .pdf" message
(textract/antiword tooling is too fragile to depend on).
`scripts/extract_local_files.py` drives it from
`local_files_source_dirs` in `config/local.json`. Makefile targets:
`make extract-local-files`, `make sanitize-local-files`, plus the
aggregates `make extract` / `make sanitize` that run every source.
`en-core-web-lg` is now a pinned pyproject dependency so
`uv sync` doesn't drop the NER model. Test suite: 82 passing
(15 new local-files cases).

## Rules for working the plan

- Do not skip milestones.
- Do not start a milestone before the previous one's "Done when" criteria pass.
- If a milestone needs a decision not covered in the docs, ask the user before inventing an answer.
- Update this file's "Where we are" line at the end of each milestone.
