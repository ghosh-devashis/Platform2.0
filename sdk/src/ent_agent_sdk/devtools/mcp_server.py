"""MCP standards server (EXT-06): lets AI code assistants (Claude Code, Copilot, Cursor...) learn and check the
enterprise standards while they write agent code.

    ent-agent mcp            # stdio transport (what editors launch)

Tools:  list_rules, explain_rule, check_code, check_manifest, check_dependencies, list_approved_models,
        list_approved_dependencies, get_sdk_docs, get_example, get_standards, scaffold_command
Resources:  ent://standards, ent://sdk/api, ent://rules

An assistant asked to "add a tool that calls the CRM API" can read the standards, copy the right example, then call
`check_code` on its own output and fix what it reports before showing it to the developer.

Implements the MCP stdio transport directly (newline-delimited JSON-RPC 2.0: `initialize`, `ping`, `tools/*`,
`resources/*`), so it needs no extra dependency. Nothing is written to stdout except protocol messages.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable, Iterable
from importlib.resources import files
from typing import Any, TextIO

from ent_agent_sdk import metadata
from ent_agent_sdk.devtools import lint, rules_catalog

SERVER_NAME = "ent-agent-standards"
SUPPORTED_PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

EXAMPLES: dict[str, str] = {
    "new_tool": '''from ent_agent_sdk import tools

@tools.tool(owner="crm-team", data_classification="internal")
def lookup_customer(customer_id: str) -> str:
    """Look up a customer by ID (read-only)."""
    return crm_client.get(customer_id)  # authenticate with identity.bearer_headers(...), not a static key
''',
    "side_effect_tool": '''from ent_agent_sdk import tools

@tools.tool(owner="payments", data_classification="confidential", side_effects=True)
def refund_order(order_id: str) -> str:
    """Refund an order. Pauses for human approval before it runs; every call is audited."""
    return payments.refund(order_id)
''',
    "fetch_secret": '''from ent_agent_sdk import secrets

api_key = secrets.get("crm/api", key="api_key")   # JSON secret field; cached 5 minutes; fails closed
''',
    "call_model": '''from langchain_core.runnables import RunnableConfig
from ent_agent_sdk.models import get_model

llm = get_model()   # the model comes from `models:` in agent.yaml; never write a model name here

def assistant(state, config: RunnableConfig):
    return {"messages": [llm.invoke(state["messages"], config)]}   # pass config so tracing/budgets see the call
''',
    "app_from_manifest": '''from ent_agent_sdk import EnterpriseAgentApp

app = EnterpriseAgentApp.from_manifest(graph, "agent.yaml", version="0.1.0")   # name, team, guardrails from the manifest

if __name__ == "__main__":
    app.run()   # port 8080 (AgentCore contract); PORT overrides locally
''',
    "test_with_fake_model": '''from fastapi.testclient import TestClient
from langchain_core.language_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

class Scripted(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self

def test_agent_answers():
    model = Scripted(messages=iter([AIMessage(content="hello")]))
    client = TestClient(create_app(model).asgi)
    assert client.post("/invocations", json={"prompt": "hi"}).json() == {"output": "hello"}
''',
    "approve_a_tool_call": '''# 1) the agent pauses:   POST /invocations {"prompt": "refund order o-1"}
#    -> 202 {"status": "approval_required", "approvals": [{"tool": "refund_order", "arguments": {...}}], "session_id": "<id>"}
# 2) a human approves:   POST /invocations {"resume": {"approved": true, "approver": "alice"}}
#    with header X-Amzn-Bedrock-AgentCore-Runtime-Session-Id: <id>
''',
}

DOC_TOPICS = ("overview", "models", "tools", "approvals", "secrets", "guardrails", "budget", "memory", "identity",
              "manifest", "observability", "testing")


def _read_package_text(*parts: str) -> str:
    return (files("ent_agent_sdk").joinpath(*parts)).read_text(encoding="utf-8")


def _standards_text() -> str:
    text = _read_package_text("templates", "agent", "CLAUDE.md")
    return text.replace("__NAME__", "your-agent").replace("__TEAM__", "your-team")


def _sdk_docs(topic: str | None = None) -> str:
    text = _read_package_text("docs", "api.md")
    if not topic:
        return text
    sections = re.split(r"(?m)^## ", text)
    for section in sections[1:]:
        title, _, body = section.partition("\n")
        if title.strip().lower() == topic.lower():
            return f"## {title.strip()}\n{body.rstrip()}\n"
    raise ValueError(f"Unknown topic '{topic}'. Topics: {', '.join(DOC_TOPICS)}.")


def _findings_result(findings: list[lint.Finding]) -> str:
    errors = sum(f.severity == "error" for f in findings)
    summary = ("No policy problems found." if not findings
               else f"{len(findings)} finding(s), {errors} error(s). Fix them and call this tool again.")
    return json.dumps({"summary": summary, "findings": [f.to_dict() for f in findings]}, indent=2)


# --- tool implementations ----------------------------------------------------------------------------------

def _t_list_rules(_: dict) -> str:
    return json.dumps([{"id": r["id"], "title": r["title"], "group": r["group"]} for r in rules_catalog.RULES], indent=2)


def _t_explain_rule(args: dict) -> str:
    rule = rules_catalog.get(str(args.get("rule_id", "")))
    if rule is None:
        raise ValueError(f"Unknown rule '{args.get('rule_id')}'. Use list_rules for the IDs.")
    return json.dumps(rule, indent=2)


def _t_check_code(args: dict) -> str:
    code = args.get("code")
    if not isinstance(code, str):
        raise ValueError("'code' (the Python source) is required.")
    return _findings_result(lint.check_source(code, str(args.get("filename") or "agent.py")))


def _t_check_manifest(args: dict) -> str:
    if not isinstance(args.get("yaml"), str):
        raise ValueError("'yaml' (the agent.yaml text) is required.")
    return _findings_result(lint.check_manifest_text(args["yaml"]))


def _t_check_dependencies(args: dict) -> str:
    if not isinstance(args.get("pyproject"), str):
        raise ValueError("'pyproject' (the pyproject.toml text) is required.")
    return _findings_result(lint.check_pyproject(args["pyproject"]))


def _t_list_models(_: dict) -> str:
    return json.dumps(lint.policy_data("approved-models.json"), indent=2)


def _t_list_dependencies(_: dict) -> str:
    return json.dumps(lint.policy_data("approved-dependencies.json"), indent=2)


def _t_get_sdk_docs(args: dict) -> str:
    return _sdk_docs(args.get("topic"))


def _t_get_example(args: dict) -> str:
    name = args.get("name")
    if name not in EXAMPLES:
        raise ValueError(f"Unknown example '{name}'. Examples: {', '.join(sorted(EXAMPLES))}.")
    return EXAMPLES[name]


def _t_get_standards(_: dict) -> str:
    return _standards_text()


def _t_scaffold(args: dict) -> str:
    name, team = args.get("name"), args.get("team")
    classification = args.get("classification", "internal")
    problems = lint.manifest_mod.validate({"name": name, "team": team, "data_classification": classification,
                                           "guardrail_profile": "strict" if classification == "restricted" else "standard"})
    if problems:
        raise ValueError("; ".join(problems))
    return f"ent-agent new {name} --team {team} --classification {classification}"


def _schema(properties: dict[str, Any], required: Iterable[str] = ()) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


TOOLS: dict[str, tuple[str, dict[str, Any], Callable[[dict], str]]] = {
    "list_rules": ("List every policy rule (ID, title, group).", _schema({}), _t_list_rules),
    "explain_rule": ("Explain one rule: why it exists, how to fix it, a bad and a good example.",
                     _schema({"rule_id": {"type": "string", "description": "For example ENT-001"}}, ["rule_id"]), _t_explain_rule),
    "check_code": ("Check Python code against the enterprise rules (ENT-001..009). Call it on code you wrote and fix "
                   "every error before presenting the code.",
                   _schema({"code": {"type": "string"}, "filename": {"type": "string"}}, ["code"]), _t_check_code),
    "check_manifest": ("Check an agent.yaml (ENT-020..023).", _schema({"yaml": {"type": "string"}}, ["yaml"]), _t_check_manifest),
    "check_dependencies": ("Check a pyproject.toml against the dependency allowlist (ENT-010..012).",
                           _schema({"pyproject": {"type": "string"}}, ["pyproject"]), _t_check_dependencies),
    "list_approved_models": ("Logical model names an agent may use (and which are cleared for restricted data).", _schema({}), _t_list_models),
    "list_approved_dependencies": ("Extra libraries agents may depend on directly.", _schema({}), _t_list_dependencies),
    "get_sdk_docs": ("SDK API documentation, whole or one topic: " + ", ".join(DOC_TOPICS) + ".",
                     _schema({"topic": {"type": "string", "enum": list(DOC_TOPICS)}}), _t_get_sdk_docs),
    "get_example": ("A compliant code example: " + ", ".join(sorted(EXAMPLES)) + ".",
                    _schema({"name": {"type": "string", "enum": sorted(EXAMPLES)}}, ["name"]), _t_get_example),
    "get_standards": ("The standards every agent in this repository must follow (Always / Never lists).", _schema({}), _t_get_standards),
    "scaffold_command": ("The command that creates a new compliant agent project.",
                         _schema({"name": {"type": "string"}, "team": {"type": "string"},
                                  "classification": {"enum": list(lint.manifest_mod.CLASSIFICATIONS)}}, ["name", "team"]), _t_scaffold),
}

RESOURCES: dict[str, tuple[str, str, Callable[[], str]]] = {
    "ent://standards": ("Enterprise agent standards", "text/markdown", _standards_text),
    "ent://sdk/api": ("Enterprise Agent SDK API summary", "text/markdown", _sdk_docs),
    "ent://rules": ("Policy rules", "text/markdown", rules_catalog.render_markdown),
}


# --- protocol -----------------------------------------------------------------------------------------------

def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def handle(message: dict[str, Any]) -> dict[str, Any] | None:
    """Handle one JSON-RPC message; returns the response, or None for notifications."""
    request_id = message.get("id")
    method = message.get("method")
    params = message.get("params") or {}
    is_notification = "id" not in message
    if not isinstance(method, str):
        return None if is_notification else _error(request_id, -32600, "Invalid request.")

    if method == "initialize":
        asked = params.get("protocolVersion")
        version = asked if asked in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0]
        return {"jsonrpc": "2.0", "id": request_id, "result": {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}, "resources": {"listChanged": False, "subscribe": False}},
            "serverInfo": {"name": SERVER_NAME, "version": metadata.collect()["sdk"]["version"]},
            "instructions": "Read ent://standards before writing agent code. Run check_code on code you write.",
        }}
    if is_notification:  # notifications/initialized, notifications/cancelled, ...
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if method == "tools/list":
        tools = [{"name": n, "description": d, "inputSchema": s} for n, (d, s, _) in TOOLS.items()]
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": tools}}
    if method == "tools/call":
        name = params.get("name")
        if name not in TOOLS:
            return _error(request_id, -32602, f"Unknown tool '{name}'.")
        try:
            text = TOOLS[name][2](params.get("arguments") or {})
            result = {"content": [{"type": "text", "text": text}], "isError": False}
        except Exception as exc:  # tool errors are results the model can read, not protocol errors
            result = {"content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}], "isError": True}
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    if method == "resources/list":
        resources = [{"uri": u, "name": n, "mimeType": m} for u, (n, m, _) in RESOURCES.items()]
        return {"jsonrpc": "2.0", "id": request_id, "result": {"resources": resources}}
    if method == "resources/read":
        uri = params.get("uri")
        if uri not in RESOURCES:
            return _error(request_id, -32002, f"Resource not found: {uri}")
        _, mime, read = RESOURCES[uri]
        return {"jsonrpc": "2.0", "id": request_id, "result": {"contents": [{"uri": uri, "mimeType": mime, "text": read()}]}}
    return _error(request_id, -32601, f"Method not found: {method}")


def serve(stdin: TextIO | None = None, stdout: TextIO | None = None) -> None:
    """Run the server on newline-delimited JSON-RPC until stdin closes."""
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            stdout.write(json.dumps(_error(None, -32700, "Parse error.")) + "\n")
            stdout.flush()
            continue
        for message in payload if isinstance(payload, list) else [payload]:
            response = handle(message) if isinstance(message, dict) else _error(None, -32600, "Invalid request.")
            if response is not None:
                stdout.write(json.dumps(response) + "\n")
                stdout.flush()
