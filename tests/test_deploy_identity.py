"""Reading the agent identity id and reporting it to the portal."""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from deploy.agent_identity import IdentityError, read_identity_id, main as identity_main
from deploy.report_identity import ReportError, report_identity, main as report_main
from tests.fake_foundry import FakeProject


def test_identity_id_is_the_principal_id_of_the_new_versions_instance_identity():
    project = FakeProject(principal_id="obj-123")
    assert read_identity_id(project, "demo", "4") == "obj-123"


def test_falls_back_to_the_agents_instance_identity():
    project = FakeProject(principal_id=None)
    project.agents.agent_identity = "obj-from-agent"
    assert read_identity_id(project, "demo", "4") == "obj-from-agent"


def test_fails_clearly_when_foundry_reports_no_identity():
    project = FakeProject(principal_id=None)
    with pytest.raises(IdentityError, match="instance_identity"):
        read_identity_id(project, "demo", "4")


def test_cli_prints_only_the_id(capsys):
    code = identity_main(
        ["--agent-name", "demo", "--agent-version", "4"],
        env={"FOUNDRY_PROJECT_ENDPOINT": "https://x"},
        client_factory=lambda endpoint: FakeProject(principal_id="obj-9"),
    )
    assert code == 0
    assert capsys.readouterr().out.strip() == "obj-9"


def test_cli_exits_non_zero_without_an_identity(capsys):
    code = identity_main(
        ["--agent-name", "demo", "--agent-version", "4"],
        env={"FOUNDRY_PROJECT_ENDPOINT": "https://x"},
        client_factory=lambda endpoint: FakeProject(principal_id=None),
    )
    assert code == 1
    assert "instance_identity" in capsys.readouterr().err


# --- portal callback ---------------------------------------------------------------


class Portal:
    def __init__(self, status=200, location=None):
        self.status = status
        self.location = location
        self.requests: list[dict] = []
        portal = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                portal.requests.append(
                    {
                        "path": self.path,
                        "content_type": self.headers["Content-Type"],
                        "auth": self.headers.get("Authorization"),
                        "body": json.loads(body),
                    }
                )
                self.send_response(portal.status)
                if portal.location:
                    self.send_header("Location", portal.location)
                self.end_headers()
                self.wfile.write(b"{}")

            def do_GET(self):
                portal.requests.append({"path": self.path, "method": "GET"})
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"<html>login</html>")

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture
def portal():
    p = Portal()
    yield p
    p.close()


def test_posts_the_identity_id_to_the_agents_identity_route(portal):
    report_identity(portal.url, "demo", "obj-123")

    (request,) = portal.requests
    assert request["path"] == "/api/agents/demo/identity"
    assert request["body"] == {"agentIdentityId": "obj-123"}
    assert request["content_type"] == "application/json"
    assert request["auth"] is None


def test_a_trailing_slash_on_the_portal_url_is_tolerated(portal):
    report_identity(portal.url + "/", "demo", "obj-123")
    assert portal.requests[0]["path"] == "/api/agents/demo/identity"


def test_a_portal_error_fails_the_report():
    p = Portal(status=500)
    try:
        with pytest.raises(ReportError, match="500"):
            report_identity(p.url, "demo", "obj-123", attempts=1)
    finally:
        p.close()


def test_report_cli_reads_the_portal_url_from_the_environment(portal):
    code = report_main(
        ["--agent-id", "demo", "--identity-id", "obj-1"], env={"PORTAL_URL": portal.url}
    )
    assert code == 0
    assert portal.requests[0]["body"] == {"agentIdentityId": "obj-1"}


def test_report_cli_needs_the_portal_url(capsys):
    code = report_main(["--agent-id", "demo", "--identity-id", "obj-1"], env={})
    assert code == 1
    assert "PORTAL_URL" in capsys.readouterr().err


def test_a_redirect_is_a_failure_not_a_followed_get():
    p = Portal(status=302, location="/login")
    try:
        with pytest.raises(ReportError, match="302"):
            report_identity(p.url, "demo", "obj-123", attempts=1)
        assert [r.get("method") for r in p.requests] == [None]  # the POST only, no GET
    finally:
        p.close()


def test_a_permanent_client_error_is_not_retried():
    p = Portal(status=404)
    try:
        with pytest.raises(ReportError, match="404"):
            report_identity(p.url, "demo", "obj-123", attempts=3, delay_seconds=0)
        assert len(p.requests) == 1
    finally:
        p.close()


@pytest.mark.parametrize("status", [429, 503])
def test_throttling_and_server_errors_are_retried(status):
    p = Portal(status=status)
    try:
        with pytest.raises(ReportError, match=str(status)):
            report_identity(p.url, "demo", "obj-123", attempts=3, delay_seconds=0)
        assert len(p.requests) == 3
    finally:
        p.close()


def _silent_server(behaviour):
    """A TCP server that accepts a connection and then `behaviour(conn)`."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(5)
    connections = []

    def run():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            connections.append(conn)
            behaviour(conn)

    threading.Thread(target=run, daemon=True).start()
    return server, f"http://127.0.0.1:{server.getsockname()[1]}", connections


def test_a_connection_dropped_without_a_response_is_a_clean_report_error():
    server, url, _ = _silent_server(lambda conn: conn.close())
    try:
        with pytest.raises(ReportError, match="portal"):
            report_identity(url, "demo", "obj-123", attempts=2, delay_seconds=0)
    finally:
        server.close()


def test_a_portal_that_never_answers_is_a_clean_report_error():
    server, url, _ = _silent_server(lambda conn: None)
    try:
        with pytest.raises(ReportError, match="portal"):
            report_identity(
                url, "demo", "obj-123", attempts=1, delay_seconds=0, timeout_seconds=0.3
            )
    finally:
        server.close()


def test_report_cli_fails_cleanly_when_the_portal_resets_the_connection(capsys):
    server, url, _ = _silent_server(lambda conn: conn.close())
    try:
        code = report_main(
            ["--agent-id", "demo", "--identity-id", "obj-1"], env={"PORTAL_URL": url}
        )
    finally:
        server.close()
    assert code == 1
    assert "Could not report the identity" in capsys.readouterr().err
