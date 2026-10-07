"""Tests for the in-process policy checker (EXT-02/03 engine, MCP check_code)."""

import json
from pathlib import Path

import pytest

from ent_agent_sdk.cli import main
from ent_agent_sdk.devtools import lint

REPO = Path(__file__).resolve().parents[2]


def rules(source, path="agent.py"):
    return [f.rule for f in lint.check_source(source, path)]


@pytest.mark.parametrize("source, rule", [
    ("from langchain_openai import ChatOpenAI\nllm = ChatOpenAI(model='x')\n", "ENT-001"),
    ("llm = ChatAnthropic()\n", "ENT-001"),
    ("m = init_chat_model('openai:gpt')\n", "ENT-001"),
    ("import openai\nc = openai.OpenAI()\n", "ENT-002"),
    ("c = boto3.client('bedrock-runtime')\n", "ENT-002"),
    ("URL = 'https://api.openai.com/v1'\n", "ENT-003"),
    ("import os\nk = os.environ['OPENAI_API_KEY']\n", "ENT-004"),
    ("import os\nk = os.environ.get('CRM_TOKEN')\n", "ENT-004"),
    ("import os\nk = os.getenv('db_password')\n", "ENT-004"),
    ("c = boto3.client('secretsmanager')\n", "ENT-005"),
    ("v = client.get_secret_value(SecretId='x')\n", "ENT-005"),
    ("v = ssm.get_parameter(Name='x', WithDecryption=True)\n", "ENT-005"),
    ("import uvicorn\nuvicorn.run(app)\n", "ENT-006"),
    ("app = FastAPI()\n", "ENT-006"),
    ("from langchain_core.tools import tool\n", "ENT-007"),
    ("g = Guardrails.off(waiver='W-1')\n", "ENT-008"),
])
def test_each_code_rule_fires(source, rule):
    assert rules(source) == [rule]


@pytest.mark.parametrize("source", [
    "from ent_agent_sdk.models import get_model\nllm = get_model('chat-default')\n",
    "import os\nregion = os.environ.get('AWS_REGION')\n",  # not a credential name
    "from ent_agent_sdk import tools\n",
    "x = 'https://example.com'\n",
    "def broken(:\n",  # syntax errors are the editor's job
])
def test_clean_code_has_no_findings(source):
    assert rules(source) == []


def test_excluded_paths_are_skipped():
    source = "llm = ChatOpenAI()\n"
    assert rules(source, "sdk/src/ent_agent_sdk/models.py") == []
    assert rules(source, "agent/tests/test_x.py") == []
    assert rules(source, "src/agent.py") == ["ENT-001"]
    assert rules("app = FastAPI()\n", "harness/router/app.py") == []


def test_nosem_comments_are_not_honoured():
    assert rules("llm = ChatOpenAI()  # nosem: ent-001\n") == ["ENT-001"]


def test_positions_and_fix_for_direct_model():
    (finding,) = lint.check_source("x = 1\nllm = ChatOpenAI(model='a')\n", "agent.py")
    assert (finding.line, finding.column, finding.end_line, finding.end_column) == (2, 7, 2, 28)
    assert finding.severity == "error"
    edit = finding.fix["edits"][0]
    assert edit["new_text"] == "get_model()"  # the model comes from agent.yaml, not from the fix
    assert (edit["line"], edit["column"], edit["end_column"]) == (2, 7, 28)
    assert finding.fix["imports"] == ["from ent_agent_sdk.models import get_model"]


def test_fix_for_env_secret_read():
    (finding,) = lint.check_source("import os\nkey = os.environ['OPENAI_API_KEY']\n")
    assert finding.fix["edits"][0]["new_text"] == 'secrets.get("openai-api-key")'
    (finding,) = lint.check_source("import os\nkey = os.getenv('CRM_TOKEN')\n")
    assert finding.fix["edits"][0]["new_text"] == 'secrets.get("crm-token")'
    assert finding.fix["imports"] == ["from ent_agent_sdk import secrets"]


def test_guardrails_off_is_a_warning():
    (finding,) = lint.check_source("g = Guardrails.off(waiver='W')\n")
    assert finding.severity == "warning"


def test_pyproject_dependency_rules():
    text = '[project]\nname = "a"\ndependencies = [\n  "langchain-core>=1",\n  "requests",\n  "leftpad",\n]\n'
    found = {(f.rule, f.line) for f in lint.check_pyproject(text)}
    assert ("ENT-010", 1) in found  # SDK missing
    assert ("ENT-011", 4) in found  # pinned by the SDK
    assert ("ENT-012", 6) in found  # not approved
    assert not any(rule == "ENT-012" and line == 5 for rule, line in found)  # requests is approved
    ok = '[project]\nname = "a"\ndependencies = ["ent-agent-sdk", "httpx"]\n'
    assert lint.check_pyproject(ok) == []


def test_manifest_rules():
    good = "name: a-agent\nteam: t\ndata_classification: internal\nmodels: [chat-default]\n"
    assert lint.check_manifest_text(good) == []
    bad = "name: a-agent\nteam: t\ndata_classification: restricted\nguardrail_profile: standard\nmodels: [chat-fast, gpt-9]\n"
    found = {f.rule for f in lint.check_manifest_text(bad)}
    assert {"ENT-021", "ENT-022", "ENT-023"} <= found
    assert [f.rule for f in lint.check_manifest_text("name: [unclosed\n")] == ["ENT-020"]


def test_manifest_finding_points_at_the_offending_line():
    text = "name: a-agent\nteam: t\ndata_classification: restricted\nguardrail_profile: standard\n"
    finding = next(f for f in lint.check_manifest_text(text) if f.rule == "ENT-021")
    assert finding.line == 4


def test_example_agents_are_clean():
    assert [f.to_dict() for f in lint.check_paths([REPO / "examples"])] == []


def test_cli_check_json_and_exit_codes(tmp_path, capsys):
    (tmp_path / "bad.py").write_text("llm = ChatOpenAI()\n", encoding="utf-8")
    assert main(["check", str(tmp_path), "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data[0]["rule"] == "ENT-001" and data[0]["fix"]["edits"]
    (tmp_path / "bad.py").write_text("x = 1\n", encoding="utf-8")
    assert main(["check", str(tmp_path)]) == 0
    assert "No findings" in capsys.readouterr().out


def test_sdk_policy_data_matches_the_policy_bundle():
    for name in ("approved-models.json", "approved-dependencies.json"):
        assert lint.policy_data(name) == json.loads((REPO / "policy" / "data" / name).read_text(encoding="utf-8")), name
