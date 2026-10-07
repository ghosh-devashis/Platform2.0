# Dependency allowlist (POL-01), for pyproject.toml. Run:
#   conftest test --policy policy/opa --data policy/data --namespace dependencies pyproject.toml
package dependencies

import rego.v1

# Names of the direct dependencies, normalised (lower case, '_' -> '-').
dep_names contains name if {
	some dep in input.project.dependencies
	raw := regex.find_n(`^[A-Za-z0-9_.-]+`, dep, 1)[0]
	name := replace(lower(raw), "_", "-")
}

# Libraries the SDK pins to approved versions: agents must not depend on them directly.
pinned_by_sdk(name) if startswith(name, "langchain")

pinned_by_sdk(name) if startswith(name, "langgraph")

pinned_by_sdk(name) if name in {"openai", "anthropic", "boto3", "botocore"}

deny contains msg if {
	not "ent-agent-sdk" in dep_names
	msg := "ENT-010: Agents must depend on 'ent-agent-sdk'."
}

deny contains msg if {
	some name in dep_names
	pinned_by_sdk(name)
	msg := sprintf("ENT-011: Don't depend on '%s' directly: ent-agent-sdk pins the approved version. Remove it from pyproject.toml.", [name])
}

deny contains msg if {
	some name in dep_names
	name != "ent-agent-sdk"
	not pinned_by_sdk(name)
	not name in data.approved_dependencies
	msg := sprintf("ENT-012: Dependency '%s' is not on the approved list (policy/data/approved-dependencies.json). Request approval from the platform team.", [name])
}
