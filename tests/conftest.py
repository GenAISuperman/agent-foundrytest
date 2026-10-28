from __future__ import annotations

from contextlib import ExitStack, contextmanager
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from dev.mock_tool_server import MockToolServer

from tests.support import REPO_ROOT, render_agent_dir


@dataclass
class RunningAgent:
    url: str
    agent_dir: Path

    @property
    def prompt_file(self) -> Path:
        return self.agent_dir / "prompts" / "system.txt"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_agent(agent_dir: Path, agent_yaml: Path, extra_env: dict | None = None):
    """Start the stub-model server as a subprocess; return (process, url)."""
    port = _free_port()
    env = {
        **os.environ,
        "AGENT_YAML_PATH": str(agent_yaml),
        "PORT": str(port),
        "PYTHONPATH": str(REPO_ROOT),
        **(extra_env or {}),
    }
    env.pop("FOUNDRY_PROJECT_ENDPOINT", None)
    env.pop("AZURE_AI_MODEL_DEPLOYMENT_NAME", None)
    proc = subprocess.Popen(
        [sys.executable, "-m", "tests.stub_server"],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    url = f"http://127.0.0.1:{port}"
    return proc, url


def wait_ready(proc: subprocess.Popen, url: str, timeout: float = 30) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"agent exited early:\n{proc.stdout.read()}")
        try:
            if httpx.get(f"{url}/readiness", timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            time.sleep(0.2)
    raise RuntimeError("agent did not become ready")


@contextmanager
def running_agent(tmp_path, tools=None, token_roles=None):
    """Start the agent with `tools` declared in its agent.yaml.

    The agent's token source is a stub whose tokens carry `token_roles`, so the
    stub tool server can play APIM's role check.
    """
    agent_dir = tmp_path / "agent"
    agent_yaml = render_agent_dir(agent_dir, tools=tools)
    env = {"STUB_TOKEN_ROLES": ",".join(token_roles or [])}
    proc, url = start_agent(agent_dir, agent_yaml, env)
    try:
        wait_ready(proc, url)
        yield RunningAgent(url=url, agent_dir=agent_dir)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture
def agent(tmp_path) -> RunningAgent:
    with running_agent(tmp_path) as running:
        yield running


@pytest.fixture
def tool_server():
    server = MockToolServer(enforce_roles=True).start()
    yield server
    server.stop()


@pytest.fixture
def agent_with_tools(tmp_path):
    """Factory: start an agent bound to the given tools; cleaned up at test end."""
    stack = ExitStack()

    def start(tools, token_roles=None):
        return stack.enter_context(running_agent(tmp_path, tools, token_roles))

    yield start
    stack.close()
