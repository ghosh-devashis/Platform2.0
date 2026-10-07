# Manifest rules (POL-06), for agent.yaml. Run:
#   conftest test --policy policy/opa --data policy/data --namespace manifest agent.yaml
# Keep in step with ent_agent_sdk/manifest.py and agent.schema.json.
package manifest

import rego.v1

classifications := {"public", "internal", "confidential", "restricted"}

deny contains msg if {
	not input.name
	msg := "ENT-020: agent.yaml needs a 'name'."
}

deny contains msg if {
	not input.team
	msg := "ENT-020: agent.yaml needs a 'team' (the owning team)."
}

deny contains msg if {
	not input.data_classification in classifications
	msg := sprintf("ENT-020: 'data_classification' must be one of %s.", [concat(", ", sort([c | some c in classifications]))])
}

# Restricted data requires the strict guardrail profile.
deny contains msg if {
	input.data_classification == "restricted"
	input.guardrail_profile != "strict"
	msg := "ENT-021: Agents handling 'restricted' data must set guardrail_profile: strict."
}

# Only models from the approved registry (logical names).
deny contains msg if {
	some model in input.models
	not model in data.approved_models
	msg := sprintf("ENT-022: Model '%s' is not in the approved registry (approved: %s).", [model, concat(", ", data.approved_models)])
}

# Restricted agents may only use models approved for restricted data.
deny contains msg if {
	input.data_classification == "restricted"
	some model in input.models
	not model in data.restricted_models
	msg := sprintf("ENT-023: Model '%s' is not approved for restricted data (approved: %s).", [model, concat(", ", data.restricted_models)])
}
