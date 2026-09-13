#!/usr/bin/env python3
"""Audit a staged Anthropic skill-creator update without installing or editing the lock manifest."""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from anthropic_skill_creator import inspect_root, tree_digest


SUSPICIOUS = {
    "network_download": re.compile(r"\b(curl|wget|urlopen|requests\.(get|post)|httpx\.)\b"),
    "shell_execution": re.compile(r"\b(os\.system|shell\s*=\s*True|subprocess\.(Popen|run|call))\b"),
    "broad_delete": re.compile(r"\b(rmtree|rm\s+-rf|unlink\()"),
}


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain an object")
    return value


def python_api(root: Path) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError):
            result[relative] = ["<parse-error>"]
            continue
        result[relative] = sorted(node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
    return result


def scan(root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    sbom: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            findings.append({"severity": "critical", "type": "symlink", "path": relative, "target": os.readlink(path)})
            continue
        if not path.is_file():
            continue
        sbom.append({"path": relative, "size": path.stat().st_size, "kind": path.suffix.lower() or "none"})
        if path.suffix.lower() not in {".py", ".sh", ".ps1", ".md", ".js", ".ts"} or path.stat().st_size > 1_000_000:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for finding_type, pattern in SUSPICIOUS.items():
            if pattern.search(text):
                findings.append({"severity": "review", "type": finding_type, "path": relative})
    return sbom, findings


def audit(manifest: dict[str, Any], candidate_root: Path, candidate_ref: str, current_root: Path | None) -> dict[str, Any]:
    dependencies = manifest.get("dependencies", [])
    dependency = next((item for item in dependencies if isinstance(item, dict) and item.get("id") == "anthropic-skill-creator"), None)
    if dependency is None:
        raise ValueError("manifest has no anthropic-skill-creator dependency")
    detected = inspect_root(candidate_root)
    sbom, findings = scan(candidate_root)
    candidate_api = python_api(candidate_root)
    current_api = python_api(current_root) if current_root and current_root.is_dir() else {}
    removed_files = sorted(set(current_api) - set(candidate_api))
    removed_symbols = {path: sorted(set(current_api[path]) - set(candidate_api.get(path, []))) for path in sorted(set(current_api) & set(candidate_api)) if set(current_api[path]) - set(candidate_api.get(path, []))}
    required = set(dependency.get("required_capabilities", []))
    capabilities = detected.get("capabilities", {})
    missing = sorted(name for name in required if not capabilities.get(name))
    license_path = candidate_root / str(dependency.get("license_file", "LICENSE.txt"))
    license_ok = license_path.is_file() and "Apache" in license_path.read_text(encoding="utf-8", errors="replace")
    hard_findings = [item for item in findings if item["severity"] == "critical"]
    candidate_digest = tree_digest(candidate_root)
    status = "pass" if detected.get("recognized") and not missing and license_ok and not hard_findings and not removed_files and not removed_symbols else "review_required"
    return {
        "schema_version": "1.0",
        "status": status,
        "installs_or_updates_dependency": False,
        "current_lock": {"ref": dependency.get("source", {}).get("ref"), "content_id": dependency.get("expected_content_id")},
        "candidate": {"ref": candidate_ref, "content_id": candidate_digest, "root": str(candidate_root.resolve()), "detected": detected},
        "compatibility": {"missing_required_capabilities": missing, "removed_python_files": removed_files, "removed_top_level_symbols": removed_symbols},
        "license": {"expected": dependency.get("license"), "file": str(license_path), "compatible": license_ok},
        "security_findings": findings,
        "sbom": sbom,
        "proposed_manifest_change": {"source.ref": candidate_ref, "expected_content_id": candidate_digest} if status == "pass" else None,
        "requires_human_review": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--candidate-root", required=True, type=Path)
    parser.add_argument("--candidate-ref", required=True)
    parser.add_argument("--current-root", type=Path)
    parser.add_argument("--compatibility-command", action="append", default=[], help="repeatable command executed without a shell in the staged provider root")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        if not re.fullmatch(r"[0-9a-f]{40}", args.candidate_ref):
            raise ValueError("candidate-ref must be a full 40-character lowercase commit")
        if args.output.exists():
            raise ValueError("refusing to overwrite output")
        report = audit(load(args.manifest), args.candidate_root.resolve(), args.candidate_ref, args.current_root.resolve() if args.current_root else None)
        compatibility_runs = []
        for raw in args.compatibility_command:
            command = shlex.split(raw)
            if not command:
                raise ValueError("compatibility command must not be empty")
            completed = subprocess.run(command, cwd=args.candidate_root.resolve(), capture_output=True, text=True, timeout=args.timeout, check=False)
            compatibility_runs.append({"command": command, "exit_code": completed.returncode, "stdout": completed.stdout[-4000:], "stderr": completed.stderr[-4000:]})
        report["compatibility"]["test_runs"] = compatibility_runs
        report["compatibility"]["tests_executed"] = bool(compatibility_runs)
        if not compatibility_runs or any(item["exit_code"] != 0 for item in compatibility_runs):
            report["status"] = "review_required"
            report["proposed_manifest_change"] = None
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["status"] == "pass" else 1
    except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
