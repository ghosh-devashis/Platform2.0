# Agent image for an agent that lives INSIDE this repo's build context (RUN-02): AgentCore runtime contract on port 8080.
# Agents created with `ent-agent new` live in their own folder and use the Dockerfile the template generated for them
# instead; this file is only for a workspace member. Build from the repo root, with AGENT_DIR relative to it:
#   docker build -f harness/agent.Dockerfile --build-arg AGENT_PACKAGE=my-agent \
#       --build-arg AGENT_DIR=agents/my-agent --build-arg AGENT_COMMAND=my-agent -t local/my-agent:dev .
FROM python:3.13-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.8 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY . .
ARG AGENT_PACKAGE
RUN test -n "${AGENT_PACKAGE}" && uv sync --frozen --no-dev --no-editable --package "${AGENT_PACKAGE}"

FROM python:3.13-slim
ARG AGENT_DIR
ARG AGENT_COMMAND
ARG ENT_GIT_SHA=unknown
ARG ENT_POLICY_VERSION=unknown
ARG ENT_BUILD_TIME=unknown
ENV ENT_GIT_SHA=${ENT_GIT_SHA} ENT_POLICY_VERSION=${ENT_POLICY_VERSION} ENT_BUILD_TIME=${ENT_BUILD_TIME} \
    AGENT_COMMAND=${AGENT_COMMAND} PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 AGENT_MANIFEST=/app/agent.yaml
LABEL org.opencontainers.image.revision=${ENT_GIT_SHA} ent.policy.version=${ENT_POLICY_VERSION}
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY ${AGENT_DIR}/agent.yaml /app/agent.yaml
RUN python -m ent_agent_sdk.metadata write /app/.ent/metadata.json \
    && useradd --system --no-create-home agent && chown -R agent /app
USER agent
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8080/ping', timeout=2).status == 200 else 1)"
CMD ["sh", "-c", "exec ${AGENT_COMMAND}"]
