from src.config import load_config
from tests.support import render_agent_dir


def test_environment_overrides_the_foundry_block_for_local_runs(tmp_path, monkeypatch):
    path = render_agent_dir(tmp_path)
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://dev.example/api/projects/dev")
    monkeypatch.setenv("AZURE_AI_MODEL_DEPLOYMENT_NAME", "gpt-dev")

    config = load_config(path)

    assert config.project_endpoint == "https://dev.example/api/projects/dev"
    assert config.model_deployment == "gpt-dev"


def test_agent_yaml_values_are_used_when_environment_is_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("FOUNDRY_PROJECT_ENDPOINT", raising=False)
    monkeypatch.delenv("AZURE_AI_MODEL_DEPLOYMENT_NAME", raising=False)

    config = load_config(render_agent_dir(tmp_path))

    assert config.project_endpoint.startswith("https://example.services.ai.azure.com")
    assert config.model_deployment == "gpt-5.4"
