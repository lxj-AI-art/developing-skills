#!/usr/bin/env python3
"""Print the stable content identity of a Skill directory."""

from __future__ import annotations

import argparse
from pathlib import Path

from prepare_iteration import tree_hash


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("skill", type=Path)
    args = parser.parse_args()
    skill = args.skill.resolve()
    if not (skill / "SKILL.md").is_file():
        parser.error(f"not a skill directory: {skill}")
    print(tree_hash(skill))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
