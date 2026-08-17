from __future__ import annotations

from pathlib import Path

SYSTEM_NAMES = {
    ".DS_Store",
    "Thumbs.db",
    "desktop.ini",
}
SYSTEM_DIRS = {
    ".Spotlight-V100",
    ".Trashes",
    ".fseventsd",
    "@eaDir",
    "#recycle",
}


def is_ignored_media_path(path: Path, *, root: Path | None = None) -> bool:
    """Return True for metadata/system files that must never be treated as media."""
    try:
        relative = path.relative_to(root) if root is not None else path
    except ValueError:
        relative = path

    parts = relative.parts
    if not parts:
        return False

    for part in parts[:-1]:
        if part in SYSTEM_DIRS or part.startswith(".") or part.startswith("_"):
            return True

    name = parts[-1]
    if name in SYSTEM_NAMES:
        return True
    if name.startswith("._"):
        return True
    if name.startswith(".") or name.startswith("_"):
        return True
    return False
