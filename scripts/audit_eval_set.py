#!/usr/bin/env python3
"""Audit labeled-case quality, semantic duplication, leakage, conflicts, and coverage."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from validate_eval_set import load_json, validate


def normalize(text: str) -> str:
    value = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(re.findall(r"[\w]+", value, flags=re.UNICODE))


def oracle_fingerprint(case: dict[str, Any]) -> str:
    oracle = case.get("oracle", {})
    if not isinstance(oracle, dict):
        oracle = {}
    material = {key: value for key, value in oracle.items() if key != "provenance"}
    expectations = [
        {key: item.get(key) for key in ("id", "text", "kind", "severity", "weight", "checker")}
        for item in case.get("expectations", [])
        if isinstance(item, dict)
    ]
    payload = json.dumps({"oracle": material, "expectations": expectations}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right, autojunk=False).ratio()


def audit(data: dict[str, Any], source: Path, *, threshold: float = 0.9, benchmark: dict[str, Any] | None = None) -> dict[str, Any]:
    validation_errors, validation_warnings, counts = validate(data, source, True)
    cases = [item for item in data.get("cases", []) if isinstance(item, dict)]
    normalized = {str(case.get("id")): normalize(str(case.get("prompt", ""))) for case in cases}
    fingerprints = {str(case.get("id")): oracle_fingerprint(case) for case in cases}
    by_id = {str(case.get("id")): case for case in cases}
    findings: list[dict[str, Any]] = []

    for index, left in enumerate(cases):
        left_id = str(left.get("id"))
        for right in cases[index + 1 :]:
            right_id = str(right.get("id"))
            score = similarity(normalized[left_id], normalized[right_id])
            if score < threshold:
                continue
            cross_holdout = "holdout" in {left.get("split"), right.get("split")} and left.get("split") != right.get("split")
            conflict = fingerprints[left_id] != fingerprints[right_id]
            severity = "critical" if cross_holdout or conflict else "major"
            finding_type = "semantic_holdout_leakage" if cross_holdout else ("label_conflict" if conflict else "semantic_duplicate")
            findings.append({
                "type": finding_type,
                "severity": severity,
                "case_ids": [left_id, right_id],
                "similarity": round(score, 6),
                "oracle_conflict": conflict,
                "message": "near-duplicate prompts cross the holdout boundary" if cross_holdout else ("near-duplicate prompts have different labels" if conflict else "near-duplicate cases reduce independent coverage"),
            })

    tag_counts: Counter[str] = Counter()
    risk_counts: Counter[str] = Counter()
    mode_counts: Counter[str] = Counter()
    provenance_counts: Counter[str] = Counter()
    uncovered: list[str] = []
    weak_truth: list[str] = []
    for case in cases:
        case_id = str(case.get("id"))
        tag_counts.update(str(tag) for tag in case.get("tags", []) if isinstance(tag, str))
        risk_counts[str(case.get("risk", "medium"))] += 1
        mode_counts[str(case.get("mode", "execution"))] += 1
        provenance = case.get("oracle", {}).get("provenance", {}) if isinstance(case.get("oracle"), dict) else {}
        kind = str(provenance.get("kind", "missing")) if isinstance(provenance, dict) else "missing"
        provenance_counts[kind] += 1
        if not case.get("expectations"):
            uncovered.append(case_id)
        if case.get("risk") in {"high", "critical"} and kind in {"observation", "hypothesis", "missing"}:
            weak_truth.append(case_id)

    if uncovered:
        findings.append({"type": "expectation_coverage", "severity": "major", "case_ids": uncovered, "message": "cases have no explicit expectations"})
    if weak_truth:
        findings.append({"type": "weak_professional_truth", "severity": "critical", "case_ids": weak_truth, "message": "high-risk cases lack authoritative truth"})

    failure_tags: Counter[str] = Counter()
    failed_case_counts: Counter[str] = Counter()
    if isinstance(benchmark, dict):
        for run in benchmark.get("runs", []):
            if not isinstance(run, dict) or run.get("configuration") != "candidate" or run.get("status") != "fail":
                continue
            case_id = str(run.get("case_id"))
            failed_case_counts[case_id] += 1
            for tag in by_id.get(case_id, {}).get("tags", []):
                if isinstance(tag, str):
                    failure_tags[tag] += 1
    active_learning = [
        {"tag": tag, "failed_runs": count, "recommendation": "add an opposite or independently sourced case for this failure cluster"}
        for tag, count in failure_tags.most_common()
    ]

    critical = [item for item in findings if item["severity"] == "critical"]
    gate = "fail" if validation_errors or critical else ("inconclusive" if findings else "pass")
    return {
        "schema_version": "1.0",
        "status": gate,
        "gate": gate,
        "eval_set_path": str(source.resolve()),
        "semantic_similarity_threshold": threshold,
        "validation": {"valid": not validation_errors, "errors": validation_errors, "warnings": validation_warnings},
        "counts": counts,
        "coverage": {
            "tags": dict(sorted(tag_counts.items())),
            "risks": dict(sorted(risk_counts.items())),
            "modes": dict(sorted(mode_counts.items())),
            "provenance": dict(sorted(provenance_counts.items())),
        },
        "findings": findings,
        "critical_findings": len(critical),
        "case_fingerprints": fingerprints,
        "failure_clusters": {"by_case": dict(sorted(failed_case_counts.items())), "by_tag": dict(sorted(failure_tags.items()))},
        "active_learning_recommendations": active_learning,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("eval_set", type=Path)
    parser.add_argument("--similarity-threshold", type=float, default=0.9)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--benchmark", type=Path, help="optional benchmark used to identify failure clusters for active-learning case selection")
    args = parser.parse_args()
    if not 0.5 <= args.similarity_threshold <= 1.0:
        parser.error("--similarity-threshold must be between 0.5 and 1.0")
    try:
        data = load_json(args.eval_set)
        if not isinstance(data, dict):
            raise ValueError("eval set root must be an object")
        benchmark = load_json(args.benchmark) if args.benchmark else None
        report = audit(data, args.eval_set, threshold=args.similarity_threshold, benchmark=benchmark)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        if args.output.exists():
            print("refusing to overwrite existing output", file=sys.stderr)
            return 2
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
