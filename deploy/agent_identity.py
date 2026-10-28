"""Print the agent identity id of a deployed hosted-agent version.

THE ASSUMPTION LIVES HERE. Spike ticket 02 (how a pipeline reads the agent identity id)
has not been run. This script relies on what the stable azure-ai-projects package
documents: the version returned by the service carries `instance_identity`
(`AgentIdentity`: `principal_id`, `client_id`), and so does the agent object. The id the
portal needs for Microsoft Graph app-role assignment is the identity's service
principal object id, which is `principal_id` (`client_id` is the app/instance id).

If the spike shows a different field or call, change `read_identity_id` only; the
workflow just passes this script's stdout on to the portal callback.

    python -m deploy.agent_identity --agent-name <name> --agent-version <n>
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import Any


class IdentityError(Exception):
    pass


def _field(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


def read_identity_id(project: Any, agent_name: str, agent_version: str) -> str:
    version = project.agents.get_version(agent_name=agent_name, agent_version=agent_version)
    principal_id = _field(_field(version, "instance_identity"), "principal_id")
    if not principal_id:
        agent = project.agents.get(agent_name=agent_name)
        principal_id = _field(_field(agent, "instance_identity"), "principal_id")
    if not principal_id:
        raise IdentityError(
            f"Foundry returned no instance_identity.principal_id for {agent_name} "
            f"version {agent_version}. See the assumption in deploy/agent_identity.py."
        )
    return str(principal_id)


def _default_client(endpoint: str) -> Any:
    from azure.ai.projects import AIProjectClient
    from azure.identity import DefaultAzureCredential

    return AIProjectClient(endpoint=endpoint, credential=DefaultAzureCredential())


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    client_factory: Callable[[str], Any] = _default_client,
) -> int:
    env = os.environ if env is None else env
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--agent-name", required=True)
    parser.add_argument("--agent-version", required=True)
    args = parser.parse_args(argv)
    try:
        endpoint = env.get("FOUNDRY_PROJECT_ENDPOINT", "").strip()
        if not endpoint:
            raise IdentityError("FOUNDRY_PROJECT_ENDPOINT is not set (repository secret)")
        print(read_identity_id(client_factory(endpoint), args.agent_name, args.agent_version))
    except IdentityError as exc:
        print(f"Could not read the agent identity id: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
