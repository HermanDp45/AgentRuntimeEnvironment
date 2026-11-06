FROM ghcr.io/astral-sh/uv:0.12.12@sha256:73d2665b478d8fa2de1cf105c6841f8e9cb6b09e568fc7700440c09f8fcd7ac4 AS uv
FROM python:3.13.15-slim-bookworm@sha256:2325bb286ec344af3e5898cc224b5844e2707ac6e26b1632516fd3edc84a5e26 AS builder
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_PYTHON_DOWNLOADS=never UV_COMPILE_BYTECODE=1
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --only-group sandbox --no-cache

FROM python:3.13.15-slim-bookworm@sha256:2325bb286ec344af3e5898cc224b5844e2707ac6e26b1632516fd3edc84a5e26
ENV PATH="/app/.venv/bin:$PATH" PYTHONPATH=/opt \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN groupadd --gid 10001 agent && useradd --uid 10001 --gid agent --no-create-home agent
COPY --from=builder /app/.venv /app/.venv
COPY contracts/__init__.py contracts/paths.py /opt/contracts/
COPY services/sandbox-manager/sandbox/sandbox_worker.py /opt/sandbox_worker.py
WORKDIR /work
USER 10001:10001
ENTRYPOINT ["python", "/opt/sandbox_worker.py"]
CMD ["idle"]
