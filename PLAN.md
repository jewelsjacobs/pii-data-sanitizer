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

## M6.5: Promote step — sanitizer JSONL → Obsidian markdown

The bridge between sanitizer output and the vault's Crew agents.

**Build:**
- `sanitizer/promote.py` — per-source converters that turn one (or, for
  imessage, several) sanitized record(s) into `(filename, markdown)`
  pairs. Filename matches the Crew convention
  `YYYY-MM-DD — Type — Title.md`. Frontmatter matches existing Crew
  templates (Postman email-info / meeting, Scribe reference) so the
  Sorter routes notes without retraining. iMessage groups by
  `(chat_identifier, day)` into one transcript note per group.
- `scripts/promote_to_inbox.py` — CLI: read
  `clean-staging/<source>/events.jsonl`, write notes into
  `promote_staging_dir/<source>/`. Skip-if-exists by default,
  `--overwrite` flag for re-runs. Disambiguates filename collisions
  with `(N)` suffixes so no note is silently overwritten.
- `promote_staging_dir` field in `LocalConfig` + `config/local.example.json`.
  Default points OUTSIDE the vault — `~/sanitizer-promote-staging` —
  by design. The user reviews and manually moves approved notes into
  the vault's `00-Inbox/`. This matches the Apple Health Option-B
  pattern already in the vault and preserves the trust boundary
  (sanitizer + promote run as the user; agents run separately).
- Makefile targets: `promote`, `promote-ical`, `promote-local-files`,
  `promote-apple-mail`, `promote-imessage`.
- `tests/test_promote.py` — per-source unit tests covering filename
  shape, frontmatter fields, sanitized-token passthrough, edge cases
  (empty content, missing fields, unsafe filename chars, double quotes
  in YAML), and imessage chat-day grouping.

**Done when:** `make test` passes; `make promote-<source>` produces
markdown notes in the staging folder for any source the user has
already sanitized.

## M7: Account split (hard gate)

The filesystem-level gate between raw and clean staging.

**Build:**
- Document exact commands in `docs/account-split.md`: creating `openclaw`, ACLs on `/raw-staging` and `/clean-staging`, moving CC agent invocation under `openclaw`
- Verification script: attempts to read `/raw-staging` from `openclaw`, expects failure

**Done when:** running the verification script under `openclaw` fails to read raw data, and running it under `youruser` succeeds.

---

## Where we are

Current milestone: **M7 complete.** Both verification profiles pass on
the real machine: user profile reads everything, agent profile is
denied on raw-staging, mapping, config, .env, ~/Library/Mail,
~/Library/Messages, and reads clean-staging + vault. Next up:
**post-M7 hardening (ongoing rule iteration as the agent crew
exercises the pipeline + apple-notes date-column fix, see below).**

Post-M7 addition (2026-07-05): incremental vault update run (ical +
apple-mail since 2026-04-29, local-files since 2026-05-05 — the
first real local-files run, closing the M6 deferral). Audit-driven
iteration produced pipeline improvements that benefit every source:
``calendar_name`` removed from ``_STRUCTURAL_KEEP_KEYS`` (was leaking
raw calendar names, including an email-address-named subscribed
calendar); a known-surface sweep (``_sub_known_entities``) that
substitutes already-mapped PERSON/ORG surfaces by literal
word-boundary match wherever NER misses them (short titles, per-field
context differences); trailing person-initials collapse
(``[PERSON_N] S.`` → ``[PERSON_N]``); a 3-4-4 international phone
branch; ``scripts/add_mapping_entity.py`` (manual mapping additions,
prints tokens only); ``scripts/locate_audit_findings.py`` (reports
which record field contains each audit excerpt, PII-free output);
and ``--modified-since`` on the local-files extractor (windowed
incremental extraction — see source-adapters.md → Open Items).
Final audits: ical 0 findings, apple-mail 0, local-files 2 confirmed
false positives. 324 tests passing. Known deferred issue: the
apple-notes adapter reads only ``ZCREATIONDATE1`` and stamps
``2001-01-01`` when NULL, which makes ``--since`` windows silently
drop those notes — fix by COALESCE-ing across the
``ZCREATIONDATE*``/``ZMODIFICATIONDATE*`` families (mirroring
``scripts/backfill_apple_notes_dates.py``) before the next notes run.

Post-M7 addition (2026-05-09): EOB sanitization pathway for SSA
disability documentation. Standard pipeline pseudonymizes provider
names, doctor names, and facility addresses (NER ORG/PERSON +
``_sub_addresses``) — fatal for EOBs used as proof of medical events.
``scripts/sanitize_eobs.py`` runs ``apply_rules`` (minus ``zip_code``,
plus new ``labeled_dob`` / ``labeled_claim``) + scrub_strings + email
pseudonymization, and skips ``_sub_phones`` (NPIs are 10 digits),
``_sub_addresses``, and ``_sub_ner``. ``scripts/promote_eobs.py``
emits ``type: eob`` markdown with ``ssa-evidence`` tag. Output lives
under the isolated ``eobs/`` source name so it never clobbers
``local-files``. Real-run hardening produced one rules.py improvement
that benefits every source: ``labeled_gov_id`` now catches ``Insured``
/ ``Group`` first-role labels and the ``Acct`` Account abbreviation.
309 tests passing. Make targets: ``extract-eobs`` / ``sanitize-eobs`` /
``promote-eobs`` (and ``eobs`` aggregate).

M7 notes: `docs/account-split.md` documents the full
youruser↔openclaw boundary on macOS — ACL chain on home →
projects → pii-data-sanitizer to grant openclaw read on
`clean-staging/` only, `chmod -R go-rwx` on `raw-staging/` and
`mapping/`, sudoers entry for password-free `sudo -u openclaw`.
The `openclaw` Standard (non-admin) account is already created on
this machine. `scripts/verify_account_split.py` probes every
sensitive and accessible path and reports per-path
PASS/FAIL against a profile (`--profile user|agent`); exits 0 iff
all probes match. Makefile targets: `make verify-split-user` and
`make verify-split-agent`. 12 new tests, 243 total passing.

M6 notes: real-run iteration cycles produced clean output for ical,
apple-mail, and imessage (local-files deferred per user request).
Auditor reports zero high-severity findings on each. Rules tightened
along the way: every URL shape collapses to `[REDACTED_LINK]`;
`labeled_2fa_code` runs before `labeled_password` so 2FA tokens don't
get re-labeled as credentials; `labeled_gov_id` accepts multi-word
labels and a parenthetical absorber; `ssn_last_four` and
`account_ending` patterns added; phone regex widened for
``(NNN)-NNN- NNNN``-shaped audit findings. The auditor itself gained
a verifier (substring + word-boundary keep-list match + bracketed-token
filter) so hallucinated findings, keep-list false positives, and
already-redacted tokens stop reaching the high-severity report.

M6.5 notes: `sanitizer/promote.py` exposes `promote_ical`,
`promote_apple_mail`, `promote_local_files`, and `promote_imessage`.
`scripts/promote_to_inbox.py` drives them from
`clean-staging/<source>/events.jsonl` into
`promote_staging_dir/<source>/`. Notes follow the Crew filename
convention and use frontmatter compatible with Postman's email-info /
meeting templates and Scribe's reference template. iMessage records
group by `(chat_identifier, day)` so a thread reads as one note.
Promote staging is OUTSIDE the vault by design — the user reviews
and moves approved notes manually into `00-Inbox/`. 209 tests
passing (19 new promote cases).

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

M4c notes: `sources/apple_mail/adapter.py` uses stdlib `imaplib.IMAP4_SSL`
against `imap.mail.me.com:993` with mandatory cert verification (no
STARTTLS fallback). Folder handling is a deny-list with case-insensitive
short-name matching (Junk/Trash/Drafts/etc.). Messages are fetched via
UID search; `since_uid={folder: N}` triggers `UID <N+1>:*` for
incremental runs. MIME parsing prefers `text/plain`, falls back to
`text/html` via `html2text`, decodes quoted-printable + base64, respects
`Content-Type` charset, halts on malformed Date headers. Attachments
are counted but never downloaded. Credentials are passed through the
adapter's parameters — never logged, never appear in exceptions.
`scripts/extract_apple_mail.py` loads `.env` via python-dotenv and
drives the adapter; Makefile target: `make extract-apple-mail`.
Test suite: 104 passing (22 new apple-mail cases, including
mocked-IMAP deny-list / UID pagination / credential-leak guards).
python-dotenv added as a direct dependency.

M4b notes: `sources/imessage/adapter.py` snapshots `chat.db` (plus the
WAL / SHM sidecars) to `raw-staging/imessage/snapshot.db`, then reads
the snapshot in read-only mode via a `file:...?mode=ro` SQLite URI.
The SQL joins `message × handle × chat × chat_message_join` and
aggregates attachment counts. Apple Cocoa timestamps are decoded
whether they're in seconds (pre-Mojave) or nanoseconds (post-Mojave).
Tapbacks / reactions (`associated_message_type` 2000-series and
3000-series) and unsent messages (`date_retracted != 0`) are skipped
silently. Edited messages use their final text with
`metadata.was_edited: true`. `attributedBody` fallback uses a
heuristic typedstream decoder when the `text` column is NULL —
handles most single-run bodies; the M5 LLM auditor is the safety net
for the rest. `scripts/extract_imessage.py` drives it from
`imessage_chat_db_path` in `config/local.json`. Makefile target:
`make extract-imessage`. Test suite: 131 passing (21 new imessage
cases covering Apple-epoch conversion, typedstream decode, snapshot
behavior, tapback / unsent / edited / reply / attachment flags,
read-only enforcement).

**Post-M4 note on apple-mail:** after the original M4c, the extract
was refactored into two phases — `fetch_and_cache` writes raw `.eml`
bytes to `raw-staging/apple-mail/cache/<quoted-folder>/<uid>.eml`
(resumable, already-cached UIDs skipped without a network call), and
`parse_cache` yields records from the cache. `--skip-fetch` re-runs
phase 2 only, for fast parser iteration. A live iCloud run produced
10k+ records after several policy relaxations (missing Date headers
fall back to the last `Received:` header and flag
`metadata.date_synthesized: true`; legacy RFC822 fetch item swapped
for `BODY.PEEK[]`; UID bytes decoded to str before passing to
`imaplib.uid`). A later iteration (after the first real full run)
unified all URL shapes — markdown `[text](url)`, bare `http(s)://` —
into a single `[REDACTED_LINK]` token to stop URL-param NER churn and
drop tracking-number / UTM / session-ID pattern-of-life signal.

M5 notes: `sanitizer/auditor.py` wraps the Ollama Python SDK with
`audit_record`, `audit_source`, and `resolve_model`. Default model is
`qwen2.5:14b-instruct` with a `qwen2.5:7b-instruct` fallback; both
overridable via `AUDIT_MODEL` env var or `--model` flag on
`scripts.audit_source`. Keep-list (user's first name + allow-listed
GPEs) is injected into the system prompt at runtime so the committed
source stays generic. Auditor is read-only — findings land in
`logs/audit/<run-id>/<source>.jsonl`, never in clean-staging.
`--sample N` audits a random subset (default 100) so first runs
don't turn into a many-hour sequential-LLM wall. Makefile targets:
`make audit` (all sources) and `make audit-<source>`. 161 tests
passing (21 new auditor cases mocking Ollama; no real Ollama
connection required for tests).

## Rules for working the plan

- Do not skip milestones.
- Do not start a milestone before the previous one's "Done when" criteria pass.
- If a milestone needs a decision not covered in the docs, ask the user before inventing an answer.
- Update this file's "Where we are" line at the end of each milestone.
