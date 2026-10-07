"""Local invoke router (RUN-03): makes `InvokeAgentRuntime` reach the real agent containers.

Floci emulates the AgentCore control plane but answers `InvokeAgentRuntime` with a canned reply. Point the
`bedrock-agentcore` client's `endpoint_url` at this router instead of Floci and test code stays identical to
production:

    POST /runtimes/{agentRuntimeArn}/invocations   -> forwarded to the agent container's POST /invocations
    everything else (control plane, other services) -> passed through to Floci unchanged

The agent for a request is found from the runtime name inside the ARN (`.../runtime/<name>-<10 chars>`), using the
mapping in `INVOKE_ROUTER_AGENTS` ("my_agent=http://host.docker.internal:8091" for an agent running on the host).
Runtime names use underscores (AgentCore forbids hyphens), so `my-agent` is registered as `my_agent`.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import AsyncIterator

import httpx2
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from ent_agent_sdk.app import FASTAPI_TELEMETRY_OFF

logger = logging.getLogger("invoke_router")

ROUTER_PORT = 4567
DEFAULT_FLOCI_URL = "http://localhost:4566"
SESSION_HEADER = "x-amzn-bedrock-agentcore-runtime-session-id"
# Request headers the AgentCore contract defines for an invocation (forwarded to the agent).
FORWARD_HEADERS = (
    "content-type", "accept", SESSION_HEADER, "x-amzn-bedrock-agentcore-runtime-user-id",
    "x-amzn-trace-id", "traceparent", "tracestate", "baggage",
)
RESPONSE_HEADERS = ("content-type", SESSION_HEADER, "x-amzn-trace-id", "traceparent", "tracestate", "baggage",
                    "x-ent-trace-id")
HOP_BY_HOP = {"host", "content-length", "connection", "transfer-encoding", "keep-alive", "upgrade", "te"}
_RUNTIME_ID = re.compile(r"^(?P<name>[A-Za-z][A-Za-z0-9_]*)-[A-Za-z0-9]{10}$")


def parse_agents(spec: str) -> dict[str, str]:
    """'a=http://a:8080,b=http://b:8080' -> {'a': 'http://a:8080', 'b': ...}"""
    agents = {}
    for item in filter(None, (part.strip() for part in spec.split(","))):
        name, _, url = item.partition("=")
        if not name or not url:
            raise ValueError(f"Invalid INVOKE_ROUTER_AGENTS entry '{item}'; expected name=url.")
        agents[name.strip()] = url.strip().rstrip("/")
    return agents


def runtime_name(identifier: str) -> str:
    """The runtime name from an ARN, a runtime ID (`name-1234567890`) or a bare name."""
    last = identifier.rsplit("/", 1)[-1]
    match = _RUNTIME_ID.match(last)
    return match.group("name") if match else last


def create_app(
    *, agents: dict[str, str] | None = None, floci_url: str | None = None, http_client: httpx2.AsyncClient | None = None
) -> FastAPI:
    agents = agents if agents is not None else parse_agents(os.environ.get("INVOKE_ROUTER_AGENTS", ""))
    floci_url = (floci_url or os.environ.get("FLOCI_URL", DEFAULT_FLOCI_URL)).rstrip("/")
    client = http_client or httpx2.AsyncClient(timeout=httpx2.Timeout(300.0, connect=2.0))
    api = FastAPI(title="invoke-router", docs_url=None, redoc_url=None, telemetry=FASTAPI_TELEMETRY_OFF)
    arn_names: dict[str, str] = {}  # runtime ARN or ID -> runtime name, learned from Floci's control plane

    async def refresh_runtimes() -> None:
        try:
            # ListAgentRuntimes is POST /runtimes/ (a GET would fall through to Floci's S3 emulation).
            response = await client.post(f"{floci_url}/runtimes/", headers={"content-type": "application/json"}, content=b"{}")
            for runtime in response.json().get("agentRuntimes", []):
                for key in (runtime.get("agentRuntimeArn"), runtime.get("agentRuntimeId")):
                    if key and runtime.get("agentRuntimeName"):
                        arn_names[key] = runtime["agentRuntimeName"]
        except (httpx2.HTTPError, ValueError, AttributeError) as exc:
            logger.warning("Could not list runtimes from Floci: %s", type(exc).__name__)

    async def resolve(identifier: str) -> str:
        """Runtime name for an ARN or ID. Real AWS ARNs contain the name; Floci's (`agent/<uuid>:1`) don't, so
        those are looked up in Floci's control plane (refreshed on a miss)."""
        name = runtime_name(identifier)
        if name in agents:
            return name
        for attempt in (0, 1):
            known = arn_names.get(identifier) or arn_names.get(identifier.rsplit(":", 1)[0])
            if known:
                return known
            if attempt == 0:
                await refresh_runtimes()
        return name

    @api.get("/_router/health")
    async def health() -> dict[str, object]:
        return {"status": "ok", "agents": sorted(agents)}

    @api.post("/runtimes/{identifier:path}/invocations")
    async def invoke(identifier: str, request: Request) -> Response:
        name = await resolve(identifier)
        target = agents.get(name)
        if target is None:  # not one of ours: let Floci answer
            logger.info("No local agent for runtime '%s'; passing through to Floci", name)
            return await _proxy(client, floci_url, request)
        headers = {h: request.headers[h] for h in FORWARD_HEADERS if h in request.headers}
        upstream = client.build_request("POST", f"{target}/invocations", content=await request.body(), headers=headers)
        try:
            response = await client.send(upstream, stream=True)
        except httpx2.HTTPError as exc:
            logger.error("Agent '%s' unreachable at %s: %s", name, target, type(exc).__name__)
            return JSONResponse({"message": f"The local agent for runtime '{name}' is unreachable."}, status_code=424)
        logger.info("invoke runtime=%s status=%s", name, response.status_code)
        out = {h: response.headers[h] for h in RESPONSE_HEADERS if h in response.headers}
        if SESSION_HEADER in request.headers:
            out.setdefault(SESSION_HEADER, request.headers[SESSION_HEADER])
        return StreamingResponse(_relay(response), status_code=response.status_code, headers=out,
                                 background=BackgroundTask(response.aclose))

    @api.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
    async def passthrough(path: str, request: Request) -> Response:
        return await _proxy(client, floci_url, request)

    return api


async def _proxy(client: httpx2.AsyncClient, base_url: str, request: Request) -> Response:
    """Forward a request to Floci unchanged (method, path, query, headers, body)."""
    url = f"{base_url}{request.url.path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP}
    upstream = client.build_request(request.method, url, content=await request.body(), headers=headers)
    try:
        response = await client.send(upstream, stream=True)
    except httpx2.HTTPError as exc:
        logger.error("Floci unreachable at %s: %s", base_url, type(exc).__name__)
        return JSONResponse({"message": "Floci is unreachable."}, status_code=502)
    out = {k: v for k, v in response.headers.items() if k.lower() not in HOP_BY_HOP and k.lower() != "content-encoding"}
    return StreamingResponse(_relay(response), status_code=response.status_code, headers=out,
                             background=BackgroundTask(response.aclose))


async def _relay(response: httpx2.Response) -> AsyncIterator[bytes]:
    async for chunk in response.aiter_raw():
        yield chunk
