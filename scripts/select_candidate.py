#!/usr/bin/env python3
"""Apply deterministic promotion gates to an optimization benchmark."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any


UNRESOLVED = {"blocked", "inconclusive", "invalid_case"}


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError(f"file not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in {path}: {exc}") from None
    if not isinstance(data, dict):
        raise ValueError(f"root must be an object: {path}")
    return data


def split_summary(benchmark: dict[str, Any], config: str, split: str) -> dict[str, Any] | None:
    return benchmark.get("configurations", {}).get(config, {}).get("by_split", {}).get(split)


def mean_score(benchmark: dict[str, Any], config: str, split: str) -> float | None:
    summary = split_summary(benchmark, config, split)
    if not summary:
        return None
    return summary.get("score", {}).get("mean")


def unresolved_count(summary: dict[str, Any] | None) -> int:
    if not summary:
        return 0
    counts = summary.get("status_counts", {})
    return sum(int(counts.get(status, 0)) for status in UNRESOLVED)


def case_scores(benchmark: dict[str, Any], config: str, split: str) -> dict[str, float]:
    result: dict[str, float] = {}
    for row in benchmark.get("case_summary", []):
        if row.get("configuration") == config and row.get("split") == split:
            value = row.get("score", {}).get("mean")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                result[str(row["case_id"])] = float(value)
    return result


def case_run_counts(benchmark: dict[str, Any], config: str, split: str) -> dict[str, int]:
    return {
        str(row["case_id"]): int(row.get("runs", 0))
        for row in benchmark.get("case_summary", [])
        if row.get("configuration") == config and row.get("split") == split
    }


def paired_delta_stats(benchmark: dict[str, Any], split: str, candidate: str, baseline: str) -> dict[str, Any]:
    pairs: dict[tuple[str, int], dict[str, float]] = {}
    for run in benchmark.get("runs", []):
        if run.get("split") != split or run.get("configuration") not in {candidate, baseline}:
            continue
        score = run.get("score")
        if not isinstance(score, (int, float)) or isinstance(score, bool):
            continue
        key = (str(run.get("case_id")), int(run.get("run_number", 0)))
        pairs.setdefault(key, {})[str(run["configuration"])] = float(score)
    deltas = [values[candidate] - values[baseline] for values in pairs.values() if candidate in values and baseline in values]
    wins = sum(1 for value in deltas if value > 0)
    losses = sum(1 for value in deltas if value < 0)
    ties = len(deltas) - wins - losses
    non_ties = wins + losses
    sign_pvalue = None
    if non_ties:
        sign_pvalue = sum(math.comb(non_ties, k) for k in range(wins, non_ties + 1)) / (2 ** non_ties)
    if not deltas:
        return {
            "count": 0, "mean": None, "stddev": None, "lower_95": None, "effect_to_noise": None,
            "wins": 0, "losses": 0, "ties": 0, "non_tie_pairs": 0, "one_sided_sign_pvalue": None,
        }
    mean = sum(deltas) / len(deltas)
    stddev = 0.0
    if len(deltas) > 1:
        stddev = math.sqrt(sum((value - mean) ** 2 for value in deltas) / (len(deltas) - 1))
    margin = 1.96 * stddev / math.sqrt(len(deltas)) if len(deltas) > 1 else 0.0
    ratio = None
    if stddev == 0:
        ratio = "infinite" if mean > 0 else 0.0
    else:
        ratio = mean / stddev
    return {
        "count": len(deltas),
        "mean": round(mean, 6),
        "stddev": round(stddev, 6),
        "lower_95": round(mean - margin, 6),
        "effect_to_noise": ratio if isinstance(ratio, str) else round(ratio, 6),
        "wins": wins,
        "losses": losses,
        "ties": ties,
        "non_tie_pairs": non_ties,
        "one_sided_sign_pvalue": round(sign_pvalue, 12) if sign_pvalue is not None else None,
    }


def metric_total(benchmark: dict[str, Any], config: str, key: str) -> tuple[float, int]:
    values = [
        float(run[key])
        for run in benchmark.get("runs", [])
        if run.get("configuration") == config
        and isinstance(run.get(key), (int, float))
        and not isinstance(run.get(key), bool)
    ]
    return sum(values), len(values)


def decide(benchmark: dict[str, Any], eval_set: dict[str, Any], candidate: str, baseline: str) -> dict[str, Any]:
    policy = eval_set.get("policy", {}) if isinstance(eval_set.get("policy"), dict) else {}
    min_train = float(policy.get("min_train_delta", 0.0))
    min_holdout = float(policy.get("min_holdout_delta", 0.0))
    max_regression_drop = float(policy.get("max_regression_drop", 0.0))
    max_stddev = policy.get("max_candidate_stddev", 0.25)
    allow_unresolved = bool(policy.get("allow_unresolved_holdout", False))
    require_identity = bool(policy.get("require_identity_binding", True))
    require_environment = bool(policy.get("require_environment_binding", True))
    require_design = bool(policy.get("require_design_binding", False))
    require_review = bool(policy.get("require_design_review", False))
    require_traceability = bool(policy.get("require_traceability", False))
    require_conformance = bool(policy.get("require_conformance_review", False))
    failed: list[str] = []
    uncertain: list[str] = []
    reasons: list[str] = []

    configs = benchmark.get("configurations", {})
    if candidate not in configs:
        failed.append(f"candidate configuration '{candidate}' is missing")
    if baseline not in configs:
        failed.append(f"baseline configuration '{baseline}' is missing")
    if failed:
        return result("keep_baseline", candidate, baseline, reasons, failed, {}, policy)

    integrity = benchmark.get("integrity", {})
    if require_identity and not integrity.get("identity_bound", False):
        uncertain.append("candidate or eval-set identity is missing or mixed")
    if require_environment and not integrity.get("environment_bound", False):
        uncertain.append("model or environment identity is missing or mixed")
    if require_design and not integrity.get("design_bound", False):
        uncertain.append("ready Skill design identity is missing or mixed")
    if require_review and not integrity.get("approval_bound", False):
        uncertain.append("version-bound design approval is missing or mixed")
    if require_traceability and not integrity.get("traceability_bound", False):
        uncertain.append("validated traceability identity is missing or mixed")
    if require_conformance and not integrity.get("conformance_bound", False):
        uncertain.append("implementation conformance review is missing or mixed")

    candidate_all = configs[candidate]
    if int(candidate_all.get("critical_failure_runs", 0)) > 0:
        failed.append("candidate has critical expectation failures")

    train_candidate = mean_score(benchmark, candidate, "train")
    train_baseline = mean_score(benchmark, baseline, "train")
    hold_candidate = mean_score(benchmark, candidate, "holdout")
    hold_baseline = mean_score(benchmark, baseline, "holdout")
    metrics = {
        "train_candidate": train_candidate,
        "train_baseline": train_baseline,
        "train_delta": None,
        "holdout_candidate": hold_candidate,
        "holdout_baseline": hold_baseline,
        "holdout_delta": None,
        "paired_train_delta": paired_delta_stats(benchmark, "train", candidate, baseline),
        "paired_holdout_delta": paired_delta_stats(benchmark, "holdout", candidate, baseline),
    }

    if train_candidate is None or train_baseline is None:
        uncertain.append("train scores are incomplete")
    else:
        metrics["train_delta"] = round(train_candidate - train_baseline, 6)
        if metrics["train_delta"] < min_train:
            failed.append(f"train delta {metrics['train_delta']:.3f} is below {min_train:.3f}")
    if hold_candidate is None or hold_baseline is None:
        uncertain.append("holdout scores are missing or indeterminate")
    else:
        metrics["holdout_delta"] = round(hold_candidate - hold_baseline, 6)
        if metrics["holdout_delta"] < min_holdout:
            failed.append(f"holdout delta {metrics['holdout_delta']:.3f} is below {min_holdout:.3f}")

    for config in (candidate, baseline):
        unresolved = unresolved_count(split_summary(benchmark, config, "holdout"))
        unresolved += unresolved_count(split_summary(benchmark, config, "regression"))
        if unresolved and not allow_unresolved:
            uncertain.append(f"{config} has {unresolved} unresolved holdout/regression runs")

    for split in ("train", "regression", "holdout"):
        candidate_counts = case_run_counts(benchmark, candidate, split)
        baseline_counts = case_run_counts(benchmark, baseline, split)
        if candidate_counts != baseline_counts:
            uncertain.append(f"candidate and baseline run counts differ for {split}")

    candidate_regression = case_scores(benchmark, candidate, "regression")
    baseline_regression = case_scores(benchmark, baseline, "regression")
    for case_id in sorted(set(candidate_regression) & set(baseline_regression)):
        drop = baseline_regression[case_id] - candidate_regression[case_id]
        if drop > max_regression_drop:
            failed.append(f"regression case '{case_id}' dropped by {drop:.3f}")
    if set(candidate_regression) != set(baseline_regression):
        uncertain.append("candidate and baseline do not have comparable determinate regression cases")

    hold_summary = split_summary(benchmark, candidate, "holdout")
    if hold_summary and isinstance(max_stddev, (int, float)) and not isinstance(max_stddev, bool):
        observed_stddev = hold_summary.get("score", {}).get("stddev")
        if isinstance(observed_stddev, (int, float)) and observed_stddev > float(max_stddev):
            uncertain.append(f"candidate holdout stddev {observed_stddev:.3f} exceeds {float(max_stddev):.3f}")

    if bool(policy.get("require_effect_over_variance", False)):
        paired = metrics["paired_holdout_delta"]
        min_pairs = int(policy.get("min_variance_pairs", 2))
        min_ratio = float(policy.get("min_effect_to_noise_ratio", 1.0))
        if paired["count"] < min_pairs:
            uncertain.append(f"only {paired['count']} paired holdout observations; need {min_pairs} for variance gate")
        elif paired["lower_95"] is None or paired["lower_95"] < min_holdout:
            uncertain.append("paired holdout improvement does not clear the configured 95% engineering lower bound")
        elif paired["effect_to_noise"] is None or (
            isinstance(paired["effect_to_noise"], (int, float))
            and paired["effect_to_noise"] < min_ratio
        ):
            uncertain.append(f"holdout effect-to-noise ratio is below {min_ratio:.3f}")

    if bool(policy.get("require_sign_test", False)):
        paired = metrics["paired_holdout_delta"]
        min_non_ties = int(policy.get("min_non_tie_pairs", 5))
        maximum_p = float(policy.get("max_sign_test_pvalue", 0.05))
        if paired["non_tie_pairs"] < min_non_ties:
            uncertain.append(f"only {paired['non_tie_pairs']} non-tied holdout pairs; need {min_non_ties} for sign-test gate")
        elif paired["one_sided_sign_pvalue"] is None or paired["one_sided_sign_pvalue"] > maximum_p:
            uncertain.append(
                f"paired holdout one-sided sign-test p={paired['one_sided_sign_pvalue']} exceeds {maximum_p:.6f}"
            )

    cost_keys = {
        "tokens": "max_candidate_tokens_ratio",
        "duration_seconds": "max_candidate_duration_seconds_ratio",
        "tool_calls": "max_candidate_tool_calls_ratio",
    }
    cost_metrics: dict[str, Any] = {}
    for key, policy_key in cost_keys.items():
        candidate_total, candidate_count = metric_total(benchmark, candidate, key)
        baseline_total, baseline_count = metric_total(benchmark, baseline, key)
        ratio = candidate_total / baseline_total if baseline_total > 0 else None
        cost_metrics[key] = {
            "candidate_total": candidate_total,
            "baseline_total": baseline_total,
            "ratio": ratio,
            "candidate_observations": candidate_count,
            "baseline_observations": baseline_count,
        }
        limit = policy.get(policy_key)
        if isinstance(limit, (int, float)) and not isinstance(limit, bool):
            if candidate_count == 0 or baseline_count == 0:
                uncertain.append(f"{key} budget gate is configured but measurements are missing")
            elif baseline_total == 0:
                uncertain.append(f"cannot compute candidate {key} ratio because baseline total is zero")
            elif ratio is not None and ratio > float(limit):
                failed.append(f"candidate {key} ratio {ratio:.3f} exceeds {float(limit):.3f}")
    metrics["costs"] = cost_metrics

    analysis = benchmark.get("analysis")
    if isinstance(analysis, dict):
        if analysis.get("gate") == "fail":
            failed.append("benchmark analyzer reported a critical quality, budget or overfit finding")
        elif analysis.get("gate") == "inconclusive":
            uncertain.append("benchmark analyzer reported unresolved overfit or eval-quality findings")

    blind = benchmark.get("blind_comparison")
    if bool(policy.get("require_blind_comparison", False)):
        if not isinstance(blind, dict) or blind.get("status") != "complete":
            uncertain.append("required blind comparison is missing or incomplete")
        else:
            if int(blind.get("inconclusive", 0)) > 0:
                uncertain.append("blind comparison contains inconclusive judgments")
            win_rate = blind.get("candidate_win_rate")
            minimum = float(policy.get("min_blind_win_rate", 0.5))
            if not isinstance(win_rate, (int, float)):
                uncertain.append("blind comparison candidate win rate is unavailable")
            elif win_rate < minimum:
                failed.append(f"blind comparison win rate {win_rate:.3f} is below {minimum:.3f}")
            if int(blind.get("critical_baseline_wins", 0)) > 0:
                failed.append("blind comparator preferred baseline on a critical difference")

    governance = benchmark.get("governance", {}) if isinstance(benchmark.get("governance"), dict) else {}
    governance_requirements = {
        "require_dataset_audit": ("dataset_audit", lambda value: value.get("gate") == "pass"),
        "require_grader_calibration": ("grader_calibration", lambda value: value.get("status") == "pass"),
        "require_professional_validation": ("professional_validation", lambda value: value.get("status") == "pass" and value.get("real_domain_claim_eligible") is True),
        "require_holdout_governance": ("holdout_governance", lambda value: value.get("status") in {"pass", "valid"} and value.get("holdout_eligible") is True),
    }
    governance_metrics: dict[str, Any] = {}
    for policy_key, (report_key, predicate) in governance_requirements.items():
        if not bool(policy.get(policy_key, False)):
            continue
        report = governance.get(report_key)
        if not isinstance(report, dict):
            uncertain.append(f"required governance evidence is missing: {report_key}")
            continue
        governance_metrics[report_key] = report
        if not predicate(report):
            failed.append(f"required governance gate did not pass: {report_key}")
    if bool(policy.get("require_hard_holdout_isolation")):
        holdout_report = governance.get("holdout_governance")
        if not isinstance(holdout_report, dict) or holdout_report.get("hard_isolation") is not True:
            failed.append("holdout authority is not system-isolated")
    metrics["governance"] = governance_metrics

    deltas = [value for value in (metrics["train_delta"], metrics["holdout_delta"]) if isinstance(value, (int, float))]
    if deltas and not any(value > 0 for value in deltas):
        uncertain.append("candidate shows no observed score improvement over baseline")

    if failed:
        decision = "keep_baseline"
    elif uncertain:
        decision = "inconclusive"
    else:
        decision = "candidate_is_best"
        reasons.append("candidate passed critical, regression, train, holdout and variability gates")
    return result(decision, candidate, baseline, reasons, failed, metrics, policy, uncertain)


def result(
    decision: str,
    candidate: str,
    baseline: str,
    reasons: list[str],
    failed: list[str],
    metrics: dict[str, Any],
    policy: dict[str, Any],
    uncertain: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": "2.0",
        "decision": decision,
        "requires_human_approval": True,
        "candidate": candidate,
        "baseline": baseline,
        "reasons": reasons,
        "failed_gates": failed,
        "uncertainties": uncertain or [],
        "metrics": metrics,
        "policy_snapshot": policy,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("benchmark", type=Path)
    parser.add_argument("eval_set", type=Path)
    parser.add_argument("--candidate", default="candidate")
    parser.add_argument("--baseline", default="baseline")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        output_data = decide(read_json(args.benchmark), read_json(args.eval_set), args.candidate, args.baseline)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    output = args.output or args.benchmark.with_name("selection.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(output_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
