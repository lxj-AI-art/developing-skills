#!/usr/bin/env python3
"""Run a grader against a golden calibration set and gate critical scoring errors."""

from __future__ import annotations

import argparse
import json
import shlex
import sys
import tempfile
from pathlib import Path
from typing import Any

from run_optimization import Adapter, validate_grading_core


def load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read calibration set {path}: {exc}") from None


def validate_set(data: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["root must be an object"]
    if data.get("schema_version") != "1.0":
        errors.append("schema_version must be '1.0'")
    items = data.get("items")
    if not isinstance(items, list) or not items:
        errors.append("items must be a non-empty array")
        return errors
    ids: set[str] = set()
    for index, item in enumerate(items):
        prefix = f"items[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{prefix} must be an object")
            continue
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id.strip() or item_id in ids:
            errors.append(f"{prefix}.id must be unique and non-empty")
        else:
            ids.add(item_id)
        if not isinstance(item.get("case"), dict) or not isinstance(item.get("execution"), dict):
            errors.append(f"{prefix} needs case and execution objects")
        gold = item.get("gold")
        if not isinstance(gold, dict):
            errors.append(f"{prefix}.gold must be an object")
            continue
        if gold.get("status") not in {"pass", "fail", "inconclusive", "blocked", "invalid_case"}:
            errors.append(f"{prefix}.gold.status is invalid")
        if not isinstance(gold.get("expectations", {}), dict):
            errors.append(f"{prefix}.gold.expectations must map expectation IDs to statuses")
    return errors


def calibrate(data: dict[str, Any], adapter: Adapter) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    status_correct = 0
    expectation_total = expectation_correct = 0
    critical_positive = critical_false_negative = 0
    score_errors: list[float] = []
    grader_ids: set[str] = set()
    for index, item in enumerate(data["items"], 1):
        item_id = item["id"]
        job_id = f"calibrate-grade:{item_id}"
        job = {
            "schema_version": "2.0",
            "job_type": "grade",
            "job_id": job_id,
            "case": item["case"],
            "execution": item["execution"],
            "configuration": "calibration",
            "constraints": {"inspect_real_artifacts": True, "calibration_answers_hidden": True, "do_not_modify_candidate": True},
        }
        response = adapter.call(job, Path("calibration") / f"{index:04d}-{item_id}")
        validate_grading_core(response, job_id)
        if isinstance(response.get("grader_id"), str):
            grader_ids.add(response["grader_id"])
        gold = item["gold"]
        row: dict[str, Any] = {"id": item_id, "gold_status": gold["status"], "observed_status": response["status"]}
        row["status_match"] = response["status"] == gold["status"]
        status_correct += int(row["status_match"])
        observed_expectations = {str(value.get("id")): value.get("status") for value in response.get("expectations", []) if isinstance(value, dict)}
        mismatches: list[dict[str, Any]] = []
        for expectation_id, expected_status in gold.get("expectations", {}).items():
            expectation_total += 1
            actual = observed_expectations.get(str(expectation_id))
            if actual == expected_status:
                expectation_correct += 1
            else:
                mismatches.append({"expectation_id": expectation_id, "expected": expected_status, "actual": actual})
        gold_critical = set(str(value) for value in gold.get("critical_failures", []))
        observed_critical = set(str(value) for value in response.get("critical_failures", []))
        critical_positive += len(gold_critical)
        missed = sorted(gold_critical - observed_critical)
        critical_false_negative += len(missed)
        row["expectation_mismatches"] = mismatches
        row["missed_critical_failures"] = missed
        if isinstance(gold.get("score"), (int, float)) and isinstance(response.get("score"), (int, float)):
            error = abs(float(gold["score"]) - float(response["score"]))
            score_errors.append(error)
            row["score_absolute_error"] = round(error, 6)
        rows.append(row)

    policy = data.get("policy", {}) if isinstance(data.get("policy"), dict) else {}
    status_accuracy = status_correct / len(rows)
    expectation_accuracy = expectation_correct / expectation_total if expectation_total else None
    critical_fn_rate = critical_false_negative / critical_positive if critical_positive else 0.0
    score_mae = sum(score_errors) / len(score_errors) if score_errors else None
    failed: list[str] = []
    if status_accuracy < float(policy.get("min_status_accuracy", 0.9)):
        failed.append("status accuracy is below threshold")
    if expectation_accuracy is not None and expectation_accuracy < float(policy.get("min_expectation_accuracy", 0.9)):
        failed.append("expectation accuracy is below threshold")
    if critical_fn_rate > float(policy.get("max_critical_false_negative_rate", 0.0)):
        failed.append("critical false-negative rate exceeds threshold")
    if score_mae is not None and score_mae > float(policy.get("max_score_mae", 0.1)):
        failed.append("score MAE exceeds threshold")
    return {
        "schema_version": "1.0",
        "status": "pass" if not failed else "fail",
        "grader_ids": sorted(grader_ids),
        "metrics": {
            "items": len(rows),
            "status_accuracy": round(status_accuracy, 6),
            "expectation_accuracy": round(expectation_accuracy, 6) if expectation_accuracy is not None else None,
            "critical_false_negative_rate": round(critical_fn_rate, 6),
            "score_mae": round(score_mae, 6) if score_mae is not None else None,
        },
        "failed_gates": failed,
        "rows": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("calibration_set", type=Path)
    parser.add_argument("--adapter-command", required=True, action="append", help="repeat for independent graders or configurations")
    parser.add_argument("--baseline-report", type=Path, help="prior passing report used for drift checks")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    try:
        data = load(args.calibration_set)
        errors = validate_set(data)
        if errors:
            raise ValueError("; ".join(errors))
        if args.output.exists():
            raise ValueError("refusing to overwrite output")
        with tempfile.TemporaryDirectory(prefix="developing-skills-calibration-") as temporary:
            reports = []
            for index, raw_command in enumerate(args.adapter_command, 1):
                command = shlex.split(raw_command)
                if not command:
                    raise ValueError("adapter command must not be empty")
                reports.append(calibrate(data, Adapter(command, args.timeout, Path(temporary) / f"grader-{index}")))
        primary = reports[0]
        disagreement_items = 0
        if len(reports) > 1:
            by_item: dict[str, set[str]] = {}
            for report_item in reports:
                for row in report_item["rows"]:
                    by_item.setdefault(row["id"], set()).add(str(row["observed_status"]))
            disagreement_items = sum(1 for statuses in by_item.values() if len(statuses) > 1)
        disagreement_rate = disagreement_items / len(data["items"])
        policy = data.get("policy", {}) if isinstance(data.get("policy"), dict) else {}
        aggregate_failures = [f"grader-{index + 1}: {failure}" for index, item in enumerate(reports) for failure in item["failed_gates"]]
        if disagreement_rate > float(policy.get("max_grader_disagreement_rate", 0.1)):
            aggregate_failures.append("inter-grader status disagreement exceeds threshold")
        drift: dict[str, Any] | None = None
        if args.baseline_report:
            baseline = load(args.baseline_report)
            baseline_metrics = baseline.get("metrics", {}) if isinstance(baseline.get("metrics"), dict) else {}
            current_metrics = primary["metrics"]
            accuracy_drop = float(baseline_metrics.get("status_accuracy", current_metrics["status_accuracy"])) - float(current_metrics["status_accuracy"])
            critical_increase = float(current_metrics["critical_false_negative_rate"]) - float(baseline_metrics.get("critical_false_negative_rate", current_metrics["critical_false_negative_rate"]))
            drift = {"baseline_report": str(args.baseline_report.resolve()), "status_accuracy_drop": round(accuracy_drop, 6), "critical_false_negative_rate_increase": round(critical_increase, 6)}
            if accuracy_drop > float(policy.get("max_status_accuracy_drop", 0.0)):
                aggregate_failures.append("grader status accuracy regressed from baseline")
            if critical_increase > float(policy.get("max_critical_fn_rate_increase", 0.0)):
                aggregate_failures.append("grader critical false-negative rate regressed from baseline")
        report = {
            **primary,
            "status": "pass" if not aggregate_failures else "fail",
            "failed_gates": aggregate_failures,
            "grader_runs": reports,
            "inter_grader": {"graders": len(reports), "disagreement_items": disagreement_items, "status_disagreement_rate": round(disagreement_rate, 6)},
            "drift": drift,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["status"] == "pass" else 1
    except (OSError, ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
