# pii-data-sanitizer

Local pre-processor that cleans raw personal data exports before a separate Claude Code agent pipeline ingests them into an Obsidian vault.

## Repository contents

This repo is a **public scaffold**. The pipeline architecture, redaction policy, source-adapter specs, working rules, and ground-truth fixtures live outside version control (see `.gitignore`). What ships here:

- [PLAN.md](PLAN.md) — implementation milestones and current position
- `pyproject.toml` + `uv.lock` — Python dependencies managed by `uv`
- `Makefile` — common targets (see below)
- Empty package skeletons under `sources/`, `sanitizer/`, and `tests/`
- `.env.example` — shape of the IMAP credentials file (the real `.env` stays local)

## Setup

```
make install
make test
make lint
```

`make install` runs `uv sync` and creates the local staging directories. Python 3.11+ and [`uv`](https://docs.astral.sh/uv/) are required.

The audit step (`make audit`) uses a local Ollama model. Optional one-time setup:

```
brew install ollama           # if you don't have it
ollama serve &                # or let the install's service start
ollama pull qwen2.5:14b-instruct   # or qwen2.5:7b-instruct for faster iteration
```

Override the default model via the `AUDIT_MODEL` env var or the `--model` flag on `scripts.audit_source`.

## A note on test fixtures

Ground-truth fixtures under `tests/fixtures/ground-truth/` are **intentionally local-only** and not version-controlled. Even though the content is hand-written and synthetic, it pattern-matches as credential / address / SSN data and is best kept off any remote host. Only `tests/fixtures/ground-truth/README.md` ships with the repo, describing the fixture shape so the tree can be rebuilt from scratch.

## Common targets

| Target          | What it does                                                     |
| --------------- | ---------------------------------------------------------------- |
| `make install`  | Install dependencies and create local staging directories.       |
| `make test`     | Run the pytest suite.                                            |
| `make lint`     | `ruff check` + `ruff format --check`.                            |
| `make format`   | Apply `ruff format` and `ruff check --fix`.                      |
| `make extract`  | Run every source adapter (ical, local-files, apple-mail, imessage).  |
| `make sanitize` | Run the sanitizer over each source's `/raw-staging` output.          |
| `make audit`    | Run the Ollama-backed LLM auditor over `/clean-staging` and write findings to `logs/audit/`. |
| `make promote`  | Render sanitized records as Obsidian-shaped markdown notes into the configured staging folder (outside the vault — review and move manually). |
| `make state`    | Print per-source processing state (last run, record count, suggested `--since` for the next incremental run). |

Per-source variants exist for every step (`make extract-apple-mail`, `make sanitize-ical`, …).
Pass extra flags through `ARGS`, e.g. `make sanitize-apple-mail ARGS="--since 2026-03-30"`.

## Refreshing the data

The sanitize / audit / promote steps accept `--since YYYY-MM-DD` so you can re-run the pipeline against only recent records without reprocessing the full archive. With `--since`, sanitize switches to **append + dedupe-by-`source_id`** mode so prior clean output is preserved; audit and promote filter their inputs to the window.

Use `make state` to see the latest record date already in each clean file — that's the value to pass as `--since` next time. Sanitize also writes `clean-staging/<source>/state.json` and prints the suggested `--since` at the end of every run.

The full step-by-step (per-source quirks, file-level cheat sheet, recovery notes, suggested cadence) lives in `files/refresh-runbook.md` (local-only).
