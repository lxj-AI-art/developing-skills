#!/usr/bin/env python3
"""Validate real-domain Skill evidence and compute expert/safety readiness gates."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any


def load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError(f"file not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON at {path}: {exc}") from None


def agreement(ratings: list[dict[str, Any]]) -> dict[str, Any]:
    comparable = [item for item in ratings if isinstance(item, dict) and isinstance(item.get("decisions"), list) and len(item["decisions"]) >= 2]
    if not comparable:
        return {"rated_cases": 0, "observed_agreement": None, "all_agree": 0}
    all_agree = sum(1 for item in comparable if len(set(str(value) for value in item["decisions"])) == 1)
    return {"rated_cases": len(comparable), "observed_agreement": round(all_agree / len(comparable), 6), "all_agree": all_agree}


def validate_suite(data: Any, source: Path, check_files: bool) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(data, dict):
        return {"status": "fail", "errors": ["root must be an object"], "warnings": []}
    if data.get("schema_version") != "1.0":
        errors.append("schema_version must be '1.0'")
    domain = data.get("domain")
    if not isinstance(domain, str) or not domain.strip():
        errors.append("domain must be non-empty")
    validation_class = data.get("validation_class")
    if validation_class not in {"constructed", "real"}:
        errors.append("validation_class must be constructed or real")
    policy = data.get("policy", {})
    if not isinstance(policy, dict):
        errors.append("policy must be an object")
        policy = {}
    min_cases = policy.get("min_cases", 1)
    min_reviewers = policy.get("min_independent_reviewers", 1)
    max_false_certain = policy.get("max_false_certain_rate", 0.0)
    min_agreement = policy.get("min_observed_agreement", 0.8)
    if not isinstance(min_cases, int) or isinstance(min_cases, bool) or min_cases < 1:
        errors.append("policy.min_cases must be an integer >= 1")
        min_cases = 1
    if not isinstance(min_reviewers, int) or isinstance(min_reviewers, bool) or min_reviewers < 1:
        errors.append("policy.min_independent_reviewers must be an integer >= 1")
        min_reviewers = 1
    for name, value in (("max_false_certain_rate", max_false_certain), ("min_observed_agreement", min_agreement)):
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= float(value) <= 1:
            errors.append(f"policy.{name} must be between 0 and 1")

    cases = data.get("cases")
    if not isinstance(cases, list):
        errors.append("cases must be an array")
        cases = []
    if len(cases) < min_cases:
        errors.append(f"suite has {len(cases)} cases; policy requires {min_cases}")
    seen: set[str] = set()
    solver_counts: Counter[str] = Counter()
    family_counts: Counter[str] = Counter()
    ratings: list[dict[str, Any]] = []
    real_eligible = validation_class == "real"
    for index, case in enumerate(cases):
        prefix = f"cases[{index}]"
        if not isinstance(case, dict):
            errors.append(f"{prefix} must be an object")
            continue
        case_id = case.get("case_id")
        if not isinstance(case_id, str) or not case_id.strip():
            errors.append(f"{prefix}.case_id must be non-empty")
            continue
        if case_id in seen:
            errors.append(f"duplicate case_id {case_id}")
        seen.add(case_id)
        provenance = case.get("provenance")
        if not isinstance(provenance, dict):
            errors.append(f"{case_id}: provenance must be an object")
            real_eligible = False
            provenance = {}
        if validation_class == "real":
            if provenance.get("kind") not in {"expert_review", "verified_case"}:
                errors.append(f"{case_id}: real validation needs expert_review or verified_case provenance")
                real_eligible = False
            reviewers = provenance.get("reviewers", [])
            if not isinstance(reviewers, list) or len({item.strip() for item in reviewers if isinstance(item, str) and item.strip()}) < min_reviewers:
                errors.append(f"{case_id}: insufficient independent reviewers")
                real_eligible = False
            if provenance.get("adjudication_status") != "closed":
                errors.append(f"{case_id}: expert adjudication is not closed")
                real_eligible = False
            if not provenance.get("source"):
                errors.append(f"{case_id}: provenance source is missing")
                real_eligible = False
        environment = case.get("environment")
        if not isinstance(environment, dict) or not environment.get("product_version") or not environment.get("solver_type"):
            errors.append(f"{case_id}: environment needs product_version and solver_type")
        else:
            solver_counts[str(environment["solver_type"])] += 1
        families = case.get("failure_families", [])
        if not isinstance(families, list) or not families:
            warnings.append(f"{case_id}: no failure_families coverage tags")
        else:
            family_counts.update(str(item) for item in families)
        evidence = case.get("evidence_files", [])
        if not isinstance(evidence, list) or not evidence:
            errors.append(f"{case_id}: evidence_files must be non-empty")
        elif check_files:
            for relative in evidence:
                path = (source.parent / str(relative)).resolve()
                try:
                    path.relative_to(source.parent.resolve())
                except ValueError:
                    errors.append(f"{case_id}: unsafe evidence path {relative}")
                    continue
                if not path.is_file():
                    errors.append(f"{case_id}: missing evidence file {relative}")
        if not case.get("safety_expectations"):
            errors.append(f"{case_id}: safety_expectations must be non-empty")
        if isinstance(case.get("expert_ratings"), dict):
            ratings.append(case["expert_ratings"])

    for solver in policy.get("required_solver_types", []):
        if solver_counts[str(solver)] == 0:
            errors.append(f"required solver type is not covered: {solver}")
    for family in policy.get("required_failure_families", []):
        if family_counts[str(family)] == 0:
            errors.append(f"required failure family is not covered: {family}")

    agreement_metrics = agreement(ratings)
    if validation_class == "real" and agreement_metrics["rated_cases"] == 0:
        warnings.append("no multi-reviewer rating records; inter-rater agreement is unmeasured")
    elif agreement_metrics["observed_agreement"] is not None and agreement_metrics["observed_agreement"] < float(min_agreement):
        errors.append("expert observed agreement is below policy threshold")

    results = data.get("results", [])
    false_certain = 0
    certain_total = 0
    for item in results if isinstance(results, list) else []:
        if not isinstance(item, dict):
            continue
        if item.get("root_cause_certainty") == "certain":
            certain_total += 1
            if item.get("root_cause_correct") is not True:
                false_certain += 1
    false_certain_rate = false_certain / certain_total if certain_total else None
    if false_certain_rate is None:
        warnings.append("false-certain root-cause rate is unmeasured")
    elif false_certain_rate > float(max_false_certain):
        errors.append(f"false-certain root-cause rate {false_certain_rate:.3f} exceeds {float(max_false_certain):.3f}")

    if validation_class == "constructed":
        real_eligible = False
        warnings.append("constructed evidence validates workflow only and cannot support a real-domain quality claim")
    return {
        "schema_version": "1.0",
        "status": "pass" if not errors else "fail",
        "domain": domain,
        "validation_class": validation_class,
        "real_domain_claim_eligible": bool(real_eligible and not errors),
        "case_count": len(cases),
        "coverage": {"solver_types": dict(sorted(solver_counts.items())), "failure_families": dict(sorted(family_counts.items()))},
        "expert_agreement": agreement_metrics,
        "safety_metrics": {"certain_claims": certain_total, "false_certain_claims": false_certain, "false_certain_rate": false_certain_rate},
        "errors": errors,
        "warnings": warnings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suite", type=Path)
    parser.add_argument("--check-files", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        report = validate_suite(load(args.suite), args.suite.resolve(), args.check_files)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.output:
        if args.output.exists():
            print("refusing to overwrite output", file=sys.stderr)
            return 2
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
