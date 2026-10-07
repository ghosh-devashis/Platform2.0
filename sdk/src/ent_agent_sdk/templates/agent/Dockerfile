# Agent image on the AgentCore runtime contract (GET /ping, POST /invocations, port 8080).
# The same image runs locally and on AgentCore. Builds for linux/amd64 and linux/arm64.
FROM python:3.13-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.8 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock* README.md ./
COPY src ./src
RUN uv sync --no-dev --no-editable

FROM python:3.13-slim
ARG ENT_GIT_SHA=unknown
ARG ENT_POLICY_VERSION=unknown
ARG ENT_BUILD_TIME=unknown
ENV ENT_GIT_SHA=${ENT_GIT_SHA} ENT_POLICY_VERSION=${ENT_POLICY_VERSION} ENT_BUILD_TIME=${ENT_BUILD_TIME} \
    PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 AGENT_MANIFEST=/app/agent.yaml
LABEL org.opencontainers.image.revision=${ENT_GIT_SHA} ent.policy.version=${ENT_POLICY_VERSION}
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY agent.yaml ./
RUN python -m ent_agent_sdk.metadata write /app/.ent/metadata.json \
    && useradd --system --no-create-home agent && chown -R agent /app
USER agent
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8080/ping', timeout=2).status == 200 else 1)"
CMD ["__NAME__"]
