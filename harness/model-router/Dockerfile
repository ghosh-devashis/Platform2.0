# Model router image. Build from the repo root: docker build -f harness/model-router/Dockerfile -t local/model-router:dev .
FROM python:3.13-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.8 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY . .
RUN uv sync --frozen --no-dev --no-editable --package model-router

FROM python:3.13-slim
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 ROUTER_REGISTRY=/app/registry.json ROUTER_RECORDINGS_DIR=/app/recordings
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY harness/model-router/registry.json /app/registry.json
RUN useradd --system --no-create-home router && mkdir -p /app/recordings && chown -R router /app/recordings
USER router
EXPOSE 8788
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8788/health', timeout=2).status == 200 else 1)"
CMD ["model-router"]
