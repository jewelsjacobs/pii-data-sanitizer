.PHONY: install test lint format extract sanitize audit

install:
	uv sync
	uv run python -m spacy download en_core_web_lg
	mkdir -p logs raw-staging clean-staging

test:
	@uv run pytest; code=$$?; if [ $$code -eq 0 ] || [ $$code -eq 5 ]; then exit 0; else exit $$code; fi

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff format .
	uv run ruff check --fix .

extract: extract-ical

extract-ical:
	uv run python -m scripts.extract_ical

sanitize:
	uv run python -m scripts.sanitize_source --source ical

audit:
	@echo "audit: not implemented yet — see PLAN.md (M5)"
	@exit 1
