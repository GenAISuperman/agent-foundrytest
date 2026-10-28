import pytest

from tests.support import REPO_ROOT, TEMPLATE_AGENT_YAML


@pytest.mark.skipif(
    "{{" not in (REPO_ROOT / "agent.yaml").read_text(),
    reason="generated repository: agent.yaml is already filled in",
)
def test_fixture_copy_matches_the_templates_agent_yaml():
    # Edit agent.yaml and tests/fixtures/agent.template.yaml together.
    assert TEMPLATE_AGENT_YAML.read_text() == (REPO_ROOT / "agent.yaml").read_text()
