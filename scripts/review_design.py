#!/usr/bin/env python3
"""Run and validate independent semantic design or implementation-conformance reviews."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from command_line import split_command
from design_gate import file_hash, tree_hash, validate_approval, validate_design


DIMENSIONS = ("requirements", "method", "capability", "evaluation", "safety")


def validate_response(value: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, dict):
        return ["reviewer response must be a JSON object"]
    if value.get("status") not in {"pass", "fail", "inconclusive", "blocked"}:
        errors.append("status must be pass, fail, inconclusive, or blocked")
    if value.get("reviewer_independence") not in {"verified", "human", "unverified"}:
        errors.append("reviewer_independence is invalid")
    if not isinstance(value.get("reviewer_id"), str) or not value["reviewer_id"].strip():
        errors.append("reviewer_id must be non-empty")
    dimensions = value.get("dimensions")
    for name in DIMENSIONS:
        dimension = dimensions.get(name) if isinstance(dimensions, dict) else None
        if not isinstance(dimension, dict) or dimension.get("status") not in {"pass", "fail", "inconclusive", "blocked"}:
            errors.append(f"dimension {name} has invalid status")
        elif not isinstance(dimension.get("evidence"), list) or not all(isinstance(x, str) and x.strip() for x in dimension["evidence"]):
            errors.append(f"dimension {name} requires textual evidence")
    if not isinstance(value.get("findings", []), list) or not isinstance(value.get("required_changes", []), list):
        errors.append("findings and required_changes must be arrays")
    return errors


def invoke(command: list[str], job: dict[str, Any], timeout: int) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="developing-skills-review-") as temporary:
        job_path = Path(temporary) / "job.json"
        response_path = Path(temporary) / "response.json"
        job_path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
        run = subprocess.run(command + [str(job_path), str(response_path)], capture_output=True, text=True, timeout=timeout, check=False)
        if run.returncode:
            raise ValueError(f"review adapter failed: {run.stderr or run.stdout}")
        if not response_path.is_file():
            raise ValueError("review adapter did not create response JSON")
        return json.loads(response_path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("review_type", choices=("design", "conformance"))
    parser.add_argument("--design", required=True, type=Path)
    parser.add_argument("--approval", type=Path)
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--eval-set", type=Path)
    parser.add_argument("--traceability-validation", type=Path)
    parser.add_argument("--adapter-command", required=True)
    parser.add_argument("--reviewer-id", required=True)
    parser.add_argument("--modifier-id", required=True, help="identity that authored the design/candidate; cannot be the sole reviewer")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        report = validate_design(args.design, True)
        if not report["valid"]:
            raise ValueError("; ".join(report["errors"]))
        if args.modifier_id == args.reviewer_id:
            raise ValueError("reviewer_id must differ from modifier_id")
        job: dict[str, Any] = {
            "job_type": f"review_{args.review_type}", "design_path": str(args.design.resolve()),
            "design_id": report["design_id"], "reviewer_id": args.reviewer_id,
            "required_dimensions": list(DIMENSIONS),
        }
        approval = None
        if args.review_type == "conformance":
            if not all((args.approval, args.candidate, args.eval_set, args.traceability_validation)):
                raise ValueError("conformance review requires approval, candidate, eval-set, and traceability validation")
            approval = validate_approval(report, args.approval)
            if not approval["valid"]:
                raise ValueError("; ".join(approval["errors"]))
            trace = json.loads(args.traceability_validation.read_text(encoding="utf-8"))
            if trace.get("status") != "pass":
                raise ValueError("traceability validation must pass before conformance review")
            job.update({
                "candidate_path": str(args.candidate.resolve()), "candidate_id": tree_hash(args.candidate.resolve()),
                "eval_set_path": str(args.eval_set.resolve()), "eval_set_id": file_hash(args.eval_set.resolve()),
                "approval_id": approval["approval_id"], "traceability_validation_path": str(args.traceability_validation.resolve()),
            })
        command = split_command(args.adapter_command)
        if not command:
            raise ValueError("adapter command is empty")
        response = invoke(command, job, args.timeout)
        errors = validate_response(response)
        if response.get("reviewer_id") != args.reviewer_id:
            errors.append("response reviewer_id does not match controller-assigned identity")
        result = dict(response)
        result.update({"schema_version": "1.0", "review_type": args.review_type, "design_id": report["design_id"]})
        if args.review_type == "conformance":
            result.update({"approval_id": approval["approval_id"], "candidate_id": job["candidate_id"], "eval_set_id": job["eval_set_id"]})
        result["controller_validation"] = {"valid": not errors, "errors": errors}
        if errors:
            result["status"] = "blocked"
        args.output.parent.mkdir(parents=True, exist_ok=True)
        if args.output.exists():
            raise ValueError("refusing to overwrite existing review artifact")
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if not errors and result.get("status") == "pass" else 1
    except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
