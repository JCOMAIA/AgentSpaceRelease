"""Workspace filesystem access.

Everything a user's agents touch goes through `resolve()`. That function is the
only place allowed to turn an untrusted string into a real path, so the escape
check lives in exactly one spot.
"""

from __future__ import annotations

import os
import re
import shutil
import zipfile
from pathlib import Path

from .config import get_settings
from .teaching import AgentSpaceError

# Files we never let user code create, because they change how the sandbox or
# the proxy behaves rather than being content.
BLOCKED_NAMES = {".dockerenv"}

# Names that usually hold credentials. Used to warn at publish time — this is a
# heuristic for a warning, never a security control. The actual control is
# `is_hidden`, which is a rule the user can reason about.
SECRET_LOOKING = re.compile(
    r"""(?ix)
    ^\.env |
    \.(pem|key|p12|pfx|keystore|jks)$ |
    ^id_(rsa|dsa|ecdsa|ed25519)$ |
    ^\.(git|ssh|aws|npmrc|netrc|htpasswd) |
    (^|[._-])(secret|secrets|credential|credentials|password|passwd|token)([._-]|$) |
    ^service[-_]account.*\.json$ |
    \.(sqlite3?|db)$
    """
)


def is_hidden(relative_path: str) -> bool:
    """Whether any segment of a path is a dotfile or dotdir.

    Publishing a directory publishes what is in it, and what is in it is often
    `.env`, `.git/config` or an SSH key the agent left behind. Rather than
    guessing which files are sensitive, hidden entries are never served — one
    rule, easy to state, easy for an agent to reason about.
    """
    return any(part.startswith(".") for part in relative_path.replace("\\", "/").split("/") if part)


def sensitive_entries(directory: Path, limit: int = 12) -> list[str]:
    """Names under `directory` that look like credentials, for a publish warning."""
    found: list[str] = []
    for path in sorted(directory.rglob("*")):
        if len(found) >= limit:
            break
        if path.is_dir() or not SECRET_LOOKING.search(path.name):
            continue
        try:
            found.append(str(path.relative_to(directory)).replace("\\", "/"))
        except ValueError:
            continue
    return found


def workspace_root(user_id: str) -> Path:
    root = get_settings().data_root_abs / user_id
    root.mkdir(parents=True, exist_ok=True)
    return root


def resolve(user_id: str, rel_path: str, *, must_exist: bool = False) -> Path:
    """Map a user-supplied path to a real path inside their workspace.

    Chroot semantics: the workspace root *is* the filesystem root as far as the
    agent is concerned. An absolute path is therefore interpreted relative to
    it rather than refused — including the `/workspace` prefix we advertise, so
    an agent that echoes back the path we told it lands where it expects.

    Traversal above the root, and symlinks pointing outside it, are rejected.
    """
    root = workspace_root(user_id)
    cleaned = (rel_path or ".").strip().replace("\\", "/")
    if cleaned == "/workspace" or cleaned.startswith("/workspace/"):
        cleaned = cleaned[len("/workspace") :]
    cleaned = cleaned.lstrip("/")
    if cleaned in ("", "."):
        candidate = root
    else:
        candidate = root / cleaned

    try:
        # strict=False so we can also resolve paths that do not exist yet, while
        # still collapsing any symlinks in the existing prefix.
        real = candidate.resolve(strict=False)
        real_root = root.resolve(strict=True)
    except OSError as exc:
        raise AgentSpaceError(
            "path_invalid",
            f"Could not resolve path {rel_path!r}: {exc}",
            "Use a simple relative path like 'src/app.py'.",
        ) from exc

    if real != real_root and real_root not in real.parents:
        raise AgentSpaceError(
            "path_escape",
            f"Path {rel_path!r} points outside your workspace.",
            "Paths are relative to your workspace root. Drop any leading '/' and any '..'.",
            status_code=403,
            try_this={"transport": "rest", "method": "GET", "path": "/api/v1/files?path=."},
        )

    if real.name in BLOCKED_NAMES:
        raise AgentSpaceError(
            "path_blocked",
            f"The name {real.name!r} is reserved.",
            "Choose a different filename.",
            status_code=403,
        )

    if must_exist and not real.exists():
        raise AgentSpaceError(
            "not_found",
            f"No such path: {rel_path!r}",
            "List the directory first to see what exists.",
            status_code=404,
            try_this={
                "transport": "rest",
                "method": "GET",
                "path": f"/api/v1/files?path={_parent_of(cleaned)}",
            },
        )
    return real


def _parent_of(cleaned: str) -> str:
    parent = str(Path(cleaned).parent).replace("\\", "/")
    return "." if parent in ("", ".") else parent


def relative(user_id: str, path: Path) -> str:
    """Inverse of `resolve` — a path the agent can send back to us."""
    rel = path.resolve().relative_to(workspace_root(user_id).resolve())
    return str(rel).replace("\\", "/") or "."


def destroy_workspace(user_id: str) -> int:
    """Delete a user's entire workspace. Returns the bytes removed."""
    root = get_settings().data_root_abs / user_id
    if not root.exists():
        return 0
    size = disk_usage(user_id)
    shutil.rmtree(root, ignore_errors=True)
    return size


def disk_usage(user_id: str) -> int:
    total = 0
    for dirpath, _dirnames, filenames in os.walk(workspace_root(user_id)):
        for name in filenames:
            try:
                total += (Path(dirpath) / name).stat().st_size
            except OSError:
                continue
    return total


def list_dir(user_id: str, rel_path: str) -> list[dict]:
    target = resolve(user_id, rel_path, must_exist=True)
    if target.is_file():
        stat = target.stat()
        return [
            {
                "path": relative(user_id, target),
                "type": "file",
                "size": stat.st_size,
                "modified": int(stat.st_mtime),
            }
        ]
    entries = []
    for child in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
        try:
            stat = child.stat()
        except OSError:
            continue
        entries.append(
            {
                "path": relative(user_id, child),
                "type": "dir" if child.is_dir() else "file",
                "size": stat.st_size if child.is_file() else None,
                "modified": int(stat.st_mtime),
            }
        )
    return entries


def write_file(user_id: str, rel_path: str, content: bytes, *, limit_bytes: int) -> Path:
    target = resolve(user_id, rel_path)
    if target.is_dir():
        raise AgentSpaceError(
            "is_a_directory",
            f"{rel_path!r} is a directory, not a file.",
            "Pick a filename inside it, e.g. "
            f"{rel_path.rstrip('/')}/index.html",
        )
    if len(content) > limit_bytes:
        raise AgentSpaceError(
            "file_too_large",
            f"That file is {len(content)} bytes; your plan allows {limit_bytes}.",
            "Split the file, compress it, or upgrade the plan.",
            status_code=413,
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return target


def delete_path(user_id: str, rel_path: str) -> None:
    target = resolve(user_id, rel_path, must_exist=True)
    if target == workspace_root(user_id):
        raise AgentSpaceError(
            "cannot_delete_root",
            "You cannot delete your workspace root.",
            "Delete individual entries instead, e.g. path='build'.",
            status_code=403,
        )
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()


def move_path(user_id: str, src: str, dest: str) -> Path:
    source = resolve(user_id, src, must_exist=True)
    target = resolve(user_id, dest)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(target))
    return target


def extract_zip(user_id: str, archive: Path, dest_rel: str, *, limit_bytes: int) -> list[str]:
    """Unpack a zip, refusing entries that would land outside the destination."""
    dest = resolve(user_id, dest_rel)
    dest.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    total = 0
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            total += info.file_size
            if total > limit_bytes:
                raise AgentSpaceError(
                    "archive_too_large",
                    f"Uncompressed archive exceeds {limit_bytes} bytes.",
                    "Upload a smaller archive or upgrade the plan.",
                    status_code=413,
                )
            # Resolve each member through the same guard as everything else.
            member_rel = f"{dest_rel.rstrip('/')}/{info.filename}".lstrip("/")
            out = resolve(user_id, member_rel)
            out.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, out.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            written.append(relative(user_id, out))
    return written
