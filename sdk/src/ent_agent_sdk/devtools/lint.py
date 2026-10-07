"""Policy checks as a library: the engine behind the VS Code extension's live diagnostics and quick fixes, the MCP
standards server's `check_code` tool, and `ent-agent check`.

It applies the same rules as the policy bundle (`policy/semgrep/ent-agent.yml`, `policy/opa/*.rego`) directly to
source text, with no external tools, so it answers in milliseconds (EXT-02: under 2 s on save). CI still runs the
real bundle (Semgrep + Conftest); `tests/test_lint.py` keeps the two in step.

Findings use 1-based `line` / `column`:

    {"rule": "ENT-001", "severity": "error", "message": "...", "path": "agent.py",
     "line": 12, "column": 7, "end_line": 12, "end_column": 40,
     "fix": {"description": "...", "edits": [{"line": 12, "column": 7, "end_line": 12, "end_column": 40,
                                              "new_text": "get_model()"}],
             "imports": ["from ent_agent_sdk.models import get_model"]}}

`fix` is optional. Inline `nosem` comments are deliberately not honoured: exceptions go in `waivers.yaml`.
"""

from __future__ import annotations

import ast
import json
import re
import tomllib
from dataclasses import asdict, dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import Any

from ent_agent_sdk import manifest as manifest_mod

PROVIDER_MODELS = {
    "ChatOpenAI", "AzureChatOpenAI", "ChatAnthropic", "ChatBedrock", "ChatBedrockConverse",
    "ChatGoogleGenerativeAI", "ChatVertexAI", "ChatOllama", "init_chat_model",
}
RAW_CLIENTS = {"OpenAI", "AsyncOpenAI", "Anthropic", "AsyncAnthropic"}
BEDROCK_SERVICES = {"bedrock-runtime", "bedrock-agent-runtime"}
PROVIDER_URL = re.compile(
    r"https?://(api\.openai\.com|api\.anthropic\.com|[a-z0-9.-]*bedrock-runtime[a-z0-9.-]*\.amazonaws\.com"
    r"|generativelanguage\.googleapis\.com|[a-z0-9-]+\.openai\.azure\.com)"
)
SECRET_KEY_NAME = re.compile(r"(?i).*(api[_-]?key|secret|token|password|credential).*")
OWN_SERVER = {"FastAPI", "Flask"}
RAW_TOOL_IMPORTS = {("langchain_core.tools", "tool"), ("langchain.tools", "tool"), ("langchain_core.tools", "StructuredTool")}

# Paths each rule ignores (mirrors the `paths.exclude` lists in policy/semgrep/ent-agent.yml).
ALWAYS_SKIP = {"ent_agent_sdk", "tests", ".venv", "node_modules", "__pycache__", ".git", "templates", "dist", "build"}
RULE_EXTRA_SKIP = {"ENT-006": {"harness"}, "ENT-003": {"policy"}}
SKIP_WALK = {".venv", "node_modules", "__pycache__", ".git", "dist", "build", ".pytest_cache"}

RULE_TITLES = {
    "ENT-001": "Don't construct provider chat models directly",
    "ENT-002": "Don't use raw provider clients",
    "ENT-003": "Don't hard-code model provider URLs",
    "ENT-004": "Don't read credentials from environment variables",
    "ENT-005": "Don't call Secrets Manager or SSM directly",
    "ENT-006": "Start the agent through EnterpriseAgentApp",
    "ENT-007": "Declare tools with @tools.tool",
    "ENT-008": "Guardrails disabled: needs an approved waiver",
    "ENT-009": "Tool approval switched off: needs an approved waiver",
}


@dataclass
class Finding:
    rule: str
    severity: str
    message: str
    path: str
    line: int
    column: int
    end_line: int
    end_column: int
    fix: dict[str, Any] | None = field(default=None)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if data["fix"] is None:
            del data["fix"]
        return data


def policy_data(name: str) -> dict[str, Any]:
    """Approved models / dependencies (copies of policy/data/*.json shipped with the SDK)."""
    return json.loads((files("ent_agent_sdk") / "policy_data" / name).read_text(encoding="utf-8"))


def _skipped(rule: str, path: str) -> bool:
    parts = set(Path(path.replace("\\", "/")).parts)
    return bool(parts & (ALWAYS_SKIP | RULE_EXTRA_SKIP.get(rule, set())))


def _callee_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _node_range(node: ast.AST) -> tuple[int, int, int, int]:
    return node.lineno, node.col_offset + 1, node.end_lineno or node.lineno, (node.end_col_offset or node.col_offset) + 1


def _str_arg(node: ast.Call, index: int = 0) -> str | None:
    if len(node.args) > index and isinstance(node.args[index], ast.Constant) and isinstance(node.args[index].value, str):
        return node.args[index].value
    return None


def check_source(source: str, path: str = "<source>") -> list[Finding]:
    """Apply the ENT code rules (ENT-001..008) to Python source."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []  # the editor already reports syntax errors
    findings: list[Finding] = []

    def add(rule: str, node: ast.AST, message: str, severity: str = "error", fix: dict[str, Any] | None = None) -> None:
        if _skipped(rule, path):
            return
        line, col, end_line, end_col = _node_range(node)
        findings.append(Finding(rule, severity, f"{rule}: {message}", path, line, col, end_line, end_col, fix))

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _callee_name(node)
            line, col, end_line, end_col = _node_range(node)
            if name in PROVIDER_MODELS:
                add("ENT-001", node, 'Use get_model() from ent_agent_sdk.models (the model comes from agent.yaml) so all model traffic goes through the gateway.',
                    fix={"description": "Replace with get_model() (uses the model listed in agent.yaml)",
                         "edits": [{"line": line, "column": col, "end_line": end_line, "end_column": end_col,
                                    "new_text": "get_model()"}],
                         "imports": ["from ent_agent_sdk.models import get_model"]})
            if name in RAW_CLIENTS or (name == "client" and _str_arg(node) in BEDROCK_SERVICES):
                add("ENT-002", node, "Don't use raw provider clients; model calls go through get_model(...) and the gateway.")
            if isinstance(node.func, ast.Attribute):
                owner = node.func.value
                is_os_environ_get = (
                    node.func.attr == "get" and isinstance(owner, ast.Attribute) and owner.attr == "environ"
                    and isinstance(owner.value, ast.Name) and owner.value.id == "os"
                )
                is_getenv = node.func.attr == "getenv" and isinstance(owner, ast.Name) and owner.id == "os"
                if (is_os_environ_get or is_getenv) and (key := _str_arg(node)) and SECRET_KEY_NAME.match(key):
                    add("ENT-004", node, 'Use secrets.get("<name>") from ent_agent_sdk instead of reading credentials from the environment.',
                        fix={"description": f'Replace with secrets.get("{key.lower().replace("_", "-")}")',
                             "edits": [{"line": line, "column": col, "end_line": end_line, "end_column": end_col,
                                        "new_text": f'secrets.get("{key.lower().replace("_", "-")}")'}],
                             "imports": ["from ent_agent_sdk import secrets"]})
                if node.func.attr == "get_secret_value" or (node.func.attr == "client" and _str_arg(node) == "secretsmanager"):
                    add("ENT-005", node, 'Use secrets.get("<name>") instead of calling Secrets Manager directly.')
                if node.func.attr == "get_parameter" and any(
                    k.arg == "WithDecryption" and isinstance(k.value, ast.Constant) and k.value.value is True for k in node.keywords
                ):
                    add("ENT-005", node, 'Use secrets.get("ssm:/path") instead of calling SSM directly.')
                if node.func.attr == "run" and isinstance(owner, ast.Name) and owner.id == "uvicorn":
                    add("ENT-006", node, "Start the agent through EnterpriseAgentApp, not your own web server.")
                if node.func.attr == "off" and isinstance(owner, ast.Name) and owner.id == "Guardrails":
                    add("ENT-008", node, "Guardrails are disabled here; this needs an approved, unexpired waiver in waivers.yaml.", "warning")
            if name == "tool" and any(
                k.arg == "requires_approval" and isinstance(k.value, ast.Constant) and k.value.value is False
                for k in node.keywords
            ):
                add("ENT-009", node, "Switching off tool approval needs an approved ENT-009 waiver in waivers.yaml.", "warning")
            if name in OWN_SERVER:
                add("ENT-006", node, "Start the agent through EnterpriseAgentApp, not your own web server.")
        elif isinstance(node, ast.Subscript):  # os.environ["..._KEY"]
            target = node.value
            key_node = node.slice
            if (
                isinstance(target, ast.Attribute) and target.attr == "environ" and isinstance(target.value, ast.Name)
                and target.value.id == "os" and isinstance(key_node, ast.Constant) and isinstance(key_node.value, str)
                and SECRET_KEY_NAME.match(key_node.value)
            ):
                line, col, end_line, end_col = _node_range(node)
                name = key_node.value.lower().replace("_", "-")
                add("ENT-004", node, 'Use secrets.get("<name>") from ent_agent_sdk instead of reading credentials from the environment.',
                    fix={"description": f'Replace with secrets.get("{name}")',
                         "edits": [{"line": line, "column": col, "end_line": end_line, "end_column": end_col,
                                    "new_text": f'secrets.get("{name}")'}],
                         "imports": ["from ent_agent_sdk import secrets"]})
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and PROVIDER_URL.search(node.value):
            add("ENT-003", node, "Don't hard-code model provider URLs; all LLM traffic goes through the gateway.")
        elif isinstance(node, ast.ImportFrom) and node.module:
            if any((node.module, alias.name) in RAW_TOOL_IMPORTS for alias in node.names):
                add("ENT-007", node, "Declare tools with @tools.tool(owner=..., data_classification=...) from ent_agent_sdk, not the raw LangChain tool.")
    findings.sort(key=lambda f: (f.line, f.column, f.rule))
    return findings


def _normalise(name: str) -> str:
    return name.lower().replace("_", "-")


def _pinned_by_sdk(name: str) -> bool:
    return name.startswith(("langchain", "langgraph")) or name in {"openai", "anthropic", "boto3", "botocore"}


def check_pyproject(text: str, path: str = "pyproject.toml") -> list[Finding]:
    """Dependency allowlist (ENT-010..012)."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return []
    deps = data.get("project", {}).get("dependencies", [])
    approved = set(policy_data("approved-dependencies.json")["approved_dependencies"])
    lines = text.splitlines()

    def locate(dep: str) -> tuple[int, int, int]:
        for index, line in enumerate(lines, start=1):
            if dep in line:
                start = line.index(dep) + 1
                return index, start, start + len(dep)
        return 1, 1, 1

    findings: list[Finding] = []
    names = {}
    for dep in deps:
        match = re.match(r"[A-Za-z0-9_.-]+", dep)
        if match:
            names[_normalise(match.group())] = dep
    if "ent-agent-sdk" not in names:
        findings.append(Finding("ENT-010", "error", "ENT-010: Agents must depend on 'ent-agent-sdk'.", path, 1, 1, 1, 1))
    for name, dep in names.items():
        line, col, end = locate(dep)
        if _pinned_by_sdk(name):
            findings.append(Finding("ENT-011", "error",
                                    f"ENT-011: Don't depend on '{name}' directly: ent-agent-sdk pins the approved version.",
                                    path, line, col, line, end))
        elif name != "ent-agent-sdk" and name not in approved:
            findings.append(Finding("ENT-012", "error",
                                    f"ENT-012: Dependency '{name}' is not on the approved list. Request approval from the platform team.",
                                    path, line, col, line, end))
    return findings


def check_manifest_text(text: str, path: str = "agent.yaml") -> list[Finding]:
    """Manifest rules (ENT-020..023) with 1-based positions of the offending key where possible."""
    import yaml

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return [Finding("ENT-020", "error", "ENT-020: agent.yaml is not valid YAML.", path, 1, 1, 1, 1)]
    lines = text.splitlines()

    def at(key: str) -> tuple[int, int, int]:
        for index, line in enumerate(lines, start=1):
            if re.match(rf"\s*{re.escape(key)}\s*:", line):
                return index, 1, len(line) + 1
        return 1, 1, 1

    findings = []
    for problem in manifest_mod.validate(data):
        key = next((k for k in ("data_classification", "guardrail_profile", "team", "name", "models", "tools", "secrets") if f"'{k}'" in problem), None)
        if "restricted" in problem and "strict" in problem:
            key, rule = "guardrail_profile", "ENT-021"
        else:
            rule = "ENT-020"
        line, col, end = at(key) if key else (1, 1, 1)
        findings.append(Finding(rule, "error", f"{rule}: {problem}", path, line, col, line, end))
    if isinstance(data, dict):
        approved = set(policy_data("approved-models.json")["approved_models"])
        restricted = set(policy_data("approved-models.json")["restricted_models"])
        for model in data.get("models") or []:
            line, col, end = at("models")
            for index, text_line in enumerate(lines, start=1):
                if re.search(rf"\b{re.escape(str(model))}\b", text_line) and index > line:
                    line, col, end = index, 1, len(text_line) + 1
                    break
            if model not in approved:
                findings.append(Finding("ENT-022", "error", f"ENT-022: Model '{model}' is not in the approved registry ({', '.join(sorted(approved))}).",
                                        path, line, col, line, end))
            elif data.get("data_classification") == "restricted" and model not in restricted:
                findings.append(Finding("ENT-023", "error", f"ENT-023: Model '{model}' is not approved for restricted data.",
                                        path, line, col, line, end))
    return findings


def check_file(path: str | Path) -> list[Finding]:
    path = Path(path)
    name = path.name
    text = path.read_text(encoding="utf-8", errors="replace")
    if name.endswith(".py"):
        return check_source(text, str(path))
    if name == "pyproject.toml":
        return check_pyproject(text, str(path))
    if name in ("agent.yaml", "agent.yml"):
        return check_manifest_text(text, str(path))
    return []


def check_paths(paths: list[str | Path]) -> list[Finding]:
    """Check files and directories (recursively, skipping virtualenvs, caches and build output)."""
    findings: list[Finding] = []
    for root in map(Path, paths):
        if root.is_file():
            findings.extend(check_file(root))
            continue
        for candidate in sorted(root.rglob("*")):
            if candidate.is_file() and not (SKIP_WALK & set(candidate.parts)) and (
                candidate.suffix == ".py" or candidate.name in ("pyproject.toml", "agent.yaml", "agent.yml")
            ):
                findings.extend(check_file(candidate))
    return findings


def to_json(findings: list[Finding]) -> str:
    return json.dumps([f.to_dict() for f in findings], indent=2)
