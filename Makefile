export UV_CACHE_DIR ?= $(CURDIR)/.cache/uv
export UV_PYTHON_INSTALL_DIR ?= $(CURDIR)/.cache/python
TEST_PROJECT ?= are-offline
TEST_COMPOSE = docker compose -p $(TEST_PROJECT) -f docker-compose.yml -f tests/infra/compose.yml

.PHONY: setup run up down check test-ci test-ui test-infra baf-smoke baf bench-fs bench-sandbox
.PHONY: up-test test-agent test-lifecycle down-test
.PHONY: setup-gui gui

setup-gui:
	uv sync --frozen --group gui

gui:
	uv run --frozen --offline --group gui python -m client.gui

test-ui:
	QT_QPA_PLATFORM=offscreen uv run --frozen --offline --group gui pytest -q tests/ui

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

up-test:
	GATEWAY_PORT=18080 SANDBOX_MANAGER_PORT=18081 $(TEST_COMPOSE) up --build --wait --wait-timeout 90

test-agent:
	$(TEST_COMPOSE) exec -T llm-proxy python -c "import os; assert os.environ['LLM_PROVIDER_URL'] == 'http://mock-llm:8000'"
	SANDBOX_TEST_URL=http://127.0.0.1:18081 HARNESS_TEST_URL=http://127.0.0.1:18080 uv run --frozen --offline pytest -q tests/infra

test-lifecycle:
	COMPOSE_PROJECT_NAME=$(TEST_PROJECT) COMPOSE_FILE=docker-compose.yml:tests/infra/compose.yml uv run --frozen --offline python tests/infra/check_lifecycle.py

down-test:
	$(TEST_COMPOSE) down -v --remove-orphans

baf-smoke baf bench-fs bench-sandbox:
	@printf '%s\n' '$@: ещё не реализовано; см. TASKS.md' >&2
	@exit 2
