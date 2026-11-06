export UV_CACHE_DIR ?= $(CURDIR)/.cache/uv
export UV_PYTHON_INSTALL_DIR ?= $(CURDIR)/.cache/python

.PHONY: setup run up down check test-ci test-ui test-infra baf-smoke baf bench-fs bench-sandbox

setup:
	uv sync --frozen
	@if [ ! -e .env ]; then cp .env.example .env; fi

run:
	uv run --frozen --offline uvicorn agent_runtime.gateway:app --app-dir services/gateway/src --host 127.0.0.1 --port 8080

up:
	docker compose up --build --wait --wait-timeout 60

down:
	docker compose down -v --remove-orphans

check:
	uv lock --check --offline
	uv run --frozen --offline ruff check .
	uv run --frozen --offline ruff format --check .

test-ci: check
	uv run --frozen --offline pytest -q

test-infra:
	SANDBOX_TEST_URL="$${SANDBOX_TEST_URL:-http://$$(docker compose port sandbox-manager 8081)}" uv run --frozen --offline pytest -q tests/infra

test-ui baf-smoke baf bench-fs bench-sandbox:
	@printf '%s\n' '$@: ещё не реализовано; см. TASKS.md' >&2
	@exit 2
