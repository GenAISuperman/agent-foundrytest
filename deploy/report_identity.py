"""Report the agent identity id to the portal callback.

    POST <PORTAL_URL>/api/agents/<agent id>/identity   {"agentIdentityId": "<id>"}

The route is unauthenticated by accepted POC decision, so no credential is sent.

    python -m deploy.report_identity --agent-id <id> --identity-id <object id>
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence


class ReportError(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: urllib turns a redirected POST into a GET, which would
    look like a successful report (the login page of a proxy answers 200)."""

    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)

# A 4xx other than these is a permanent answer (wrong URL, bad id, refused): retrying
# the same request cannot change it. Throttling, timeouts and 5xx are worth another try.
_RETRYABLE_4XX = (408, 425, 429)


def report_identity(
    portal_url: str,
    agent_id: str,
    identity_id: str,
    *,
    attempts: int = 3,
    delay_seconds: float = 3,
    timeout_seconds: float = 30,
) -> None:
    url = f"{portal_url.rstrip('/')}/api/agents/{urllib.parse.quote(agent_id, safe='')}/identity"
    body = json.dumps({"agentIdentityId": identity_id}).encode()
    last = ""
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(
            url, data=body, method="POST", headers={"Content-Type": "application/json"}
        )
        try:
            with _OPENER.open(request, timeout=timeout_seconds) as response:
                if 200 <= response.status < 300:
                    return
                last = f"portal answered HTTP {response.status}"
        except urllib.error.HTTPError as exc:  # 3xx (not followed) and 4xx/5xx
            last = f"portal answered HTTP {exc.code}"
            if 400 <= exc.code < 500 and exc.code not in _RETRYABLE_4XX:
                break
        except urllib.error.URLError as exc:
            last = f"portal unreachable: {exc.reason}"
        except (TimeoutError, ConnectionError, http.client.HTTPException, OSError) as exc:
            # Raised mid-request (read timeout, reset, RemoteDisconnected): urllib only
            # wraps connect-time failures in URLError.
            last = f"portal unreachable: {exc or type(exc).__name__}"
        if attempt < attempts:
            time.sleep(delay_seconds)
    raise ReportError(f"{last} for POST {url}")


def main(argv: Sequence[str] | None = None, *, env: Mapping[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--identity-id", required=True)
    args = parser.parse_args(argv)
    try:
        portal_url = env.get("PORTAL_URL", "").strip()
        if not portal_url:
            raise ReportError("PORTAL_URL is not set (repository secret)")
        report_identity(portal_url, args.agent_id, args.identity_id)
    except ReportError as exc:
        print(f"Could not report the identity to the portal: {exc}", file=sys.stderr)
        return 1
    print(f"Reported the identity of {args.agent_id} to the portal")
    return 0


if __name__ == "__main__":
    sys.exit(main())
