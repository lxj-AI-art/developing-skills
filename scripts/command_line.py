"""Cross-platform parsing for command prefixes accepted by CLI options."""

from __future__ import annotations

import os
import shlex


def split_command(value: str) -> list[str]:
    """Split a command prefix without treating Windows path separators as escapes."""
    windows = os.name == "nt"
    parts = shlex.split(value, posix=not windows)
    if windows:
        parts = [
            part[1:-1]
            if len(part) >= 2 and part[0] == part[-1] and part[0] in {'"', "'"}
            else part
            for part in parts
        ]
    return parts
