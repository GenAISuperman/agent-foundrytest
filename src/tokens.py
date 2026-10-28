"""Token acquisition for tool calls: the one place that knows how the agent gets a token.

Open question (spike ticket 02, not yet run): whether a hosted agent's own token for a
tool scope carries the tool's app roles. If it does not, only `default_token_source`
changes; callers just see a function from scope to bearer token.
"""

from __future__ import annotations

import os
from typing import Callable

TokenSource = Callable[[str], str]

ENV_MOCK_TOOLS = "AGENT_LOCAL_MOCK_TOOLS"


def default_token_source() -> TokenSource:
    """The agent's own Entra token for a scope, via DefaultAzureCredential."""
    from azure.identity import DefaultAzureCredential

    credential = DefaultAzureCredential()

    def get_token(scope: str) -> str:
        return credential.get_token(scope).token

    return get_token


def mock_token_source(_scope: str) -> str:
    return "mock-token"


def token_source_from_env() -> TokenSource:
    """Real token source, or a fake one when AGENT_LOCAL_MOCK_TOOLS=1 (local runs with
    the mock tool server only; APIM rejects the fake token)."""
    if os.environ.get(ENV_MOCK_TOOLS) == "1":
        return mock_token_source
    return default_token_source()
