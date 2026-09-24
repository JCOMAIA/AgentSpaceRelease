"""One Jinja environment factory, so every page agrees about asset URLs.

There are three template environments (human pages, hosted profiles, the paste
publisher) and one shared base.html. Anything base.html needs has to exist in
all three, so it is registered here rather than passed as context -- a context
key added in one place and forgotten in another renders as an empty string, and
an empty string in a URL is a 404 nobody notices until a stranger does.
"""

from __future__ import annotations

from hashlib import blake2b
from pathlib import Path

from fastapi.templating import Jinja2Templates

STATIC_DIR = Path(__file__).parent / "static"

# Fingerprints, computed once per process.
#
# Without one, /static/style.css is a stable URL for changing bytes: Cloudflare
# held a month-old stylesheet at max-age=14400 while the origin served a new
# one, so a template deployed with a new class rendered unstyled for everybody
# for four hours. Server-rendered HTML ships instantly; its stylesheet does not.
# Putting the content hash in the query makes a changed file a different URL,
# which turns that cache from a hazard into the thing it is for.
_fingerprints: dict[str, str] = {}


def static_url(name: str) -> str:
    if name not in _fingerprints:
        path = STATIC_DIR / name
        try:
            digest = blake2b(path.read_bytes(), digest_size=6).hexdigest()
        except OSError:
            # A missing asset is the template's problem, not a reason to refuse
            # the page. Serve the bare URL and let the 404 say so plainly.
            return f"/static/{name}"
        _fingerprints[name] = digest
    return f"/static/{name}?v={_fingerprints[name]}"


def build_templates(directory: Path | str) -> Jinja2Templates:
    templates = Jinja2Templates(directory=str(directory))
    templates.env.globals["static_url"] = static_url
    return templates
