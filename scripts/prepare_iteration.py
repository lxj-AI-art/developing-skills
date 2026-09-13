#!/usr/bin/env python3
"""Create immutable eval input and an isolated candidate copy for one iteration."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from design_gate import file_hash as assurance_file_hash
from design_gate import freeze_approved_design, tree_hash as design_tree_hash, validate_conformance
from design_gate import validate_approval, validate_design
from validate_eval_set import load_json, validate


IGNORED_PARTS = {".git", "__pycache__", ".pytest_cache"}


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file() and not set(item.parts) & IGNORED_PARTS):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_hash(path).encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skill", required=True, type=Path)
    parser.add_argument("--eval-set", required=True, type=Path)
    parser.add_argument("--design", type=Path, help="ready skill-design.md to freeze and bind")
    parser.add_argument("--approval", type=Path, help="version-bound design approval")
    parser.add_argument("--traceability-validation", type=Path)
    parser.add_argument("--conformance-review", type=Path)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--iteration", required=True, type=int)
    parser.add_argument("--parent-candidate", type=Path)
    parser.add_argument("--baseline-mode", choices=("original", "no-skill"), default="original")
    args = parser.parse_args()

    skill = args.skill.resolve()
    eval_set = args.eval_set.resolve()
    workspace = args.workspace.resolve()
    if args.iteration < 1:
        parser.error("--iteration must be >= 1")
    if not (skill / "SKILL.md").is_file():
        parser.error(f"not a skill directory: {skill}")
    if not eval_set.is_file():
        parser.error(f"eval set not found: {eval_set}")
    try:
        workspace.relative_to(skill)
    except ValueError:
        pass
    else:
        parser.error("workspace must not be inside the skill being optimized")

    try:
        eval_data = load_json(eval_set)
        eval_errors, _, _ = validate(eval_data, eval_set, True)
    except ValueError as exc:
        parser.error(str(exc))
    if eval_errors:
        parser.error("invalid eval set: " + "; ".join(eval_errors))
    policy = eval_data.get("policy", {}) if isinstance(eval_data.get("policy"), dict) else {}
    requires_design = any(bool(policy.get(key, False)) for key in ("require_design_binding", "require_design_review", "require_traceability", "require_conformance_review"))
    if not args.design and any((args.approval, args.traceability_validation, args.conformance_review)):
        parser.error("approval, traceability, and conformance arguments require --design")
    if requires_design and not args.design:
        parser.error("eval policy requires --design")
    if args.design and not args.approval:
        parser.error("--design requires --approval")
    if bool(policy.get("require_traceability", False)) and not args.traceability_validation:
        parser.error("eval policy requires --traceability-validation")
    if bool(policy.get("require_conformance_review", False)) and not args.conformance_review:
        parser.error("eval policy requires --conformance-review")

    design_report = approval_report = None
    if args.design:
        design_report = validate_design(args.design, True)
        if not design_report["valid"]:
            parser.error("; ".join(design_report["errors"]))
        approval_report = validate_approval(design_report, args.approval)
        if not approval_report["valid"]:
            parser.error("; ".join(approval_report["errors"]))
        if bool(policy.get("require_design_review", False)) and not approval_report["approval"].get("review_artifact_id"):
            parser.error("eval policy requires an approval backed by semantic design review")
        if design_report["skill_name"] != skill_name_from_path(skill):
            parser.error("design skill_name does not match candidate")
    candidate_id_now = design_tree_hash(skill)
    traceability_id = conformance_id = None
    if args.traceability_validation:
        trace = load_json(args.traceability_validation)
        if trace.get("status") != "pass" or trace.get("design_id") != design_report["design_id"] or trace.get("approval_id") != approval_report["approval_id"] or trace.get("candidate_id") != candidate_id_now or trace.get("eval_set_id") != assurance_file_hash(eval_set):
            parser.error("traceability validation is not a pass for these exact frozen inputs")
        traceability_id = assurance_file_hash(args.traceability_validation)
    if args.conformance_review:
        conformance = validate_conformance(design_report, approval_report, skill, args.conformance_review)
        if not conformance["valid"] or conformance["review"].get("eval_set_id") != assurance_file_hash(eval_set):
            parser.error("conformance review is not a pass for these exact frozen inputs")
        conformance_id = conformance["conformance_id"]

    workspace.mkdir(parents=True, exist_ok=True)
    design_id = None
    if args.design:
        try:
            design_report = freeze_approved_design(args.design, args.approval, workspace / "design" / "skill-design.md", skill)
        except ValueError as exc:
            parser.error(str(exc))
        design_id = design_report["design_id"]
        for source, name in ((args.traceability_validation, "traceability-validation.json"), (args.conformance_review, "conformance-review.json")):
            if source:
                shutil.copy2(source, workspace / "design" / name)
    frozen_eval = workspace / "eval-set.json"
    eval_id = file_hash(eval_set)
    if frozen_eval.exists():
        if file_hash(frozen_eval) != eval_id:
            parser.error("workspace already contains a different eval-set.json; use a new workspace")
    else:
        shutil.copy2(eval_set, frozen_eval)

    baseline_id = "no-skill"
    if args.baseline_mode == "original":
        baseline = workspace / "baseline" / "original"
        if not baseline.exists():
            baseline.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(skill, baseline, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"))
        baseline_id = tree_hash(baseline)

    source = args.parent_candidate.resolve() if args.parent_candidate else skill
    if not (source / "SKILL.md").is_file():
        parser.error(f"candidate source is not a skill directory: {source}")
    candidate = workspace / "candidates" / f"iteration-{args.iteration}"
    if candidate.exists():
        parser.error(f"candidate already exists: {candidate}")
    candidate.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, candidate, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"))
    candidate_id = tree_hash(candidate)
    manifest = {
        "schema_version": "2.0",
        "iteration": args.iteration,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "candidate_path": str(candidate),
        "candidate_id_before_change": candidate_id,
        "candidate_source": str(source),
        "baseline_mode": args.baseline_mode,
        "baseline_id": baseline_id,
        "eval_set_id": eval_id,
        "design_id": design_id,
        "approval_id": approval_report["approval_id"] if approval_report else None,
        "traceability_id": traceability_id,
        "conformance_review_id": conformance_id,
        "formal_skill_untouched": True,
    }
    manifest_path = workspace / "metadata" / f"iteration-{args.iteration}.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


def skill_name_from_path(skill: Path) -> str | None:
    text = (skill / "SKILL.md").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("name:"):
            return line.split(":", 1)[1].strip().strip("\"'")
    return None


if __name__ == "__main__":
    sys.exit(main())
