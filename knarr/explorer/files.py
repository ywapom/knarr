"""File-system helpers shared by the MCP server, the runner and the doctor."""

from __future__ import annotations

from pathlib import Path


def make_private_dirs(path: Path) -> None:
    """Create ``path`` and any missing parents readable by the user only.

    ``mkdir(parents=True, mode=...)`` applies the mode to the last directory
    only; the runs root and ``~/.knarr`` would be left readable by others, and
    their names list the sites visited. Directories that already exist are left
    as they are."""
    missing = []
    current = Path(path)
    while not current.exists():
        missing.append(current)
        if current.parent == current:
            break
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(exist_ok=True, mode=0o700)
        directory.chmod(0o700)  # the umask can only take bits away, but be explicit
