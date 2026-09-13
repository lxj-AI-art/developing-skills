#!/usr/bin/env python3
"""Validate a developing-skills labeled eval set using only stdlib."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


SPLITS = {"train", "regression", "holdout"}
CASE_MODES = {"execution", "trigger"}
RISKS = {"low", "medium", "high", "critical"}
PROVENANCE_KINDS = {"standard", "expert_review", "verified_case", "observation", "hypothesis"}
CONFIDENCES = {"confirmed", "high", "medium", "low"}
EXPECTATION_KINDS = {"deterministic", "semantic", "domain"}
SEVERITIES = {"critical", "major", "minor"}
CHECKER_TYPES = {
    "execution_status",
    "artifact_exists",
    "artifact_text_contains",
    "artifact_text_equals",
    "artifact_text_not_contains",
    "artifact_regex",
    "artifact_json_path_equals",
}


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError(f"file not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}") from None


def nonempty_strings(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value)


def validate(data: Any, source: Path, check_files: bool) -> tuple[list[str], list[str], dict[str, int]]:
    errors: list[str] = []
    warnings: list[str] = []
    counts = {name: 0 for name in sorted(SPLITS)}

    if not isinstance(data, dict):
        return ["root must be a JSON object"], warnings, counts
    if data.get("schema_version") != "2.0":
        errors.append("schema_version must be '2.0'")
    if not isinstance(data.get("skill_name"), str) or not data["skill_name"].strip():
        errors.append("skill_name must be a non-empty string")

    policy = data.get("policy", {})
    if not isinstance(policy, dict):
        errors.append("policy must be an object")
        policy = {}
    if policy.get("promotion", "manual") not in {"manual", "automatic"}:
        errors.append("policy.promotion must be 'manual' or 'automatic'")
    for key in ("max_iterations", "default_repetitions"):
        value = policy.get(key, 1)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            errors.append(f"policy.{key} must be an integer >= 1")
    for key in (
        "min_train_delta",
        "min_holdout_delta",
        "max_regression_drop",
        "max_candidate_stddev",
        "max_total_tokens",
        "max_total_duration_seconds",
        "max_total_tool_calls",
        "max_candidate_tokens_ratio",
        "max_candidate_duration_seconds_ratio",
        "max_candidate_tool_calls_ratio",
        "min_effect_to_noise_ratio",
        "semantic_duplicate_threshold",
    ):
        value = policy.get(key)
        if value is not None and (not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0):
            errors.append(f"policy.{key} must be a number >= 0")
    min_blind_win_rate = policy.get("min_blind_win_rate")
    if min_blind_win_rate is not None and (
        not isinstance(min_blind_win_rate, (int, float))
        or isinstance(min_blind_win_rate, bool)
        or not 0 <= min_blind_win_rate <= 1
    ):
        errors.append("policy.min_blind_win_rate must be between 0 and 1")
    max_sign_test_pvalue = policy.get("max_sign_test_pvalue")
    if max_sign_test_pvalue is not None and (
        not isinstance(max_sign_test_pvalue, (int, float))
        or isinstance(max_sign_test_pvalue, bool)
        or not 0 <= max_sign_test_pvalue <= 1
    ):
        errors.append("policy.max_sign_test_pvalue must be between 0 and 1")
    for key in (
        "allow_unresolved_holdout",
        "require_identity_binding",
        "require_environment_binding",
        "require_design_binding",
        "require_design_review",
        "require_traceability",
        "require_conformance_review",
        "require_effect_over_variance",
        "require_blind_comparison",
        "run_blind_comparison",
        "fail_on_overfit_signal",
        "require_opposite_cases",
        "require_sign_test",
        "auto_refresh_assurance",
        "require_dataset_audit",
        "require_grader_calibration",
        "require_professional_validation",
        "require_holdout_governance",
        "require_hard_holdout_isolation",
    ):
        if not isinstance(policy.get(key, False), bool):
            errors.append(f"policy.{key} must be boolean")
    if policy.get("require_design_review") and not policy.get("require_design_binding"):
        errors.append("policy.require_design_review requires require_design_binding")
    if policy.get("require_traceability") and not policy.get("require_design_binding"):
        errors.append("policy.require_traceability requires require_design_binding")
    if policy.get("require_conformance_review") and not policy.get("require_traceability"):
        errors.append("policy.require_conformance_review requires require_traceability")
    if policy.get("auto_refresh_assurance") and not policy.get("require_conformance_review"):
        errors.append("policy.auto_refresh_assurance requires require_conformance_review")
    if policy.get("require_hard_holdout_isolation") and not policy.get("require_holdout_governance"):
        errors.append("policy.require_hard_holdout_isolation requires require_holdout_governance")
    semantic_threshold = policy.get("semantic_duplicate_threshold", 0.9)
    if not isinstance(semantic_threshold, (int, float)) or isinstance(semantic_threshold, bool) or not 0.5 <= float(semantic_threshold) <= 1:
        errors.append("policy.semantic_duplicate_threshold must be between 0.5 and 1")
    min_variance_pairs = policy.get("min_variance_pairs", 2)
    if not isinstance(min_variance_pairs, int) or isinstance(min_variance_pairs, bool) or min_variance_pairs < 2:
        errors.append("policy.min_variance_pairs must be an integer >= 2")
    min_non_tie_pairs = policy.get("min_non_tie_pairs", 5)
    if not isinstance(min_non_tie_pairs, int) or isinstance(min_non_tie_pairs, bool) or min_non_tie_pairs < 1:
        errors.append("policy.min_non_tie_pairs must be an integer >= 1")
    candidate_checks = policy.get("candidate_checks", [])
    if not isinstance(candidate_checks, list):
        errors.append("policy.candidate_checks must be an array")
    else:
        for index, check in enumerate(candidate_checks):
            command = check.get("command") if isinstance(check, dict) else None
            if not isinstance(command, list) or not command or not all(isinstance(item, str) and item for item in command):
                errors.append(f"policy.candidate_checks[{index}].command must be a non-empty string array")
                continue
            timeout = check.get("timeout_seconds", 120)
            expected_exit = check.get("expected_exit_code", 0)
            if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout < 1:
                errors.append(f"policy.candidate_checks[{index}].timeout_seconds must be an integer >= 1")
            if not isinstance(expected_exit, int) or isinstance(expected_exit, bool):
                errors.append(f"policy.candidate_checks[{index}].expected_exit_code must be an integer")

    cases = data.get("cases")
    if not isinstance(cases, list) or not cases:
        return errors + ["cases must be a non-empty array"], warnings, counts

    seen_ids: set[str] = set()
    seen_prompts: dict[str, tuple[str, str]] = {}
    seen_file_sets: dict[tuple[str, ...], tuple[str, str]] = {}
    base = source.parent.resolve()

    for index, case in enumerate(cases):
        prefix = f"cases[{index}]"
        if not isinstance(case, dict):
            errors.append(f"{prefix} must be an object")
            continue
        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id.strip():
            errors.append(f"{prefix}.id must be a non-empty string")
            case_id = f"index-{index}"
        elif not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", case_id):
            errors.append(f"{prefix}.id must use letters, digits, dot, underscore or hyphen")
        elif case_id in seen_ids:
            errors.append(f"{prefix}.id duplicates '{case_id}'")
        else:
            seen_ids.add(case_id)
        prefix = f"case '{case_id}'"

        split = case.get("split")
        if split not in SPLITS:
            errors.append(f"{prefix}: split must be one of {sorted(SPLITS)}")
        else:
            counts[split] += 1
        mode = case.get("mode", "execution")
        if mode not in CASE_MODES:
            errors.append(f"{prefix}: mode must be one of {sorted(CASE_MODES)}")
        prompt = case.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            errors.append(f"{prefix}: prompt must be a non-empty string")
        elif prompt.strip() in seen_prompts:
            prior_id, prior_split = seen_prompts[prompt.strip()]
            if "holdout" in {split, prior_split} and split != prior_split:
                errors.append(f"{prefix}: prompt leaks across {prior_split} and holdout case '{prior_id}'")
            else:
                warnings.append(f"{prefix}: prompt duplicates case '{prior_id}'")
        else:
            seen_prompts[prompt.strip()] = (case_id, split)
        risk = case.get("risk", "medium")
        if risk not in RISKS:
            errors.append(f"{prefix}: risk must be one of {sorted(RISKS)}")
        repetitions = case.get("repetitions", policy.get("default_repetitions", 1))
        if not isinstance(repetitions, int) or isinstance(repetitions, bool) or repetitions < 1:
            errors.append(f"{prefix}: repetitions must be an integer >= 1")

        files = case.get("files", [])
        if not isinstance(files, list) or not all(isinstance(item, str) and item for item in files):
            errors.append(f"{prefix}: files must be an array of non-empty paths")
        else:
            for item in files:
                if Path(item).is_absolute():
                    errors.append(f"{prefix}: file path must be relative: {item}")
                    continue
                path = (base / item).resolve()
                try:
                    path.relative_to(base)
                except ValueError:
                    errors.append(f"{prefix}: file escapes eval-set directory: {item}")
                    continue
                if check_files and not path.is_file():
                    errors.append(f"{prefix}: file not found: {item}")
            file_key = tuple(sorted(files))
            if file_key and file_key in seen_file_sets:
                prior_id, prior_split = seen_file_sets[file_key]
                if "holdout" in {split, prior_split} and split != prior_split:
                    errors.append(f"{prefix}: identical input files leak across {prior_split} and holdout case '{prior_id}'")
            elif file_key:
                seen_file_sets[file_key] = (case_id, split)

        tags = case.get("tags", [])
        if not isinstance(tags, list) or not all(isinstance(item, str) and item.strip() for item in tags):
            errors.append(f"{prefix}: tags must be an array of non-empty strings")
        elif mode == "trigger":
            trigger_labels = {"should-trigger", "should-not-trigger"} & set(tags)
            if len(trigger_labels) != 1:
                errors.append(f"{prefix}: trigger mode needs exactly one tag: should-trigger or should-not-trigger")

        oracle = case.get("oracle", {})
        expectations = case.get("expectations", [])
        if not isinstance(oracle, dict):
            errors.append(f"{prefix}: oracle must be an object")
            oracle = {}
        if not isinstance(expectations, list):
            errors.append(f"{prefix}: expectations must be an array")
            expectations = []
        oracle_lists = ("expected_outcomes", "forbidden_claims", "allowed_uncertainty", "required_actions")
        for key in oracle_lists:
            value = oracle.get(key, [])
            if value and not nonempty_strings(value):
                errors.append(f"{prefix}: oracle.{key} must contain non-empty strings")
        required_evidence = oracle.get("required_evidence", [])
        if required_evidence and (
            not isinstance(required_evidence, list)
            or not all(isinstance(item, dict) and isinstance(item.get("statement"), str) and item["statement"].strip() for item in required_evidence)
        ):
            errors.append(f"{prefix}: oracle.required_evidence items need a non-empty statement")
        has_oracle = any(oracle.get(key) for key in oracle_lists) or bool(required_evidence)
        if not has_oracle and not expectations:
            errors.append(f"{prefix}: at least one oracle entry or expectation is required")

        provenance = oracle.get("provenance")
        if not isinstance(provenance, dict):
            errors.append(f"{prefix}: oracle.provenance must be an object")
            provenance = {}
        if provenance.get("kind") not in PROVENANCE_KINDS:
            errors.append(f"{prefix}: provenance.kind must be one of {sorted(PROVENANCE_KINDS)}")
        if provenance.get("confidence") not in CONFIDENCES:
            errors.append(f"{prefix}: provenance.confidence must be one of {sorted(CONFIDENCES)}")
        if not isinstance(provenance.get("source"), str) or not provenance.get("source", "").strip():
            errors.append(f"{prefix}: provenance.source must be a non-empty string")
        if risk in {"high", "critical"} and provenance.get("kind") in {"observation", "hypothesis"}:
            warnings.append(f"{prefix}: {risk}-risk truth is only {provenance.get('kind')}; do not use it alone for promotion")

        seen_expectations: set[str] = set()
        for exp_index, expectation in enumerate(expectations):
            exp_prefix = f"{prefix}.expectations[{exp_index}]"
            if not isinstance(expectation, dict):
                errors.append(f"{exp_prefix} must be an object")
                continue
            exp_id = expectation.get("id")
            if not isinstance(exp_id, str) or not exp_id.strip():
                errors.append(f"{exp_prefix}.id must be a non-empty string")
            elif exp_id in seen_expectations:
                errors.append(f"{exp_prefix}.id duplicates '{exp_id}'")
            else:
                seen_expectations.add(exp_id)
            if not isinstance(expectation.get("text"), str) or not expectation.get("text", "").strip():
                errors.append(f"{exp_prefix}.text must be a non-empty string")
            if expectation.get("kind") not in EXPECTATION_KINDS:
                errors.append(f"{exp_prefix}.kind must be one of {sorted(EXPECTATION_KINDS)}")
            if expectation.get("severity") not in SEVERITIES:
                errors.append(f"{exp_prefix}.severity must be one of {sorted(SEVERITIES)}")
            weight = expectation.get("weight")
            if not isinstance(weight, (int, float)) or isinstance(weight, bool) or weight <= 0:
                errors.append(f"{exp_prefix}.weight must be a number > 0")
            if expectation.get("kind") == "deterministic":
                checker = expectation.get("checker")
                if not isinstance(checker, dict):
                    errors.append(f"{exp_prefix}: deterministic expectation needs a checker object")
                elif checker.get("type") not in CHECKER_TYPES:
                    errors.append(f"{exp_prefix}: checker.type must be one of {sorted(CHECKER_TYPES)}")
                else:
                    checker_type = checker["type"]
                    artifact = checker.get("artifact")
                    if artifact is not None and (not isinstance(artifact, str) or not artifact.strip()):
                        errors.append(f"{exp_prefix}: checker.artifact must be a non-empty string")
                    if checker_type in {"artifact_text_contains", "artifact_text_equals", "artifact_text_not_contains"}:
                        if not isinstance(checker.get("text"), str):
                            errors.append(f"{exp_prefix}: {checker_type} needs string checker.text")
                    if checker_type == "artifact_regex":
                        pattern = checker.get("pattern")
                        if not isinstance(pattern, str):
                            errors.append(f"{exp_prefix}: artifact_regex needs string checker.pattern")
                        else:
                            try:
                                re.compile(pattern)
                            except re.error as exc:
                                errors.append(f"{exp_prefix}: invalid checker.pattern: {exc}")
                    if checker_type == "artifact_json_path_equals":
                        if not isinstance(checker.get("path"), str):
                            errors.append(f"{exp_prefix}: artifact_json_path_equals needs string checker.path")
                        if "equals" not in checker:
                            errors.append(f"{exp_prefix}: artifact_json_path_equals needs checker.equals")
                    if checker_type == "artifact_text_equals" and not isinstance(checker.get("strip", True), bool):
                        errors.append(f"{exp_prefix}: artifact_text_equals checker.strip must be boolean")
                    if checker_type == "execution_status" and checker.get("equals", "completed") not in {"completed", "blocked"}:
                        errors.append(f"{exp_prefix}: execution_status checker.equals must be completed or blocked")

    if counts["train"] == 0:
        errors.append("at least one train case is required for automatic optimization")
    if counts["holdout"] == 0:
        warnings.append("no holdout cases: candidate promotion will be inconclusive")
    holdout_observations = sum(
        int(case.get("repetitions", policy.get("default_repetitions", 1)))
        for case in cases
        if isinstance(case, dict)
        and case.get("split") == "holdout"
        and isinstance(case.get("repetitions", policy.get("default_repetitions", 1)), int)
        and not isinstance(case.get("repetitions", policy.get("default_repetitions", 1)), bool)
        and case.get("repetitions", policy.get("default_repetitions", 1)) >= 1
    )
    if policy.get("require_sign_test") and holdout_observations < min_non_tie_pairs:
        errors.append(
            f"sign-test gate needs at least {min_non_tie_pairs} possible holdout pairs; eval set provides {holdout_observations}"
        )
    if policy.get("require_effect_over_variance") and holdout_observations < min_variance_pairs:
        errors.append(
            f"variance gate needs at least {min_variance_pairs} possible holdout pairs; eval set provides {holdout_observations}"
        )
    if counts["regression"] == 0:
        warnings.append("no regression cases yet; move exposed fixed cases here after the first iteration")
    if policy.get("require_opposite_cases") and not any(
        isinstance(case, dict) and "opposite-case" in case.get("tags", []) for case in cases
    ):
        errors.append("policy requires at least one case tagged opposite-case")
    if policy.get("promotion") == "automatic":
        warnings.append("automatic promotion is high risk; developing-skills defaults to manual promotion")
    return errors, warnings, counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("eval_set", type=Path)
    parser.add_argument("--check-files", action="store_true")
    parser.add_argument("--output", type=Path, help="optional JSON validation report")
    args = parser.parse_args()
    try:
        data = load_json(args.eval_set)
        errors, warnings, counts = validate(data, args.eval_set, args.check_files)
    except ValueError as exc:
        errors, warnings, counts = [str(exc)], [], {}
    report = {"valid": not errors, "errors": errors, "warnings": warnings, "case_counts": counts}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
