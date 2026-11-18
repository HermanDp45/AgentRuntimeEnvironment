export UV_CACHE_DIR ?= $(CURDIR)/.cache/uv
export UV_PYTHON_INSTALL_DIR ?= $(CURDIR)/.cache/python
WORKDIR_IMAGE = rclone/rclone:1.75.2@sha256:2687085f718d3c628f7fdfb77c52a1d344332aed543110bb88088f2c70d43eb5
TEST_PROJECT ?= are-offline
TEST_COMPOSE = docker compose -p $(TEST_PROJECT) -f docker-compose.yml -f tests/infra/compose.yml

.PHONY: setup run up down check test-ci test-unit test-ui test-infra baf-early baf-smoke baf bench-fs bench-sandbox
.PHONY: up-test test-agent test-lifecycle down-test
.PHONY: setup-gui gui build-client prepare-workdir

setup-gui:
	uv sync --frozen --group gui

.PHONY: start
start: MODEL ?= deepseek
start: setup-gui up

start gui: export ARE_GUI_MODEL = $(MODEL)
start gui:
	uv run --frozen --offline --group gui python -m client.gui $${ARE_GUI_MODEL:+--model "$$ARE_GUI_MODEL"}

test-ui:
	QT_QPA_PLATFORM=offscreen uv run --frozen --offline --group gui pytest -q tests/ui

setup:
	uv sync --frozen
	@if [ ! -e .env ]; then cp .env.example .env; fi

run:
	uv run --frozen --offline uvicorn agent_runtime.gateway:app --app-dir services/gateway/src --host 127.0.0.1 --port 8080

prepare-workdir:
	@docker image inspect $(WORKDIR_IMAGE) >/dev/null 2>&1 || docker pull $(WORKDIR_IMAGE)

up: prepare-workdir
	docker compose up --build --wait --wait-timeout 60

down:
	docker compose down -v --remove-orphans

check:
	uv lock --check --offline
	uv run --frozen --offline ruff check .
	uv run --frozen --offline ruff format --check .

test-unit: check
	uv run --frozen --offline pytest -q tests/unit

test-infra:
	SANDBOX_TEST_URL="$${SANDBOX_TEST_URL:-http://$$(docker compose port sandbox-manager 8081)}" uv run --frozen --offline pytest -q tests/infra

up-test: prepare-workdir
	GATEWAY_PORT=18080 SANDBOX_MANAGER_PORT=18081 $(TEST_COMPOSE) up --build --wait --wait-timeout 90

test-agent:
	$(TEST_COMPOSE) exec -T sandbox-manager python -c "import os; assert os.environ['ARE_TEST_CANARY'] == 'fixture-manager-canary-C2'"
	$(TEST_COMPOSE) exec -T llm-proxy python -c "import os; assert os.environ['LLM_PROVIDER_URL'] == 'http://mock-llm:8000'"
	SANDBOX_TEST_URL=http://127.0.0.1:18081 HARNESS_TEST_URL=http://127.0.0.1:18080 uv run --frozen --offline pytest -q tests/infra

test-lifecycle:
	COMPOSE_PROJECT_NAME=$(TEST_PROJECT) COMPOSE_FILE=docker-compose.yml:tests/infra/compose.yml uv run --frozen --offline python tests/infra/check_lifecycle.py

down-test:
	$(TEST_COMPOSE) down -v --remove-orphans

baf-early:
	uv run --frozen --offline python -m baf.runner $(BAF_ARGS)

test-ci: test-unit
	@{ \
		trap '$(MAKE) down-test' EXIT; \
		$(MAKE) up-test && \
		$(MAKE) test-agent && \
		$(MAKE) test-ui && \
		$(MAKE) baf-smoke && \
		$(MAKE) test-lifecycle; \
	}

baf-smoke:
	$(TEST_COMPOSE) exec -T llm-proxy python -c "import os; assert os.environ['LLM_PROVIDER_URL'] == 'http://mock-llm:8000'"
	CLIENT_TOKEN=fixture-client-token SANDBOX_TEST_URL=http://127.0.0.1:18081 uv run --frozen --offline python -m baf.runner --url http://127.0.0.1:18080 $(BAF_ARGS)

baf:
	uv run --frozen --offline python -m baf.runner --suite full --mount --repeat 3 $(BAF_ARGS)

build-client:
	uv sync --frozen --group gui --group packaging
	PYINSTALLER_CONFIG_DIR=$(CURDIR)/.cache/pyinstaller uv run --frozen --offline --group gui --group packaging pyinstaller --noconfirm --windowed --onedir --name AgentRuntime --paths . --specpath build --distpath dist --workpath build/pyinstaller client/desktop.py

bench-fs bench-sandbox:
	@printf '%s\n' '$@: ещё не реализовано; см. TASKS.md' >&2
	@exit 2
