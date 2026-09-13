#!/usr/bin/env python3
"""Refresh traceability and independent conformance evidence for an exact changed candidate."""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from design_gate import bind, file_hash, tree_hash, validate_approval, validate_conformance, validate_design
from review_design import DIMENSIONS, validate_response
from validate_traceability import load, validate_traceability


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


class Adapter:
    def __init__(self, command: list[str], timeout: int, jobs_root: Path) -> None:
        self.command = command
        self.timeout = timeout
        self.jobs_root = jobs_root

    def call(self, job: dict[str, Any], relative: Path) -> dict[str, Any]:
        job_path = self.jobs_root / relative.with_suffix(".job.json")
        response_path = self.jobs_root / relative.with_suffix(".response.json")
        log_path = self.jobs_root / relative.with_suffix(".adapter.log")
        write_json(job_path, job)
        response_path.parent.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        completed = subprocess.run([*self.command, str(job_path), str(response_path)], capture_output=True, text=True, timeout=self.timeout, check=False)
        log_path.write_text(f"exit_code: {completed.returncode}\nelapsed_seconds: {time.monotonic() - started:.6f}\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}\n", encoding="utf-8")
        if completed.returncode:
            raise RuntimeError(f"review adapter failed; see {log_path}")
        if not response_path.is_file():
            raise RuntimeError("review adapter did not create response JSON")
        response = load(response_path)
        if response.get("job_id") not in (None, job["job_id"]):
            raise RuntimeError("review adapter returned a mismatched job_id")
        response["job_id"] = job["job_id"]
        write_json(response_path, response)
        return response


def affected_scope(trace: dict[str, Any], changed_files: list[str]) -> dict[str, Any]:
    changed = set(changed_files)
    items: set[str] = set()
    cases: set[str] = set()
    untracked = set(changed)
    for mapping in trace.get("mappings", []):
        if not isinstance(mapping, dict):
            continue
        paths = {str(entry.get("path")) for entry in mapping.get("implementation", []) if isinstance(entry, dict) and entry.get("kind") == "file"}
        matched = {path for path in changed if path in paths}
        if not matched:
            continue
        untracked -= matched
        items.add(str(mapping.get("design_item_id")))
        for evaluation in mapping.get("evaluations", []):
            if isinstance(evaluation, dict) and evaluation.get("case_id"):
                cases.add(str(evaluation["case_id"]))
    return {
        "changed_files": sorted(changed),
        "affected_design_items": sorted(items),
        "affected_eval_cases": sorted(cases),
        "untracked_changed_files": sorted(untracked),
        "revalidation_scope": "full" if untracked else "mapped-plus-full-regression",
    }


def refresh(
    *,
    design_path: Path,
    approval_path: Path,
    traceability_source: Path,
    candidate: Path,
    eval_path: Path,
    output_dir: Path,
    adapter: Any,
    reviewer_id: str,
    modifier_id: str,
    provider: str,
    iteration: int,
    changed_files: list[str] | None = None,
) -> dict[str, Any]:
    if reviewer_id == modifier_id:
        raise ValueError("reviewer_id must differ from modifier_id")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError("assurance output directory must be new or empty")
    output_dir.mkdir(parents=True, exist_ok=True)
    design = validate_design(design_path, True)
    if not design["valid"]:
        raise ValueError("design gate failed: " + "; ".join(design["errors"]))
    approval = validate_approval(design, approval_path)
    if not approval["valid"]:
        raise ValueError("approval gate failed: " + "; ".join(approval["errors"]))
    trace = load(traceability_source)
    rebound = dict(trace)
    rebound.update({
        "schema_version": "1.0",
        "design_id": design["design_id"],
        "approval_id": approval["approval_id"],
        "candidate_id": tree_hash(candidate),
        "eval_set_id": file_hash(eval_path),
    })
    trace_path = output_dir / "traceability.json"
    write_json(trace_path, rebound)
    validation = validate_traceability(trace_path, design_path, approval_path, candidate, eval_path)
    validation_path = output_dir / "traceability-validation.json"
    write_json(validation_path, validation)
    impact = affected_scope(rebound, changed_files or [])
    write_json(output_dir / "change-impact.json", impact)
    if validation.get("status") != "pass":
        return {"status": "traceability_failed", "validation": validation, "impact": impact, "output_dir": str(output_dir)}

    job_id = f"review_conformance:assurance-refresh:{iteration}:{tree_hash(candidate)[:12]}"
    job = {
        "schema_version": "1.0",
        "job_type": "review_conformance",
        "job_id": job_id,
        "design_path": str(design_path.resolve()),
        "design_id": design["design_id"],
        "approval_id": approval["approval_id"],
        "candidate_path": str(candidate.resolve()),
        "candidate_id": tree_hash(candidate),
        "eval_set_path": str(eval_path.resolve()),
        "eval_set_id": file_hash(eval_path),
        "traceability_validation_path": str(validation_path.resolve()),
        "reviewer_id": reviewer_id,
        "required_dimensions": list(DIMENSIONS),
        "constraints": {"independent_from_modifier": modifier_id, "inspect_real_candidate": True, "do_not_modify_candidate": True},
    }
    response = adapter.call(job, Path(f"iteration-{iteration}") / "assurance" / "conformance")
    errors = validate_response(response)
    if response.get("reviewer_id") != reviewer_id:
        errors.append("response reviewer_id does not match controller-assigned identity")
    if response.get("reviewer_independence") not in {"verified", "human"}:
        errors.append("high-assurance refresh requires verified or human reviewer independence")
    conformance = dict(response)
    conformance.update({
        "schema_version": "1.0",
        "review_type": "conformance",
        "design_id": design["design_id"],
        "approval_id": approval["approval_id"],
        "candidate_id": tree_hash(candidate),
        "eval_set_id": file_hash(eval_path),
        "controller_validation": {"valid": not errors, "errors": errors},
    })
    if errors:
        conformance["status"] = "blocked"
    conformance_path = output_dir / "conformance-review.json"
    write_json(conformance_path, conformance)
    checked = validate_conformance(design, approval, candidate, conformance_path)
    if not checked["valid"] or conformance.get("eval_set_id") != file_hash(eval_path):
        return {"status": "conformance_failed", "conformance": conformance, "errors": checked["errors"], "impact": impact, "output_dir": str(output_dir)}
    binding_path = output_dir / "implementation-binding.json"
    binding = bind(
        design,
        candidate,
        binding_path,
        provider,
        approval_path=approval_path,
        stage="implementation",
        traceability_validation=validation_path,
        conformance_review=conformance_path,
    )
    return {
        "status": "pass",
        "design_id": design["design_id"],
        "approval_id": approval["approval_id"],
        "candidate_id": tree_hash(candidate),
        "traceability_id": file_hash(validation_path),
        "conformance_review_id": file_hash(conformance_path),
        "implementation_binding_id": file_hash(binding_path),
        "paths": {
            "traceability": str(trace_path),
            "traceability_validation": str(validation_path),
            "conformance_review": str(conformance_path),
            "implementation_binding": str(binding_path),
        },
        "impact": impact,
        "binding": binding,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--design", required=True, type=Path)
    parser.add_argument("--approval", required=True, type=Path)
    parser.add_argument("--traceability", required=True, type=Path)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--eval-set", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--adapter-command", required=True)
    parser.add_argument("--reviewer-id", required=True)
    parser.add_argument("--modifier-id", required=True)
    parser.add_argument("--provider", default="configured-runtime")
    parser.add_argument("--iteration", type=int, default=1)
    parser.add_argument("--changed-file", action="append", default=[])
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    command = shlex.split(args.adapter_command)
    if not command:
        parser.error("adapter command must not be empty")
    try:
        report = refresh(
            design_path=args.design.resolve(), approval_path=args.approval.resolve(),
            traceability_source=args.traceability.resolve(), candidate=args.candidate.resolve(),
            eval_path=args.eval_set.resolve(), output_dir=args.output_dir.resolve(),
            adapter=Adapter(command, args.timeout, args.output_dir.resolve() / "jobs"),
            reviewer_id=args.reviewer_id, modifier_id=args.modifier_id, provider=args.provider,
            iteration=args.iteration, changed_files=args.changed_file,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["status"] == "pass" else 1
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
