"""Tests for the MCP standards server (EXT-06) and the rules catalogue (NFR-04)."""

import io
import json
import re
from pathlib import Path

import pytest

from ent_agent_sdk.devtools import lint, mcp_server, rules_catalog

REPO = Path(__file__).resolve().parents[2]


def call(method, params=None, request_id=1):
    message = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return mcp_server.handle(message)


def tool(tool_name, **arguments):
    return call("tools/call", {"name": tool_name, "arguments": arguments})["result"]


def text_of(result):
    return result["content"][0]["text"]


def test_initialize_handshake_and_notifications():
    response = call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}})
    result = response["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["serverInfo"]["name"] == "ent-agent-standards"
    assert "tools" in result["capabilities"] and "resources" in result["capabilities"]
    assert call("initialize", {"protocolVersion": "1999-01-01"})["result"]["protocolVersion"] == mcp_server.SUPPORTED_PROTOCOLS[0]
    assert mcp_server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None  # no reply to notifications
    assert call("ping")["result"] == {}


def test_tools_are_listed_with_schemas():
    tools = {t["name"]: t for t in call("tools/list")["result"]["tools"]}
    assert {"check_code", "explain_rule", "list_rules", "get_sdk_docs", "get_example", "get_standards",
            "list_approved_models", "list_approved_dependencies", "check_manifest", "check_dependencies",
            "scaffold_command"} <= set(tools)
    for entry in tools.values():
        assert entry["description"] and entry["inputSchema"]["type"] == "object"
    assert tools["check_code"]["inputSchema"]["required"] == ["code"]


def test_check_code_finds_violations_and_passes_clean_code():
    bad = json.loads(text_of(tool("check_code", code="llm = ChatOpenAI()\n")))
    assert bad["findings"][0]["rule"] == "ENT-001" and "fix" in bad["findings"][0] and "1 finding" in bad["summary"]
    good = json.loads(text_of(tool("check_code", code='llm = get_model("chat-default")\n')))
    assert good["findings"] == [] and "No policy problems" in good["summary"]


def test_every_example_is_itself_compliant():
    for name, code in mcp_server.EXAMPLES.items():
        findings = [f for f in lint.check_source(code, "agent.py") if f.severity == "error"]
        assert findings == [], f"example '{name}' breaks the rules: {findings}"


def test_manifest_and_dependency_tools():
    findings = json.loads(text_of(tool("check_manifest", yaml="name: a-agent\nteam: t\ndata_classification: restricted\n")))
    assert any(f["rule"] == "ENT-021" for f in findings["findings"])
    findings = json.loads(text_of(tool("check_dependencies", pyproject='[project]\ndependencies = ["openai"]\n')))
    assert {f["rule"] for f in findings["findings"]} >= {"ENT-010", "ENT-011"}


def test_reference_tools():
    assert "chat-default" in text_of(tool("list_approved_models"))
    assert "httpx" in text_of(tool("list_approved_dependencies"))
    assert "ENT-001" in text_of(tool("list_rules"))
    explained = json.loads(text_of(tool("explain_rule", rule_id="ent-004")))
    assert explained["id"] == "ENT-004" and "secrets.get" in explained["fix"]
    assert tool("explain_rule", rule_id="ENT-999")["isError"] is True
    assert "Never" in text_of(tool("get_standards")) and "__NAME__" not in text_of(tool("get_standards"))
    assert text_of(tool("scaffold_command", name="crm-agent", team="sales")) == "ent-agent new crm-agent --team sales --classification internal"
    assert tool("scaffold_command", name="Bad Name", team="sales")["isError"] is True


def test_sdk_docs_whole_and_by_topic():
    whole = text_of(tool("get_sdk_docs"))
    assert "## approvals" in whole
    topic = text_of(tool("get_sdk_docs", topic="budget"))
    assert topic.startswith("## budget") and "BudgetExceeded" in topic
    for name in mcp_server.DOC_TOPICS:
        assert text_of(tool("get_sdk_docs", topic=name)).startswith(f"## {name}")
    assert tool("get_sdk_docs", topic="nope")["isError"] is True


def test_examples():
    assert "@tools.tool" in text_of(tool("get_example", name="new_tool"))
    assert tool("get_example", name="nope")["isError"] is True


def test_resources():
    listed = {r["uri"] for r in call("resources/list")["result"]["resources"]}
    assert listed == {"ent://standards", "ent://sdk/api", "ent://rules"}
    contents = call("resources/read", {"uri": "ent://rules"})["result"]["contents"][0]
    assert contents["mimeType"] == "text/markdown" and "ENT-001" in contents["text"]
    assert call("resources/read", {"uri": "ent://nope"})["error"]["code"] == -32002


def test_errors():
    assert call("tools/call", {"name": "nope"})["error"]["code"] == -32602
    assert call("does/not/exist")["error"]["code"] == -32601
    assert tool("check_code")["isError"] is True  # missing required argument is a tool error the model can read


def test_stdio_loop():
    lines = [
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        "",
        "not json",
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
    ]
    out = io.StringIO()
    mcp_server.serve(io.StringIO("\n".join(lines) + "\n"), out)
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [r.get("id") for r in replies] == [1, None, 2]
    assert replies[1]["error"]["code"] == -32700
    assert len(replies[2]["result"]["tools"]) >= 10


# --- the rules catalogue keeps docs, lint and policy files in step -------------------------------------------

def test_rules_page_is_generated_from_the_catalogue():
    assert (REPO / "policy" / "docs" / "rules.md").read_text(encoding="utf-8") == rules_catalog.render_markdown(), (
        "policy/docs/rules.md is stale: regenerate with `ent-agent rules --markdown`"
    )


def test_every_rule_id_used_anywhere_is_in_the_catalogue():
    used = set()
    for path in [*(REPO / "policy").rglob("*"), *(REPO / "sdk" / "src").rglob("*.py")]:
        if path.is_file() and path.suffix in {".yml", ".rego", ".py", ".sh", ".ps1"} and path.name != "rules_catalog.py":
            used |= set(re.findall(r"\bENT-\d{3}\b", path.read_text(encoding="utf-8", errors="replace")))
    assert used, "no rule IDs found: scan broke"
    assert used <= set(rules_catalog.BY_ID), f"rule IDs missing from the catalogue: {sorted(used - set(rules_catalog.BY_ID))}"
    assert set(lint.RULE_TITLES) <= set(rules_catalog.BY_ID)


def test_every_catalogue_rule_has_an_example_pair():
    for rule in rules_catalog.RULES:
        assert rule["bad"] and rule["good"] and rule["fix"] and rule["why"], rule["id"]


@pytest.mark.parametrize("source, rule", [
    ("@tools.tool(owner='a', side_effects=True, requires_approval=False, approval_waiver='W')\ndef f():\n    pass\n", "ENT-009"),
])
def test_approval_waiver_rule_in_lint(source, rule):
    assert [f.rule for f in lint.check_source(source)] == [rule]
    assert lint.check_source(source)[0].severity == "warning"
