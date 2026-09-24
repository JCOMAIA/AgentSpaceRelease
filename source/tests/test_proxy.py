"""Reverse-proxy routing, without needing Docker.

A real HTTP server stands in for a deployed service, so these tests cover the
proxy path itself: which port it dials, what it forwards, and what it does when
the upstream is gone.
"""

from __future__ import annotations

import http.server
import json
import socketserver
import threading

import pytest
from sqlalchemy import select

from app.db import session_scope
from app.models import Deployment, User

# The port recorded as the user's *declared* port. Deliberately not the one the
# upstream listens on: the proxy must dial `internal_port`, and a regression to
# `port` would silently 404 every deployed service.
DECLARED_PORT = 9999


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps(
            {
                "hit": True,
                "path": self.path,
                "forwarded_host": self.headers.get("x-forwarded-host"),
                "deployment": self.headers.get("x-agentspace-deployment"),
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def upstream():
    socketserver.TCPServer.allow_reuse_address = True
    server = socketserver.TCPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


async def _register_service(username: str, name: str, internal_port: int | None, status="running"):
    async with session_scope() as session:
        user = await session.scalar(select(User).where(User.username == username))
        session.add(
            Deployment(
                user_id=user.id,
                name=name,
                kind="service",
                source_dir=".",
                command="irrelevant",
                port=DECLARED_PORT,
                internal_host="127.0.0.1",
                internal_port=internal_port,
                container_id="fake",
                status=status,
            )
        )


async def test_proxy_dials_the_internal_port_not_the_declared_one(client, account, upstream):
    await _register_service(account["username"], "api", upstream)

    res = await client.get(f"/u/{account['username']}/api/hello")
    assert res.status_code == 200, res.text
    assert res.json()["hit"] is True


async def test_proxy_forwards_the_remaining_path(client, account, upstream):
    await _register_service(account["username"], "api", upstream)

    res = await client.get(f"/u/{account['username']}/api/deep/path")
    assert res.json()["path"] == "/deep/path"


async def test_proxy_tells_the_service_how_it_was_reached(client, account, upstream):
    await _register_service(account["username"], "api", upstream)

    res = await client.get(f"/u/{account['username']}/api/", headers={"Host": "example.test"})
    body = res.json()
    assert body["forwarded_host"] == "example.test"
    assert body["deployment"] == "api"


async def test_dead_upstream_explains_the_binding_requirement(client, account):
    """The most common cause is a service bound to localhost inside its container."""
    await _register_service(account["username"], "api", 1)  # nothing listens on port 1

    res = await client.get(f"/u/{account['username']}/api/")
    assert res.status_code == 404
    assert "not accepting connections" in res.text
    assert f"0.0.0.0:{DECLARED_PORT}" in res.text


async def test_stopped_service_points_at_its_logs(client, account, upstream):
    await _register_service(account["username"], "api", upstream, status="stopped")

    res = await client.get(f"/u/{account['username']}/api/")
    assert res.status_code == 404
    assert "/logs" in res.text
