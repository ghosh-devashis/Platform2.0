"""The policy rules in one place: ID, title, why, how to fix, and a bad/good example.

Single source for `ent-agent rules`, the MCP `explain_rule` tool and `policy/docs/rules.md` (a test keeps the page in
step: regenerate it with `ent-agent rules --markdown > policy/docs/rules.md`). The checks themselves live in
`policy/` (Semgrep, OPA, Python tools) and `devtools/lint.py`.
"""

from __future__ import annotations

from typing import TypedDict


class Rule(TypedDict, total=False):
    id: str
    group: str
    title: str
    why: str
    fix: str
    bad: str
    good: str
    waivable: bool


RULES: list[Rule] = [
    {"id": "ENT-001", "group": "Code", "title": "No direct provider chat models",
     "why": "All LLM traffic must pass the gateway for routing, guardrails, audit and key custody.",
     "fix": "Get the model with `get_model()` from `ent_agent_sdk.models`: it uses the model listed under `models:` in `agent.yaml`, so the name is not repeated in code.",
     "bad": 'llm = ChatOpenAI(model="gpt-5")', "good": "llm = get_model()   # the model comes from agent.yaml"},
    {"id": "ENT-002", "group": "Code", "title": "No raw provider clients (`openai`, `anthropic`, `bedrock-runtime`)",
     "why": "Same as ENT-001: a raw client bypasses the gateway.",
     "fix": "Use `get_model(...)`; call tools and APIs through registered tools.",
     "bad": "client = openai.OpenAI()", "good": "llm = get_model()   # the model comes from agent.yaml"},
    {"id": "ENT-003", "group": "Code", "title": "No hard-coded provider URLs",
     "why": "A provider URL in code is a bypass of the gateway and a place for keys to leak.",
     "fix": "Remove it. The SDK knows the gateway URL (`ENT_MODEL_GATEWAY_URL`).",
     "bad": 'URL = "https://api.openai.com/v1"', "good": "# nothing: use get_model(...)"},
    {"id": "ENT-004", "group": "Code", "title": "No credentials read from environment variables",
     "why": "Secrets must not live in config or the process environment.",
     "fix": 'Fetch them at runtime with `secrets.get("name")`.',
     "bad": 'key = os.environ["CRM_API_KEY"]', "good": 'key = secrets.get("crm/api", key="api_key")'},
    {"id": "ENT-005", "group": "Code", "title": "No direct Secrets Manager / SSM calls",
     "why": "The SDK adds caching, timeouts and fail-closed behaviour.",
     "fix": 'Use `secrets.get("name")` or `secrets.get("ssm:/path")`.',
     "bad": 'boto3.client("secretsmanager").get_secret_value(SecretId="x")', "good": 'secrets.get("x")'},
    {"id": "ENT-006", "group": "Code", "title": "No own web server (`uvicorn.run`, `FastAPI`, `Flask`)",
     "why": "The AgentCore contract, tracing, guardrails and health live in the SDK.",
     "fix": "Create `EnterpriseAgentApp` (usually `from_manifest`) and call `.run()`.",
     "bad": "app = FastAPI()", "good": 'app = EnterpriseAgentApp.from_manifest(graph, "agent.yaml")'},
    {"id": "ENT-007", "group": "Code", "title": "No raw LangChain `@tool`",
     "why": "Tools need an owner, classification, guardrails, audit, timeouts and approval.",
     "fix": "Declare tools with `@tools.tool(owner=..., data_classification=...)`.",
     "bad": "from langchain_core.tools import tool", "good": "from ent_agent_sdk import tools"},
    {"id": "ENT-008", "group": "Code", "title": "Disabling guardrails needs a waiver (warning)",
     "why": "Turning guardrails off is a security exception.", "waivable": True,
     "fix": "Remove it, or add an approved waiver in `waivers.yaml` and reference its ID.",
     "bad": "Guardrails.off(waiver='')", "good": 'Guardrails.off(waiver="WAIVER-2026-014")  # approved in waivers.yaml'},
    {"id": "ENT-009", "group": "Code", "title": "Switching off tool approval needs a waiver (warning)",
     "why": "Tools with side effects pause for human approval by default; skipping it is a security exception.",
     "waivable": True, "fix": "Keep the default, or add an approved ENT-009 waiver and reference it.",
     "bad": "@tools.tool(owner='a', side_effects=True, requires_approval=False, approval_waiver='W-1')",
     "good": "@tools.tool(owner='a', side_effects=True)  # pauses for approval"},
    {"id": "ENT-010", "group": "Dependencies", "title": "`ent-agent-sdk` is required",
     "why": "The SDK carries the standards (tracing, guardrails, secrets, gateway access).",
     "fix": 'Add `"ent-agent-sdk"` to `[project] dependencies`.',
     "bad": 'dependencies = ["requests"]', "good": 'dependencies = ["ent-agent-sdk", "requests"]'},
    {"id": "ENT-011", "group": "Dependencies", "title": "No direct `langchain*`, `langgraph*`, `openai`, `anthropic`, `boto3`",
     "why": "The SDK pins the approved versions; a direct pin can conflict or bypass them.",
     "fix": "Remove it from pyproject.toml; import what you need through the SDK's environment.",
     "bad": 'dependencies = ["ent-agent-sdk", "langchain-core>=1"]', "good": 'dependencies = ["ent-agent-sdk"]'},
    {"id": "ENT-012", "group": "Dependencies", "title": "Other dependencies must be on the approved list",
     "why": "Every dependency is supply-chain risk and needs review.",
     "fix": "Request approval from the platform team (`policy/data/approved-dependencies.json`).",
     "bad": 'dependencies = ["ent-agent-sdk", "leftpad"]', "good": 'dependencies = ["ent-agent-sdk", "httpx"]'},
    {"id": "ENT-020", "group": "Manifest", "title": "`name`, `team` and a valid `data_classification` are required",
     "why": "Ownership and data sensitivity drive routing, guardrails and audit.",
     "fix": "Fill them in `agent.yaml`.", "bad": "name: a", "good": "name: crm-agent\nteam: sales\ndata_classification: internal"},
    {"id": "ENT-021", "group": "Manifest", "title": "`restricted` data requires `guardrail_profile: strict`",
     "why": "Restricted data must never get the lenient profile.", "fix": "Set `guardrail_profile: strict`.",
     "bad": "data_classification: restricted\nguardrail_profile: standard",
     "good": "data_classification: restricted\nguardrail_profile: strict"},
    {"id": "ENT-022", "group": "Manifest", "title": "Models must be in the approved registry",
     "why": "Only reviewed models may be used.", "fix": "Use a logical name from `policy/data/approved-models.json`.",
     "bad": "models: [gpt-9]", "good": "models: [chat-default]"},
    {"id": "ENT-023", "group": "Manifest", "title": "Restricted agents may only use models approved for restricted data",
     "why": "Not every approved model is cleared for restricted data.", "fix": "Pick a model listed under `restricted_models`.",
     "bad": "data_classification: restricted\nmodels: [chat-fast]", "good": "data_classification: restricted\nmodels: [chat-default]"},
    {"id": "ENT-030", "group": "Waivers and pipeline", "title": "Every waiver needs `id`, `rule`, `reason`, `approver`, `expires`",
     "why": "Exceptions must be approved and time-boxed.", "fix": "Complete the waiver entry.",
     "bad": "- id: W-1\n  rule: ENT-008", "good": "- id: W-1\n  rule: ENT-008\n  reason: eval\n  approver: a@b.com\n  expires: 2026-12-31"},
    {"id": "ENT-031", "group": "Waivers and pipeline", "title": "Expired waivers fail the build",
     "why": "Exceptions must end.", "fix": "Fix the code, or renew the waiver with a new approval.",
     "bad": "expires: 2026-01-01", "good": "expires: 2026-12-31"},
    {"id": "ENT-032", "group": "Waivers and pipeline", "title": "Waiver references in code must be approved and unexpired",
     "why": "`Guardrails.off(waiver=...)` / `approval_waiver=...` must name a real waiver for the right rule (ENT-008 / ENT-009).",
     "fix": "Add the approved waiver to `waivers.yaml`, or remove the exception.",
     "bad": 'Guardrails.off(waiver="W-404")', "good": "# W-404 exists in waivers.yaml, rule ENT-008, not expired"},
    {"id": "ENT-033", "group": "Waivers and pipeline", "title": "No inline `nosem` suppressions",
     "why": "Suppressions hide violations from review.", "fix": "Use `waivers.yaml`.",
     "bad": "llm = ChatOpenAI()  # nosem", "good": "# fix the code, or add a waiver"},
    {"id": "ENT-040", "group": "Waivers and pipeline", "title": "CI must call the platform pipeline",
     "why": "The gates only protect repos that run them.", "fix": "Call `agent-ci.yml@<tag>` from `.github/workflows`.",
     "bad": "# custom pipeline", "good": "uses: org/platform2.0/.github/workflows/agent-ci.yml@v0.1.0"},
    {"id": "ENT-041", "group": "Waivers and pipeline", "title": "Pin the pipeline to a release tag or commit SHA",
     "why": "A branch can change under you.", "fix": "Use a tag like `@v0.1.0` or a SHA.",
     "bad": "uses: org/platform2.0/.github/workflows/agent-ci.yml@main", "good": "uses: org/platform2.0/.github/workflows/agent-ci.yml@v0.1.0"},
    {"id": "ENT-042", "group": "Waivers and pipeline", "title": "The pipeline job can't be conditional or allowed to fail",
     "why": "A skippable gate is not a gate.", "fix": "Remove `if:` and `continue-on-error`.",
     "bad": "if: github.ref == 'refs/heads/main'", "good": "# no condition"},
    {"id": "ENT-043", "group": "Waivers and pipeline", "title": "CI must run on pull requests",
     "why": "Problems must be caught before merge.", "fix": "Add `pull_request` to `on:`.",
     "bad": "on: [push]", "good": "on: [push, pull_request]"},
    {"id": "ENT-060", "group": "Waivers and pipeline", "title": "Release past its sunset or no longer supported",
     "why": "Old SDK and policy releases stop getting fixes; after the sunset date the build fails (N-1 support, 90-day sunset).",
     "fix": "Upgrade `ent-agent-sdk` / the pipeline tag to a supported release (see `policy/data/versions.json`).",
     "bad": "uses: org/platform2.0/.github/workflows/agent-ci.yml@v0.1.0   # sunset 2027-01-01, today is later",
     "good": "uses: org/platform2.0/.github/workflows/agent-ci.yml@v0.2.0"},
    {"id": "ENT-061", "group": "Waivers and pipeline", "title": "Release sunsets soon (warning)",
     "why": "Plan the upgrade before the sunset date turns this into a build failure.", "fix": "Upgrade to the latest release.",
     "bad": "ent-agent-sdk 0.1.x with a sunset in 30 days", "good": "ent-agent-sdk 0.2.x"},
    {"id": "ENT-062", "group": "Waivers and pipeline", "title": "Could not determine the SDK or pipeline version (warning)",
     "why": "The deprecation check needs `uv.lock` (or a pinned `ent-agent-sdk`) and a pinned pipeline tag.",
     "fix": "Commit `uv.lock` and call the pipeline with a tag.", "bad": "no uv.lock, unpinned dependency", "good": "uv.lock committed"},    {"id": "ENT-050", "group": "Waivers and pipeline", "title": "Dependency licences must not match denied patterns",
     "why": "Some licences (AGPL, SSPL, non-commercial) are incompatible with our use.", "fix": "Replace the dependency or get a licence exception.",
     "bad": "a dependency licensed AGPL-3.0", "good": "an MIT / BSD / Apache-2.0 dependency"},
]

BY_ID = {rule["id"]: rule for rule in RULES}


def get(rule_id: str) -> Rule | None:
    return BY_ID.get(rule_id.upper())


def render_markdown() -> str:
    """The rules page (`policy/docs/rules.md`)."""
    lines = [
        "# Policy rules", "",
        "One versioned bundle (`policy/VERSION`) runs in three places: the IDE (the VS Code extension), pre-commit",
        "(`policy/run-checks.sh --fast`) and CI (the gate that can't be skipped). Every failure names a rule ID, the reason",
        "and the fix. Exceptions go in `waivers.yaml` (approved, time-boxed). Inline `nosem` suppressions are not accepted.", "",
        "<!-- Generated by `ent-agent rules --markdown` from ent_agent_sdk/devtools/rules_catalog.py: edit the catalogue. -->", "",
    ]
    group = None
    for rule in RULES:
        if rule["group"] != group:
            group = rule["group"]
            lines += [f"## {group}", ""]
        lines += [f'### <a id="{rule["id"].lower()}"></a>{rule["id"]}: {rule["title"]}', "",
                  f'**Why:** {rule["why"]}', "", f'**Fix:** {rule["fix"]}', ""]
        if rule.get("waivable"):
            lines += ["**Exceptions:** allowed only with an approved, unexpired waiver in `waivers.yaml`.", ""]
        lines += ["```text", "# bad", rule["bad"], "", "# good", rule["good"], "```", ""]
    lines += [
        "## Other gates (in the pipeline, `.github/workflows/agent-ci.yml`)", "",
        "Secret scanning (gitleaks), dependency vulnerabilities (pip-audit) and SAST (Semgrep) with severity thresholds,",
        "SBOM (CycloneDX) and licence check, unit tests and local-runtime integration tests in replay mode, the evaluation",
        "gate (golden questions with minimum scores), SDK/policy version support (deprecation) and image signing.", "",
    ]
    return "\n".join(lines)
