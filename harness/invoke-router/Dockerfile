# Invoke router image. Build from the repo root: docker build -f harness/invoke-router/Dockerfile -t local/invoke-router:dev .
FROM python:3.13-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.8 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY . .
RUN uv sync --frozen --no-dev --no-editable --package invoke-router

FROM python:3.13-slim
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
RUN useradd --system --no-create-home router
USER router
EXPOSE 4567
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:4567/_router/health', timeout=2).status == 200 else 1)"
CMD ["invoke-router"]
