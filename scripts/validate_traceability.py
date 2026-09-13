#!/usr/bin/env python3
"""Create and validate design-to-implementation-to-evaluation traceability."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from design_gate import file_hash, tree_hash, validate_approval, validate_design
from validate_candidate import validate_candidate
from validate_eval_set import validate as validate_eval


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def validate_traceability(trace_path: Path, design_path: Path, approval_path: Path, candidate: Path, eval_path: Path) -> dict[str, Any]:
    design = validate_design(design_path, True)
    approval = validate_approval(design, approval_path)
    candidate_report = validate_candidate(candidate)
    eval_set = load(eval_path)
    eval_errors, eval_warnings, _ = validate_eval(eval_set, eval_path, True)
    trace = load(trace_path)
    errors: list[str] = []
    warnings = list(eval_warnings)
    if not design["valid"]: errors.extend(design["errors"])
    if not approval["valid"]: errors.extend(approval["errors"])
    if not candidate_report["valid"]: errors.extend(candidate_report["errors"])
    errors.extend(eval_errors)
    identities = {"design_id": design.get("design_id"), "approval_id": approval.get("approval_id"), "candidate_id": candidate_report.get("skill_id"), "eval_set_id": file_hash(eval_path.resolve())}
    for key, value in identities.items():
        if trace.get(key) != value: errors.append(f"traceability {key} mismatch")
    cases = {case["id"]: {item["id"] for item in case.get("expectations", [])} for case in eval_set.get("cases", []) if isinstance(case, dict) and isinstance(case.get("id"), str)}
    design_items = {item["id"]: item for item in design.get("design_items", []) if isinstance(item, dict) and isinstance(item.get("id"), str)}
    mappings = trace.get("mappings")
    if not isinstance(mappings, list):
        errors.append("mappings must be an array"); mappings = []
    by_id: dict[str, dict[str, Any]] = {}
    for index, mapping in enumerate(mappings):
        if not isinstance(mapping, dict): errors.append(f"mappings[{index}] must be an object"); continue
        item_id = mapping.get("design_item_id")
        if item_id not in design_items: errors.append(f"unknown design_item_id: {item_id}"); continue
        if item_id in by_id: errors.append(f"duplicate mapping: {item_id}")
        by_id[item_id] = mapping
        implementations = mapping.get("implementation", [])
        evaluations = mapping.get("evaluations", [])
        if not isinstance(implementations, list): errors.append(f"{item_id}.implementation must be an array"); implementations = []
        if not isinstance(evaluations, list): errors.append(f"{item_id}.evaluations must be an array"); evaluations = []
        for entry in implementations:
            if not isinstance(entry, dict) or entry.get("kind") not in {"file", "external"}:
                errors.append(f"{item_id} has invalid implementation entry"); continue
            if entry["kind"] == "file":
                raw = entry.get("path")
                if not isinstance(raw, str) or not raw or Path(raw).is_absolute() or ".." in Path(raw).parts or not (candidate.resolve() / raw).is_file():
                    errors.append(f"{item_id} references missing or unsafe candidate file: {raw}")
            elif entry.get("status") != "verified" or not entry.get("evidence_ref"):
                errors.append(f"{item_id} external dependency is not verified with evidence")
        for entry in evaluations:
            if not isinstance(entry, dict) or entry.get("case_id") not in cases:
                errors.append(f"{item_id} references unknown eval case"); continue
            expectation_ids = entry.get("expectation_ids")
            if not isinstance(expectation_ids, list) or not expectation_ids:
                errors.append(f"{item_id} eval mapping needs expectation_ids"); continue
            unknown = set(expectation_ids) - cases[entry["case_id"]]
            if unknown: errors.append(f"{item_id} references unknown expectations: {sorted(unknown)}")
    critical_total = critical_covered = 0
    for item_id, item in design_items.items():
        mapping = by_id.get(item_id, {})
        has_impl = bool(mapping.get("implementation")); has_eval = bool(mapping.get("evaluations"))
        if item.get("criticality") == "critical":
            critical_total += 1
            if has_impl and has_eval: critical_covered += 1
            else: errors.append(f"critical design item lacks complete coverage: {item_id}")
        elif not has_impl or not has_eval:
            warnings.append(f"noncritical design item has incomplete coverage: {item_id}")
    result = {"schema_version": "1.0", "status": "pass" if not errors else "fail", **identities, "traceability_source_id": file_hash(trace_path.resolve()), "coverage": {"critical_total": critical_total, "critical_covered": critical_covered, "mapped_items": len(by_id), "design_items": len(design_items)}, "errors": errors, "warnings": warnings}
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    init = sub.add_parser("init"); check = sub.add_parser("validate")
    for command in (init, check):
        command.add_argument("--design", required=True, type=Path); command.add_argument("--approval", required=True, type=Path); command.add_argument("--candidate", required=True, type=Path); command.add_argument("--eval-set", required=True, type=Path); command.add_argument("--output", required=True, type=Path)
    if check is not None: check.add_argument("--traceability", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.action == "init":
            design = validate_design(args.design, True); approval = validate_approval(design, args.approval); candidate = validate_candidate(args.candidate)
            eval_set = load(args.eval_set); errors, _, _ = validate_eval(eval_set, args.eval_set, True)
            if not design["valid"] or not approval["valid"] or not candidate["valid"] or errors: raise ValueError("inputs do not pass their gates")
            value = {"schema_version": "1.0", "design_id": design["design_id"], "approval_id": approval["approval_id"], "candidate_id": candidate["skill_id"], "eval_set_id": file_hash(args.eval_set.resolve()), "mappings": [{"design_item_id": item["id"], "implementation": [], "evaluations": []} for item in design["design_items"]]}
        else:
            value = validate_traceability(args.traceability, args.design, args.approval, args.candidate, args.eval_set)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        if args.output.exists(): raise ValueError("refusing to overwrite existing output")
        args.output.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(value, ensure_ascii=False, indent=2)); return 0 if value.get("status", "pass") == "pass" else 1
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr); return 2


if __name__ == "__main__": sys.exit(main())
