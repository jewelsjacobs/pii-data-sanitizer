.PHONY: install test lint format extract extract-ical extract-local-files extract-apple-mail extract-imessage sanitize sanitize-ical sanitize-local-files sanitize-apple-mail sanitize-imessage audit

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

extract: extract-ical extract-local-files extract-apple-mail extract-imessage

extract-ical:
	uv run python -m scripts.extract_ical

extract-local-files:
	uv run python -m scripts.extract_local_files

extract-apple-mail:
	uv run python -m scripts.extract_apple_mail

extract-imessage:
	uv run python -m scripts.extract_imessage

sanitize: sanitize-ical sanitize-local-files sanitize-apple-mail sanitize-imessage

sanitize-ical:
	uv run python -m scripts.sanitize_source --source ical

sanitize-local-files:
	uv run python -m scripts.sanitize_source --source local-files

sanitize-apple-mail:
	uv run python -m scripts.sanitize_source --source apple-mail

sanitize-imessage:
	uv run python -m scripts.sanitize_source --source imessage

audit:
	@echo "audit: not implemented yet — see PLAN.md (M5)"
	@exit 1
