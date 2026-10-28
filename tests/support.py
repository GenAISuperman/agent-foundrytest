"""Test helpers: render the real template files the way provisioning does."""

from __future__ import annotations

import shutil
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

# The tests render this pristine copy of the template's agent.yaml, not the repository's
# own agent.yaml: in a generated repository that file is filled in and edited by bind PRs.
# tests/test_template_fixture.py keeps the copy in step with agent.yaml in the template.
TEMPLATE_AGENT_YAML = REPO_ROOT / "tests" / "fixtures" / "agent.template.yaml"

PLACEHOLDER_VALUES = {
    "AGENT_ID": "shopping",
    "TEAM_NAME": "retail-eng",
    "INTERACTION_MODEL": "standalone",
    "GUARDRAIL_PROFILE": "strict",
    "FOUNDRY_PROJECT_ENDPOINT": "https://example.services.ai.azure.com/api/projects/demo",
    "MODEL_DEPLOYMENT": "gpt-5.4",
    "VIOLATION_THRESHOLD": "3",
    "WINDOW_MINUTES": "10",
    "OUTPUT_ACTION": "alert",
    "NOTIFY_WEBHOOK": "https://example.invalid/hook",
}


def fill_placeholders(text: str, overrides: dict[str, str] | None = None) -> str:
    values = {**PLACEHOLDER_VALUES, **(overrides or {})}
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def render_agent_dir(
    target: Path,
    overrides: dict[str, str] | None = None,
    tools: list[dict] | None = None,
) -> Path:
    """Copy agent.yaml and prompts/ into target with placeholders filled.

    `tools` replaces the template's empty `tools: []` the way a bind PR would.
    Returns the path of the rendered agent.yaml.
    """
    target.mkdir(parents=True, exist_ok=True)
    rendered = fill_placeholders(TEMPLATE_AGENT_YAML.read_text(), overrides)
    if tools is not None:
        rendered = rendered.replace("tools: []", yaml.safe_dump({"tools": tools}))
    (target / "agent.yaml").write_text(rendered)
    shutil.copytree(REPO_ROOT / "prompts", target / "prompts", dirs_exist_ok=True)
    return target / "agent.yaml"
