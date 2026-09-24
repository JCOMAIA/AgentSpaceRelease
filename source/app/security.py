"""Password hashing, API-key minting and signed session cookies.

Deliberately stdlib-only: fewer moving parts to audit, no native build step on
the box. `scrypt` is memory-hard and shipped with CPython.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

from .config import get_settings

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_KEY_PREFIX = "ask"  # AgentSpace Key


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# --------------------------------------------------------------------------
# Passwords
# --------------------------------------------------------------------------
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(
        password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64e(salt)}${_b64e(dk)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(
            password.encode(),
            salt=_b64d(salt_b64),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(_b64d(hash_b64)),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk, _b64d(hash_b64))


# --------------------------------------------------------------------------
# API keys
# --------------------------------------------------------------------------
def mint_api_key() -> tuple[str, str, str]:
    """Return `(full_key, prefix, key_hash)`.

    The full key is shown to the human exactly once. We persist only the hash,
    plus a short prefix so the dashboard can label keys without storing them.
    """
    secret = secrets.token_urlsafe(32)
    prefix = secrets.token_hex(4)
    full = f"{_KEY_PREFIX}_{prefix}_{secret}"
    return full, prefix, hash_api_key(full)


def hash_api_key(full_key: str) -> str:
    """Fast keyed hash — API keys are already high-entropy, so no KDF needed."""
    settings = get_settings()
    return hmac.new(settings.secret_key.encode(), full_key.encode(), hashlib.sha256).hexdigest()


def looks_like_api_key(value: str) -> bool:
    return value.startswith(f"{_KEY_PREFIX}_")


# --------------------------------------------------------------------------
# Session tokens (signed, stateless, for the human-facing dashboard)
# --------------------------------------------------------------------------
def sign_session(user_id: str, ttl_seconds: int = 60 * 60 * 24 * 14) -> str:
    return _sign(user_id, scope=None, ttl_seconds=ttl_seconds)


def read_session(token: str) -> str | None:
    return _read(token, scope=None)


def sign_scoped(user_id: str, scope: str, ttl_seconds: int) -> str:
    """A token that can do exactly one narrow thing.

    Same signature as a session, plus a scope that the reader has to ask for by
    name. A scoped token presented where a session is expected does not
    authenticate, and vice versa — which is what makes it safe to put one in a
    URL and hand it to something that will log it.
    """
    return _sign(user_id, scope=scope, ttl_seconds=ttl_seconds)


def read_scoped(token: str, scope: str) -> str | None:
    return _read(token, scope=scope)


def _sign(user_id: str, scope: str | None, ttl_seconds: int) -> str:
    payload: dict[str, object] = {"sub": user_id, "exp": int(time.time()) + ttl_seconds}
    if scope is not None:
        payload["scp"] = scope
    body = _b64e(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    sig = hmac.new(get_settings().secret_key.encode(), body.encode(), hashlib.sha256).digest()
    return f"{body}.{_b64e(sig)}"


def _read(token: str, scope: str | None) -> str | None:
    try:
        body, sig_b64 = token.split(".")
        expected = hmac.new(
            get_settings().secret_key.encode(), body.encode(), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(expected, _b64d(sig_b64)):
            return None
        payload = json.loads(_b64d(body))
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    if payload.get("exp", 0) < time.time():
        return None
    # A draft token must never open a session, and a session must never stage a
    # draft: each reader names the scope it will accept, and anything else fails.
    if payload.get("scp") != scope:
        return None
    return payload.get("sub")
