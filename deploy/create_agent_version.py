"""Create a new hosted-agent version in the Foundry project and wait until it is active.

Run from the deploy workflow after the image is pushed:

    python -m deploy.create_agent_version --image <registry>/agents/<id>:<tag> \
        --deploy-id <run id>-<attempt> --git-sha <sha>

Needs FOUNDRY_PROJECT_ENDPOINT in the environment and a credential that
DefaultAzureCredential can find (the workflow's azure/login session).

Nothing is ever deleted. If the new version fails, the script exits non-zero and the
endpoint keeps serving the versions that already exist.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

PROTOCOL_VERSION = "2.0.0"  # the version string Microsoft Learn's hosted-agent examples use
CPU = "1"
MEMORY = "2Gi"
POLL_SECONDS = 5
TIMEOUT_SECONDS = 600

_PLACEHOLDER = re.compile(r"\{\{[A-Z_]+\}\}")


class DeployError(Exception):
    """The deploy failed. The message is meant for a human reading the workflow log."""


def build_definition(
    image: str, model_deployment: str, deploy_id: str
) -> Any:
    from azure.ai.projects.models import (
        AgentEndpointProtocol,
        ContainerConfiguration,
        HostedAgentDefinition,
        ProtocolVersionRecord,
    )

    return HostedAgentDefinition(
        cpu=CPU,
        memory=MEMORY,
        container_configuration=ContainerConfiguration(image=image),
        protocol_versions=[
            ProtocolVersionRecord(
                protocol=AgentEndpointProtocol.INVOCATIONS, version=PROTOCOL_VERSION
            )
        ],
        environment_variables={
            "AZURE_AI_MODEL_DEPLOYMENT_NAME": model_deployment,
            "PYTHONNOUSERSITE": "1",
            # Not AGENT_*/FOUNDRY_*: Foundry reserves those prefixes for the platform.
            # Changes on every workflow run, so a manual re-run of the same commit still
            # sends a different definition and Foundry records a new version.
            "DEPLOY_ID": deploy_id,
        },
    )


def _field(obj: Any, name: str) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


def run_deploy(
    project: Any,
    *,
    agent_name: str,
    image: str,
    model_deployment: str,
    deploy_id: str,
    git_sha: str,
    poll_seconds: float = POLL_SECONDS,
    timeout_seconds: float = TIMEOUT_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Create the version, wait for it to become active and return its version id."""
    created = project.agents.create_version(
        agent_name=agent_name,
        definition=build_definition(image, model_deployment, deploy_id),
        metadata={"git_sha": git_sha, "deploy_id": deploy_id},
    )
    version = str(_field(created, "version"))
    print(f"Created {agent_name} version {version}; waiting for it to become active")

    waited = 0.0
    while True:
        details = project.agents.get_version(agent_name=agent_name, agent_version=version)
        status = str(_field(details, "status") or "")
        status = status.rsplit(".", 1)[-1].lower()  # tolerate AgentVersionStatus.ACTIVE
        print(f"Status: {status}")
        if status == "active":
            return version
        if status == "failed":
            error = _field(details, "error") or {}
            raise DeployError(
                f"{agent_name} version {version} failed to provision: "
                f"{_field(error, 'code')}: {_field(error, 'message')}. "
                "Previous versions are untouched."
            )
        if waited >= timeout_seconds:
            raise DeployError(
                f"{agent_name} version {version} did not become active within "
                f"{int(timeout_seconds)}s (last status: {status}). "
                "Previous versions are untouched."
            )
        sleep(poll_seconds)
        waited += poll_seconds


def read_agent_yaml(path: Path) -> tuple[str, str, str]:
    """Return (agent id, Foundry agent name, model deployment) from agent.yaml."""
    raw = path.read_text(encoding="utf-8")
    leftovers = sorted(set(_PLACEHOLDER.findall(raw)))
    if leftovers:
        raise DeployError(f"{path} still has placeholders: {', '.join(leftovers)}")
    doc = yaml.safe_load(raw) or {}
    agent = doc.get("agent") or {}
    foundry = doc.get("foundry") or {}
    values = (agent.get("id"), foundry.get("agent_name"), foundry.get("model_deployment"))
    if not all(isinstance(v, str) and v.strip() for v in values):
        raise DeployError(
            f"{path} needs agent.id, foundry.agent_name and foundry.model_deployment"
        )
    return values  # type: ignore[return-value]


def _default_client(endpoint: str) -> Any:
    from azure.ai.projects import AIProjectClient
    from azure.identity import DefaultAzureCredential

    return AIProjectClient(endpoint=endpoint, credential=DefaultAzureCredential())


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    client_factory: Callable[[str], Any] = _default_client,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    env = os.environ if env is None else env
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--agent-yaml", default="agent.yaml", type=Path)
    parser.add_argument("--image", required=True)
    parser.add_argument("--deploy-id", required=True)
    parser.add_argument("--git-sha", required=True)
    args = parser.parse_args(argv)

    try:
        endpoint = env.get("FOUNDRY_PROJECT_ENDPOINT", "").strip()
        if not endpoint:
            raise DeployError("FOUNDRY_PROJECT_ENDPOINT is not set (repository secret)")
        agent_id, agent_name, model_deployment = read_agent_yaml(args.agent_yaml)
        version = run_deploy(
            client_factory(endpoint),
            agent_name=agent_name,
            image=args.image,
            model_deployment=model_deployment,
            deploy_id=args.deploy_id,
            git_sha=args.git_sha,
            sleep=sleep,
        )
    except DeployError as exc:
        print(f"Deploy failed: {exc}", file=sys.stderr)
        return 1

    output = env.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as fh:
            fh.write(f"agent_id={agent_id}\nagent_name={agent_name}\nagent_version={version}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
