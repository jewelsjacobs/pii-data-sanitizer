.PHONY: install test lint format extract extract-ical extract-local-files sanitize sanitize-ical sanitize-local-files audit

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

extract: extract-ical extract-local-files

extract-ical:
	uv run python -m scripts.extract_ical

extract-local-files:
	uv run python -m scripts.extract_local_files

sanitize: sanitize-ical sanitize-local-files

sanitize-ical:
	uv run python -m scripts.sanitize_source --source ical

sanitize-local-files:
	uv run python -m scripts.sanitize_source --source local-files

audit:
	@echo "audit: not implemented yet — see PLAN.md (M5)"
	@exit 1
