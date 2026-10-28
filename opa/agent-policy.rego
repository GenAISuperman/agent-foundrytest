package agent.policy

import rego.v1

# Policy for agent.yaml in the Foundry template. One policy with a runtime-specific
# branch: the Foundry branch applies when agent.runtime is "foundry"; any other runtime
# is denied here (an AKS agent comes from the AKS template, not this one).
#
# Run: opa eval --format raw --input agent.yaml \
#        --data opa/agent-policy.rego 'data.agent.policy.deny'
# Every element of `deny` is a reason; an empty set means the file is valid.
# Test: opa test opa/ -v

placeholder := `\{\{[A-Za-z0-9_]+\}\}`

# --- helpers --------------------------------------------------------------

filled(value) if {
	is_string(value)
	trim_space(value) != ""
}

as_object(value) := value if is_object(value) else := {}

agent := as_object(object.get(input, "agent", {}))

foundry := as_object(object.get(input, "foundry", {}))

tools := object.get(input, "tools", [])

is_foundry if agent.runtime == "foundry"

# --- all runtimes ---------------------------------------------------------

# A {{PLACEHOLDER}} left anywhere in the file means provisioning did not finish.
deny contains msg if {
	walk(input, [path, value])
	is_string(value)
	regex.match(placeholder, value)
	where := concat(".", [sprintf("%v", [part]) | some part in path])
	msg := sprintf("placeholder not replaced: %s (at %s)", [value, where])
}

deny contains "agent.id is required" if not filled(agent.id)

deny contains "agent.team is required" if not filled(agent.team)

deny contains "prompts.active_version is required" if not filled(object.get(as_object(object.get(input, "prompts", {})), "active_version", null))

deny contains "prompts.source is required" if not filled(object.get(as_object(object.get(input, "prompts", {})), "source", null))

deny contains msg if {
	not is_foundry
	msg := sprintf("agent.runtime must be 'foundry' in this template (found %v)", [object.get(agent, "runtime", null)])
}

# --- Foundry branch -------------------------------------------------------

deny contains msg if {
	is_foundry
	some field in ["project_endpoint", "model_deployment", "agent_name"]
	not filled(object.get(foundry, field, null))
	msg := sprintf("foundry.%s is required", [field])
}

# An AKS-style identity block does not belong to a Foundry agent.
deny contains "identity block belongs to the aks runtime and must not be present when runtime is foundry" if {
	is_foundry
	"identity" in object.keys(input)
}

# --- tools ----------------------------------------------------------------

deny contains "tools must be a list" if not is_array(tools)

deny contains msg if {
	is_foundry
	is_array(tools)
	some i, tool in tools
	not is_object(tool)
	msg := sprintf("tools[%d] must be a mapping", [i])
}

deny contains msg if {
	is_foundry
	is_array(tools)
	some i, tool in tools
	is_object(tool)
	some field in ["id", "apim_base_url", "scope"]
	not filled(object.get(tool, field, null))
	msg := sprintf("tools[%d].%s is required", [i, field])
}

deny contains msg if {
	is_foundry
	is_array(tools)
	some i, tool in tools
	is_object(tool)
	not valid_roles(object.get(tool, "roles", null))
	msg := sprintf("tools[%d].roles must be a non-empty list of role names", [i])
}

# The agent sends its own bearer token to this URL: https only. Plain http is allowed
# for local runs against the mock tool server (localhost, 127.0.0.1, host.docker.internal).
deny contains msg if {
	is_foundry
	is_array(tools)
	some i, tool in tools
	is_object(tool)
	filled(object.get(tool, "apim_base_url", null))
	not allowed_base_url(tool.apim_base_url)
	msg := sprintf("tools[%d].apim_base_url must start with https:// (http:// is allowed only for localhost, 127.0.0.1, host.docker.internal)", [i])
}

allowed_base_url(url) if regex.match(`^https://[^/?#@\s]+`, lower(trim_space(url)))

allowed_base_url(url) if regex.match(`^http://(localhost|127\.0\.0\.1|host\.docker\.internal)(:[0-9]+)?([/?#]|$)`, lower(trim_space(url)))

# Two entries with the same id would offer the model colliding functions.
deny contains msg if {
	is_foundry
	is_array(tools)
	some i, tool in tools
	is_object(tool)
	filled(object.get(tool, "id", null))
	some j, earlier in tools
	j < i
	is_object(earlier)
	earlier.id == tool.id
	not duplicate_seen_before(i, j)
	msg := sprintf("tools[%d].id duplicates tools[%d].id ('%s')", [i, j, tool.id])
}

# Report each duplicate against its first occurrence only.
duplicate_seen_before(i, j) if {
	some k, middle in tools
	k < j
	is_object(middle)
	middle.id == tools[i].id
}

valid_roles(roles) if {
	is_array(roles)
	count(roles) > 0
	every role in roles {
		filled(role)
	}
}
