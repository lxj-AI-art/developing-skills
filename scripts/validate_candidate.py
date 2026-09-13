#!/usr/bin/env python3
"""Validate a candidate Skill and run explicitly configured self-check commands."""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from prepare_iteration import tree_hash


LINK_PATTERN = re.compile(r"\[[^\]]+\]\((?!https?://|mailto:|#)([^)]+)\)")


def read_frontmatter(path: Path) -> tuple[dict[str, str], list[str]]:
    errors: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return {}, [f"cannot read SKILL.md: {exc}"]
    if not lines or lines[0].strip() != "---":
        return {}, ["SKILL.md must start with YAML frontmatter"]
    try:
        end = next(index for index, line in enumerate(lines[1:], 1) if line.strip() == "---")
    except StopIteration:
        return {}, ["SKILL.md frontmatter is not closed"]
    values: dict[str, str] = {}
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            errors.append(f"unsupported frontmatter line: {line}")
            continue
        key, value = line.split(":", 1)
        values[key.strip()] = value.strip().strip('"\'')
    for key in ("name", "description"):
        if not values.get(key):
            errors.append(f"frontmatter needs non-empty {key}")
    name = values.get("name", "")
    if name and not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", name):
        errors.append("frontmatter name must use lowercase letters, digits and hyphens, max 64 characters")
    return values, errors


def validate_references(skill: Path) -> list[str]:
    errors: list[str] = []
    for markdown in sorted(skill.rglob("*.md")):
        if any(part in {".git", "__pycache__"} for part in markdown.relative_to(skill).parts):
            continue
        try:
            text = markdown.read_text(encoding="utf-8")
        except OSError as exc:
            errors.append(f"cannot read {markdown.relative_to(skill)}: {exc}")
            continue
        for target in LINK_PATTERN.findall(text):
            clean = target.split("#", 1)[0]
            if clean and not (markdown.parent / clean).resolve().is_file():
                errors.append(f"broken reference in {markdown.relative_to(skill)}: {target}")
    return errors


def validate_python(skill: Path) -> list[str]:
    errors: list[str] = []
    scripts = skill / "scripts"
    if not scripts.is_dir():
        return errors
    for path in sorted(scripts.rglob("*.py")):
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError) as exc:
            errors.append(f"Python validation failed for {path.relative_to(skill)}: {exc}")
    return errors


def run_checks(skill: Path, checks: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    before = tree_hash(skill)
    for index, check in enumerate(checks):
        command = check.get("command")
        if not isinstance(command, list) or not command or not all(isinstance(item, str) and item for item in command):
            errors.append(f"candidate_checks[{index}].command must be a non-empty string array")
            continue
        timeout = check.get("timeout_seconds", 120)
        expected = check.get("expected_exit_code", 0)
        if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout < 1:
            errors.append(f"candidate_checks[{index}].timeout_seconds must be an integer >= 1")
            continue
        if not isinstance(expected, int) or isinstance(expected, bool):
            errors.append(f"candidate_checks[{index}].expected_exit_code must be an integer")
            continue
        try:
            completed = subprocess.run(
                command,
                cwd=skill,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            result = {
                "command": command,
                "exit_code": completed.returncode,
                "expected_exit_code": expected,
                "stdout": completed.stdout[-8000:],
                "stderr": completed.stderr[-8000:],
                "passed": completed.returncode == expected,
            }
        except subprocess.TimeoutExpired as exc:
            result = {
                "command": command,
                "exit_code": None,
                "expected_exit_code": expected,
                "stdout": str(exc.stdout or "")[-8000:],
                "stderr": str(exc.stderr or "")[-8000:],
                "passed": False,
                "error": f"timeout after {timeout}s",
            }
        results.append(result)
        if not result["passed"]:
            errors.append(f"candidate check {index} failed: {command}")
    after = tree_hash(skill)
    if after != before:
        errors.append("candidate validation commands modified Skill content")
    return results, errors


def validate_candidate(skill: Path, checks: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    skill = skill.resolve()
    errors: list[str] = []
    metadata, frontmatter_errors = read_frontmatter(skill / "SKILL.md")
    errors.extend(frontmatter_errors)
    errors.extend(validate_references(skill))
    errors.extend(validate_python(skill))
    check_results, check_errors = run_checks(skill, checks or [])
    errors.extend(check_errors)
    return {
        "schema_version": "2.0",
        "valid": not errors,
        "skill_path": str(skill),
        "skill_id": tree_hash(skill) if (skill / "SKILL.md").is_file() else None,
        "metadata": metadata,
        "checks": check_results,
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("skill", type=Path)
    parser.add_argument("--checks", type=Path, help="JSON array or object with candidate_checks")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    checks: list[dict[str, Any]] = []
    if args.checks:
        value = json.loads(args.checks.read_text(encoding="utf-8"))
        value = value.get("candidate_checks", []) if isinstance(value, dict) else value
        if not isinstance(value, list):
            parser.error("--checks must contain an array")
        checks = value
    report = validate_candidate(args.skill, checks)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    sys.exit(main())
