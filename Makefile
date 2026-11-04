export UV_CACHE_DIR ?= $(CURDIR)/.cache/uv
export UV_PYTHON_INSTALL_DIR ?= $(CURDIR)/.cache/python

.PHONY: setup run check test-ci

setup:
	uv sync --frozen
	@if [ ! -e .env ]; then cp .env.example .env; fi

run:
	uv run --frozen --offline uvicorn agent_runtime.gateway:app --app-dir src --host 127.0.0.1 --port 8080

check:
	uv lock --check --offline
	uv run --frozen --offline ruff check .
	uv run --frozen --offline ruff format --check .

test-ci: check
	uv run --frozen --offline pytest -q
