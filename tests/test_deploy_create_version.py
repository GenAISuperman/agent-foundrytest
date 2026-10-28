"""Behaviour of deploy/create_agent_version.py against a faked Foundry project client."""

from __future__ import annotations

import json

import pytest

from deploy.create_agent_version import DeployError, main, run_deploy
from tests.fake_foundry import FakeProject

IMAGE = "acr.azurecr.io/agents/demo:abc123-77-1"


def deploy(project, **overrides):
    args = dict(
        agent_name="demo",
        image=IMAGE,
        model_deployment="gpt-5.4",
        deploy_id="77-1",
        git_sha="abc123",
        sleep=lambda _: None,
    )
    args.update(overrides)
    return run_deploy(project, **args)


def test_creates_a_hosted_version_of_the_pushed_image_over_invocations():
    project = FakeProject()
    version = deploy(project)

    assert version == "1"
    (call,) = project.agents.created
    definition = call["definition"]
    assert call["agent_name"] == "demo"
    assert definition["kind"] == "hosted"
    assert definition["container_configuration"]["image"] == IMAGE
    assert [p["protocol"] for p in definition["protocol_versions"]] == ["invocations"]
    assert definition["environment_variables"]["AZURE_AI_MODEL_DEPLOYMENT_NAME"] == "gpt-5.4"
    assert definition["environment_variables"]["PYTHONNOUSERSITE"] == "1"


def test_a_re_run_sends_a_different_definition_so_a_new_version_is_minted():
    project = FakeProject()
    deploy(project, deploy_id="77-1")
    deploy(project, deploy_id="77-2", image=IMAGE.replace("77-1", "77-2"))

    first, second = (c["definition"] for c in project.agents.created)
    assert first["environment_variables"]["AGENT_DEPLOY_ID"] == "77-1"
    assert second["environment_variables"]["AGENT_DEPLOY_ID"] == "77-2"
    assert dict(first) != dict(second)


def test_the_same_source_commit_is_recorded_in_version_metadata():
    project = FakeProject()
    deploy(project)
    assert project.agents.created[0]["metadata"]["git_sha"] == "abc123"


def test_waits_until_the_version_is_active():
    project = FakeProject(statuses=("creating", "creating", "active"))
    assert deploy(project) == "1"
    assert project.agents.polls == 3


def test_a_failed_version_fails_the_deploy_with_the_platform_error_and_deletes_nothing():
    project = FakeProject(
        statuses=("creating", "failed"),
        error={"code": "image_pull_failed", "message": "cannot pull"},
    )
    with pytest.raises(DeployError, match="image_pull_failed"):
        deploy(project)
    assert project.agents.deleted == []


def test_a_version_that_never_becomes_active_times_out_and_deletes_nothing():
    project = FakeProject(statuses=("creating",))
    with pytest.raises(DeployError, match="did not become active"):
        deploy(project, timeout_seconds=30, poll_seconds=10)
    assert project.agents.deleted == []


# --- command line seam -------------------------------------------------------------


def write_agent_yaml(tmp_path, agent_name="demo", placeholder=False):
    name = "{{AGENT_ID}}" if placeholder else agent_name
    path = tmp_path / "agent.yaml"
    path.write_text(
        f"agent:\n  id: {name}\n  runtime: foundry\n"
        f"foundry:\n  project_endpoint: https://x.services.ai.azure.com/api/projects/p\n"
        f"  model_deployment: gpt-5.4\n  agent_name: {name}\n"
    )
    return path


def run_main(tmp_path, project, env=None, **flags):
    out = tmp_path / "github_output"
    base_env = {
        "FOUNDRY_PROJECT_ENDPOINT": "https://x.services.ai.azure.com/api/projects/p",
        "GITHUB_OUTPUT": str(out),
    }
    base_env.update(env or {})
    argv = [
        "--agent-yaml", str(flags.get("agent_yaml") or write_agent_yaml(tmp_path)),
        "--image", IMAGE,
        "--deploy-id", "77-1",
        "--git-sha", "abc123",
    ]
    code = main(argv, env=base_env, client_factory=lambda endpoint: project, sleep=lambda _: None)
    return code, out


def test_main_reads_names_from_agent_yaml_and_publishes_step_outputs(tmp_path):
    project = FakeProject()
    code, out = run_main(tmp_path, project)

    assert code == 0
    assert project.agents.created[0]["agent_name"] == "demo"
    outputs = dict(line.split("=", 1) for line in out.read_text().splitlines())
    assert outputs == {"agent_id": "demo", "agent_name": "demo", "agent_version": "1"}


def test_main_passes_the_project_endpoint_secret_to_the_client(tmp_path):
    seen = []
    code = main(
        ["--agent-yaml", str(write_agent_yaml(tmp_path)), "--image", IMAGE,
         "--deploy-id", "1-1", "--git-sha", "s"],
        env={"FOUNDRY_PROJECT_ENDPOINT": "https://endpoint.example/api/projects/p"},
        client_factory=lambda endpoint: seen.append(endpoint) or FakeProject(),
        sleep=lambda _: None,
    )
    assert code == 0
    assert seen == ["https://endpoint.example/api/projects/p"]


def test_main_refuses_to_deploy_an_agent_yaml_with_leftover_placeholders(tmp_path, capsys):
    project = FakeProject()
    code, _ = run_main(tmp_path, project, agent_yaml=write_agent_yaml(tmp_path, placeholder=True))

    assert code == 1
    assert "{{AGENT_ID}}" in capsys.readouterr().err
    assert project.agents.created == []


def test_main_requires_the_project_endpoint(tmp_path, capsys):
    project = FakeProject()
    code, _ = run_main(tmp_path, project, env={"FOUNDRY_PROJECT_ENDPOINT": ""})

    assert code == 1
    assert "FOUNDRY_PROJECT_ENDPOINT" in capsys.readouterr().err
    assert project.agents.created == []


def test_main_exits_non_zero_when_the_version_fails(tmp_path, capsys):
    project = FakeProject(statuses=("failed",), error={"code": "AcrImageNotFound", "message": "no"})
    code, out = run_main(tmp_path, project)

    assert code == 1
    assert "AcrImageNotFound" in capsys.readouterr().err
    assert not out.exists() or "agent_version" not in out.read_text()
