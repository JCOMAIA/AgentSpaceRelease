"""Sliding-window rate limiting for the endpoints that attackers care about.

Kept in process memory rather than the database: a limiter that writes a row per
attempt hands an attacker a cheap way to hammer the disk, which is the opposite
of the goal. The cost is that each uvicorn worker keeps its own counters, so the
effective ceiling is `limit x workers` — a bound worth knowing, and still the
difference between "a few tries a minute" and the unlimited brute force this
replaces.

If this ever needs to be exact across workers or across machines, move the
counters to Redis and keep the same interface.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass

from fastapi import Request

from .teaching import AgentSpaceError


@dataclass(frozen=True)
class Rule:
    limit: int
    window_seconds: int

    @property
    def human_window(self) -> str:
        if self.window_seconds % 3600 == 0:
            hours = self.window_seconds // 3600
            return f"{hours} hour" + ("s" if hours > 1 else "")
        minutes = max(1, self.window_seconds // 60)
        return f"{minutes} minute" + ("s" if minutes > 1 else "")


# Login is per-IP *and* per-account: the per-IP rule stops one host churning
# through passwords, the per-account rule stops a botnet spreading the same
# attack across many hosts.
LOGIN_PER_IP = Rule(limit=10, window_seconds=900)
LOGIN_PER_ACCOUNT = Rule(limit=6, window_seconds=900)
REGISTER_PER_IP = Rule(limit=5, window_seconds=3600)

_hits: dict[str, deque[float]] = defaultdict(deque)
# Bound the table so a flood of distinct keys cannot grow it without limit.
_MAX_TRACKED_KEYS = 20_000


def client_ip(request: Request) -> str:
    """The caller's address, trusting the proxy we put in front of ourselves.

    uvicorn runs with --proxy-headers, so `request.client.host` is already the
    real address behind Caddy. The header is only consulted as a fallback.
    """
    if request.client and request.client.host:
        return request.client.host
    forwarded = request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[0].strip() or "unknown"


def _prune(bucket: deque[float], cutoff: float) -> None:
    while bucket and bucket[0] <= cutoff:
        bucket.popleft()


def check(bucket: str, key: str, rule: Rule) -> int | None:
    """Record an attempt. Returns seconds to wait if the rule is exceeded."""
    now = time.monotonic()
    entry = _hits[f"{bucket}:{key}"]
    _prune(entry, now - rule.window_seconds)

    if len(entry) >= rule.limit:
        return max(1, int(rule.window_seconds - (now - entry[0])) + 1)

    entry.append(now)
    if len(_hits) > _MAX_TRACKED_KEYS:
        _evict_stale(now)
    return None


def _evict_stale(now: float) -> None:
    longest = max(LOGIN_PER_IP.window_seconds, REGISTER_PER_IP.window_seconds)
    for key in [k for k, v in _hits.items() if not v or v[-1] <= now - longest]:
        _hits.pop(key, None)


def enforce(bucket: str, key: str, rule: Rule, *, what: str, fix: str) -> None:
    """Raise a teaching 429 when the caller has exceeded `rule`."""
    retry_after = check(bucket, key, rule)
    if retry_after is None:
        return
    raise AgentSpaceError(
        "rate_limited",
        f"Too many {what}. This endpoint allows {rule.limit} per {rule.human_window}.",
        fix,
        status_code=429,
        details={"retry_after_seconds": retry_after},
    )


def reset() -> None:
    """Test helper: forget every counter."""
    _hits.clear()
