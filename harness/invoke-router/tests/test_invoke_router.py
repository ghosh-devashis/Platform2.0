"""Tests for the local invoke router (RUN-03). Agents and Floci are stubbed."""

import httpx2
from fastapi.testclient import TestClient

from invoke_router.app import SESSION_HEADER, create_app, parse_agents, runtime_name

ARN = "arn:aws:bedrock-agentcore:us-east-1:000000000000:runtime/chat_agent-AbCdEfGh12"
SESSION = "session-0123456789-0123456789-0123456789"


async def _chunks(data: bytes):
    yield data


def router(agents=None):
    """A router whose agents answer {"output": "from agent"} and whose Floci answers {"from": "floci"}."""
    seen = []

    def handler(request):
        seen.append(request)
        if str(request.url).startswith("http://chat-agent:8080"):
            return httpx2.Response(200, headers={"content-type": "application/json"}, content=_chunks(b'{"output":"from agent"}'))
        return httpx2.Response(200, headers={"content-type": "application/json"}, content=_chunks(b'{"from":"floci"}'))

    app = create_app(agents=agents if agents is not None else {"chat_agent": "http://chat-agent:8080"},
                     floci_url="http://floci.test:4566",
                     http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))
    return TestClient(app), seen


def test_runtime_name_from_arn_id_or_name():
    assert runtime_name(ARN) == "chat_agent"
    assert runtime_name("chat_agent-AbCdEfGh12") == "chat_agent"
    assert runtime_name("chat_agent") == "chat_agent"


def test_parse_agents():
    assert parse_agents("a=http://a:8080, b=http://b:8080/") == {"a": "http://a:8080", "b": "http://b:8080"}
    assert parse_agents("") == {}


def test_invoke_is_forwarded_to_the_agent_container():
    client, seen = router()
    response = client.post(f"/runtimes/{ARN}/invocations", content=b'{"prompt":"hi"}',
                           headers={"content-type": "application/json", SESSION_HEADER: SESSION,
                                    "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"})
    assert response.status_code == 200
    assert response.json() == {"output": "from agent"}
    assert response.headers[SESSION_HEADER] == SESSION

    forwarded = seen[0]
    assert str(forwarded.url) == "http://chat-agent:8080/invocations"
    assert forwarded.content == b'{"prompt":"hi"}'
    assert forwarded.headers[SESSION_HEADER] == SESSION
    assert forwarded.headers["traceparent"].startswith("00-4bf92f")


def test_url_encoded_arn_is_accepted():
    client, seen = router()
    encoded = ARN.replace(":", "%3A").replace("/", "%2F")
    assert client.post(f"/runtimes/{encoded}/invocations", content=b"{}").json() == {"output": "from agent"}
    assert str(seen[0].url) == "http://chat-agent:8080/invocations"


def test_unknown_runtime_and_other_calls_pass_through_to_floci():
    client, seen = router()
    unknown = client.post("/runtimes/other_agent-AbCdEfGh12/invocations", content=b"{}")
    assert unknown.json() == {"from": "floci"}
    assert str(seen[-1].url) == "http://floci.test:4566/runtimes/other_agent-AbCdEfGh12/invocations"

    listing = client.get("/runtimes/?maxResults=5")
    assert listing.json() == {"from": "floci"}
    assert str(seen[-1].url) == "http://floci.test:4566/runtimes/?maxResults=5"


def test_floci_style_arns_are_resolved_through_the_control_plane():
    """Floci's ARNs (`agent/<uuid>:1`) don't contain the runtime name, so the router looks them up."""
    floci_arn = "arn:aws:bedrock-agentcore:us-east-1:000000000000:agent/f54387f7-a96c-434f-9650-22f949c48de5:1"
    seen = []

    def handler(request):
        seen.append(request)
        url = str(request.url)
        if url.startswith("http://chat-agent:8080"):
            return httpx2.Response(200, headers={"content-type": "application/json"}, content=_chunks(b'{"output":"from agent"}'))
        if request.method == "POST" and url == "http://floci.test:4566/runtimes/":
            listing = b'{"agentRuntimes":[{"agentRuntimeName":"chat_agent","agentRuntimeArn":"%s"}]}' % floci_arn.encode()
            return httpx2.Response(200, headers={"content-type": "application/json"}, content=_chunks(listing))
        return httpx2.Response(200, headers={"content-type": "application/json"}, content=_chunks(b'{"from":"floci"}'))

    app = create_app(agents={"chat_agent": "http://chat-agent:8080"}, floci_url="http://floci.test:4566",
                     http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))
    client = TestClient(app)
    assert client.post(f"/runtimes/{floci_arn}/invocations", content=b"{}").json() == {"output": "from agent"}
    def lookups():
        return [r for r in seen if r.method == "POST" and str(r.url) == "http://floci.test:4566/runtimes/"]

    assert len(lookups()) == 1
    assert client.post(f"/runtimes/{floci_arn}/invocations", content=b"{}").json() == {"output": "from agent"}
    assert len(lookups()) == 1  # second call served from the cache


def test_unreachable_agent_is_a_clear_error():
    def down(request):
        raise httpx2.ConnectError("refused")

    app = create_app(agents={"chat_agent": "http://chat-agent:8080"}, floci_url="http://floci.test:4566",
                     http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(down)))
    response = TestClient(app).post(f"/runtimes/{ARN}/invocations", content=b"{}")
    assert response.status_code == 424
    assert "unreachable" in response.json()["message"]
