"""Start a personal AgentSpace with one command.

    python scripts/start.py              # local only, http://localhost:8000
    python scripts/start.py --tunnel     # also gets a public https URL

It writes a config if there is none, starts the sandbox broker and the control
plane, optionally raises a Cloudflare tunnel, and prints the API key and the MCP
snippet to paste into a client.

Personal mode: one owner, no signup, no plans, no billing. Ctrl-C stops
everything it started.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
DEFAULT_CONTROL_PORT = 8000
BROKER_PORT = 9000
TUNNEL_LOG = ROOT / "data" / "cloudflared.log"
TUNNEL_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")

children: list[subprocess.Popen] = []


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------
def say(message: str) -> None:
    print(f"  {message}", flush=True)


def step(message: str) -> None:
    print(f"\n{message}", flush=True)


def die(problem: str, fix: str) -> None:
    print(f"\nStopped: {problem}\n\n  Fix: {fix}\n", file=sys.stderr)
    shutdown()
    sys.exit(1)


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------
def port_is_free(port: int) -> bool:
    with socket.socket() as probe:
        return probe.connect_ex(("127.0.0.1", port)) != 0


def docker_is_running() -> bool:
    if shutil.which("docker") is None:
        return False
    result = subprocess.run(
        ["docker", "info", "--format", "{{.ServerVersion}}"],
        capture_output=True, text=True,
    )
    return result.returncode == 0


def runtime_image_exists() -> bool:
    result = subprocess.run(
        ["docker", "image", "inspect", "agentspace/runtime:latest"],
        capture_output=True, text=True,
    )
    return result.returncode == 0


def wait_for_http(url: str, timeout: float, what: str) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3):
                return True
        except (urllib.error.URLError, OSError, TimeoutError):
            time.sleep(0.5)
    say(f"{what} did not answer within {timeout:.0f}s")
    return False


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
def write_env(use_docker: bool, port: int) -> None:
    """Create a personal-mode config. Never overwrites an existing one."""
    ENV_FILE.write_text(
        "# Written by scripts/start.py. Personal mode: one owner, no signup.\n"
        "SINGLE_USER_MODE=true\n"
        "OWNER_USERNAME=me\n\n"
        f"SECRET_KEY={secrets.token_urlsafe(48)}\n"
        f"BASE_DOMAIN=localhost:{port}\n"
        f"PUBLIC_URL=http://localhost:{port}\n\n"
        "DATABASE_URL=sqlite+aiosqlite:///./data/agentspace.db\n"
        "DATA_ROOT=./data/workspaces\n\n"
        + (
            "# The control plane never touches the Docker socket; the broker does.\n"
            "SANDBOX_DRIVER=remote\n"
            f"SANDBOX_BROKER_URL=http://127.0.0.1:{BROKER_PORT}\n"
            f"SANDBOX_BROKER_TOKEN={secrets.token_urlsafe(32)}\n"
            if use_docker
            else
            "# No Docker found. Code runs as a subprocess with NO isolation.\n"
            "SANDBOX_DRIVER=local_unsafe\n"
        )
        + "SANDBOX_IMAGE=agentspace/runtime:latest\n"
        "SANDBOX_NETWORK=agentspace_sandbox\n"
        "SANDBOX_ALLOW_EGRESS=false\n"
        # Room for both concurrent runs the personal plan allows. Below one
        # run's memory, nothing can be admitted at all.
        "EXEC_MEMORY_BUDGET_MB=4096\n"
        "SANDBOX_RUNTIME=\n"
        "SANDBOX_REQUIRE_RUNTIME=false\n"
        "SANDBOX_REQUIRE_ROOTLESS=false\n",
        encoding="ascii",
    )


def set_env_values(**values: str) -> None:
    """Rewrite keys in .env in place, leaving comments and order alone."""
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    remaining = dict(values)
    for index, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if key in remaining:
            lines[index] = f"{key}={remaining.pop(key)}"
    lines.extend(f"{k}={v}" for k, v in remaining.items())
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------
# Processes
# --------------------------------------------------------------------------
def spawn(args: list[str], name: str, **kwargs) -> subprocess.Popen:
    process = subprocess.Popen(args, cwd=ROOT, **kwargs)
    children.append(process)
    say(f"{name} started (pid {process.pid})")
    return process


def shutdown(*_) -> None:
    for process in reversed(children):
        if process.poll() is None:
            process.terminate()
    for process in reversed(children):
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()


def start_tunnel(port: int) -> str | None:
    binary = shutil.which("cloudflared") or _windows_cloudflared()
    if binary is None:
        say("cloudflared not found — skipping the tunnel")
        say("install it with: winget install --id Cloudflare.cloudflared")
        say("or on macOS:     brew install cloudflared")
        return None

    TUNNEL_LOG.parent.mkdir(parents=True, exist_ok=True)
    TUNNEL_LOG.unlink(missing_ok=True)
    # 127.0.0.1 rather than localhost: localhost often resolves to ::1 first,
    # where nothing is listening, and the tunnel then returns an HTML 502.
    spawn(
        [binary, "tunnel", "--url", f"http://127.0.0.1:{port}",
         "--no-autoupdate", "--logfile", str(TUNNEL_LOG)],
        "cloudflared",
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if TUNNEL_LOG.exists():
            found = TUNNEL_URL_RE.search(TUNNEL_LOG.read_text(encoding="utf-8", errors="ignore"))
            if found:
                return found.group(0)
        time.sleep(1)
    say("the tunnel did not report a URL in time; continuing locally")
    return None


def _windows_cloudflared() -> str | None:
    for candidate in (
        r"C:\Program Files (x86)\cloudflared\cloudflared.exe",
        r"C:\Program Files\cloudflared\cloudflared.exe",
    ):
        if Path(candidate).exists():
            return candidate
    return None


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tunnel", action="store_true",
                        help="expose it on a public https URL via Cloudflare")
    parser.add_argument("--port", type=int, default=DEFAULT_CONTROL_PORT)
    args = parser.parse_args()

    port = args.port
    python = sys.executable

    print("\nAgentSpace — personal mode\n" + "=" * 34)

    step("Checking this machine")
    # noqa is deliberate: ruff reads the project's minimum version and calls this
    # dead code, but this script is run by whatever `python` the user has, which
    # is exactly the interpreter that might be too old.
    if sys.version_info < (3, 11):  # noqa: UP036
        die(f"Python {sys.version_info.major}.{sys.version_info.minor} is too old",
            "install Python 3.11 or newer")
    say(f"python {sys.version_info.major}.{sys.version_info.minor}")

    use_docker = docker_is_running()
    if use_docker:
        say("docker is running — code will run in isolated containers")
        if not runtime_image_exists():
            say("building the sandbox image (first run only, a few minutes)")
            build = subprocess.run(
                ["docker", "build", "-t", "agentspace/runtime:latest",
                 "-f", "docker/runtime.Dockerfile", "."],
                cwd=ROOT,
            )
            if build.returncode != 0:
                die("the sandbox image failed to build",
                    "check the docker output above; without it, code execution fails")
    else:
        say("docker NOT available — code will run WITHOUT ISOLATION on this machine")
        say("this is fine to try locally; do not expose it to anyone else")

    for busy in ([port, BROKER_PORT] if use_docker else [port]):
        if not port_is_free(busy):
            die(f"port {busy} is already in use",
                f"stop whatever is listening on {busy}, or pass --port")

    step("Configuration")
    if ENV_FILE.exists():
        say(".env already exists — leaving it alone")
    else:
        write_env(use_docker, port)
        say("wrote .env with a fresh secret key")

    env = {**os.environ, "PYTHONUNBUFFERED": "1"}

    if use_docker:
        step("Starting the sandbox broker")
        spawn([python, "-m", "uvicorn", "app.broker:app",
               "--host", "127.0.0.1", "--port", str(BROKER_PORT)],
              "broker", env=env)
        if not wait_for_http(f"http://127.0.0.1:{BROKER_PORT}/health", 45, "the broker"):
            die("the broker did not start",
                "run it alone to see why: python -m uvicorn app.broker:app --port 9000")

    def start_control_plane() -> subprocess.Popen:
        return spawn(
            [python, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
             "--port", str(port), "--proxy-headers",
             "--forwarded-allow-ips", "127.0.0.1,::1"],
            "control plane", env=env,
        )

    step("Starting AgentSpace")
    control = start_control_plane()
    if not wait_for_http(f"http://127.0.0.1:{port}/health", 60, "AgentSpace"):
        die("the server did not start",
            "run it alone to see why: python -m uvicorn app.main:app --port 8000")

    public_url = f"http://localhost:{port}"
    if args.tunnel:
        step("Opening a public tunnel")
        tunnel_url = start_tunnel(port)
        if tunnel_url:
            public_url = tunnel_url
            say(f"tunnel up: {tunnel_url}")
            # The app builds every URL it hands an agent from PUBLIC_URL. Left as
            # localhost, a remote agent follows those to its own machine.
            set_env_values(BASE_DOMAIN=tunnel_url.removeprefix("https://"),
                           PUBLIC_URL=tunnel_url)
            say("restarting so it advertises the public address")
            # Drop it from the watch list first: the loop at the end treats any
            # dead child as a crash, and would shut everything down over a
            # process we replaced on purpose.
            children.remove(control)
            control.terminate()
            control.wait(timeout=10)
            control = start_control_plane()
            wait_for_http(f"http://127.0.0.1:{port}/health", 60, "AgentSpace")

    report(public_url, use_docker, bool(args.tunnel))

    signal.signal(signal.SIGINT, shutdown)
    try:
        while all(p.poll() is None for p in children):
            time.sleep(1)
        say("\na process exited; shutting the rest down")
    except KeyboardInterrupt:
        pass
    finally:
        shutdown()
    return 0


def report(public_url: str, use_docker: bool, tunnelled: bool) -> None:
    credentials = ROOT / "data" / "owner-credentials.txt"
    api_key = "(see the credentials file)"
    if credentials.exists():
        found = re.search(r"API key\s+(\S+)", credentials.read_text(encoding="utf-8"))
        if found:
            api_key = found.group(1)

    print("\n" + "=" * 68)
    print("  AgentSpace is running")
    print("=" * 68)
    print(f"\n  Dashboard   {public_url}/dashboard")
    print(f"  For agents  {public_url}/api/v1/hello")
    print(f"\n  API key     {api_key}")
    print(f"\n  Credentials {credentials}")

    print("\n  Paste this into an MCP client:\n")
    print(json.dumps(
        {"mcpServers": {"agentspace": {
            "url": f"{public_url}/mcp",
            "headers": {"Authorization": f"Bearer {api_key}"}}}},
        indent=2,
    ).replace("\n", "\n  "))

    if not use_docker:
        print("\n  WARNING: no Docker, so code runs unisolated as you.")
        print("           Keep this local; do not share the URL.")
    if tunnelled:
        print("\n  This URL is public. Anyone with it can reach the dashboard, and")
        print("  anyone with the API key can run code on this machine. The URL")
        print("  changes every time the tunnel restarts.")

    print("\n  Ctrl-C stops everything.\n")


if __name__ == "__main__":
    raise SystemExit(main())
