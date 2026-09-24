"""Full agent journey against a real Docker daemon.

The pytest suite runs with the local sandbox driver so it works anywhere. This
script is the other half: it exercises the paths that only exist with real
containers — sandboxed execution, service deployment, and the reverse proxy —
by walking through what an agent actually does, in order.

    docker build -t agentspace/runtime:latest -f docker/runtime.Dockerfile .
    python scripts/e2e_docker.py

Everything runs in a throwaway database and workspace; the only lasting side
effect is the Docker network it creates.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="agentspace-e2e-"))
os.environ.update(
    {
        "DATABASE_URL": f"sqlite+aiosqlite:///{(TMP / 'e2e.db').as_posix()}",
        "DATA_ROOT": str(TMP / "workspaces"),
        "SANDBOX_DRIVER": "docker",
        "SANDBOX_IMAGE": "agentspace/runtime:latest",
        "SANDBOX_NETWORK": "agentspace_e2e",
        "SECRET_KEY": "e2e-only-secret",
        "PUBLIC_URL": "http://testserver",
        "BASE_DOMAIN": "agentspace.test",
    }
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datetime import UTC, datetime, timedelta  # noqa: E402

from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import select, update  # noqa: E402

from app import reaper  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import init_db, session_scope  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Deployment  # noqa: E402


def _container_running(name: str) -> bool:
    """Ask Docker directly — the database's opinion is what we are checking."""
    import docker

    client = docker.from_env()
    for container in client.containers.list(all=True, filters={"label": "agentspace.kind=service"}):
        if container.labels.get("agentspace.name") == name:
            return container.status == "running"
    return False

# A service an agent might plausibly write: no dependencies, prints when ready.
SERVER_CODE = '''
import http.server, socketserver, os, json

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"served_by": "agent-built-service", "path": self.path}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a):
        print("request:", self.path, flush=True)

port = int(os.environ.get("PORT", "8080"))
socketserver.TCPServer.allow_reuse_address = True
print("listening on", port, flush=True)
with socketserver.TCPServer(("0.0.0.0", port), Handler) as httpd:
    httpd.serve_forever()
'''

passed = failed = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}  {detail}")


async def main() -> int:
    await init_db()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        print("\n1. first contact, no credentials")
        r = await c.get("/api/v1/hello")
        check("hello is public", r.status_code == 200)
        check("it explains how to get access", "how_to_get_access" in r.json()["data"])

        print("\n2. a human registers")
        r = await c.post(
            "/api/v1/account/register",
            json={"username": "ada", "email": "ada@example.com", "password": "long-enough-pass"},
        )
        key = r.json()["data"]["api_key"]
        h = {"Authorization": f"Bearer {key}"}
        check("account created with a key", r.status_code == 200 and key.startswith("ask_"))

        print("\n3. the agent orients itself")
        r = await c.get("/api/v1/whoami", headers=h)
        check("whoami works", r.status_code == 200)
        check("the guide offers next steps", len(r.json()["guide"]["next_steps"]) > 0)

        print("\n4. the agent runs code in the sandbox")
        r = await c.post(
            "/api/v1/exec",
            headers=h,
            json={"language": "python", "code": "import os; print(os.getuid())"},
        )
        data = r.json()["data"]
        check("execution succeeded", data["exit_code"] == 0, data.get("stderr", ""))
        check("it ran as the sandbox uid", "10001" in data["stdout"], data["stdout"])

        print("\n5. the agent generates a site and publishes it")
        r = await c.post(
            "/api/v1/exec",
            headers=h,
            json={
                "language": "python",
                "code": (
                    "import os\n"
                    "os.makedirs('www', exist_ok=True)\n"
                    "open('www/index.html','w').write('<h1>built by an agent</h1>')\n"
                    "print('generated')"
                ),
            },
        )
        check("generation ran", r.json()["data"]["exit_code"] == 0, r.json()["data"]["stderr"])

        r = await c.post(
            "/api/v1/deployments",
            headers=h,
            json={"name": "site", "kind": "static", "source_dir": "www"},
        )
        check("static deploy accepted", r.status_code == 200, r.text[:300])
        check("live at the path url", "built by an agent" in (await c.get("/u/ada/site/")).text)
        subdomain = await c.get("/", headers={"Host": "ada.agentspace.test"})
        check("live at the subdomain", "built by an agent" in subdomain.text)

        print("\n6. the agent deploys a running service")
        await c.put("/api/v1/files?path=svc/server.py", headers=h, json={"content": SERVER_CODE})
        r = await c.post(
            "/api/v1/deployments",
            headers=h,
            json={
                "name": "api",
                "kind": "service",
                "source_dir": "svc",
                "command": "python server.py",
                "port": 8080,
            },
        )
        check("service deploy accepted", r.status_code == 200, r.text[:400])

        if r.status_code == 200:
            proxied = None
            for _ in range(20):
                await asyncio.sleep(0.5)
                proxied = await c.get("/u/ada/api/hello")
                if proxied.status_code == 200:
                    break
            reached = proxied is not None and proxied.status_code == 200
            check("the proxy reaches the service", reached,
                  proxied.text[:200] if proxied else "no response")
            if reached:
                check("the service's response passes through",
                      proxied.json().get("served_by") == "agent-built-service")
                check("the remaining path is forwarded",
                      proxied.json().get("path") == "/hello")

            r = await c.get("/api/v1/deployments/api/logs", headers=h)
            check("logs are readable", "listening on 8080" in r.json()["data"]["logs"],
                  str(r.json())[:300])

        print("\n7. the service sleeps when idle and wakes on the next request")
        # Force the reaper's hand rather than waiting two hours for it.
        settings = get_settings()
        settings.service_idle_minutes = 1
        async with session_scope() as session:
            await session.execute(
                update(Deployment)
                .where(Deployment.name == "api")
                .values(last_request_at=datetime.now(UTC) - timedelta(hours=2))
            )

        reaped = await reaper.reap_once()
        check("the idle service was reaped", reaped == 1, f"reaped={reaped}")
        check("its container is really stopped", not _container_running("api"))

        async with session_scope() as session:
            dep = await session.scalar(select(Deployment).where(Deployment.name == "api"))
            check("it is marked idle, not broken", dep.status == "idle", dep.status)
            slept_port = dep.internal_port

        woken = await c.get("/u/ada/api/awake")
        check("the next request is served anyway", woken.status_code == 200,
              woken.text[:200])
        check("and it is the real service answering",
              woken.status_code == 200
              and woken.json().get("served_by") == "agent-built-service")
        check("its container is running again", _container_running("api"))

        async with session_scope() as session:
            dep = await session.scalar(select(Deployment).where(Deployment.name == "api"))
            check("the republished port was recorded",
                  dep.internal_port != slept_port and dep.status == "running",
                  f"{slept_port} -> {dep.internal_port} ({dep.status})")

        print("\n8. the MCP door sees the same space")
        r = await c.post(
            "/mcp",
            headers=h,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "list_files", "arguments": {"path": "www"}},
            },
        )
        text = r.json()["result"]["content"][0]["text"]
        check("MCP lists the generated file", "index.html" in text, text[:200])
        check("MCP appends next steps", "WHAT YOU CAN DO NEXT" in text)

        print("\n9. cleanup")
        r = await c.delete("/api/v1/deployments/api", headers=h)
        check("service removed", r.status_code == 200, r.text[:200])
        gone = await c.get("/u/ada/api/hello")
        check("service no longer answering", "agent-built-service" not in gone.text)

    print(f"\n{'=' * 46}\n  {passed} passed, {failed} failed\n{'=' * 46}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
