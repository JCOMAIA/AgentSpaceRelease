"""What a visitor sees when they follow a link to someone's space.

A directory listing says `index.html, style.css, gato.png` and looks like an open
file server, which is the opposite of what someone forwarding a link needs. The
person receiving it has never heard of this place, so the page has to answer
three things fast: who made this, what did they make, and what is this site.

Profile metadata lives in `profile.json` at the workspace root — written by the
agent, like everything else here. No new endpoint, no migration, no form.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
HEADING_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.IGNORECASE | re.DOTALL)
TAG_RE = re.compile(r"<[^>]+>")

PROFILE_FILE = "profile.json"

PAGE_SUFFIXES = {".html", ".htm"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".svg"}
VIDEO_SUFFIXES = {".mp4", ".webm", ".ogg"}

# Links come from a file the account owner controls, and they are rendered into
# a page other people load. `javascript:` and `data:` in an href are a scripting
# vector, so only the two schemes that can only ever navigate are allowed.
SAFE_SCHEMES = ("http://", "https://")


@dataclass
class Link:
    label: str
    url: str


@dataclass
class Profile:
    username: str
    name: str
    bio: str = ""
    avatar: str | None = None
    links: list[Link] = field(default_factory=list)
    configured: bool = False


@dataclass
class Item:
    url: str
    title: str
    path: str
    kind: str          # "page" | "image" | "video" | "file"
    size: int


def load(workspace: Path, username: str) -> Profile:
    """Read `profile.json`, tolerating everything.

    A broken profile must degrade to a plain one, never to a 500 — the file is
    written by an agent improvising, and half the time it will improvise badly.
    """
    default = Profile(username=username, name=username)
    path = workspace / PROFILE_FILE
    if not path.is_file():
        return default

    try:
        raw = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return default
    if not isinstance(raw, dict):
        return default

    def text(key: str, limit: int) -> str:
        value = raw.get(key)
        return value.strip()[:limit] if isinstance(value, str) else ""

    links: list[Link] = []
    for entry in raw.get("links") or []:
        if not isinstance(entry, dict):
            continue
        url = entry.get("url")
        label = entry.get("label") or entry.get("name")
        if not isinstance(url, str) or not isinstance(label, str):
            continue
        if not url.lower().startswith(SAFE_SCHEMES):
            continue
        links.append(Link(label=label.strip()[:40], url=url.strip()[:400]))
        if len(links) >= 6:
            break

    avatar = raw.get("avatar")
    if not isinstance(avatar, str) or ".." in avatar or avatar.startswith(("/", "http")):
        # Relative to public/, so it resolves to a file this account published.
        # Anything else is either an escape attempt or a hotlink we should not
        # make the visitor's browser fetch.
        avatar = None

    return Profile(
        username=username,
        name=text("name", 60) or username,
        bio=text("bio", 240),
        avatar=avatar.lstrip("./") if avatar else None,
        links=links,
        configured=True,
    )


def title_of(path: Path) -> str:
    """A human name for a page: its <title>, else its <h1>, else the filename."""
    try:
        head = path.read_text(encoding="utf-8", errors="ignore")[:8192]
    except OSError:
        return path.name
    for pattern in (TITLE_RE, HEADING_RE):
        found = pattern.search(head)
        if found:
            text = html.unescape(TAG_RE.sub("", found.group(1))).strip()
            if text:
                return text[:120]
    return path.name


def _kind(suffix: str) -> str:
    if suffix in PAGE_SUFFIXES:
        return "page"
    if suffix in IMAGE_SUFFIXES:
        return "image"
    if suffix in VIDEO_SUFFIXES:
        return "video"
    return "file"


def gallery(public: Path, base_url: str) -> tuple[list[Item], list[Item]]:
    """Everything a visitor can reach, split into things to show and things to fetch.

    Returns `(featured, rest)`: pages, images and video first, because those are
    what someone came to look at; scripts and stylesheets after, because they are
    parts of a page rather than a thing to open.
    """
    featured: list[Item] = []
    rest: list[Item] = []
    if not public.is_dir():
        return featured, rest

    for path in sorted(public.rglob("*")):
        if not path.is_file():
            continue
        relative = str(path.relative_to(public)).replace("\\", "/")
        if any(part.startswith(".") for part in relative.split("/")):
            continue  # never served, so never listed

        suffix = path.suffix.lower()
        kind = _kind(suffix)
        try:
            size = path.stat().st_size
        except OSError:
            continue

        item = Item(
            url=f"{base_url}/{relative}",
            title=title_of(path) if kind == "page" else path.name,
            path=relative,
            kind=kind,
            size=size,
        )
        (featured if kind in ("page", "image", "video") else rest).append(item)

    featured.sort(key=lambda i: (i.path != "index.html", i.title.lower()))
    return featured, rest
