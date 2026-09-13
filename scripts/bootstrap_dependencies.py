#!/usr/bin/env python3
"""Plan, install, and verify pinned optional providers for developing-skills."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from anthropic_skill_creator import inspect_root


HERE = Path(__file__).resolve().parent
DEFAULT_SKILL_ROOT = HERE.parent
DEFAULT_MANIFEST = DEFAULT_SKILL_ROOT / "references" / "recommended-dependencies.json"
PINNED_REF = re.compile(r"[0-9a-f]{40}")
CONTENT_ID = re.compile(r"[0-9a-f]{64}")


def read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def dependency(manifest: dict[str, Any]) -> dict[str, Any]:
    if manifest.get("schema_version") != "1.0":
        raise ValueError("dependency manifest schema_version must be 1.0")
    values = manifest.get("dependencies")
    if not isinstance(values, list):
        raise ValueError("dependency manifest needs a dependencies array")
    matches = [item for item in values if isinstance(item, dict) and item.get("id") == "anthropic-skill-creator"]
    if len(matches) != 1:
        raise ValueError("dependency manifest needs exactly one anthropic-skill-creator entry")
    item = matches[0]
    source = item.get("source")
    install = item.get("install")
    if not isinstance(source, dict) or source.get("repo") != "anthropics/skills" or source.get("path") != "skills/skill-creator":
        raise ValueError("Anthropic dependency source must be the official anthropics/skills path")
    if not isinstance(source.get("ref"), str) or not PINNED_REF.fullmatch(source["ref"]):
        raise ValueError("Anthropic dependency ref must be a full immutable commit SHA")
    if not isinstance(item.get("expected_content_id"), str) or not CONTENT_ID.fullmatch(item["expected_content_id"]):
        raise ValueError("Anthropic dependency needs a 64-character expected_content_id")
    if not isinstance(install, dict) or install.get("mode") != "private-provider":
        raise ValueError("Anthropic dependency must use private-provider installation")
    return item


def safe_base(skill_root: Path, item: dict[str, Any]) -> Path:
    relative = Path(item["install"]["relative_root"])
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("dependency relative_root must stay inside the Skill")
    root = skill_root.resolve()
    base = (root / relative).resolve()
    try:
        base.relative_to(root)
    except ValueError:
        raise ValueError("dependency root escapes the Skill") from None
    return base


def paths(skill_root: Path, item: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = safe_base(skill_root, item)
    target = base / item["source"]["ref"]
    return base, target, base / "installation.json"


def installer_candidates(explicit: Path | None) -> list[Path]:
    values: list[Path] = []
    if explicit:
        values.append(explicit)
    configured = os.environ.get("CODEX_SKILL_INSTALLER")
    if configured:
        values.append(Path(configured))
    codex_root = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    values.append(codex_root / "skills" / ".system" / "skill-installer" / "scripts" / "install-skill-from-github.py")
    result: list[Path] = []
    for value in values:
        resolved = value.expanduser().resolve()
        if resolved not in result:
            result.append(resolved)
    return result


def find_installer(explicit: Path | None) -> tuple[Path | None, list[str]]:
    candidates = installer_candidates(explicit)
    return next((path for path in candidates if path.is_file()), None), [str(path) for path in candidates]


def verify_target(target: Path, item: dict[str, Any]) -> dict[str, Any]:
    inspection = inspect_root(target.resolve())
    errors: list[str] = []
    if not inspection["recognized"]:
        errors.append("installed directory is not a recognized Anthropic skill-creator")
    if inspection.get("content_id") != item["expected_content_id"]:
        errors.append("installed content hash does not match the pinned manifest")
    for capability in item.get("required_capabilities", []):
        if inspection.get("capabilities", {}).get(capability) is not True:
            errors.append(f"required capability is missing: {capability}")
    license_file = target / str(item.get("license_file", "LICENSE.txt"))
    if not license_file.is_file():
        errors.append("provider license file is missing")
    return {"valid": not errors, "errors": errors, "inspection": inspection, "license": item.get("license"), "license_file": str(license_file)}


def installed_status(skill_root: Path, item: dict[str, Any]) -> dict[str, Any]:
    base, target, binding = paths(skill_root, item)
    verification = verify_target(target, item) if target.is_dir() else None
    binding_value: dict[str, Any] | None = None
    if binding.is_file():
        try:
            binding_value = read_object(binding)
        except (OSError, ValueError, json.JSONDecodeError):
            binding_value = None
    expected_relative = str(target.relative_to(skill_root.resolve()))
    binding_valid = bool(
        binding_value
        and binding_value.get("dependency") == item["id"]
        and binding_value.get("source") == item["source"]
        and binding_value.get("relative_root") == expected_relative
        and binding_value.get("content_id") == item["expected_content_id"]
        and binding_value.get("global_skill_registration") is False
    )
    provider_content_valid = bool(verification and verification["valid"])
    return {
        "dependency": item["id"],
        "recommended": bool(item.get("recommended")),
        "default_selected": bool(item.get("default_selected")),
        "source": item["source"],
        "expected_content_id": item["expected_content_id"],
        "license": item.get("license"),
        "private_root": str(target),
        "global_skill_registration": False,
        "installed": provider_content_valid and binding_valid,
        "provider_content_valid": provider_content_valid,
        "binding_valid": binding_valid,
        "binding": str(binding),
        "verification": verification,
        "base_exists": base.exists(),
    }


def bind_provider(skill_root: Path, item: dict[str, Any], target: Path, binding: Path, content_id: str) -> None:
    record = {
        "schema_version": "1.0", "dependency": item["id"], "installed_at": datetime.now(timezone.utc).isoformat(),
        "source": item["source"], "relative_root": str(target.relative_to(skill_root.resolve())),
        "content_id": content_id, "license": item.get("license"), "global_skill_registration": False,
    }
    write_json(binding, record)


def install(skill_root: Path, item: dict[str, Any], installer: Path | None, method: str) -> dict[str, Any]:
    base, target, binding = paths(skill_root, item)
    current = installed_status(skill_root, item)
    if current["installed"]:
        return {**current, "status": "already_installed"}
    if target.exists():
        if current["provider_content_valid"]:
            bind_provider(skill_root, item, target, binding, item["expected_content_id"])
            return {**installed_status(skill_root, item), "status": "bound_existing"}
        raise ValueError(f"dependency target exists but does not match the pinned provider: {target}")
    resolved_installer, searched = find_installer(installer)
    if resolved_installer is None:
        return {**current, "status": "blocked", "reason": "Codex skill-installer is unavailable", "searched_installers": searched}
    base.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable, str(resolved_installer), "--repo", item["source"]["repo"],
        "--path", item["source"]["path"], "--ref", item["source"]["ref"],
        "--dest", str(base), "--name", item["source"]["ref"], "--method", method,
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    execution = {"command": command, "exit_code": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr}
    if completed.returncode != 0:
        if target.exists():
            shutil.rmtree(target)
        return {**current, "status": "failed", "reason": "dependency installer failed", "execution": execution}
    verification = verify_target(target, item)
    if not verification["valid"]:
        shutil.rmtree(target)
        return {**current, "status": "failed", "reason": "post-install provider verification failed; fresh target was removed", "execution": execution, "verification": verification}
    bind_provider(skill_root, item, target, binding, verification["inspection"]["content_id"])
    return {**installed_status(skill_root, item), "status": "installed", "execution": execution}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "status", "install", "verify"), nargs="?", default="plan")
    parser.add_argument("--skill-root", type=Path, default=DEFAULT_SKILL_ROOT)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--installer", type=Path)
    parser.add_argument("--method", choices=("auto", "download", "git"), default="auto")
    parser.add_argument("--confirm-external-install", action="store_true")
    parser.add_argument("--without-anthropic", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        item = dependency(read_object(args.manifest.resolve()))
        state = installed_status(args.skill_root, item)
        if args.action == "plan":
            result = {**state, "status": "planned", "selected": not args.without_anthropic, "confirmation_required": not state["installed"] and not args.without_anthropic}
            code = 0
        elif args.action in {"status", "verify"}:
            result = {**state, "status": "available" if state["installed"] else "unavailable"}
            code = 0 if state["installed"] else 3
        elif args.without_anthropic:
            result = {**state, "status": "skipped", "reason": "user opted out of the recommended dependency"}
            code = 0
        elif state["installed"]:
            result = {**state, "status": "already_installed"}
            code = 0
        elif not args.confirm_external_install:
            result = {**state, "status": "needs_confirmation", "confirmation_required": True, "instruction": "Review source, commit, license, destination, and network write; rerun with --confirm-external-install after explicit approval."}
            code = 3
        else:
            result = install(args.skill_root, item, args.installer, args.method)
            code = 0 if result["status"] in {"installed", "already_installed", "bound_existing"} else 3 if result["status"] == "blocked" else 1
        if args.report:
            write_json(args.report.resolve(), result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return code
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        result = {"status": "failed", "error": str(exc)}
        if args.report:
            write_json(args.report.resolve(), result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    sys.exit(main())
