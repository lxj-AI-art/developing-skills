#!/usr/bin/env python3
"""Analyze benchmark quality, cost, variance, discriminating power and overfit signals."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable


def read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"root must be an object: {path}")
    return value


def add(findings: list[dict[str, Any]], code: str, severity: str, message: str, evidence: Any) -> None:
    findings.append({"code": code, "severity": severity, "message": message, "evidence": evidence})


def numeric_measurements(runs: Iterable[dict[str, Any]], key: str) -> tuple[float, int]:
    values = [
        float(item[key])
        for item in runs
        if isinstance(item.get(key), (int, float)) and not isinstance(item.get(key), bool)
    ]
    return sum(values), len(values)


def all_strings(value: Any) -> list[str]:
    result: list[str] = []
    if isinstance(value, str):
        result.append(value)
    elif isinstance(value, list):
        for item in value:
            result.extend(all_strings(item))
    elif isinstance(value, dict):
        for item in value.values():
            result.extend(all_strings(item))
    return result


def changed_text(parent: Path | None, candidate: Path | None) -> str:
    if candidate is None or not candidate.is_dir():
        return ""
    chunks: list[str] = []
    for path in sorted(candidate.rglob("*")):
        if not path.is_file() or any(part in {".git", "__pycache__"} for part in path.relative_to(candidate).parts):
            continue
        try:
            current = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        previous = ""
        if parent:
            old = parent / path.relative_to(candidate)
            if old.is_file():
                try:
                    previous = old.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    previous = ""
        previous_lines = set(previous.splitlines())
        chunks.extend(line for line in current.splitlines() if line not in previous_lines)
    return "\n".join(chunks).lower()


def analyze(
    benchmark: dict[str, Any],
    eval_set: dict[str, Any],
    parent: Path | None = None,
    candidate: Path | None = None,
) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    policy = eval_set.get("policy", {}) if isinstance(eval_set.get("policy"), dict) else {}
    cases = eval_set.get("cases", []) if isinstance(eval_set.get("cases"), list) else []
    for case in cases:
        expectations = case.get("expectations", []) if isinstance(case, dict) else []
        if not expectations:
            add(findings, "no_expectations", "info", f"case {case.get('id')} relies on its complete oracle instead of explicit expectations", case.get("oracle", {}))
        for expectation in expectations:
            if expectation.get("kind") == "deterministic" and not isinstance(expectation.get("checker"), dict):
                add(
                    findings,
                    "deterministic_checker_missing",
                    "major",
                    f"deterministic expectation {case.get('id')}:{expectation.get('id')} has no executable checker object",
                    expectation,
                )
    for row in benchmark.get("case_summary", []):
        score = row.get("score", {})
        stddev = score.get("stddev")
        threshold = policy.get("max_candidate_stddev", 0.25)
        if row.get("configuration") == "candidate" and isinstance(stddev, (int, float)) and stddev > threshold:
            add(findings, "high_variance", "major", f"candidate variance exceeds policy for {row.get('case_id')}", {"stddev": stddev, "threshold": threshold})
    runs = benchmark.get("runs", [])
    candidate_runs = [item for item in runs if item.get("configuration") == "candidate"]
    baseline_runs = [item for item in runs if item.get("configuration") == "baseline"]
    costs: dict[str, Any] = {}
    for key in ("tokens", "duration_seconds", "tool_calls", "errors"):
        candidate_total, candidate_count = numeric_measurements(candidate_runs, key)
        baseline_total, baseline_count = numeric_measurements(baseline_runs, key)
        costs[key] = {
            "candidate_total": candidate_total,
            "baseline_total": baseline_total,
            "ratio": candidate_total / baseline_total if baseline_total > 0 else None,
            "candidate_observations": candidate_count,
            "baseline_observations": baseline_count,
        }
        limit = policy.get(f"max_candidate_{key}_ratio")
        if isinstance(limit, (int, float)):
            if not candidate_count or not baseline_count:
                add(findings, "budget_measurement_missing", "major", f"{key} ratio gate is configured but measurements are missing", costs[key])
            elif baseline_total <= 0:
                add(findings, "budget_ratio_unavailable", "major", f"{key} ratio gate cannot use a zero baseline", costs[key])
            elif candidate_total / baseline_total > float(limit):
                add(findings, "cost_regression", "critical", f"candidate {key} ratio exceeds policy", {"ratio": candidate_total / baseline_total, "limit": limit})
    for key, metric in (("tokens", "max_total_tokens"), ("duration_seconds", "max_total_duration_seconds"), ("tool_calls", "max_total_tool_calls")):
        limit = policy.get(metric)
        total, count = numeric_measurements(runs, key)
        if isinstance(limit, (int, float)):
            if not count:
                add(findings, "budget_measurement_missing", "major", f"total {key} budget is configured but no measurements exist", {"limit": limit})
            elif total > float(limit):
                add(findings, "budget_exceeded", "critical", f"total {key} exceeds budget", {"total": total, "limit": limit})
    rows = benchmark.get("case_summary", [])
    keyed: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        keyed[(str(row.get("case_id")), str(row.get("configuration")))] = row
    for case in cases:
        case_id = str(case.get("id"))
        candidate_row = keyed.get((case_id, "candidate"))
        baseline_row = keyed.get((case_id, "baseline"))
        if candidate_row and baseline_row and candidate_row.get("score", {}).get("mean") == baseline_row.get("score", {}).get("mean"):
            add(findings, "no_discrimination", "info", f"case {case_id} does not distinguish candidate and baseline", {"score": candidate_row.get("score", {}).get("mean")})
    additions = changed_text(parent, candidate)
    if additions:
        for case in cases:
            if case.get("split") != "train":
                continue
            case_id = str(case.get("id", ""))
            if case_id and case_id.lower() in additions:
                add(findings, "case_id_overfit", "critical", f"candidate added training case id {case_id}", case_id)
            fragments: set[str] = set()
            for source in [case.get("prompt", ""), *all_strings(case.get("oracle", {}))]:
                normalized = " ".join(str(source).lower().split())
                if len(normalized) >= 24:
                    fragments.add(normalized)
                fragments.update(token for token in re.findall(r"[a-z0-9_-]{14,}", normalized))
            matched = sorted(fragment for fragment in fragments if fragment and fragment in additions)
            if matched:
                add(findings, "possible_literal_overfit", "major", f"candidate copied distinctive training text for {case_id}", matched[:10])
    severities = {"critical": 0, "major": 0, "info": 0}
    for finding in findings:
        severities[finding["severity"]] += 1
    fail_on_overfit = bool(policy.get("fail_on_overfit_signal", True))
    blocking_major = any(
        item["severity"] == "major"
        and (item["code"] != "possible_literal_overfit" or fail_on_overfit)
        for item in findings
    )
    if severities["critical"]:
        gate = "fail"
    elif blocking_major:
        gate = "inconclusive"
    else:
        gate = "pass"
    return {
        "schema_version": "2.0",
        "gate": gate,
        "finding_counts": severities,
        "findings": findings,
        "costs": costs,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("benchmark", type=Path)
    parser.add_argument("eval_set", type=Path)
    parser.add_argument("--parent", type=Path)
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        result = analyze(read_object(args.benchmark), read_object(args.eval_set), args.parent, args.candidate)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(exc, file=sys.stderr)
        return 1
    output = args.output or args.benchmark.with_name("analysis.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(output)
    return 0 if result["gate"] == "pass" else 3


if __name__ == "__main__":
    sys.exit(main())
