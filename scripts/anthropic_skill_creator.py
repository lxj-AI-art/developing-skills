#!/usr/bin/env python3
"""Audited adapter for optional Anthropic skill-creator capabilities.

This script never downloads a provider package and never modifies a formal Skill.
It invokes only a caller-supplied or locally discovered Anthropic skill-creator root.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


CAPABILITY_FILES = {
    "validate": "scripts/quick_validate.py",
    "package": "scripts/package_skill.py",
    "description_optimize": "scripts/run_loop.py",
    "viewer": "eval-viewer/generate_review.py",
}
IGNORED_PARTS = {".git", "__pycache__", ".pytest_cache"}


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"root must be an object: {path}")
    return value


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if not path.is_file() or set(path.relative_to(root).parts) & IGNORED_PARTS:
            continue
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(file_digest(path)))
    return digest.hexdigest()


def bound_dependency_root(skill_root: Path) -> Path | None:
    dependency_base = skill_root.resolve() / "dependencies" / "anthropic-skill-creator"
    binding_path = dependency_base / "installation.json"
    if not binding_path.is_file():
        return None
    try:
        binding = read_object(binding_path)
        relative_root = Path(str(binding.get("relative_root", "")))
        if relative_root.is_absolute() or ".." in relative_root.parts:
            return None
        dependency_root = (skill_root.resolve() / relative_root).resolve()
        dependency_root.relative_to(dependency_base.resolve())
        if not dependency_root.is_dir() or binding.get("content_id") != tree_digest(dependency_root):
            return None
        return dependency_root
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def candidate_roots(explicit: Path | None) -> list[Path]:
    values: list[Path] = []
    if explicit:
        values.append(explicit)
    configured = os.environ.get("ANTHROPIC_SKILL_CREATOR_ROOT")
    if configured:
        values.append(Path(configured))
    dependency_root = bound_dependency_root(Path(__file__).resolve().parent.parent)
    if dependency_root:
        values.append(dependency_root)
    values.extend(
        [
            Path.cwd() / ".claude" / "skills" / "skill-creator",
            Path.cwd() / "skills" / "skill-creator",
            Path.home() / ".claude" / "skills" / "skill-creator",
        ]
    )
    result: list[Path] = []
    seen: set[Path] = set()
    for value in values:
        resolved = value.expanduser().resolve()
        if resolved not in seen:
            seen.add(resolved)
            result.append(resolved)
    return result


def inspect_root(root: Path) -> dict[str, Any]:
    capabilities = {
        name: (root / relative).is_file() for name, relative in CAPABILITY_FILES.items()
    }
    skill_file = root / "SKILL.md"
    recognized = skill_file.is_file() and capabilities["package"] and capabilities["viewer"]
    return {
        "provider": "anthropic-skill-creator",
        "root": str(root),
        "status": "available" if recognized else "unavailable",
        "recognized": recognized,
        "capabilities": capabilities,
        "content_id": tree_digest(root) if recognized else None,
    }


def discover_root(explicit: Path | None) -> tuple[Path | None, dict[str, Any]]:
    inspections = [inspect_root(path) for path in candidate_roots(explicit)]
    for path, inspection in zip(candidate_roots(explicit), inspections):
        if inspection["recognized"]:
            inspection["searched"] = [item["root"] for item in inspections]
            return path, inspection
    return None, {
        "provider": "anthropic-skill-creator",
        "status": "unavailable",
        "reason": "no recognized Anthropic skill-creator package was found; no download was attempted",
        "searched": [item["root"] for item in inspections],
        "inspections": inspections,
    }


def run_command(arguments: list[str], cwd: Path, timeout: int) -> dict[str, Any]:
    completed = subprocess.run(
        arguments,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return {
        "command": arguments,
        "exit_code": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def require_capability(root: Path, name: str) -> Path:
    path = root / CAPABILITY_FILES[name]
    if not path.is_file():
        raise RuntimeError(f"Anthropic skill-creator capability is unavailable: {name} ({path})")
    return path


def ensure_skill(path: Path) -> Path:
    resolved = path.resolve()
    if not (resolved / "SKILL.md").is_file():
        raise ValueError(f"not a Skill directory: {resolved}")
    return resolved


def safe_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")
    return slug[:80] or hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def copy_artifacts(paths: list[Any], output_dir: Path) -> list[str]:
    copied: list[str] = []
    output_dir.mkdir(parents=True, exist_ok=False)
    for index, raw in enumerate(paths):
        source = Path(str(raw)).resolve()
        if not source.is_file():
            continue
        target = output_dir / f"{index:03d}-{safe_slug(source.name)}"
        shutil.copy2(source, target)
        copied.append(target.name)
    return copied


def expectation_for_viewer(item: dict[str, Any]) -> dict[str, Any]:
    status = str(item.get("status", "inconclusive"))
    evidence = item.get("evidence", "")
    if isinstance(evidence, list):
        evidence = "; ".join(str(value) for value in evidence)
    return {
        "text": str(item.get("text") or item.get("id") or "expectation"),
        "passed": status == "pass",
        "evidence": str(evidence),
        "original_status": status,
        "severity": item.get("severity"),
    }


def export_viewer_workspace(benchmark_path: Path, eval_path: Path, workspace: Path) -> dict[str, Any]:
    benchmark = read_object(benchmark_path)
    eval_set = read_object(eval_path)
    if workspace.exists() and any(workspace.iterdir()):
        raise ValueError("viewer compatibility workspace must be new or empty")
    workspace.mkdir(parents=True, exist_ok=True)
    cases = {str(item["id"]): item for item in eval_set.get("cases", []) if isinstance(item, dict) and "id" in item}
    exported_runs: list[dict[str, Any]] = []
    mapping = {"candidate": "with_skill", "baseline": "without_skill"}
    for index, run in enumerate(benchmark.get("runs", []), start=1):
        if not isinstance(run, dict):
            continue
        case_id = str(run.get("case_id", f"case-{index}"))
        original_configuration = str(run.get("configuration", "unknown"))
        configuration = mapping.get(original_configuration, original_configuration)
        run_number = int(run.get("run_number", 1))
        run_dir = workspace / f"eval-{index:03d}-{safe_slug(case_id)}" / configuration / f"run-{run_number}"
        copied = copy_artifacts(run.get("artifacts", []), run_dir / "outputs")
        case = cases.get(case_id, {})
        metadata = {
            "eval_id": index,
            "eval_name": case_id,
            "prompt": case.get("prompt", ""),
            "configuration": configuration,
            "original_configuration": original_configuration,
            "run_number": run_number,
            "skill_name": eval_set.get("skill_name"),
        }
        write_json(run_dir / "eval_metadata.json", metadata)
        expectations = [
            expectation_for_viewer(item)
            for item in run.get("expectations", [])
            if isinstance(item, dict)
        ]
        status = str(run.get("status", "inconclusive"))
        grading = {
            "expectations": expectations,
            "summary": {
                "passed": sum(1 for item in expectations if item["original_status"] == "pass"),
                "failed": sum(1 for item in expectations if item["original_status"] == "fail"),
                "unresolved": sum(1 for item in expectations if item["original_status"] not in {"pass", "fail"}),
                "status": status,
                "score": run.get("score"),
            },
        }
        write_json(run_dir / "grading.json", grading)
        exported_runs.append({
            "eval_id": index,
            "eval_name": case_id,
            "configuration": configuration,
            "original_configuration": original_configuration,
            "run_number": run_number,
            "result": {"status": status, "score": run.get("score"), "artifacts": copied},
        })
    configurations = benchmark.get("configurations", {})
    run_summary = {
        mapping.get(str(name), str(name)): {
            "pass_rate": summary.get("determinate_pass_rate"),
            "runs": summary.get("runs"),
            "status_counts": summary.get("status_counts", {}),
            "score": summary.get("score", {}),
        }
        for name, summary in configurations.items()
        if isinstance(summary, dict)
    }
    compatible_benchmark = {
        "metadata": {
            "source_schema": benchmark.get("schema_version"),
            "source_benchmark": str(benchmark_path.resolve()),
            "configuration_mapping": mapping,
        },
        "runs": exported_runs,
        "run_summary": run_summary,
        "notes": [
            json.dumps(item, ensure_ascii=False) if isinstance(item, dict) else str(item)
            for item in (benchmark.get("analysis", {}).get("findings", []) if isinstance(benchmark.get("analysis"), dict) else [])
        ],
    }
    compatible_path = workspace / "benchmark-anthropic.json"
    write_json(compatible_path, compatible_benchmark)
    write_json(workspace / "compatibility-map.json", {
        "source_benchmark": str(benchmark_path.resolve()),
        "source_eval_set": str(eval_path.resolve()),
        "configuration_mapping": mapping,
        "exported_runs": len(exported_runs),
        "losses": ["non-binary developing-skills statuses remain in original_status because Anthropic passed is boolean"],
    })
    return {"workspace": str(workspace), "benchmark": str(compatible_path), "exported_runs": len(exported_runs)}


def frontmatter_description(skill_file: Path) -> str:
    text = skill_file.read_text(encoding="utf-8")
    match = re.search(r"(?m)^description:\s*(.+?)\s*$", text)
    if not match:
        raise ValueError(f"single-line frontmatter description not found: {skill_file}")
    raw = match.group(1).strip()
    if raw.startswith('"'):
        try:
            value = json.loads(raw)
            if isinstance(value, str):
                return value
        except json.JSONDecodeError:
            pass
    if len(raw) >= 2 and raw.startswith("'") and raw.endswith("'"):
        return raw[1:-1].replace("''", "'")
    return raw


def replace_frontmatter_description(skill_file: Path, description: str) -> None:
    if not description.strip() or "\n" in description or "\r" in description or len(description) > 1024:
        raise ValueError("optimized description must be one non-empty line of at most 1024 characters")
    text = skill_file.read_text(encoding="utf-8")
    replacement = "description: " + json.dumps(description.strip(), ensure_ascii=False)
    changed, count = re.subn(r"(?m)^description:\s*.*$", lambda _: replacement, text, count=1)
    if count != 1:
        raise ValueError("candidate must have exactly one single-line description")
    skill_file.write_text(changed, encoding="utf-8")


def trigger_eval_data(eval_set: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    values: list[dict[str, Any]] = []
    excluded = {"non_trigger": 0, "holdout": 0}
    for case in eval_set.get("cases", []):
        if not isinstance(case, dict) or case.get("mode", "execution") != "trigger":
            excluded["non_trigger"] += 1
            continue
        if case.get("split") == "holdout":
            excluded["holdout"] += 1
            continue
        tags = set(case.get("tags", []))
        if len(tags & {"should-trigger", "should-not-trigger"}) != 1:
            raise ValueError(f"trigger case {case.get('id')} lacks exactly one direction tag")
        values.append({"query": case.get("prompt", ""), "should_trigger": "should-trigger" in tags})
    if len(values) < 2 or len({item["should_trigger"] for item in values}) < 2:
        raise ValueError("Anthropic description optimization needs at least two visible trigger cases covering both directions")
    return values, excluded


def parse_last_json_object(text: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    objects: list[dict[str, Any]] = []
    for index, character in enumerate(text):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            objects.append(value)
    if not objects:
        raise ValueError("Anthropic run_loop output contains no JSON object")
    return objects[-1]


def print_result(value: dict[str, Any], report: Path | None) -> None:
    if report:
        write_json(report, value)
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, help="Anthropic skill-creator package root")
    parser.add_argument("--report", type=Path, help="also write the structured result here")
    subparsers = parser.add_subparsers(dest="action", required=True)

    subparsers.add_parser("detect")
    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("--skill", required=True, type=Path)
    validate_parser.add_argument("--timeout", type=int, default=120)
    package_parser = subparsers.add_parser("package")
    package_parser.add_argument("--skill", required=True, type=Path)
    package_parser.add_argument("--output-dir", required=True, type=Path)
    package_parser.add_argument("--timeout", type=int, default=120)
    viewer_parser = subparsers.add_parser("viewer")
    viewer_parser.add_argument("--benchmark", required=True, type=Path)
    viewer_parser.add_argument("--eval-set", required=True, type=Path)
    viewer_parser.add_argument("--workspace", required=True, type=Path)
    viewer_parser.add_argument("--static", required=True, type=Path)
    viewer_parser.add_argument("--skill-name", required=True)
    viewer_parser.add_argument("--timeout", type=int, default=300)
    description_parser = subparsers.add_parser("description-optimize")
    description_parser.add_argument("--formal-skill", required=True, type=Path)
    description_parser.add_argument("--candidate", required=True, type=Path)
    description_parser.add_argument("--eval-set", required=True, type=Path)
    description_parser.add_argument("--model", required=True)
    description_parser.add_argument("--results-dir", required=True, type=Path)
    description_parser.add_argument("--max-iterations", type=int, default=5)
    description_parser.add_argument("--runs-per-query", type=int, default=3)
    description_parser.add_argument("--timeout", type=int, default=600)
    description_parser.add_argument("--apply-to-candidate", action="store_true")

    args = parser.parse_args()
    root, detected = discover_root(args.root)
    if args.action == "detect":
        print_result(detected, args.report)
        return 0 if root else 3
    if root is None:
        print_result(detected, args.report)
        return 3
    try:
        if args.action == "validate":
            skill = ensure_skill(args.skill)
            script = require_capability(root, "validate")
            execution = run_command([sys.executable, str(script), str(skill)], root, args.timeout)
            result = {**detected, "action": "validate", "status": "completed" if execution["exit_code"] == 0 else "failed", "execution": execution}
        elif args.action == "package":
            skill = ensure_skill(args.skill)
            require_capability(root, "package")
            output_dir = args.output_dir.resolve()
            if skill == output_dir or skill in output_dir.parents:
                raise ValueError("package output directory must not be inside the candidate Skill")
            if output_dir.exists() and any(output_dir.iterdir()):
                raise ValueError("package output directory must be new or empty")
            output_dir.mkdir(parents=True, exist_ok=True)
            execution = run_command([sys.executable, "-m", "scripts.package_skill", str(skill), str(output_dir)], root, args.timeout)
            result = {**detected, "action": "package", "status": "completed" if execution["exit_code"] == 0 else "failed", "execution": execution, "output_dir": str(output_dir)}
        elif args.action == "viewer":
            script = require_capability(root, "viewer")
            exported = export_viewer_workspace(args.benchmark.resolve(), args.eval_set.resolve(), args.workspace.resolve())
            static = args.static.resolve()
            if static.exists():
                raise ValueError("viewer static output already exists; refusing to overwrite it")
            static.parent.mkdir(parents=True, exist_ok=True)
            command = [sys.executable, str(script), exported["workspace"], "--skill-name", args.skill_name, "--benchmark", exported["benchmark"], "--static", str(static)]
            execution = run_command(command, root, args.timeout)
            result = {**detected, "action": "viewer", "status": "completed" if execution["exit_code"] == 0 and static.is_file() else "failed", "execution": execution, "export": exported, "static": str(static)}
        else:
            require_capability(root, "description_optimize")
            formal = ensure_skill(args.formal_skill)
            candidate = ensure_skill(args.candidate)
            if formal == candidate or formal in candidate.parents or candidate in formal.parents:
                raise ValueError("formal Skill and candidate must be separate directories without containment")
            if shutil.which("claude") is None:
                result = {**detected, "action": "description-optimize", "status": "blocked", "reason": "Claude CLI is unavailable; Anthropic run_loop was not executed"}
                print_result(result, args.report)
                return 3
            formal_before = tree_digest(formal)
            eval_set = read_object(args.eval_set.resolve())
            trigger_cases, excluded = trigger_eval_data(eval_set)
            results_dir = args.results_dir.resolve()
            if results_dir.exists() and any(results_dir.iterdir()):
                raise ValueError("description optimization results directory must be new or empty")
            results_dir.mkdir(parents=True, exist_ok=True)
            inner_eval = results_dir / "anthropic-trigger-evals.json"
            write_json(inner_eval, trigger_cases)
            command = [
                sys.executable, "-m", "scripts.run_loop", "--eval-set", str(inner_eval),
                "--skill-path", str(candidate), "--description", frontmatter_description(candidate / "SKILL.md"),
                "--model", args.model, "--max-iterations", str(args.max_iterations),
                "--runs-per-query", str(args.runs_per_query), "--timeout", str(args.timeout),
                "--results-dir", str(results_dir),
            ]
            process_timeout = args.timeout * max(1, args.max_iterations) * max(1, args.runs_per_query) * max(1, len(trigger_cases)) + 60
            execution = run_command(command, root, process_timeout)
            best = parse_last_json_object(execution["stdout"]) if execution["exit_code"] == 0 else {}
            description = best.get("best_description")
            if args.apply_to_candidate and isinstance(description, str):
                replace_frontmatter_description(candidate / "SKILL.md", description)
            formal_after = tree_digest(formal)
            if formal_after != formal_before:
                raise RuntimeError("formal Skill changed during Anthropic description optimization")
            result = {
                **detected,
                "action": "description-optimize",
                "status": "completed" if execution["exit_code"] == 0 else "failed",
                "execution": execution,
                "best": best,
                "applied_to_candidate": bool(args.apply_to_candidate and isinstance(description, str)),
                "formal_skill_untouched": True,
                "excluded_cases": excluded,
                "final_holdout_exposed": False,
            }
        print_result(result, args.report)
        return 0 if result["status"] == "completed" else 1
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        result = {**detected, "action": args.action, "status": "failed", "error": str(exc)}
        print_result(result, args.report)
        return 1


if __name__ == "__main__":
    sys.exit(main())
