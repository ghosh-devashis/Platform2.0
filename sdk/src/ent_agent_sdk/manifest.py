"""The agent manifest, `agent.yaml` (TPL-02): what an agent is, who owns it and what it may use.

    name: crm-agent                  # lowercase letters, digits, hyphens
    team: sales-platform             # owning team
    data_classification: confidential   # public | internal | confidential | restricted
    guardrail_profile: standard      # standard | strict (restricted data requires strict)
    description: Answers CRM questions.
    models: [chat-default]           # logical model names (approved registry); the first is the default model
    tools: [lookup_customer]         # registered tool names
    secrets: [crm/api]               # secrets the agent reads via secrets.get()

`models:` is the one place that says which model the agent uses: `get_model()` with no name returns the first entry,
so the model is never repeated (and never drifts) in agent code.

The same rules run in the IDE, pre-commit and CI: this module (Python), `agent.schema.json` (editors) and
`policy/opa/manifest.rego` (Conftest). Keep them in step.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,47}$")
CLASSIFICATIONS = ("public", "internal", "confidential", "restricted")
GUARDRAIL_PROFILES = ("standard", "strict")
_KEYS = {"name", "team", "data_classification", "guardrail_profile", "description", "models", "tools", "secrets",
         "token_budget", "resources", "memory"}
_LISTS = ("models", "tools", "secrets")
MEMORY_BACKENDS = ("memory", "dynamodb")
BUCKET_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.-]{2,62}$")
RESOURCE_KEYS = {"buckets", "tables", "memories"}


class ManifestError(ValueError):
    """The manifest is invalid. `problems` lists every issue found."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        super().__init__("Invalid agent manifest:\n- " + "\n- ".join(self.problems))


@dataclass(frozen=True)
class AgentManifest:
    name: str
    team: str
    data_classification: str
    guardrail_profile: str
    description: str = ""
    models: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    secrets: tuple[str, ...] = ()
    token_budget: dict[str, int] = field(default_factory=dict)  # {"soft": n, "hard": n}
    resources: dict[str, Any] = field(default_factory=dict)  # {"buckets": [...], "tables": [...], "memories": [...]}
    memory: dict[str, str] = field(default_factory=dict)  # {"backend": "memory"|"dynamodb", "table": "..."}

    @property
    def runtime_name(self) -> str:
        """The AgentCore runtime name: letters, digits and underscores only (no hyphens)."""
        return self.name.replace("-", "_")


def validate(data: Any) -> list[str]:
    """All problems with a parsed manifest (empty list if valid)."""
    if not isinstance(data, dict):
        return ["The manifest must be a mapping of keys to values."]
    problems = []
    for unknown in sorted(set(data) - _KEYS):
        problems.append(f"Unknown key '{unknown}'.")
    name = data.get("name")
    if not isinstance(name, str) or not NAME_PATTERN.match(name):
        problems.append("'name' is required: 2-48 characters, lowercase letters, digits and hyphens, starting with a letter.")
    if not isinstance(data.get("team"), str) or not data["team"].strip():
        problems.append("'team' is required (the owning team).")
    classification = data.get("data_classification")
    if classification not in CLASSIFICATIONS:
        problems.append(f"'data_classification' is required and must be one of {', '.join(CLASSIFICATIONS)}.")
    profile = data.get("guardrail_profile")
    if profile is not None and profile not in GUARDRAIL_PROFILES:
        problems.append(f"'guardrail_profile' must be one of {', '.join(GUARDRAIL_PROFILES)}.")
    if classification == "restricted" and profile != "strict":
        problems.append("Agents handling 'restricted' data must set guardrail_profile: strict.")
    for key in _LISTS:
        value = data.get(key, [])
        if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
            problems.append(f"'{key}' must be a list of non-empty strings.")
        elif len(set(value)) != len(value):
            problems.append(f"'{key}' contains duplicates.")
    if "description" in data and not isinstance(data["description"], str):
        problems.append("'description' must be text.")
    problems += _validate_token_budget(data.get("token_budget"))
    problems += _validate_resources(data.get("resources"))
    problems += _validate_memory(data.get("memory"))
    return problems


def _validate_token_budget(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, dict) or not value or set(value) - {"soft", "hard"}:
        return ["'token_budget' must set 'soft' and/or 'hard' (whole numbers of tokens)."]
    if not all(isinstance(v, int) and not isinstance(v, bool) and v > 0 for v in value.values()):
        return ["'token_budget' limits must be positive whole numbers."]
    if "soft" in value and "hard" in value and value["soft"] > value["hard"]:
        return ["'token_budget': 'soft' must not exceed 'hard'."]
    return []


def _validate_resources(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, dict) or set(value) - RESOURCE_KEYS:
        return [f"'resources' may only contain {', '.join(sorted(RESOURCE_KEYS))}."]
    problems = []
    for bucket in value.get("buckets", []):
        if not isinstance(bucket, str) or not BUCKET_PATTERN.match(bucket):
            problems.append(f"'resources.buckets': '{bucket}' is not a valid bucket name (3-63 lowercase letters, digits, '.', '-').")
    for table in value.get("tables", []):
        if not (isinstance(table, dict) and isinstance(table.get("name"), str) and isinstance(table.get("partition_key"), str)
                and set(table) <= {"name", "partition_key", "sort_key"}):
            problems.append("'resources.tables' entries need 'name' and 'partition_key' (optional 'sort_key').")
    for memory in value.get("memories", []):
        if not isinstance(memory, str) or not re.match(r"^[A-Za-z][A-Za-z0-9_]{0,47}$", memory):
            problems.append(f"'resources.memories': '{memory}' is not a valid memory name (letters, digits, underscores).")
    return problems


def _validate_memory(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, dict) or set(value) - {"backend", "table"} or value.get("backend") not in MEMORY_BACKENDS:
        return [f"'memory' needs 'backend' ({' or '.join(MEMORY_BACKENDS)}) and optionally 'table'."]
    if "table" in value and not (isinstance(value["table"], str) and value["table"].strip()):
        return ["'memory.table' must be a table name."]
    return []


def parse(data: Any) -> AgentManifest:
    problems = validate(data)
    if problems:
        raise ManifestError(problems)
    classification = data["data_classification"]
    return AgentManifest(
        name=data["name"],
        team=data["team"].strip(),
        data_classification=classification,
        guardrail_profile=data.get("guardrail_profile") or ("strict" if classification == "restricted" else "standard"),
        description=data.get("description", ""),
        models=tuple(data.get("models", [])),
        tools=tuple(data.get("tools", [])),
        secrets=tuple(data.get("secrets", [])),
        token_budget=dict(data.get("token_budget") or {}),
        resources=dict(data.get("resources") or {}),
        memory=dict(data.get("memory") or {}),
    )


MANIFEST_ENV = "AGENT_MANIFEST"


def default_path() -> Path:
    """Where this agent's manifest is: the `AGENT_MANIFEST` setting if present (containers set it), else `agent.yaml`
    in the working directory."""
    return Path(os.environ.get(MANIFEST_ENV) or "agent.yaml")


def load(path: str | Path = "agent.yaml") -> AgentManifest:
    """Read and validate a manifest file."""
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ManifestError([f"Manifest file '{path}' was not found."]) from None
    except yaml.YAMLError:
        raise ManifestError([f"Manifest file '{path}' is not valid YAML."]) from None
    return parse(data)
