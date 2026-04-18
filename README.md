# pii-data-sanitizer

Local pre-processor that cleans raw personal data exports before a separate Claude Code agent pipeline ingests them into an Obsidian vault.

## Start here

- [CLAUDE.md](CLAUDE.md) — working rules for Claude Code sessions in this repo
- [PLAN.md](PLAN.md) — implementation milestones and current position
- [files/pipeline-architecture.md](files/pipeline-architecture.md) — what the pipeline does
- [files/redaction-policy.md](files/redaction-policy.md) — what gets redacted, pseudonymized, kept
- [files/source-adapters.md](files/source-adapters.md) — per-source extraction specs

## Setup

```
make install
make test
make lint
```

`make install` runs `uv sync` and creates the local staging directories. Python 3.11+ and [`uv`](https://docs.astral.sh/uv/) are required.

## Common targets

| Target          | What it does                                                     |
| --------------- | ---------------------------------------------------------------- |
| `make install`  | Install dependencies and create local staging directories.       |
| `make test`     | Run the pytest suite.                                            |
| `make lint`     | `ruff check` + `ruff format --check`.                            |
| `make format`   | Apply `ruff format` and `ruff check --fix`.                      |
| `make extract`  | Run source adapters (not implemented until M3/M4).               |
| `make sanitize` | Run the sanitizer over `/raw-staging` (not implemented until M2). |
| `make audit`    | Run the LLM auditor over `/clean-staging` (not implemented until M5). |
