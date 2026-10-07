#!/usr/bin/env bash
# One-command local stack for macOS and Linux (RUN-01, RUN-08). Same as harness/dev.ps1. Run from the repo root:
#   ./harness/dev.sh up [real|record|replay|local]   build and start Floci, Floci UI, Portkey, Jaeger and the two routers
#   ./harness/dev.sh down [--wipe]                    stop everything (--wipe also deletes Floci data)
#   ./harness/dev.sh seed ../my-agent/agent.yaml      create the secrets, tables and AgentCore runtime an agent declares
#   ./harness/dev.sh status                           show container health
# Agents are not part of this stack: create each one in its own folder (see instructions.txt).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

command="${1:-status}"
compose=(docker compose --profile app)
export AWS_ENDPOINT_URL="http://localhost:4566" AWS_ACCESS_KEY_ID="test" AWS_SECRET_ACCESS_KEY="test" AWS_DEFAULT_REGION="us-east-1"

seed() {
  local manifest="${1:-}"
  if [ -z "$manifest" ]; then echo "Say which agent to seed:  ./harness/dev.sh seed ../my-agent/agent.yaml" >&2; exit 2; fi
  uv run ent-agent dev seed --manifest "$manifest"
}

case "$command" in
  up)
    export ROUTER_MODE="${2:-real}"
    export ENT_GIT_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo local)"
    "${compose[@]}" up -d --build
    for _ in $(seq 1 30); do curl -fsS http://localhost:4566/_floci/health >/dev/null 2>&1 && break || sleep 2; done
    echo
    echo "Stack is up (router mode: $ROUTER_MODE)."
    echo "  model router  http://localhost:8788   invoke router http://localhost:4567"
    echo "  Portkey       http://localhost:8787   Floci        http://localhost:4566"
    echo "  Jaeger        http://localhost:16686  Floci UI     http://localhost:4500"
    echo "Next: create an agent (uv run ent-agent new ...), then seed it:  ./harness/dev.sh seed ../my-agent/agent.yaml"
    ;;
  down)
    if [ "${2:-}" = "--wipe" ]; then "${compose[@]}" down -v; else "${compose[@]}" down; fi
    ;;
  seed) seed "${2:-}" ;;
  status) docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' ;;
  *) echo "usage: $0 up [mode] | down [--wipe] | seed <agent.yaml> | status" >&2; exit 2 ;;
esac
