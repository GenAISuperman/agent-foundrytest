import os
import subprocess
import sys

import pytest

from src.config import ConfigError, load_config
from tests.support import REPO_ROOT, TEMPLATE_AGENT_YAML, fill_placeholders, render_agent_dir


def start_main(agent_yaml, extra_env=None):
    env = {**os.environ, "AGENT_YAML_PATH": str(agent_yaml), "PYTHONPATH": str(REPO_ROOT)}
    env.pop("FOUNDRY_PROJECT_ENDPOINT", None)
    env.pop("AZURE_AI_MODEL_DEPLOYMENT_NAME", None)
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, "-m", "src.main"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def rendered(tmp_path, edit=lambda text: text):
    path = render_agent_dir(tmp_path)
    path.write_text(edit(path.read_text()))
    return path


def assert_clear_failure(result, *expected_fragments):
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "Traceback" not in output
    assert "Invalid agent.yaml" in output
    for fragment in expected_fragments:
        assert fragment in output


def test_leftover_placeholder_fails_startup_naming_the_field(tmp_path):
    path = rendered(tmp_path, lambda t: t.replace("retail-eng", "{{TEAM_NAME}}"))
    assert_clear_failure(start_main(path), "{{TEAM_NAME}}", "placeholder")


def test_non_foundry_runtime_fails_startup(tmp_path):
    path = rendered(tmp_path, lambda t: t.replace("runtime: foundry", "runtime: aks"))
    assert_clear_failure(start_main(path), "agent.runtime", "foundry")


def test_missing_model_deployment_fails_startup(tmp_path):
    path = rendered(
        tmp_path, lambda t: t.replace('  model_deployment: "gpt-5.4"\n', "")
    )
    assert_clear_failure(start_main(path), "foundry.model_deployment")


def test_missing_foundry_block_fails_startup(tmp_path):
    def drop_block(text):
        head, _, rest = text.partition("foundry:\n")
        _, _, tail = rest.partition("\ntools:")
        return head + "tools:" + tail

    assert_clear_failure(start_main(rendered(tmp_path, drop_block)), "foundry")


def test_malformed_yaml_fails_startup(tmp_path):
    path = rendered(tmp_path, lambda t: "agent: [unclosed\n" + t)
    assert_clear_failure(start_main(path), "not valid YAML")


def test_missing_file_fails_startup(tmp_path):
    assert_clear_failure(start_main(tmp_path / "nope.yaml"), "not found")


def test_missing_prompt_file_fails_startup(tmp_path):
    path = rendered(tmp_path)
    (tmp_path / "prompts" / "system.txt").unlink()
    assert_clear_failure(start_main(path), "prompts.source")


def test_unfilled_template_itself_is_rejected(tmp_path):
    # The template's agent.yaml still has placeholders: it must never start.
    result = start_main(TEMPLATE_AGENT_YAML)
    assert_clear_failure(result, "placeholder")


GOOD_TOOL = {
    "id": "mock-shop-api",
    "roles": ["read"],
    "apim_base_url": "https://apim.example.com",
    "scope": "api://abc/.default",
}


def with_tools(tmp_path, tools):
    return render_agent_dir(tmp_path, tools=tools)


@pytest.mark.parametrize("field", ["id", "roles", "apim_base_url", "scope"])
def test_tool_entry_missing_a_field_fails_startup_naming_it(tmp_path, field):
    tool = {k: v for k, v in GOOD_TOOL.items() if k != field}
    assert_clear_failure(
        start_main(with_tools(tmp_path, [tool])), f"tools[0].{field}"
    )


def test_tool_entry_with_empty_roles_fails_startup(tmp_path):
    tool = {**GOOD_TOOL, "roles": []}
    assert_clear_failure(start_main(with_tools(tmp_path, [tool])), "tools[0].roles")


# These call load_config directly (what src.main calls first): when validation wrongly
# passes, a subprocess would go on to serve forever.


def config_error(tmp_path, tools) -> str:
    with pytest.raises(ConfigError) as caught:
        load_config(with_tools(tmp_path, tools))
    return str(caught.value)


def test_duplicate_tool_ids_fail_startup(tmp_path):
    tools = [GOOD_TOOL, {**GOOD_TOOL, "apim_base_url": "https://other.example.com"}]
    message = config_error(tmp_path, tools)
    assert "Invalid agent.yaml" in message
    assert "tools[1].id" in message and "duplicate" in message and "mock-shop-api" in message


@pytest.mark.parametrize(
    "url",
    [
        "http://apim.example.com/tools/x",
        "http://localhost.evil.example/tools/x",
        "http://localhost@evil.example/tools/x",
        "ftp://apim.example.com",
        "apim.example.com/tools/x",
    ],
)
def test_apim_base_url_must_be_https(tmp_path, url):
    message = config_error(tmp_path, [{**GOOD_TOOL, "apim_base_url": url}])
    assert "tools[0].apim_base_url" in message and "https://" in message


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8001/tools/x",
        "http://127.0.0.1:8001/tools/x",
        "http://host.docker.internal:8001/tools/x",
        "https://apim.example.com/tools/x",
    ],
)
def test_https_and_local_http_base_urls_pass_validation(tmp_path, url):
    tool = {**GOOD_TOOL, "apim_base_url": url}
    assert load_config(with_tools(tmp_path, [tool])).tools[0].apim_base_url == url
