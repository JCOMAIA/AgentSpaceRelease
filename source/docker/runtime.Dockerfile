# The image user code actually runs in.
#
# Two rules for anything added here:
#   1. No secrets, no host paths, no Docker client.
#   2. Anything installed is reachable by untrusted code — keep it boring.
#
# Build:  docker build -t agentspace/runtime:latest -f docker/runtime.Dockerfile .
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    NODE_MAJOR=22 \
    HOME=/tmp

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl gnupg git bash \
 && mkdir -p /etc/apt/keyrings \
 && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
      | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
 && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_$NODE_MAJOR.x nodistro main" \
      > /etc/apt/sources.list.d/nodesource.list \
 && apt-get update && apt-get install -y --no-install-recommends nodejs \
 && apt-get purge -y gnupg && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

# A batteries-included baseline so a first `run_code` does something useful
# without network access to install anything.
RUN pip install --no-cache-dir \
      requests httpx flask fastapi uvicorn \
      numpy pandas pillow jinja2 markdown pyyaml python-dateutil

# Matches SANDBOX_UID in app/sandbox/docker_driver.py. Containers are started
# with --user 10001:10001, so this account owns nothing sensitive.
RUN useradd --uid 10001 --create-home --home-dir /home/sandbox --shell /bin/bash sandbox \
 && mkdir -p /workspace /app \
 && chown 10001:10001 /workspace /app

WORKDIR /workspace
USER 10001:10001

CMD ["python", "-c", "print('agentspace runtime ready')"]
