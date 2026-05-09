.PHONY: install test lint format extract extract-ical extract-local-files extract-apple-mail extract-imessage extract-apple-notes extract-eobs sanitize sanitize-ical sanitize-local-files sanitize-apple-mail sanitize-imessage sanitize-apple-notes sanitize-eobs audit audit-ical audit-local-files audit-apple-mail audit-imessage audit-apple-notes promote promote-ical promote-local-files promote-apple-mail promote-imessage promote-apple-notes promote-eobs eobs state verify-split verify-split-user verify-split-agent

# Pass extra flags through to the underlying scripts, e.g.
#   make sanitize-apple-mail ARGS="--since 2026-03-30"
#   make audit-imessage ARGS="--since 2026-03-30 --sample 200"
#   make promote-ical ARGS="--since 2026-03-30 --overwrite"
ARGS ?=

install:
	uv sync
	mkdir -p logs raw-staging clean-staging

test:
	@uv run pytest; code=$$?; if [ $$code -eq 0 ] || [ $$code -eq 5 ]; then exit 0; else exit $$code; fi

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff format .
	uv run ruff check --fix .

extract: extract-ical extract-local-files extract-apple-mail extract-imessage extract-apple-notes

extract-ical:
	uv run python -m scripts.extract_ical $(ARGS)

extract-local-files:
	uv run python -m scripts.extract_local_files $(ARGS)

extract-apple-mail:
	uv run python -m scripts.extract_apple_mail $(ARGS)

extract-imessage:
	uv run python -m scripts.extract_imessage $(ARGS)

extract-apple-notes:
	uv run python -m scripts.extract_apple_notes $(ARGS)

sanitize: sanitize-ical sanitize-local-files sanitize-apple-mail sanitize-imessage sanitize-apple-notes

sanitize-ical:
	uv run python -m scripts.sanitize_source --source ical $(ARGS)

sanitize-local-files:
	uv run python -m scripts.sanitize_source --source local-files $(ARGS)

sanitize-apple-mail:
	uv run python -m scripts.sanitize_source --source apple-mail $(ARGS)

sanitize-imessage:
	uv run python -m scripts.sanitize_source --source imessage $(ARGS)

sanitize-apple-notes:
	uv run python -m scripts.sanitize_source --source apple-notes $(ARGS)

audit: audit-ical audit-local-files audit-apple-mail audit-imessage audit-apple-notes

audit-ical:
	uv run python -m scripts.audit_source --source ical $(ARGS)

audit-local-files:
	uv run python -m scripts.audit_source --source local-files $(ARGS)

audit-apple-mail:
	uv run python -m scripts.audit_source --source apple-mail $(ARGS)

audit-imessage:
	uv run python -m scripts.audit_source --source imessage $(ARGS)

audit-apple-notes:
	uv run python -m scripts.audit_source --source apple-notes $(ARGS)

promote: promote-ical promote-local-files promote-apple-mail promote-imessage promote-apple-notes

promote-ical:
	uv run python -m scripts.promote_to_inbox --source ical $(ARGS)

promote-local-files:
	uv run python -m scripts.promote_to_inbox --source local-files $(ARGS)

promote-apple-mail:
	uv run python -m scripts.promote_to_inbox --source apple-mail $(ARGS)

promote-imessage:
	uv run python -m scripts.promote_to_inbox --source imessage $(ARGS)

promote-apple-notes:
	uv run python -m scripts.promote_to_inbox --source apple-notes $(ARGS)

state:
	uv run python -m scripts.show_state $(ARGS)

# Defaults to the user profile (run from your normal account).
# Use `make verify-split-agent` (which adds --profile agent) when running
# under sudo -u openclaw, e.g.
#   sudo -u openclaw -i bash -lc 'cd ~<your-user>/projects/pii-data-sanitizer && make verify-split-agent'
verify-split: verify-split-user

verify-split-user:
	uv run python -m scripts.verify_account_split --profile user $(ARGS)

verify-split-agent:
	uv run python -m scripts.verify_account_split --profile agent $(ARGS)

# EOB pipeline — tailored sanitizer for SSA disability documentation.
# Standard pipeline pseudonymizes provider names + addresses; EOBs need
# them preserved verbatim. See scripts/sanitize_eobs.py for the redaction
# divergence. Pass EOB source dirs via --source-dir on extract:
#   make extract-eobs ARGS="--source-dir /path/to/EOBs"
extract-eobs:
	uv run python -m scripts.extract_local_files --include-extensions .pdf --out-dir raw-staging/eobs $(ARGS)

sanitize-eobs:
	uv run python -m scripts.sanitize_eobs $(ARGS)

promote-eobs:
	uv run python -m scripts.promote_eobs $(ARGS)

# Aggregate: sanitize + promote (re-run friendly with ARGS="--overwrite")
eobs: sanitize-eobs promote-eobs
