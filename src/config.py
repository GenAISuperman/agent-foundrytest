"""Agent configuration, read from agent.yaml (plus environment overrides)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

DEFAULT_AGENT_YAML = "agent.yaml"
ENV_PROJECT_ENDPOINT = "FOUNDRY_PROJECT_ENDPOINT"
ENV_MODEL_DEPLOYMENT = "AZURE_AI_MODEL_DEPLOYMENT_NAME"

_PLACEHOLDER = re.compile(r"\{\{[A-Z_]+\}\}")

# Plain http is for local runs against the mock tool server only.
LOCAL_HTTP_HOSTS = ("localhost", "127.0.0.1", "host.docker.internal")


class ConfigError(Exception):
    """agent.yaml is missing or invalid. The message is meant for a human."""


@dataclass(frozen=True)
class ToolBinding:
    """One bound tool, as declared in agent.yaml."""

    id: str
    roles: tuple[str, ...]
    apim_base_url: str
    scope: str


@dataclass(frozen=True)
class AgentConfig:
    agent_id: str
    team: str
    agent_name: str
    prompt_path: Path
    project_endpoint: str
    model_deployment: str
    tools: tuple[ToolBinding, ...]


def _fail(path: Path, problems: list[str]) -> ConfigError:
    lines = "\n".join(f"  - {p}" for p in problems)
    return ConfigError(f"Invalid agent.yaml ({path}):\n{lines}")


def _section(doc: dict, name: str) -> dict:
    value = doc.get(name)
    return value if isinstance(value, dict) else {}


def _text(section: dict, key: str, label: str, problems: list[str]) -> str:
    value = section.get(key)
    if not isinstance(value, str) or not value.strip():
        problems.append(f"{label} is required")
        return ""
    return value


def _base_url_problem(url: str) -> str | None:
    """Why `url` may not be a tool's gateway route, or None. The agent sends its own
    bearer token to this URL, so it must be https (local hosts may use http)."""
    try:
        parts = urlsplit(url.strip())
        host = parts.hostname
        parts.port  # noqa: B018 - raises ValueError on a malformed port
    except ValueError:
        return "is not a valid URL"
    if parts.scheme == "https" and host:
        return None
    if parts.scheme == "http" and host in LOCAL_HTTP_HOSTS and "@" not in parts.netloc:
        return None
    return (
        "must start with https:// (http:// is allowed only for "
        + ", ".join(LOCAL_HTTP_HOSTS)
        + ")"
    )


def _parse_tools(raw: Any, problems: list[str]) -> tuple[ToolBinding, ...]:
    if not isinstance(raw, list):
        problems.append("tools must be a list")
        return ()
    bindings = []
    first_index: dict[str, int] = {}
    for index, entry in enumerate(raw):
        label = f"tools[{index}]"
        if not isinstance(entry, dict):
            problems.append(f"{label} must be a mapping")
            continue
        found = len(problems)
        tool_id = _text(entry, "id", f"{label}.id", problems)
        base_url = _text(entry, "apim_base_url", f"{label}.apim_base_url", problems)
        scope = _text(entry, "scope", f"{label}.scope", problems)
        roles = entry.get("roles")
        if (
            not isinstance(roles, list)
            or not roles
            or not all(isinstance(r, str) and r.strip() for r in roles)
        ):
            problems.append(f"{label}.roles must be a non-empty list of role names")
        if base_url and (why := _base_url_problem(base_url)):
            problems.append(f"{label}.apim_base_url {why}")
        if tool_id and tool_id in first_index:
            problems.append(
                f"{label}.id duplicates tools[{first_index[tool_id]}].id ('{tool_id}')"
            )
        elif tool_id:
            first_index[tool_id] = index
        if len(problems) == found:
            bindings.append(ToolBinding(tool_id, tuple(roles), base_url, scope))
    return tuple(bindings)


def load_config(path: str | os.PathLike | None = None) -> AgentConfig:
    path = Path(path or os.environ.get("AGENT_YAML_PATH", DEFAULT_AGENT_YAML))
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise _fail(path, [f"file not found: {path}"]) from None

    leftovers = sorted(set(_PLACEHOLDER.findall(raw)))
    if leftovers:
        raise _fail(
            path,
            [f"placeholder not replaced: {token}" for token in leftovers],
        )

    try:
        doc = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise _fail(path, [f"not valid YAML: {exc}"]) from None
    if not isinstance(doc, dict):
        raise _fail(path, ["not valid YAML: expected a mapping at the top level"])

    problems: list[str] = []
    agent, foundry = _section(doc, "agent"), _section(doc, "foundry")
    prompts = _section(doc, "prompts")

    agent_id = _text(agent, "id", "agent.id", problems)
    team = _text(agent, "team", "agent.team", problems)
    if agent.get("runtime") != "foundry":
        problems.append(
            f"agent.runtime must be 'foundry' (found {agent.get('runtime')!r})"
        )

    endpoint = os.environ.get(ENV_PROJECT_ENDPOINT) or foundry.get("project_endpoint")
    deployment = os.environ.get(ENV_MODEL_DEPLOYMENT) or foundry.get("model_deployment")
    if not isinstance(endpoint, str) or not endpoint.strip():
        problems.append("foundry.project_endpoint is required")
    if not isinstance(deployment, str) or not deployment.strip():
        problems.append("foundry.model_deployment is required")
    agent_name = _text(foundry, "agent_name", "foundry.agent_name", problems)

    tools = _parse_tools(doc.get("tools", []), problems)

    prompt_source = _text(prompts, "source", "prompts.source", problems)
    prompt_path = path.parent / prompt_source
    if prompt_source and not prompt_path.is_file():
        problems.append(f"prompts.source file not found: {prompt_source}")

    if problems:
        raise _fail(path, problems)

    return AgentConfig(
        agent_id=agent_id,
        team=team,
        agent_name=agent_name,
        prompt_path=prompt_path,
        project_endpoint=endpoint,
        model_deployment=deployment,
        tools=tools,
    )
