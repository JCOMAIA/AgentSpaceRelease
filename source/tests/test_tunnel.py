"""Running behind a tunnel (ngrok, cloudflared) or any reverse proxy.

The self-teaching layer hands agents URLs. Behind a tunnel those must be the
tunnel's, not the loopback address the process happens to bind — an agent given
`http://localhost:8000/u/ada/` from another machine follows it and reaches its
own machine, which is a confusing failure with no error message.

These tests pin the configuration `docs/ForLLMInstall.md` tells people to use.
"""

from __future__ import annotations

import pytest

from app.config import get_settings

TUNNEL_HOST = "agentspace-demo.ngrok-free.app"


@pytest.fixture
def behind_tunnel(monkeypatch):
    """What the install doc tells you to set when putting a tunnel in front."""
    settings = get_settings()
    monkeypatch.setattr(settings, "base_domain", TUNNEL_HOST)
    monkeypatch.setattr(settings, "public_url", f"https://{TUNNEL_HOST}")


async def test_the_manual_advertises_the_tunnel_not_localhost(client, behind_tunnel):
    """An agent reading this from another machine has to get a reachable address."""
    res = await client.get("/api/v1/hello")
    doors = res.json()["data"]["doors"]

    assert doors["rest"]["base_url"] == f"https://{TUNNEL_HOST}/api/v1"
    assert doors["mcp"]["url"] == f"https://{TUNNEL_HOST}/mcp"
    assert TUNNEL_HOST in doors["a2a"]["agent_card"]
    assert "localhost" not in str(doors)


async def test_the_agent_card_points_at_the_tunnel(client, behind_tunnel):
    card = (await client.get("/.well-known/agent-card.json")).json()
    assert card["url"] == f"https://{TUNNEL_HOST}/a2a"
    assert card["documentationUrl"].startswith(f"https://{TUNNEL_HOST}")


async def test_published_urls_use_the_tunnel(client, account, behind_tunnel):
    await client.put("/api/v1/files?path=index.html", json={"content": "<p>hi</p>"},
                     headers=account["headers"])
    res = await client.post(
        "/api/v1/deployments",
        json={"name": "site", "kind": "static", "source_dir": "."},
        headers=account["headers"],
    )
    urls = res.json()["data"]["urls"]
    assert urls["path"] == f"https://{TUNNEL_HOST}/@{account['username']}/site/"
    assert "localhost" not in urls["path"]


async def test_the_tunnel_host_is_treated_as_the_apex(client, account, behind_tunnel):
    """The tunnel hostname must not be mistaken for a user's subdomain.

    `resolve_host` maps `<name>.<base>` to a user. With BASE_DOMAIN set to the
    tunnel host, the tunnel host itself equals the base and must fall through to
    the marketing site instead of being read as a username.
    """
    res = await client.get("/", headers={"Host": TUNNEL_HOST})
    assert res.status_code == 200
    assert "AgentSpace" in res.text


async def test_path_routing_works_through_the_tunnel(client, account, behind_tunnel):
    """Free tunnels give one hostname, so `/u/<name>/` is the routing that works."""
    h = account["headers"]
    await client.put("/api/v1/files?path=index.html", json={"content": "<p>via tunnel</p>"},
                     headers=h)
    await client.post("/api/v1/deployments", headers=h,
                      json={"name": "site", "kind": "static", "source_dir": "."})

    res = await client.get(f"/u/{account['username']}/site/", headers={"Host": TUNNEL_HOST})
    assert res.status_code == 200
    assert "via tunnel" in res.text


async def test_api_and_mcp_answer_on_the_tunnel_host(client, account, behind_tunnel):
    """Platform paths must not be swallowed by host-based rewriting."""
    headers = {**account["headers"], "Host": TUNNEL_HOST}

    res = await client.get("/api/v1/whoami", headers=headers)
    assert res.status_code == 200

    res = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers=headers,
    )
    assert "tools" in res.json()["result"]


async def test_a_forwarded_client_ip_is_used_for_rate_limiting(client):
    """Behind a tunnel every request arrives from the tunnel's address.

    uvicorn must run with --proxy-headers, or one visitor exhausts the limit for
    everyone. This checks the limiter reads the address the app was given rather
    than a constant.
    """
    from app import ratelimit

    ratelimit.reset()
    assert ratelimit.check("test-bucket", "1.2.3.4", ratelimit.LOGIN_PER_IP) is None
    assert ratelimit.check("test-bucket", "5.6.7.8", ratelimit.LOGIN_PER_IP) is None

    for _ in range(ratelimit.LOGIN_PER_IP.limit):
        ratelimit.check("test-bucket", "1.2.3.4", ratelimit.LOGIN_PER_IP)

    assert ratelimit.check("test-bucket", "1.2.3.4", ratelimit.LOGIN_PER_IP) is not None
    # The other address must still be free — limits are per client, not global.
    assert ratelimit.check("test-bucket", "5.6.7.8", ratelimit.LOGIN_PER_IP) is None
