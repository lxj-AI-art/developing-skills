#!/usr/bin/env python3
"""Unified entrypoint for design, optimization, recovery, status, assurance views, and promotion packets."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from design_gate import file_hash, tree_hash


HERE = Path(__file__).resolve().parent


def load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {path}: {exc}") from None
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain an object")
    return value


def run_script(name: str, arguments: list[str]) -> int:
    return subprocess.run([sys.executable, str(HERE / name), *arguments], check=False).returncode


def highest_candidate(workspace: Path) -> tuple[int, Path]:
    candidates: list[tuple[int, Path]] = []
    for path in (workspace / "candidates").glob("iteration-*"):
        try:
            number = int(path.name.split("-", 1)[1])
        except (IndexError, ValueError):
            continue
        if (path / "SKILL.md").is_file():
            candidates.append((number, path.resolve()))
    if not candidates:
        raise ValueError("workspace contains no valid candidate")
    return max(candidates)


def workspace_status(workspace: Path) -> dict[str, Any]:
    history = load(workspace / "history.json")
    iteration, candidate = highest_candidate(workspace)
    current_id = tree_hash(candidate)
    formal_path = Path(history["formal_skill_path"]) if history.get("formal_skill_path") else None
    formal_unchanged = None
    if formal_path and formal_path.is_dir():
        formal_unchanged = tree_hash(formal_path) == history.get("formal_skill_content_id")
    frozen_eval = workspace / "eval" / "eval-set.json"
    holdout_runs = sum(int(item.get("holdout_runs", 0)) for item in history.get("iterations", []) if isinstance(item, dict))
    assurance = history.get("current_assurance", {}) if isinstance(history.get("current_assurance"), dict) else {}
    assurance_exact = not history.get("design_id") or assurance.get("candidate_id") == current_id
    return {
        "schema_version": "1.0",
        "workspace": str(workspace.resolve()),
        "history_status": history.get("status"),
        "latest_iteration": iteration,
        "latest_candidate": str(candidate),
        "latest_candidate_id": current_id,
        "current_assurance_exact": assurance_exact,
        "formal_skill_unchanged": formal_unchanged,
        "frozen_eval_present": frozen_eval.is_file(),
        "holdout_runs_consumed": holdout_runs,
        "recoverable": frozen_eval.is_file() and formal_unchanged is not False and assurance_exact,
    }


def assurance_paths(workspace: Path, candidate_id: str) -> dict[str, Path]:
    for report_path in sorted((workspace / "assurance").glob("iteration-*/refresh-report.json"), reverse=True):
        report = load(report_path)
        if report.get("status") == "pass" and report.get("candidate_id") == candidate_id:
            return {
                "traceability": Path(report["paths"]["traceability"]),
                "traceability_validation": Path(report["paths"]["traceability_validation"]),
                "conformance_review": Path(report["paths"]["conformance_review"]),
            }
    return {
        "traceability": workspace / "design" / "traceability.json",
        "traceability_validation": workspace / "design" / "traceability-validation.json",
        "conformance_review": workspace / "design" / "conformance-review.json",
    }


def resume(args: argparse.Namespace) -> int:
    workspace = args.workspace.resolve()
    successor = args.successor_workspace.resolve()
    if successor.exists() and any(successor.iterdir()):
        raise ValueError("successor workspace must be new or empty")
    status = workspace_status(workspace)
    if not status["recoverable"]:
        raise ValueError("workspace integrity does not support recovery")
    if status["holdout_runs_consumed"] and not args.replacement_eval_set:
        raise ValueError("prior workspace already consumed holdout runs; provide a replacement eval set to avoid repeated holdout fitting")
    _, candidate = highest_candidate(workspace)
    eval_path = args.replacement_eval_set.resolve() if args.replacement_eval_set else workspace / "eval" / "eval-set.json"
    command = [
        "--skill", str(candidate), "--eval-set", str(eval_path), "--workspace", str(successor),
        "--adapter-command", args.adapter_command, "--baseline-mode", "original",
        "--adapter-retries", str(args.adapter_retries),
    ]
    design = workspace / "design" / "skill-design.md"
    approval = workspace / "design" / "design-approval.json"
    if design.is_file() and approval.is_file():
        if args.replacement_eval_set:
            if not all((args.replacement_traceability, args.replacement_traceability_validation, args.replacement_conformance_review)):
                raise ValueError("a replacement eval set changes assurance identity; provide replacement traceability, validation, and conformance evidence")
            paths = {
                "traceability": args.replacement_traceability.resolve(),
                "traceability_validation": args.replacement_traceability_validation.resolve(),
                "conformance_review": args.replacement_conformance_review.resolve(),
            }
        else:
            paths = assurance_paths(workspace, status["latest_candidate_id"])
        command += ["--design", str(design), "--approval", str(approval)]
        for option, key in (("--traceability", "traceability"), ("--traceability-validation", "traceability_validation"), ("--conformance-review", "conformance_review")):
            if paths[key].is_file():
                command += [option, str(paths[key])]
        if args.assurance_reviewer_id:
            command += ["--assurance-reviewer-id", args.assurance_reviewer_id, "--modifier-id", args.modifier_id]
    successor.parent.mkdir(parents=True, exist_ok=True)
    lineage = {
        "schema_version": "1.0",
        "source_workspace": str(workspace),
        "source_history_id": file_hash(workspace / "history.json"),
        "source_candidate_id": status["latest_candidate_id"],
        "holdout_runs_consumed_in_source": status["holdout_runs_consumed"],
        "replacement_eval_set": str(eval_path) if args.replacement_eval_set else None,
        "evidence_overwritten": False,
    }
    completed = subprocess.run([sys.executable, str(HERE / "run_optimization.py"), *command], check=False)
    successor.mkdir(exist_ok=True)
    (successor / "recovery-lineage.json").write_text(json.dumps(lineage, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return completed.returncode


def main() -> int:
    passthrough = {
        "design": "design_gate.py",
        "run": "run_optimization.py",
        "assure": "refresh_assurance.py",
        "audit-data": "audit_eval_set.py",
        "holdout": "holdout_vault.py",
        "calibrate-grader": "calibrate_grader.py",
        "validate-professional": "validate_professional_suite.py",
        "audit-dependency": "audit_dependency_update.py",
        "promotion-packet": "build_promotion_packet.py",
        "viewer": "generate_assurance_view.py",
    }
    if len(sys.argv) > 1 and sys.argv[1] in passthrough:
        return run_script(passthrough[sys.argv[1]], sys.argv[2:])
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("design", help="delegate to design_gate.py; append its normal arguments")
    sub.add_parser("run", help="delegate to run_optimization.py; append its normal arguments")
    sub.add_parser("assure", help="refresh exact-candidate assurance evidence")
    sub.add_parser("audit-data", help="audit labeled-case quality and coverage")
    sub.add_parser("holdout", help="create, checkout, or inspect a governed holdout vault")
    sub.add_parser("calibrate-grader", help="calibrate one or more graders")
    sub.add_parser("validate-professional", help="validate real-domain evidence readiness")
    sub.add_parser("audit-dependency", help="audit a staged dependency update")
    status_parser = sub.add_parser("status", help="inspect workspace identity and recovery readiness")
    status_parser.add_argument("--workspace", required=True, type=Path)
    resume_parser = sub.add_parser("resume", help="create an evidence-preserving successor run")
    resume_parser.add_argument("--workspace", required=True, type=Path)
    resume_parser.add_argument("--successor-workspace", required=True, type=Path)
    resume_parser.add_argument("--adapter-command", required=True)
    resume_parser.add_argument("--replacement-eval-set", type=Path)
    resume_parser.add_argument("--replacement-traceability", type=Path)
    resume_parser.add_argument("--replacement-traceability-validation", type=Path)
    resume_parser.add_argument("--replacement-conformance-review", type=Path)
    resume_parser.add_argument("--assurance-reviewer-id")
    resume_parser.add_argument("--modifier-id", default="candidate-modifier")
    resume_parser.add_argument("--adapter-retries", type=int, default=0)
    packet = sub.add_parser("promotion-packet", help="build a non-publishing human approval packet")
    packet.add_argument("arguments", nargs=argparse.REMAINDER)
    viewer = sub.add_parser("viewer", help="render the unified assurance HTML view")
    viewer.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        if args.action == "status":
            print(json.dumps(workspace_status(args.workspace.resolve()), ensure_ascii=False, indent=2))
            return 0
        if args.action == "resume":
            if args.adapter_retries < 0:
                raise ValueError("adapter-retries must be >= 0")
            return resume(args)
        raise ValueError("unknown action")
    except (OSError, ValueError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
