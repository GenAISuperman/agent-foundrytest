package agent.policy_test

import rego.v1

import data.agent.policy

valid := {
	"agent": {
		"id": "shopping",
		"team": "retail-eng",
		"runtime": "foundry",
		"interaction_model": "standalone",
		"guardrail_profile": "strict",
	},
	"foundry": {
		"project_endpoint": "https://example.services.ai.azure.com/api/projects/demo",
		"model_deployment": "gpt-5.4",
		"agent_name": "shopping",
	},
	"tools": [{
		"id": "mock-shop-api",
		"roles": ["read"],
		"apim_base_url": "https://apim.example.com",
		"scope": "api://abc/.default",
	}],
	"output_policy": {"violation_threshold": 3, "window_minutes": 10, "action": "alert", "notify_webhook": "https://example.invalid/hook"},
	"prompts": {"active_version": "v1.0", "source": "prompts/system.txt"},
}

# True when some denial message contains `fragment`.
denied_with(input_doc, fragment) if {
	some msg in policy.deny with input as input_doc
	contains(msg, fragment)
}

first_tool_without(field) := object.union(valid, {"tools": [object.remove(valid.tools[0], [field])]})

test_valid_file_passes if {
	count(policy.deny) == 0 with input as valid
}

test_valid_file_with_no_tools_passes if {
	count(policy.deny) == 0 with input as object.union(valid, {"tools": []})
}

test_leftover_placeholder_is_denied if {
	doc := object.union(valid, {"agent": {"team": "doundryguy"}})
	denied_with(doc, "placeholder not replaced: doundryguy")
}

test_leftover_placeholder_in_foundry_block_is_denied if {
	doc := object.union(valid, {"foundry": {"model_deployment": "gpt-5.4"}})
	denied_with(doc, "gpt-5.4")
}

test_tool_missing_id_is_denied if {
	denied_with(first_tool_without("id"), "tools[0].id is required")
}

test_tool_missing_roles_is_denied if {
	denied_with(first_tool_without("roles"), "tools[0].roles")
}

test_tool_with_empty_roles_is_denied if {
	doc := object.union(valid, {"tools": [object.union(valid.tools[0], {"roles": []})]})
	denied_with(doc, "tools[0].roles")
}

test_tool_missing_apim_base_url_is_denied if {
	denied_with(first_tool_without("apim_base_url"), "tools[0].apim_base_url is required")
}

test_tool_missing_scope_is_denied if {
	denied_with(first_tool_without("scope"), "tools[0].scope is required")
}

test_tool_error_names_the_offending_entry if {
	second := object.remove(object.union(valid.tools[0], {"id": "other-api"}), ["scope"])
	doc := object.union(valid, {"tools": [valid.tools[0], second]})
	denied_with(doc, "tools[1].scope is required")
	not denied_with(doc, "tools[0]")
}

test_non_foundry_runtime_is_denied if {
	doc := object.union(valid, {"agent": {"runtime": "aks"}})
	denied_with(doc, "agent.runtime must be 'foundry'")
}

test_aks_identity_block_is_denied if {
	doc := object.union(valid, {"identity": {"sp_client_id": "abc", "keyvault_url": "https://kv.example"}})
	denied_with(doc, "identity block belongs to the aks runtime")
}

test_missing_foundry_block_is_denied if {
	doc := object.remove(valid, ["foundry"])
	denied_with(doc, "foundry.project_endpoint is required")
}

with_url(url) := object.union(valid, {"tools": [object.union(valid.tools[0], {"apim_base_url": url})]})

test_http_base_url_is_denied if {
	denied_with(with_url("http://apim.example.com/tools/x"), "tools[0].apim_base_url must start with https://")
}

test_lookalike_local_host_is_denied if {
	denied_with(with_url("http://localhost.evil.example/tools/x"), "apim_base_url must start with https://")
	denied_with(with_url("http://localhost@evil.example/tools/x"), "apim_base_url must start with https://")
	denied_with(with_url("http://127.0.0.1.evil.example/"), "apim_base_url must start with https://")
}

test_schemeless_and_other_schemes_are_denied if {
	denied_with(with_url("apim.example.com/tools/x"), "apim_base_url must start with https://")
	denied_with(with_url("ftp://apim.example.com"), "apim_base_url must start with https://")
}

test_https_base_url_passes if {
	count(policy.deny) == 0 with input as with_url("https://apim.example.com/tools/x")
}

test_http_is_allowed_for_local_runs if {
	count(policy.deny) == 0 with input as with_url("http://localhost:8001/tools/x")
	count(policy.deny) == 0 with input as with_url("http://127.0.0.1:8001/tools/x")
	count(policy.deny) == 0 with input as with_url("http://host.docker.internal:8001/tools/x")
	count(policy.deny) == 0 with input as with_url("http://localhost")
}

test_duplicate_tool_ids_are_denied if {
	doc := object.union(valid, {"tools": [valid.tools[0], valid.tools[0]]})
	denied_with(doc, "tools[1].id duplicates tools[0].id")
}

test_distinct_tool_ids_pass if {
	second := object.union(valid.tools[0], {"id": "other-api"})
	count(policy.deny) == 0 with input as object.union(valid, {"tools": [valid.tools[0], second]})
}
