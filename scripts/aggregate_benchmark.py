#!/usr/bin/env python3
"""Aggregate grading.json files without conflating unresolved runs with failures."""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


STATUSES = {"pass", "fail", "inconclusive", "blocked", "invalid_case"}
SPLITS = {"train", "regression", "holdout"}


def stats(values: Iterable[float]) -> dict[str, float | int | None]:
    items = list(values)
    if not items:
        return {"count": 0, "mean": None, "stddev": None, "min": None, "max": None}
    mean = sum(items) / len(items)
    stddev = 0.0
    if len(items) > 1:
        stddev = math.sqrt(sum((item - mean) ** 2 for item in items) / (len(items) - 1))
    return {
        "count": len(items),
        "mean": round(mean, 6),
        "stddev": round(stddev, 6),
        "min": round(min(items), 6),
        "max": round(max(items), 6),
    }


def load_runs(root: Path) -> tuple[list[dict[str, Any]], list[str]]:
    runs: list[dict[str, Any]] = []
    warnings: list[str] = []
    seen_runs: set[tuple[str, str, str, int]] = set()
    for path in sorted(root.rglob("grading.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            warnings.append(f"ignored unreadable {path}: {exc}")
            continue
        required = ("case_id", "split", "configuration", "run_number", "status")
        missing = [key for key in required if key not in data]
        if missing:
            warnings.append(f"ignored {path}: missing {', '.join(missing)}")
            continue
        if data["status"] not in STATUSES:
            warnings.append(f"ignored {path}: unknown status {data['status']!r}")
            continue
        if data["split"] not in SPLITS:
            warnings.append(f"ignored {path}: unknown split {data['split']!r}")
            continue
        if not isinstance(data["configuration"], str) or not data["configuration"].strip():
            warnings.append(f"ignored {path}: configuration must be a non-empty string")
            continue
        if not isinstance(data["run_number"], int) or isinstance(data["run_number"], bool) or data["run_number"] < 1:
            warnings.append(f"ignored {path}: run_number must be an integer >= 1")
            continue
        score = data.get("score")
        if score is None and data["status"] in {"pass", "fail"}:
            score = 1.0 if data["status"] == "pass" else 0.0
        if score is not None and (not isinstance(score, (int, float)) or isinstance(score, bool) or not 0 <= score <= 1):
            warnings.append(f"ignored {path}: score must be between 0 and 1")
            continue
        metrics = data.get("metrics") if isinstance(data.get("metrics"), dict) else {}
        critical_failures = data.get("critical_failures", [])
        if not isinstance(critical_failures, list) or not all(isinstance(item, str) for item in critical_failures):
            warnings.append(f"ignored {path}: critical_failures must be an array of strings")
            continue
        run_key = (str(data["case_id"]), str(data["split"]), data["configuration"], data["run_number"])
        if run_key in seen_runs:
            warnings.append(f"ignored duplicate run {run_key} at {path}")
            continue
        seen_runs.add(run_key)
        runs.append(
            {
                "case_id": str(data["case_id"]),
                "split": str(data["split"]),
                "configuration": str(data["configuration"]),
                "run_number": data["run_number"],
                "status": data["status"],
                "score": float(score) if score is not None else None,
                "critical_failures": critical_failures,
                "duration_seconds": metrics.get("duration_seconds"),
                "tokens": metrics.get("tokens"),
                "tool_calls": metrics.get("tool_calls"),
                "errors": metrics.get("errors"),
                "candidate_id": data.get("candidate_id"),
                "eval_set_id": data.get("eval_set_id"),
                "design_id": data.get("design_id"),
                "approval_id": data.get("approval_id"),
                "traceability_id": data.get("traceability_id"),
                "conformance_review_id": data.get("conformance_review_id"),
                "assurance_candidate_id": data.get("assurance_candidate_id"),
                "model_id": data.get("model_id"),
                "environment_id": data.get("environment_id"),
                "execution_independence": data.get("execution_independence", "unverified"),
                "artifacts": list(data.get("artifacts", [])),
                "grading_path": str(path),
            }
        )
    return runs, warnings


def summarize(items: list[dict[str, Any]]) -> dict[str, Any]:
    status_counts = {status: sum(1 for item in items if item["status"] == status) for status in sorted(STATUSES)}
    determinate = status_counts["pass"] + status_counts["fail"]
    numeric = lambda key: [float(item[key]) for item in items if isinstance(item.get(key), (int, float)) and not isinstance(item.get(key), bool)]
    return {
        "runs": len(items),
        "status_counts": status_counts,
        "determinate_pass_rate": round(status_counts["pass"] / determinate, 6) if determinate else None,
        "score": stats(numeric("score")),
        "duration_seconds": stats(numeric("duration_seconds")),
        "tokens": stats(numeric("tokens")),
        "tool_calls": stats(numeric("tool_calls")),
        "errors": stats(numeric("errors")),
        "critical_failure_runs": sum(1 for item in items if item["critical_failures"]),
        "unverified_independence_runs": sum(1 for item in items if item["execution_independence"] != "verified"),
    }


def aggregate(runs: list[dict[str, Any]], source: Path, warnings: list[str]) -> dict[str, Any]:
    by_config: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_config_split: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    by_case: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for run in runs:
        by_config[run["configuration"]].append(run)
        by_config_split[(run["configuration"], run["split"])].append(run)
        by_case[(run["configuration"], run["split"], run["case_id"])].append(run)
    summary: dict[str, Any] = {}
    for configuration, items in sorted(by_config.items()):
        summary[configuration] = summarize(items)
        summary[configuration]["by_split"] = {
            split: summarize(split_items)
            for (config, split), split_items in sorted(by_config_split.items())
            if config == configuration
        }
    case_summary = []
    for (configuration, split, case_id), items in sorted(by_case.items()):
        row = {"configuration": configuration, "split": split, "case_id": case_id}
        row.update(summarize(items))
        case_summary.append(row)
    eval_set_ids = sorted({str(run["eval_set_id"]) for run in runs if run.get("eval_set_id")})
    design_ids = sorted({str(run["design_id"]) for run in runs if run.get("design_id")})
    approval_ids = sorted({str(run["approval_id"]) for run in runs if run.get("approval_id")})
    traceability_ids = sorted({str(run["traceability_id"]) for run in runs if run.get("traceability_id")})
    conformance_ids = sorted({str(run["conformance_review_id"]) for run in runs if run.get("conformance_review_id")})
    model_ids = sorted({str(run["model_id"]) for run in runs if run.get("model_id")})
    environment_ids = sorted({str(run["environment_id"]) for run in runs if run.get("environment_id")})
    candidate_ids = {
        config: sorted({str(item["candidate_id"]) for item in items if item.get("candidate_id")})
        for config, items in sorted(by_config.items())
    }
    candidate_runs = by_config.get("candidate", [])
    candidate_traceability_ids = sorted({str(run["traceability_id"]) for run in candidate_runs if run.get("traceability_id")})
    candidate_conformance_ids = sorted({str(run["conformance_review_id"]) for run in candidate_runs if run.get("conformance_review_id")})
    assurance_by_configuration = {
        config: {
            "traceability_ids": sorted({str(item["traceability_id"]) for item in items if item.get("traceability_id")}),
            "conformance_review_ids": sorted({str(item["conformance_review_id"]) for item in items if item.get("conformance_review_id")}),
            "candidate_ids": sorted({str(item["candidate_id"]) for item in items if item.get("candidate_id")}),
            "exact_candidate_binding": all(item.get("assurance_candidate_id") == item.get("candidate_id") for item in items),
        }
        for config, items in sorted(by_config.items())
    }
    integrity = {
        "identity_bound": (
            all(run.get("candidate_id") and run.get("eval_set_id") for run in runs)
            and len(eval_set_ids) == 1
            and all(len(ids) == 1 for ids in candidate_ids.values())
        ),
        "environment_bound": (
            all(run.get("model_id") and run.get("environment_id") for run in runs)
            and len(model_ids) == 1
            and len(environment_ids) == 1
        ),
        "design_bound": all(run.get("design_id") for run in runs) and len(design_ids) == 1,
        "approval_bound": all(run.get("approval_id") for run in runs) and len(approval_ids) == 1,
        "traceability_bound": bool(candidate_runs) and all(run.get("traceability_id") for run in candidate_runs) and len(candidate_traceability_ids) == 1 and all(run.get("assurance_candidate_id") == run.get("candidate_id") for run in candidate_runs),
        "conformance_bound": bool(candidate_runs) and all(run.get("conformance_review_id") for run in candidate_runs) and len(candidate_conformance_ids) == 1 and all(run.get("assurance_candidate_id") == run.get("candidate_id") for run in candidate_runs),
        "eval_set_ids": eval_set_ids,
        "design_ids": design_ids,
        "approval_ids": approval_ids,
        "traceability_ids": traceability_ids,
        "conformance_review_ids": conformance_ids,
        "candidate_traceability_ids": candidate_traceability_ids,
        "candidate_conformance_review_ids": candidate_conformance_ids,
        "assurance_by_configuration": assurance_by_configuration,
        "candidate_ids": candidate_ids,
        "model_ids": model_ids,
        "environment_ids": environment_ids,
    }
    return {
        "schema_version": "2.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": str(source),
        "warnings": warnings,
        "integrity": integrity,
        "configurations": summary,
        "case_summary": case_summary,
        "runs": runs,
    }


def markdown(benchmark: dict[str, Any]) -> str:
    lines = ["# Skill optimization benchmark", "", "| Configuration | Split | Runs | Pass rate | Score mean ± stddev | Unresolved | Critical |", "| --- | --- | ---: | ---: | ---: | ---: | ---: |"]
    for config, config_summary in benchmark["configurations"].items():
        rows = [("all", config_summary)] + list(config_summary.get("by_split", {}).items())
        for split, row in rows:
            score = row["score"]
            score_text = "—" if score["mean"] is None else f"{score['mean']:.3f} ± {score['stddev']:.3f}"
            pass_rate = row["determinate_pass_rate"]
            pass_text = "—" if pass_rate is None else f"{pass_rate:.1%}"
            counts = row["status_counts"]
            unresolved = counts["blocked"] + counts["inconclusive"] + counts["invalid_case"]
            lines.append(f"| {config} | {split} | {row['runs']} | {pass_text} | {score_text} | {unresolved} | {row['critical_failure_runs']} |")
    if benchmark["warnings"]:
        lines.extend(["", "## Warnings", ""] + [f"- {item}" for item in benchmark["warnings"]])
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs_dir", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.runs_dir.is_dir():
        parser.error(f"runs directory not found: {args.runs_dir}")
    runs, warnings = load_runs(args.runs_dir)
    if not runs:
        print("no valid grading.json files found", file=sys.stderr)
        return 1
    benchmark = aggregate(runs, args.runs_dir, warnings)
    output = args.output or args.runs_dir / "benchmark.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(benchmark, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    output.with_suffix(".md").write_text(markdown(benchmark), encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
