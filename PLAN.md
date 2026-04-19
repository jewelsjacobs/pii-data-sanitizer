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

Current milestone: **M3 complete.** Next up: **M4a — local-files adapter.**

M3 notes: `sources/ical/adapter.py` extracts events from .ics files into
normalized records; halts on missing UID, floating datetimes, or unknown
inputs. `sanitizer/manifest.py` writes `/clean-staging/<source>/manifest.json`
per the shape in the pipeline architecture doc. `scripts/extract_ical.py`
and `scripts/sanitize_source.py` drive the end-to-end flow, reading
self-identity / keep-list / mapping path from `config/local.json`
(gitignored; `config/local.example.json` documents the shape).
Makefile targets: `make extract-ical`, `make sanitize`. 67 tests total
(ical adapter, manifest, end-to-end smoke, plus everything from M0–M2).
The user runs the real pipeline; CC never does.

## Rules for working the plan

- Do not skip milestones.
- Do not start a milestone before the previous one's "Done when" criteria pass.
- If a milestone needs a decision not covered in the docs, ask the user before inventing an answer.
- Update this file's "Where we are" line at the end of each milestone.
