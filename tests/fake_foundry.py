"""A fake Foundry project client: just enough of `AIProjectClient.agents` for the deploy scripts."""

from __future__ import annotations

from azure.ai.projects.models import AgentDetails, AgentVersionDetails


def version_details(
    name: str,
    version: str,
    status: str = "active",
    principal_id: str | None = None,
    error: dict | None = None,
) -> AgentVersionDetails:
    body: dict = {
        "object": "agent.version",
        "id": f"{name}:{version}",
        "name": name,
        "version": version,
        "metadata": {},
        "created_at": 1790000000,
        "definition": {"kind": "hosted", "cpu": "1", "memory": "2Gi"},
        "status": status,
    }
    if principal_id:
        body["instance_identity"] = {"principal_id": principal_id, "client_id": "client-1"}
    if error:
        body["error"] = error
    return AgentVersionDetails(body)


class FakeAgents:
    def __init__(self, statuses=("active",), principal_id: str | None = "pid-1", error=None):
        self.statuses = list(statuses)
        self.principal_id = principal_id
        self.error = error
        self.created: list[dict] = []
        self.polls = 0
        self.deleted: list[tuple] = []
        self.agent_identity: str | None = None

    def create_version(self, agent_name, definition, **kwargs):
        self.created.append({"agent_name": agent_name, "definition": definition, **kwargs})
        return version_details(agent_name, str(len(self.created)), "creating")

    def get_version(self, agent_name, agent_version):
        status = self.statuses[min(self.polls, len(self.statuses) - 1)]
        self.polls += 1
        return version_details(
            agent_name, agent_version, status, self.principal_id, self.error
        )

    def get(self, agent_name):
        body: dict = {
            "object": "agent",
            "id": agent_name,
            "name": agent_name,
            "state": "enabled",
            "configuration_state": "enabled",
            "versions": {"latest": {}},
        }
        if self.agent_identity:
            body["instance_identity"] = {"principal_id": self.agent_identity, "client_id": "c"}
        return AgentDetails(body)

    def delete(self, *args, **kwargs):
        self.deleted.append(args)

    def delete_version(self, *args, **kwargs):
        self.deleted.append(args)


class FakeProject:
    def __init__(self, **kwargs):
        self.agents = FakeAgents(**kwargs)
