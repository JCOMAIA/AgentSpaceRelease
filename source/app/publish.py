"""Publishing without an authenticated HTTP client.

A chatbot in a browser can produce text, and some can perform a GET. None of
them can send an `Authorization` header, so neither ChatGPT nor Gemini nor
DeepSeek can call this API — which excluded exactly the audience that has no
machine of its own. Two ways in, both landing in the same place:

    paste   the human copies the chatbot's answer and drops it into a box.
            Works with every chatbot alive, because it asks nothing of them.

    draft   the chatbot performs GETs that stage content, and a human confirms.
            The agent does the work; the human is the gate.

The gate is not decoration. A GET that published on its own would fire from
link previews in chat clients, from browser prefetch, and from every crawler
that follows a URL — so a draft is inert until a person clicks Publish while
signed in.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from . import storage
from .security import read_scoped, sign_scoped
from .teaching import AgentSpaceError

# Drafts live inside the owner's workspace under a dot-directory, which the
# hosting layer refuses to serve at all. No new table, no migration, and they
# are removed with the account like everything else.
DRAFT_DIR = ".drafts"
DRAFT_TTL_S = 60 * 60 * 6
MAX_DRAFT_BYTES = 512 * 1024
MAX_DRAFTS = 20

FENCE_RE = re.compile(
    r"```[ \t]*(?P<lang>[a-zA-Z0-9_+-]*)[ \t]*\r?\n(?P<body>.*?)(?:```|\Z)",
    re.DOTALL,
)
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
HEADING_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.IGNORECASE | re.DOTALL)
HTML_HINT_RE = re.compile(r"<(!doctype|html|head|body|div|section|h1|p|style|script)\b", re.I)


# --------------------------------------------------------------------------
# Reading a chatbot's answer
# --------------------------------------------------------------------------
@dataclass
class Extracted:
    content: str
    name: str
    note: str = ""


def extract_html(pasted: str) -> Extracted:
    """Pull a publishable page out of whatever the human pasted.

    They will paste the entire reply — prose, code fences, "here you go!", the
    lot. Asking them to trim it first is asking them to do the one step that
    makes this worse than just using the API.
    """
    text = (pasted or "").strip()
    if not text:
        raise AgentSpaceError(
            "nothing_pasted",
            "There was nothing in the box.",
            "Copy the chatbot's whole answer — code fence, chatter and all — and paste it "
            "here. The page is picked out of it for you.",
        )

    blocks = [
        (m.group("lang") or "").lower().strip()
        for m in FENCE_RE.finditer(text)
    ]
    bodies = [m.group("body") for m in FENCE_RE.finditer(text)]

    chosen: str | None = None
    # Prefer a fence the model labelled as html, then any fence that looks like
    # markup, then the longest fence, then the raw text.
    for lang, body in zip(blocks, bodies, strict=True):
        if lang in ("html", "htm") and body.strip():
            chosen = body
            break
    if chosen is None:
        markup = [b for b in bodies if HTML_HINT_RE.search(b)]
        if markup:
            chosen = max(markup, key=len)
    if chosen is None and bodies:
        chosen = max(bodies, key=len)
    if chosen is None:
        chosen = text

    content = chosen.strip()
    note = ""
    if not HTML_HINT_RE.search(content):
        # Plain text still deserves to become a page rather than an error: the
        # person has something they want at a URL, and refusing is unhelpful.
        note = "That did not look like HTML, so it was wrapped in a plain page."
        content = _wrap_plain_text(content)

    return Extracted(content=content, name=suggest_name(content), note=note)


def _wrap_plain_text(text: str) -> str:
    from html import escape

    return (
        '<!doctype html><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>Note</title>"
        "<style>body{font:16px/1.7 system-ui,sans-serif;max-width:40rem;margin:6vh auto;"
        "padding:0 1.5rem;white-space:pre-wrap}"
        "@media(prefers-color-scheme:dark){body{background:#111;color:#eee}}</style>"
        f"{escape(text)}"
    )


def slugify(value: str, fallback: str = "page") -> str:
    """A filename from a human title, with accents folded rather than dropped.

    Without the fold, "Olá, Mundo" loses the á outright and becomes `ol-mundo` —
    and in Portuguese, or any language with accents, that mangles nearly every
    title. Decomposing first turns á into a + a combining mark, so removing the
    marks leaves the letter behind.
    """
    decomposed = unicodedata.normalize("NFKD", value or "")
    folded = "".join(c for c in decomposed if not unicodedata.combining(c))
    slug = re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-")
    return (slug[:60] or fallback).strip("-") or fallback


def suggest_name(content: str) -> str:
    """A filename from the page's own title, so URLs read like their pages."""
    for pattern in (TITLE_RE, HEADING_RE):
        found = pattern.search(content)
        if found:
            text = re.sub(r"<[^>]+>", "", found.group(1)).strip()
            if text:
                return f"{slugify(text)}.html"
    return "page.html"


def normalise_name(name: str | None, content: str) -> str:
    """Turn a requested filename into one that is safe and ends in .html."""
    if not name or not name.strip():
        return suggest_name(content)
    cleaned = name.strip().replace("\\", "/").split("/")[-1]
    stem, _, ext = cleaned.rpartition(".")
    if ext.lower() in ("html", "htm"):
        return f"{slugify(stem, 'page')}.html"
    return f"{slugify(cleaned, 'page')}.html"


# --------------------------------------------------------------------------
# Draft tokens
# --------------------------------------------------------------------------
DRAFT_SCOPE = "draft"


def mint_draft_token(user_id: str, ttl_s: int = DRAFT_TTL_S) -> str:
    """A credential safe to put in a URL and hand to a chatbot.

    It can only stage a draft. It cannot publish, cannot read the workspace, and
    cannot mint anything else, so a token that leaks through a chat log, a link
    preview or a browser history costs the owner an unwanted draft and nothing
    more. It expires on its own.
    """
    return sign_scoped(user_id, DRAFT_SCOPE, ttl_seconds=ttl_s)


def read_draft_token(token: str) -> str | None:
    return read_scoped(token, DRAFT_SCOPE)


def require_draft_token(token: str | None) -> str:
    user_id = read_draft_token(token or "")
    if user_id is None:
        raise AgentSpaceError(
            "bad_draft_token",
            "That publishing token is missing, expired or not valid.",
            "Ask the person you are helping to open /publish and generate a fresh one. "
            "Tokens last a few hours on purpose.",
            status_code=401,
        )
    return user_id


# --------------------------------------------------------------------------
# Draft storage
# --------------------------------------------------------------------------
def _drafts_root(user_id: str) -> Path:
    root = storage.workspace_root(user_id) / DRAFT_DIR
    root.mkdir(parents=True, exist_ok=True)
    return root


def _draft_path(user_id: str, draft_id: str) -> Path:
    # Hashed rather than trusted: the id arrives in a URL, and this way there is
    # no path to traverse even if the check above is ever loosened.
    safe = hashlib.sha256(draft_id.encode()).hexdigest()[:32]
    return _drafts_root(user_id) / f"{safe}.json"


def create_draft(user_id: str, name: str | None, content: str) -> str:
    _expire_drafts(user_id)
    existing = sorted(_drafts_root(user_id).glob("*.json"))
    if len(existing) >= MAX_DRAFTS:
        raise AgentSpaceError(
            "too_many_drafts",
            f"There are already {len(existing)} unpublished drafts here.",
            "Publish or discard some at /publish before staging another.",
            status_code=429,
        )
    draft_id = secrets.token_urlsafe(9)
    _write_draft(user_id, draft_id, {
        "name": name or "",
        "content": content,
        "created_at": int(time.time()),
    })
    return draft_id


def append_draft(user_id: str, draft_id: str, content: str) -> int:
    draft = load_draft(user_id, draft_id)
    combined = draft["content"] + content
    if len(combined.encode()) > MAX_DRAFT_BYTES:
        raise AgentSpaceError(
            "draft_too_large",
            f"That draft is over {MAX_DRAFT_BYTES // 1024} KB.",
            "Publish what is there, or start a smaller page. Very large pages are better "
            "written through the API with a key.",
            status_code=413,
        )
    draft["content"] = combined
    _write_draft(user_id, draft_id, draft)
    return len(combined)


def load_draft(user_id: str, draft_id: str) -> dict:
    path = _draft_path(user_id, draft_id)
    if not path.is_file():
        raise AgentSpaceError(
            "draft_not_found",
            "There is no draft with that id.",
            "Drafts expire after a few hours. Stage it again with a fresh token.",
            status_code=404,
        )
    return json.loads(path.read_text(encoding="utf-8"))


def discard_draft(user_id: str, draft_id: str) -> None:
    _draft_path(user_id, draft_id).unlink(missing_ok=True)


def list_drafts(user_id: str) -> list[dict]:
    _expire_drafts(user_id)
    out = []
    for path in sorted(_drafts_root(user_id).glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append({
            "id": data.get("id", ""),
            "name": data.get("name") or suggest_name(data.get("content", "")),
            "bytes": len(data.get("content", "").encode()),
            "created_at": data.get("created_at", 0),
        })
    return sorted(out, key=lambda d: d["created_at"], reverse=True)


def _write_draft(user_id: str, draft_id: str, data: dict) -> None:
    data["id"] = draft_id
    _draft_path(user_id, draft_id).write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )


def _expire_drafts(user_id: str) -> None:
    cutoff = int(time.time()) - DRAFT_TTL_S
    for path in _drafts_root(user_id).glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            path.unlink(missing_ok=True)
            continue
        if data.get("created_at", 0) < cutoff:
            path.unlink(missing_ok=True)


__all__ = [
    "Extracted",
    "append_draft",
    "create_draft",
    "discard_draft",
    "extract_html",
    "list_drafts",
    "load_draft",
    "mint_draft_token",
    "normalise_name",
    "read_draft_token",
    "require_draft_token",
    "slugify",
    "suggest_name",
]
