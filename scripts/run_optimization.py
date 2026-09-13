#!/usr/bin/env python3
"""Run an isolated execute-grade-attribute-modify Skill optimization loop."""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aggregate_benchmark import aggregate, load_runs, markdown
from analyze_benchmark import analyze
from audit_eval_set import audit as audit_eval_set
from command_line import split_command
from design_gate import ensure_binding_match, file_hash as assurance_file_hash
from design_gate import bind, freeze_approved_design, validate_approval, validate_conformance, validate_design
from generate_eval_report import render
from prepare_iteration import tree_hash
from refresh_assurance import refresh as refresh_assurance_evidence
from select_candidate import decide
from validate_eval_set import load_json, validate
from validate_candidate import validate_candidate
from validate_traceability import validate_traceability


ATTRIBUTION_LAYERS = {
    "requirement",
    "method",
    "knowledge",
    "retrieval",
    "tool",
    "runtime",
    "instruction",
    "scoring",
}
RESULT_STATUSES = {"pass", "fail", "inconclusive", "blocked", "invalid_case"}
IGNORED_PARTS = {".git", "__pycache__", ".pytest_cache"}
COPY_IGNORE = shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache")
EDITABLE_LAYER_PATHS = {
    "method": ("SKILL.md", "references/"),
    "knowledge": ("references/",),
    "retrieval": ("SKILL.md", "references/", "scripts/"),
    "tool": ("scripts/",),
    "instruction": ("SKILL.md", "references/"),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RuntimeError(f"adapter did not create response: {path}") from None
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"adapter response is invalid JSON at {path}: {exc}") from None
    if not isinstance(value, dict):
        raise RuntimeError(f"adapter response root must be an object: {path}")
    return value


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def tree_file_manifest(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): sha256_path(path)
        for path in sorted(root.rglob("*"))
        if path.is_file() and not set(path.relative_to(root).parts) & IGNORED_PARTS
    }


def path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def without_frontmatter_description(text: str) -> str:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return text
    closed = False
    normalized: list[str] = []
    for index, line in enumerate(lines):
        if index > 0 and line.strip() == "---":
            closed = True
        if not closed and line.startswith("description:"):
            normalized.append("description: <DESCRIPTION>\n")
        else:
            normalized.append(line)
    return "".join(normalized)


def freeze_eval_bundle(data: dict[str, Any], source: Path, destination: Path) -> str:
    destination.mkdir(parents=True, exist_ok=False)
    frozen = destination / "eval-set.json"
    shutil.copy2(source, frozen)
    copied: set[str] = set()
    for case in data["cases"]:
        for relative in case.get("files", []):
            if relative in copied:
                continue
            copied.add(relative)
            source_file = (source.parent / relative).resolve()
            target_file = destination / relative
            target_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_file, target_file)
    return eval_bundle_hash(data, destination)


def eval_bundle_hash(data: dict[str, Any], root: Path) -> str:
    digest = hashlib.sha256()
    relative_paths = {"eval-set.json"}
    relative_paths.update(relative for case in data["cases"] for relative in case.get("files", []))
    for relative in sorted(relative_paths):
        path = root / relative
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(sha256_path(path).encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


class AdapterCallError(RuntimeError):
    def __init__(self, message: str, failure_class: str, retryable: bool) -> None:
        super().__init__(message)
        self.failure_class = failure_class
        self.retryable = retryable


class Adapter:
    def __init__(self, command: list[str], timeout: int, jobs_root: Path, max_retries: int = 0) -> None:
        self.command = command
        self.timeout = timeout
        self.jobs_root = jobs_root
        self.max_retries = max_retries

    def call(self, job: dict[str, Any], relative: Path) -> dict[str, Any]:
        job_path = self.jobs_root / relative.with_suffix(".job.json")
        response_path = self.jobs_root / relative.with_suffix(".response.json")
        write_json(job_path, job)
        response_path.parent.mkdir(parents=True, exist_ok=True)
        retry_safe = job.get("job_type") in {"grade", "attribute", "compare", "review_design", "review_conformance"}
        last_error: AdapterCallError | None = None
        for attempt in range(1, self.max_retries + 2):
            attempt_response = response_path.with_name(response_path.stem + f".attempt-{attempt}.json")
            log_path = self.jobs_root / relative.with_suffix(f".attempt-{attempt}.adapter.log")
            started = time.monotonic()
            try:
                completed = subprocess.run(
                    [*self.command, str(job_path), str(attempt_response)],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                )
            except subprocess.TimeoutExpired as exc:
                log_path.write_text(
                    f"failure_class: transient-timeout\ntimeout after {self.timeout}s\nstdout:\n{exc.stdout or ''}\nstderr:\n{exc.stderr or ''}\n",
                    encoding="utf-8",
                )
                last_error = AdapterCallError(f"adapter timed out for {job['job_id']}; see {log_path}", "transient-timeout", retry_safe)
                if retry_safe and attempt <= self.max_retries:
                    continue
                raise last_error from None
            log_path.write_text(
                f"failure_class: {'none' if completed.returncode == 0 else ('transient-exit-75' if completed.returncode == 75 else 'permanent-exit')}\n"
                f"exit_code: {completed.returncode}\nelapsed_seconds: {time.monotonic() - started:.6f}\n"
                f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}\n",
                encoding="utf-8",
            )
            if completed.returncode != 0:
                transient = completed.returncode == 75
                last_error = AdapterCallError(f"adapter failed for {job['job_id']}; see {log_path}", "transient-exit-75" if transient else "permanent-exit", transient and retry_safe)
                if transient and retry_safe and attempt <= self.max_retries:
                    continue
                raise last_error
            try:
                response = read_object(attempt_response)
            except RuntimeError as exc:
                raise AdapterCallError(str(exc), "protocol", False) from exc
            write_json(response_path, response)
            break
        else:
            raise last_error or AdapterCallError(f"adapter failed for {job['job_id']}", "unknown", False)
        if response.get("job_id") not in (None, job["job_id"]):
            raise AdapterCallError(f"adapter response job_id mismatch for {job['job_id']}", "protocol", False)
        response["job_id"] = job["job_id"]
        write_json(response_path, response)
        return response


def opaque_case_ref(case_id: str) -> str:
    return hashlib.sha256(case_id.encode("utf-8")).hexdigest()[:16]


def public_case(case: dict[str, Any], eval_root: Path) -> dict[str, Any]:
    """Return only fields an executor may see; labels and split stay hidden."""
    return {
        "ref": opaque_case_ref(case["id"]),
        "mode": case.get("mode", "execution"),
        "prompt": case["prompt"],
        "files": [str((eval_root / item).resolve()) for item in case.get("files", [])],
        "risk": case.get("risk", "medium"),
    }


def repetitions(case: dict[str, Any], policy: dict[str, Any]) -> int:
    return int(case.get("repetitions", policy.get("default_repetitions", 1)))


def validate_execute_response(response: dict[str, Any], job_id: str, mode: str, output_dir: Path) -> None:
    if response.get("status") not in {"completed", "blocked"}:
        raise RuntimeError(f"execute response for {job_id} needs status completed or blocked")
    if response["status"] == "completed" and not isinstance(response.get("transcript_path"), str):
        raise RuntimeError(f"execute response for {job_id} needs transcript_path")
    if response["status"] == "completed":
        transcript = Path(response["transcript_path"])
        if not transcript.is_file() or not path_is_within(transcript, output_dir):
            raise RuntimeError(f"execute response for {job_id} has transcript outside output_dir or missing")
    artifacts = response.get("artifacts", [])
    if not isinstance(artifacts, list) or not all(isinstance(item, str) for item in artifacts):
        raise RuntimeError(f"execute response for {job_id} needs artifacts as an array of paths")
    for item in artifacts:
        artifact = Path(item)
        if not artifact.exists() or not path_is_within(artifact, output_dir):
            raise RuntimeError(f"execute response for {job_id} has artifact outside output_dir or missing: {item}")
    if not isinstance(response.get("metrics", {}), dict):
        raise RuntimeError(f"execute response for {job_id} needs metrics as an object")
    for field in ("model_id", "environment_id", "execution_independence"):
        if not isinstance(response.get(field), str) or not response[field]:
            raise RuntimeError(f"execute response for {job_id} needs {field}")
    if response["execution_independence"] not in {"verified", "unverified"}:
        raise RuntimeError(f"execute response for {job_id} has invalid execution_independence")
    if mode == "trigger":
        activation = response.get("activation")
        if not isinstance(activation, dict) or not isinstance(activation.get("observed"), bool):
            raise RuntimeError(f"trigger execute response for {job_id} needs activation.observed")
        if not isinstance(activation.get("evidence"), str) or not activation["evidence"].strip():
            raise RuntimeError(f"trigger execute response for {job_id} needs activation.evidence")


def blocked_grading(execution: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "2.0",
        "status": "blocked",
        "score": None,
        "expectations": [],
        "critical_failures": [],
        "claims": [],
        "eval_feedback": [execution.get("error", "execution was blocked")],
    }


def validate_grading_core(grading: dict[str, Any], job_id: str) -> None:
    status = grading.get("status")
    if status not in RESULT_STATUSES:
        raise RuntimeError(f"grading response for {job_id} has invalid status")
    score = grading.get("score")
    if score is not None and (
        not isinstance(score, (int, float)) or isinstance(score, bool) or not 0 <= float(score) <= 1
    ):
        raise RuntimeError(f"grading response for {job_id} has invalid score")
    if status in {"pass", "fail"} and score is None:
        raise RuntimeError(f"grading response for {job_id} needs score for a determinate status")
    if status in {"inconclusive", "blocked", "invalid_case"} and score is not None:
        raise RuntimeError(f"grading response for {job_id} must not score an unresolved status")
    for field in ("expectations", "critical_failures", "claims", "eval_feedback"):
        if not isinstance(grading.get(field, []), list):
            raise RuntimeError(f"grading response for {job_id} needs array field {field}")
    if not all(isinstance(item, dict) for item in grading.get("expectations", [])):
        raise RuntimeError(f"grading response for {job_id} has invalid expectation results")
    if not all(isinstance(item, str) for item in grading.get("critical_failures", [])):
        raise RuntimeError(f"grading response for {job_id} has invalid critical_failures")


def execute_and_grade(
    adapter: Adapter,
    case: dict[str, Any],
    split: str,
    configuration: str,
    skill_path: Path | None,
    candidate_id: str,
    eval_set_id: str,
    design_id: str | None,
    assurance_ids: dict[str, str | None],
    eval_root: Path,
    run_root: Path,
    iteration: int,
    run_number: int,
) -> dict[str, Any]:
    case_ref = opaque_case_ref(case["id"])
    stem = Path(f"iteration-{iteration}") / "cases" / case_ref / configuration / f"run-{run_number}"
    output_dir = run_root / case_ref / configuration / f"run-{run_number}"
    output_dir.mkdir(parents=True, exist_ok=False)
    execute_id = f"execute:{iteration}:{case_ref}:{configuration}:{run_number}"
    execute_job = {
        "schema_version": "2.0",
        "job_type": "execute",
        "job_id": execute_id,
        "iteration": iteration,
        "case": public_case(case, eval_root),
        "configuration": configuration,
        "skill_path": str(skill_path) if skill_path else None,
        "candidate_id": candidate_id,
        "eval_set_id": eval_set_id,
        "design_id": design_id,
        **assurance_ids,
        "run_number": run_number,
        "output_dir": str(output_dir),
        "constraints": {"labels_hidden": True, "do_not_self_grade": True},
    }
    if case.get("mode", "execution") == "trigger":
        execute_job["constraints"].update(
            {"implicit_activation_only": True, "require_activation_observation": True}
        )
    execution = adapter.call(execute_job, stem / "execute")
    validate_execute_response(execution, execute_id, case.get("mode", "execution"), output_dir)
    write_json(output_dir / "execution.json", execution)

    grade_id = f"grade:{iteration}:{case['id']}:{configuration}:{run_number}"
    grade_job = {
        "schema_version": "2.0",
        "job_type": "grade",
        "job_id": grade_id,
        "iteration": iteration,
        "case": case,
        "configuration": configuration,
        "candidate_id": candidate_id,
        "eval_set_id": eval_set_id,
        "design_id": design_id,
        **assurance_ids,
        "run_number": run_number,
        "execution": execution,
        "output_dir": str(output_dir),
        "constraints": {"inspect_real_artifacts": True, "do_not_modify_candidate": True},
    }
    grading_core = (
        blocked_grading(execution)
        if execution["status"] == "blocked"
        else adapter.call(grade_job, stem / "grade")
    )
    validate_grading_core(grading_core, grade_id)
    grading = dict(grading_core)
    grading.update(
        {
            "schema_version": "2.0",
            "case_id": case["id"],
            "split": split,
            "configuration": configuration,
            "run_number": run_number,
            "candidate_id": candidate_id,
            "eval_set_id": eval_set_id,
            "design_id": design_id,
            **assurance_ids,
            "model_id": execution["model_id"],
            "environment_id": execution["environment_id"],
            "execution_independence": execution["execution_independence"],
            "metrics": execution.get("metrics", {}),
            "artifacts": execution.get("artifacts", []),
        }
    )
    grading.pop("job_id", None)
    write_json(output_dir / "grading.json", grading)
    return {
        "case": case,
        "configuration": configuration,
        "skill_path": skill_path,
        "candidate_id": candidate_id,
        "run_number": run_number,
        "output_dir": output_dir,
        "execution": execution,
        "grading": grading,
    }


def run_split(
    adapter: Adapter,
    cases: list[dict[str, Any]],
    configurations: list[tuple[str, Path | None, str, dict[str, str | None]]],
    policy: dict[str, Any],
    eval_set_id: str,
    design_id: str | None,
    eval_root: Path,
    run_root: Path,
    iteration: int,
    workers: int,
) -> list[dict[str, Any]]:
    tasks: list[tuple[dict[str, Any], str, Path | None, str, dict[str, str | None], int]] = []
    for case in cases:
        for run_number in range(1, repetitions(case, policy) + 1):
            for configuration, skill_path, candidate_id, configuration_assurance in configurations:
                tasks.append((case, configuration, skill_path, candidate_id, configuration_assurance, run_number))
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                execute_and_grade,
                adapter,
                case,
                case["split"],
                configuration,
                skill_path,
                candidate_id,
                eval_set_id,
                design_id,
                configuration_assurance,
                eval_root,
                run_root,
                iteration,
                run_number,
            ): (case["id"], configuration, run_number)
            for case, configuration, skill_path, candidate_id, configuration_assurance, run_number in tasks
        }
        for future in as_completed(futures):
            label = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:
                raise RuntimeError(f"run failed for {label}: {exc}") from exc
    return sorted(results, key=lambda item: (item["case"]["id"], item["configuration"], item["run_number"]))


def write_benchmark(
    run_root: Path,
    output_root: Path,
    eval_set: dict[str, Any] | None = None,
    parent: Path | None = None,
    candidate: Path | None = None,
    blind_comparison: dict[str, Any] | None = None,
) -> dict[str, Any]:
    runs, warnings = load_runs(run_root)
    if not runs:
        raise RuntimeError(f"no valid grading results below {run_root}")
    benchmark = aggregate(runs, run_root, warnings)
    if eval_set is not None:
        benchmark["analysis"] = analyze(benchmark, eval_set, parent, candidate)
    if blind_comparison is not None:
        benchmark["blind_comparison"] = blind_comparison
    write_json(output_root / "benchmark.json", benchmark)
    (output_root / "benchmark.md").write_text(markdown(benchmark), encoding="utf-8")
    return benchmark


def verify_frozen_inputs(
    formal_skill: Path,
    formal_hash: str,
    candidate: Path,
    candidate_id: str,
    baseline: Path | None,
    baseline_id: str,
    eval_set: dict[str, Any],
    eval_root: Path,
    eval_set_id: str,
    design_id: str | None,
    assurance_ids: dict[str, str | None],
    assurance_paths: dict[str, Path] | None = None,
) -> None:
    if tree_hash(formal_skill) != formal_hash:
        raise RuntimeError("formal Skill changed during execute/grade")
    if tree_hash(candidate) != candidate_id:
        raise RuntimeError("candidate changed during execute/grade; only the modify phase may edit it")
    if baseline is not None and tree_hash(baseline) != baseline_id:
        raise RuntimeError("baseline changed during execute/grade")
    if eval_bundle_hash(eval_set, eval_root) != eval_set_id:
        raise RuntimeError("frozen eval set or case inputs changed during execute/grade")
    if design_id is not None:
        frozen_design = eval_root.parent / "design" / "skill-design.md"
        report = validate_design(frozen_design, True)
        if not report["valid"] or report["design_id"] != design_id:
            raise RuntimeError("frozen Skill design changed or no longer passes the ready design gate")
        assurance_files = assurance_paths or {
            "approval_id": eval_root.parent / "design" / "design-approval.json",
            "traceability_id": eval_root.parent / "design" / "traceability-validation.json",
            "conformance_review_id": eval_root.parent / "design" / "conformance-review.json",
        }
        for key, path in assurance_files.items():
            expected = assurance_ids.get(key)
            if expected and (not path.is_file() or assurance_file_hash(path) != expected):
                raise RuntimeError(f"frozen design assurance artifact changed or is missing: {key}")
        frozen_approval = load_json(eval_root.parent / "design" / "design-approval.json")
        frozen_review = eval_root.parent / "design" / "design-review.json"
        if frozen_approval.get("review_artifact_id") and (not frozen_review.is_file() or assurance_file_hash(frozen_review) != frozen_approval["review_artifact_id"]):
            raise RuntimeError("frozen semantic design review changed or is missing")


def attribute_failures(
    adapter: Adapter,
    failed_runs: list[dict[str, Any]],
    iteration: int,
) -> list[dict[str, Any]]:
    attributions: list[dict[str, Any]] = []
    for item in failed_runs:
        case = item["case"]
        job_id = f"attribute:{iteration}:{case['id']}:candidate:{item['run_number']}"
        job = {
            "schema_version": "2.0",
            "job_type": "attribute",
            "job_id": job_id,
            "iteration": iteration,
            "case": case,
            "candidate_id": item["candidate_id"],
            "skill_path": str(item["skill_path"]),
            "execution": item["execution"],
            "grading": item["grading"],
            "output_dir": str(item["output_dir"]),
            "layers": sorted(ATTRIBUTION_LAYERS),
            "constraints": {"do_not_modify_candidate": True, "require_discriminating_evidence": True},
        }
        case_ref = opaque_case_ref(case["id"])
        relative = Path(f"iteration-{iteration}") / "cases" / case_ref / "candidate" / f"run-{item['run_number']}" / "attribute"
        result = adapter.call(job, relative)
        status = result.get("status")
        layer = result.get("responsible_layer")
        confidence = result.get("confidence")
        if status not in {"attributed", "inconclusive"}:
            raise RuntimeError(f"attribution response for {job_id} has invalid status")
        if status == "attributed" and layer not in ATTRIBUTION_LAYERS:
            raise RuntimeError(f"attribution response for {job_id} has invalid responsible_layer")
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= confidence <= 1:
            raise RuntimeError(f"attribution response for {job_id} has invalid confidence")
        if not isinstance(result.get("evidence", []), list) or not all(
            isinstance(value, str) for value in result.get("evidence", [])
        ):
            raise RuntimeError(f"attribution response for {job_id} has invalid evidence")
        if not isinstance(result.get("alternatives", []), list) or not all(
            value in ATTRIBUTION_LAYERS for value in result.get("alternatives", [])
        ):
            raise RuntimeError(f"attribution response for {job_id} has invalid alternatives")
        if not isinstance(result.get("discriminating_check"), str) or not result["discriminating_check"].strip():
            raise RuntimeError(f"attribution response for {job_id} needs discriminating_check")
        if status == "attributed" and (
            not isinstance(result.get("observed_failure"), str) or not result["observed_failure"].strip()
        ):
            raise RuntimeError(f"attribution response for {job_id} needs observed_failure")
        if status == "attributed" and (
            not isinstance(result.get("recommended_change_target"), str)
            or not result["recommended_change_target"].strip()
        ):
            raise RuntimeError(f"attribution response for {job_id} needs recommended_change_target")
        result.update(
            {
                "case_id": case["id"],
                "split": case["split"],
                "mode": case.get("mode", "execution"),
                "run_number": item["run_number"],
            }
        )
        write_json(item["output_dir"] / "attribution.json", result)
        attributions.append(result)
    return attributions


def modify_candidate(
    adapter: Adapter,
    current: Path,
    next_candidate: Path,
    attributions: list[dict[str, Any]],
    exposed_cases: list[dict[str, Any]],
    iteration: int,
    formal_hash: str,
    formal_skill: Path,
    metadata_root: Path,
    primary_layer: str,
    design_id: str | None,
) -> dict[str, Any]:
    if any(case.get("split") == "holdout" for case in exposed_cases):
        raise RuntimeError("internal error: holdout case reached modifier")
    if any(item.get("split") == "holdout" for item in attributions):
        raise RuntimeError("internal error: holdout attribution reached modifier")
    if primary_layer not in EDITABLE_LAYER_PATHS:
        raise RuntimeError(f"responsibility layer is not candidate-editable: {primary_layer}")
    if any(item.get("responsible_layer") != primary_layer for item in attributions):
        raise RuntimeError("modify phase must contain exactly one responsibility layer")
    next_candidate.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(current, next_candidate, ignore=COPY_IGNORE)
    parent_id = tree_hash(current)
    parent_files = tree_file_manifest(current)
    trigger_description_only = all(item.get("mode") == "trigger" for item in attributions)
    parent_skill_text = (current / "SKILL.md").read_text(encoding="utf-8")
    job_id = f"modify:{iteration}:to:{iteration + 1}"
    job = {
        "schema_version": "2.0",
        "job_type": "modify",
        "job_id": job_id,
        "source_iteration": iteration,
        "target_iteration": iteration + 1,
        "parent_candidate_id": parent_id,
        "design_id": design_id,
        "target_skill_path": str(next_candidate),
        "attributions": attributions,
        "cases": exposed_cases,
        "constraints": {
            "holdout_exposed": False,
            "modify_only_target_skill_path": True,
            "minimal_responsible_layer_change": True,
            "primary_responsible_layer": primary_layer,
            "allowed_changed_paths": list(EDITABLE_LAYER_PATHS[primary_layer]),
            "trigger_description_only": trigger_description_only,
            "formal_skill_content_id_before": formal_hash,
        },
    }
    response = adapter.call(job, Path(f"iteration-{iteration}") / "modify")
    if response.get("status") not in {"modified", "no_change", "blocked"}:
        raise RuntimeError(f"modifier response for {job_id} has invalid status")
    if response["status"] == "modified":
        for field in ("change_summary", "similar_defect_check", "opposite_case"):
            if not isinstance(response.get(field), str) or not response[field].strip():
                raise RuntimeError(f"modifier response for {job_id} needs {field}")
        if not isinstance(response.get("validation"), list) or not all(
            isinstance(item, str) and item.strip() for item in response["validation"]
        ):
            raise RuntimeError(f"modifier response for {job_id} needs non-empty string validation entries")
    if tree_hash(formal_skill) != formal_hash:
        raise RuntimeError("formal Skill changed during optimization; stop and inspect immediately")
    new_id = tree_hash(next_candidate)
    new_files = tree_file_manifest(next_candidate)
    actual_changed = sorted(
        relative for relative in set(parent_files) | set(new_files) if parent_files.get(relative) != new_files.get(relative)
    )
    disallowed = [
        relative
        for relative in actual_changed
        if not any(
            relative == prefix or relative.startswith(prefix)
            for prefix in EDITABLE_LAYER_PATHS[primary_layer]
        )
    ]
    if disallowed:
        raise RuntimeError(f"modifier changed files outside {primary_layer} layer: {disallowed}")
    if trigger_description_only:
        next_skill_text = (next_candidate / "SKILL.md").read_text(encoding="utf-8")
        if actual_changed != ["SKILL.md"] or without_frontmatter_description(parent_skill_text) != without_frontmatter_description(next_skill_text):
            raise RuntimeError("trigger optimization may change only the SKILL.md frontmatter description")
    declared_changed = response.get("changed_files", [])
    if not isinstance(declared_changed, list) or not all(isinstance(item, str) for item in declared_changed):
        raise RuntimeError("modifier response changed_files must be an array of relative paths")
    if sorted(declared_changed) != actual_changed:
        raise RuntimeError(f"modifier changed_files mismatch: declared={sorted(declared_changed)} actual={actual_changed}")
    if response["status"] == "modified" and new_id == parent_id:
        raise RuntimeError("modifier reported modified but candidate content did not change")
    if response["status"] in {"no_change", "blocked"} and new_id != parent_id:
        raise RuntimeError(f"modifier returned {response['status']} but candidate content changed")
    manifest = {
        "schema_version": "2.0",
        "iteration": iteration + 1,
        "created_at": utc_now(),
        "parent_candidate_id": parent_id,
        "new_candidate_id": new_id,
        "design_id": design_id,
        "responsible_layers": sorted({str(item.get("responsible_layer")) for item in attributions}),
        "primary_responsible_layer": primary_layer,
        "source_cases": sorted({str(item.get("case_id")) for item in attributions}),
        "observed_failures": [str(item["observed_failure"]) for item in attributions],
        "causal_evidence": [
            str(value) for item in attributions for value in item.get("evidence", [])
        ],
        "changed_files": actual_changed,
        "change_summary": response.get("change_summary", ""),
        "similar_defect_check": response.get("similar_defect_check", ""),
        "opposite_case": response.get("opposite_case", ""),
        "holdout_exposed_to_modifier": False,
        "formal_skill_untouched": True,
        "required_revalidation": sorted(str(case["id"]) for case in exposed_cases),
        "modifier_status": response["status"],
        "adapter_response": response,
        "status": "pending-eval" if response["status"] == "modified" else response["status"],
    }
    write_json(metadata_root / f"iteration-{iteration + 1}.json", manifest)
    return manifest


def choose_primary_layer(attributions: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    ranked = sorted(
        attributions,
        key=lambda item: (-float(item.get("confidence", 0.0)), str(item.get("responsible_layer")), str(item.get("case_id"))),
    )
    primary = str(ranked[0]["responsible_layer"])
    selected = [item for item in ranked if item.get("responsible_layer") == primary]
    deferred = [item for item in ranked if item.get("responsible_layer") != primary]
    return primary, selected, deferred


def candidate_change_preflight(
    benchmark: dict[str, Any],
    eval_set: dict[str, Any],
    parent: Path,
    candidate: Path,
) -> dict[str, Any]:
    """Run only change-dependent checks before the new candidate has execution results."""
    full = analyze(benchmark, eval_set, parent, candidate)
    findings = [
        item
        for item in full.get("findings", [])
        if item.get("code") in {"case_id_overfit", "possible_literal_overfit"}
    ]
    if any(item.get("severity") == "critical" for item in findings):
        gate = "fail"
    elif findings and bool(eval_set.get("policy", {}).get("fail_on_overfit_signal", True)):
        gate = "inconclusive"
    else:
        gate = "pass"
    return {
        "schema_version": "2.0",
        "gate": gate,
        "scope": "candidate-change-only",
        "finding_counts": {
            severity: sum(1 for item in findings if item.get("severity") == severity)
            for severity in ("critical", "major", "info")
        },
        "findings": findings,
    }


def copy_blind_artifacts(execution: dict[str, Any], destination: Path) -> list[str]:
    destination.mkdir(parents=True, exist_ok=False)
    copied: list[str] = []
    for index, value in enumerate(execution.get("artifacts", []), 1):
        source = Path(value)
        suffix = source.suffix
        target = destination / f"artifact-{index}{suffix}"
        if source.is_file():
            shutil.copy2(source, target)
            copied.append(str(target))
    return copied


def run_blind_comparisons(
    adapter: Adapter,
    results: list[dict[str, Any]],
    iteration: int,
    workspace: Path,
) -> dict[str, Any]:
    indexed = {
        (str(item["case"]["id"]), int(item["run_number"]), str(item["configuration"])): item
        for item in results
    }
    judgments: list[dict[str, Any]] = []
    case_runs = sorted({(case_id, run_number) for case_id, run_number, _ in indexed})
    for case_id, run_number in case_runs:
        candidate = indexed.get((case_id, run_number, "candidate"))
        baseline = indexed.get((case_id, run_number, "baseline"))
        if not candidate or not baseline:
            continue
        case = candidate["case"]
        case_ref = opaque_case_ref(case_id)
        blind_root = workspace / "blind" / f"iteration-{iteration}" / case_ref / f"run-{run_number}"
        order = ["candidate", "baseline"]
        if secrets.randbelow(2):
            order.reverse()
        source_by_config = {"candidate": candidate, "baseline": baseline}
        alias_to_config = {"A": order[0], "B": order[1]}
        variants: dict[str, Any] = {}
        for alias, configuration in alias_to_config.items():
            source = source_by_config[configuration]
            variants[alias] = {
                "artifacts": copy_blind_artifacts(source["execution"], blind_root / alias),
                "status": source["execution"].get("status"),
            }
        compare_output = blind_root / "review"
        compare_output.mkdir(parents=True, exist_ok=False)
        job_id = f"compare:{iteration}:{case_ref}:{run_number}"
        job = {
            "schema_version": "2.0",
            "job_type": "compare",
            "job_id": job_id,
            "iteration": iteration,
            "case": case,
            "variants": variants,
            "output_dir": str(compare_output),
            "constraints": {"configuration_identity_hidden": True, "do_not_modify_artifacts": True},
        }
        response = adapter.call(
            job,
            Path(f"iteration-{iteration}") / "blind" / case_ref / f"run-{run_number}" / "compare",
        )
        winner = response.get("winner")
        confidence = response.get("confidence")
        evidence = response.get("evidence")
        if winner not in {"A", "B", "tie", "inconclusive"}:
            raise RuntimeError(f"blind comparator for {job_id} has invalid winner")
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= confidence <= 1:
            raise RuntimeError(f"blind comparator for {job_id} has invalid confidence")
        if not isinstance(evidence, list) or not all(isinstance(item, str) for item in evidence):
            raise RuntimeError(f"blind comparator for {job_id} has invalid evidence")
        rubric = response.get("rubric")
        if rubric is not None:
            if not isinstance(rubric, dict) or set(rubric) != {"A", "B"}:
                raise RuntimeError(f"blind comparator for {job_id} has invalid rubric")
            for alias in ("A", "B"):
                row = rubric.get(alias)
                if not isinstance(row, dict):
                    raise RuntimeError(f"blind comparator for {job_id} has invalid {alias} rubric")
                for dimension in ("content", "structure"):
                    score = row.get(dimension)
                    if not isinstance(score, (int, float)) or isinstance(score, bool) or not 0 <= score <= 1:
                        raise RuntimeError(f"blind comparator for {job_id} has invalid {alias}.{dimension}")
                for field in ("strengths", "weaknesses"):
                    values = row.get(field, [])
                    if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
                        raise RuntimeError(f"blind comparator for {job_id} has invalid {alias}.{field}")
        expectation_results = response.get("expectation_results")
        if expectation_results is not None and (
            not isinstance(expectation_results, list) or not all(isinstance(item, dict) for item in expectation_results)
        ):
            raise RuntimeError(f"blind comparator for {job_id} has invalid expectation_results")
        mapped = alias_to_config.get(winner, winner)
        judgment = {
            "case_id": case_id,
            "split": case["split"],
            "run_number": run_number,
            "winner": mapped,
            "blind_winner": winner,
            "confidence": confidence,
            "critical_difference": bool(response.get("critical_difference", False)),
            "evidence": evidence,
            "rubric": rubric,
            "expectation_results": expectation_results,
        }
        write_json(blind_root / "mapping.json", {"alias_to_configuration": alias_to_config})
        write_json(blind_root / "judgment.json", judgment)
        judgments.append(judgment)
    candidate_wins = sum(1 for item in judgments if item["winner"] == "candidate")
    baseline_wins = sum(1 for item in judgments if item["winner"] == "baseline")
    ties = sum(1 for item in judgments if item["winner"] == "tie")
    inconclusive = sum(1 for item in judgments if item["winner"] == "inconclusive")
    determinate = candidate_wins + baseline_wins
    return {
        "schema_version": "2.0",
        "status": "complete" if judgments else "incomplete",
        "judgments": judgments,
        "candidate_wins": candidate_wins,
        "baseline_wins": baseline_wins,
        "ties": ties,
        "inconclusive": inconclusive,
        "candidate_win_rate": candidate_wins / determinate if determinate else (0.5 if ties else None),
        "critical_baseline_wins": sum(
            1 for item in judgments if item["winner"] == "baseline" and item["critical_difference"]
        ),
    }


def finalise(
    workspace: Path,
    iteration_root: Path,
    benchmark: dict[str, Any],
    eval_set: dict[str, Any],
) -> dict[str, Any]:
    selection = decide(benchmark, eval_set, "candidate", "baseline")
    write_json(iteration_root / "selection.json", selection)
    (iteration_root / "report.html").write_text(render(benchmark, selection), encoding="utf-8")
    write_json(workspace / "benchmark.json", benchmark)
    write_json(workspace / "selection.json", selection)
    (workspace / "report.html").write_text(render(benchmark, selection), encoding="utf-8")
    return selection


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skill", required=True, type=Path)
    parser.add_argument("--eval-set", required=True, type=Path)
    parser.add_argument("--design", type=Path, help="ready skill-design.md to freeze and bind")
    parser.add_argument("--approval", type=Path, help="version-bound design approval")
    parser.add_argument("--traceability-validation", type=Path)
    parser.add_argument("--traceability", type=Path, help="traceability source mappings used for post-change assurance refresh")
    parser.add_argument("--conformance-review", type=Path)
    parser.add_argument("--assurance-reviewer-id", help="independent reviewer identity for automatic conformance refresh")
    parser.add_argument("--modifier-id", default="candidate-modifier")
    parser.add_argument("--assurance-provider", default="configured-runtime")
    parser.add_argument("--grader-calibration", type=Path)
    parser.add_argument("--professional-validation", type=Path)
    parser.add_argument("--holdout-status", type=Path)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--adapter-command", required=True, help="command prefix; job and response paths are appended")
    parser.add_argument("--baseline-mode", choices=("original", "no-skill"), default="original")
    parser.add_argument("--max-iterations", type=int)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--adapter-retries", type=int, default=0, help="retries for explicitly transient read-only adapter jobs; modify/execute are never retried")
    args = parser.parse_args()

    formal_skill = args.skill.resolve()
    eval_source = args.eval_set.resolve()
    workspace = args.workspace.resolve()
    if not (formal_skill / "SKILL.md").is_file():
        parser.error(f"not a Skill directory: {formal_skill}")
    if not eval_source.is_file():
        parser.error(f"eval set not found: {eval_source}")
    if path_is_within(workspace, formal_skill):
        parser.error("workspace must not be inside the formal Skill")
    if workspace.exists() and any(workspace.iterdir()):
        parser.error("workspace must be new or empty; optimization never overwrites prior evidence")
    if args.workers < 1 or args.timeout < 1 or args.adapter_retries < 0:
        parser.error("--workers and --timeout must be >= 1 and --adapter-retries must be >= 0")
    command = split_command(args.adapter_command)
    if not command:
        parser.error("--adapter-command must not be empty")

    try:
        eval_set = load_json(eval_source)
        errors, warnings, counts = validate(eval_set, eval_source, True)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    if errors:
        print(json.dumps({"valid": False, "errors": errors, "warnings": warnings, "case_counts": counts}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2

    policy = eval_set.get("policy", {}) if isinstance(eval_set.get("policy"), dict) else {}
    max_iterations = args.max_iterations or int(policy.get("max_iterations", 1))
    if max_iterations < 1:
        parser.error("--max-iterations must be >= 1")
    auto_refresh_assurance = bool(policy.get("auto_refresh_assurance", policy.get("require_traceability", False) and policy.get("require_conformance_review", False)))
    if auto_refresh_assurance and max_iterations > 1:
        missing_refresh = []
        if not args.traceability: missing_refresh.append("--traceability")
        if not args.assurance_reviewer_id: missing_refresh.append("--assurance-reviewer-id")
        if missing_refresh:
            print("automatic assurance refresh requires " + " and ".join(missing_refresh), file=sys.stderr)
            return 2
        if args.assurance_reviewer_id == args.modifier_id:
            print("assurance reviewer must differ from modifier identity", file=sys.stderr)
            return 2
    governance_inputs = {
        "grader_calibration": args.grader_calibration,
        "professional_validation": args.professional_validation,
        "holdout_governance": args.holdout_status,
    }
    for policy_key, report_key in (("require_grader_calibration", "grader_calibration"), ("require_professional_validation", "professional_validation"), ("require_holdout_governance", "holdout_governance")):
        path = governance_inputs[report_key]
        if policy.get(policy_key) and (path is None or not path.is_file()):
            print(f"eval policy requires --{report_key.replace('_', '-')}", file=sys.stderr)
            return 2
    design_report = approval_report = None
    assurance_ids: dict[str, str | None] = {"approval_id": None, "traceability_id": None, "conformance_review_id": None, "implementation_binding_id": None, "assurance_candidate_id": None}
    requires_design = any(bool(policy.get(key, False)) for key in ("require_design_binding", "require_design_review", "require_traceability", "require_conformance_review"))
    if not args.design and any((args.approval, args.traceability, args.traceability_validation, args.conformance_review)):
        print("approval, traceability, and conformance arguments require --design", file=sys.stderr)
        return 2
    if args.design:
        design_report = validate_design(args.design, True)
        if not design_report["valid"]:
            print(json.dumps(design_report, ensure_ascii=False, indent=2), file=sys.stderr)
            return 2
        try:
            ensure_binding_match(design_report, formal_skill)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        if not args.approval:
            print("--design requires --approval", file=sys.stderr)
            return 2
        approval_report = validate_approval(design_report, args.approval)
        if not approval_report["valid"]:
            print(json.dumps(approval_report, ensure_ascii=False, indent=2), file=sys.stderr)
            return 2
        assurance_ids["approval_id"] = approval_report["approval_id"]
        if bool(policy.get("require_design_review", False)) and not approval_report["approval"].get("review_artifact_id"):
            print("eval policy requires an approval backed by semantic design review", file=sys.stderr)
            return 2
    elif requires_design:
        print("eval assurance policy requires --design and --approval", file=sys.stderr)
        return 2

    if bool(policy.get("require_traceability", False)) and not args.traceability_validation:
        print("eval policy requires --traceability-validation", file=sys.stderr)
        return 2
    if bool(policy.get("require_conformance_review", False)) and not args.conformance_review:
        print("eval policy requires --conformance-review", file=sys.stderr)
        return 2
    formal_candidate_id = tree_hash(formal_skill)
    if args.traceability_validation:
        trace = load_json(args.traceability_validation)
        expected = {"status": "pass", "design_id": design_report["design_id"], "approval_id": approval_report["approval_id"], "candidate_id": formal_candidate_id, "eval_set_id": assurance_file_hash(eval_source)}
        if any(trace.get(key) != value for key, value in expected.items()):
            print("traceability validation is not a pass for these exact inputs", file=sys.stderr)
            return 2
        assurance_ids["traceability_id"] = assurance_file_hash(args.traceability_validation)
        assurance_ids["assurance_candidate_id"] = formal_candidate_id
        if args.traceability:
            source_validation = validate_traceability(args.traceability, args.design, args.approval, formal_skill, eval_source)
            if source_validation.get("status") != "pass" or source_validation.get("candidate_id") != formal_candidate_id:
                print("traceability source does not validate for the initial candidate", file=sys.stderr)
                return 2
    if args.conformance_review:
        conformance = validate_conformance(design_report, approval_report, formal_skill, args.conformance_review)
        if not conformance["valid"] or conformance["review"].get("eval_set_id") != assurance_file_hash(eval_source):
            print("conformance review is not a pass for these exact inputs", file=sys.stderr)
            return 2
        assurance_ids["conformance_review_id"] = conformance["conformance_id"]

    workspace.mkdir(parents=True, exist_ok=True)
    design_id = None
    if args.design:
        design_report = freeze_approved_design(
            args.design,
            args.approval,
            workspace / "design" / "skill-design.md",
            formal_skill,
        )
        design_id = str(design_report["design_id"])
        for source, name in ((args.traceability, "traceability.json"), (args.traceability_validation, "traceability-validation.json"), (args.conformance_review, "conformance-review.json")):
            if source:
                shutil.copy2(source, workspace / "design" / name)
    formal_hash = tree_hash(formal_skill)
    eval_root = workspace / "eval"
    eval_set_id = freeze_eval_bundle(eval_set, eval_source, eval_root)
    frozen_eval = eval_root / "eval-set.json"
    eval_set = load_json(frozen_eval)
    dataset_audit = audit_eval_set(eval_set, frozen_eval, threshold=float(policy.get("semantic_duplicate_threshold", 0.9)))
    governance: dict[str, Any] = {"dataset_audit": dataset_audit}
    governance_root = workspace / "governance"
    governance_root.mkdir(parents=True, exist_ok=True)
    write_json(governance_root / "dataset-audit.json", dataset_audit)
    if policy.get("require_dataset_audit") and dataset_audit.get("status") != "pass":
        print(json.dumps(dataset_audit, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    for key, source in governance_inputs.items():
        if source:
            value = load_json(source)
            governance[key] = value
            shutil.copy2(source, governance_root / f"{key.replace('_', '-')}.json")
    governance_checks = {
        "grader_calibration": lambda value: value.get("status") == "pass",
        "professional_validation": lambda value: value.get("status") == "pass" and value.get("real_domain_claim_eligible") is True,
        "holdout_governance": lambda value: value.get("status") in {"pass", "valid"} and value.get("holdout_eligible") is True,
    }
    for policy_key, report_key in (("require_grader_calibration", "grader_calibration"), ("require_professional_validation", "professional_validation"), ("require_holdout_governance", "holdout_governance")):
        if policy.get(policy_key) and not governance_checks[report_key](governance.get(report_key, {})):
            print(f"required governance report did not pass: {report_key}", file=sys.stderr)
            return 2
    if policy.get("require_holdout_governance"):
        authority = eval_set.get("holdout_authority", {}) if isinstance(eval_set.get("holdout_authority"), dict) else {}
        holdout_report = governance.get("holdout_governance", {})
        if not authority.get("vault_id") or authority.get("vault_id") != holdout_report.get("vault_id") or authority.get("ledger_entry_hash") != holdout_report.get("last_entry_hash"):
            print("holdout governance report is not bound to the assembled eval set checkout", file=sys.stderr)
            return 2
        if policy.get("require_hard_holdout_isolation") and holdout_report.get("hard_isolation") is not True:
            print("eval policy requires a system-isolated external holdout authority", file=sys.stderr)
            return 2
    baseline_path: Path | None = None
    baseline_id = "no-skill"
    if args.baseline_mode == "original":
        baseline_path = workspace / "baseline" / "original"
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(formal_skill, baseline_path, ignore=COPY_IGNORE)
        baseline_id = tree_hash(baseline_path)
    candidate = workspace / "candidates" / "iteration-1"
    candidate.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(formal_skill, candidate, ignore=COPY_IGNORE)
    initial_assurance_paths: dict[str, Path] = {"approval_id": workspace / "design" / "design-approval.json"}
    if args.traceability_validation:
        initial_assurance_paths["traceability_id"] = workspace / "design" / "traceability-validation.json"
    if args.conformance_review:
        initial_assurance_paths["conformance_review_id"] = workspace / "design" / "conformance-review.json"
    if design_report and args.traceability_validation and args.conformance_review:
        binding_path = workspace / "design" / "implementation-binding.json"
        bind(
            design_report,
            candidate,
            binding_path,
            args.assurance_provider,
            approval_path=workspace / "design" / "design-approval.json",
            stage="implementation",
            traceability_validation=workspace / "design" / "traceability-validation.json",
            conformance_review=workspace / "design" / "conformance-review.json",
        )
        assurance_ids["implementation_binding_id"] = assurance_file_hash(binding_path)
    assurance_by_candidate: dict[str, dict[str, str | None]] = {tree_hash(candidate): dict(assurance_ids)}
    assurance_paths_by_candidate: dict[str, dict[str, Path]] = {tree_hash(candidate): dict(initial_assurance_paths)}
    write_json(
        workspace / "metadata" / "iteration-1.json",
        {
            "schema_version": "2.0",
            "iteration": 1,
            "created_at": utc_now(),
            "candidate_path": str(candidate),
            "candidate_id_before_change": tree_hash(candidate),
            "candidate_source": str(formal_skill),
            "baseline_mode": args.baseline_mode,
            "baseline_id": baseline_id,
            "eval_set_id": eval_set_id,
            "design_id": design_id,
            **assurance_ids,
            "formal_skill_untouched": True,
        },
    )

    cases = eval_set["cases"]
    development_cases = [case for case in cases if case["split"] in {"train", "regression"}]
    holdout_cases = [case for case in cases if case["split"] == "holdout"]
    adapter = Adapter(command, args.timeout, workspace / "jobs", args.adapter_retries)
    initial_validation = validate_candidate(candidate, policy.get("candidate_checks", []))
    write_json(workspace / "metadata" / "iteration-1-validation.json", initial_validation)
    if not initial_validation["valid"]:
        print(json.dumps(initial_validation, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    history: dict[str, Any] = {
        "schema_version": "2.0",
        "started_at": utc_now(),
        "formal_skill_path": str(formal_skill),
        "formal_skill_content_id": formal_hash,
        "eval_set_id": eval_set_id,
        "design_id": design_id,
        **assurance_ids,
        "eval_validation_warnings": warnings,
        "baseline_mode": args.baseline_mode,
        "max_iterations": max_iterations,
        "adapter_command_sha256": hashlib.sha256("\0".join(command).encode("utf-8")).hexdigest(),
        "adapter_retries": args.adapter_retries,
        "governance": {key: value.get("status") or value.get("gate") for key, value in governance.items() if isinstance(value, dict)},
        "iterations": [],
        "status": "running",
        "current_assurance": {"candidate_id": tree_hash(candidate), **assurance_ids},
    }
    write_json(workspace / "history.json", history)

    try:
        for iteration in range(1, max_iterations + 1):
            if tree_hash(formal_skill) != formal_hash:
                raise RuntimeError("formal Skill changed during optimization")
            candidate_id = tree_hash(candidate)
            candidate_assurance = assurance_by_candidate.get(candidate_id, {"approval_id": assurance_ids.get("approval_id"), "traceability_id": None, "conformance_review_id": None, "implementation_binding_id": None, "assurance_candidate_id": None})
            baseline_assurance = assurance_by_candidate.get(baseline_id, {"approval_id": assurance_ids.get("approval_id"), "traceability_id": None, "conformance_review_id": None, "implementation_binding_id": None, "assurance_candidate_id": None})
            run_root = workspace / "runs" / f"iteration-{iteration}"
            iteration_root = workspace / "iterations" / f"iteration-{iteration}"
            configurations = [
                ("candidate", candidate, candidate_id, candidate_assurance),
                ("baseline", baseline_path, baseline_id, baseline_assurance),
            ]
            development_results = run_split(
                adapter,
                development_cases,
                configurations,
                policy,
                eval_set_id,
                design_id,
                eval_root,
                run_root,
                iteration,
                args.workers,
            )
            verify_frozen_inputs(
                formal_skill,
                formal_hash,
                candidate,
                candidate_id,
                baseline_path,
                baseline_id,
                eval_set,
                eval_root,
                eval_set_id,
                design_id,
                candidate_assurance,
                assurance_paths_by_candidate.get(candidate_id),
            )
            benchmark = write_benchmark(run_root, iteration_root, eval_set, baseline_path, candidate)
            benchmark["governance"] = governance
            write_json(iteration_root / "benchmark.json", benchmark)
            failed = [
                item
                for item in development_results
                if item["configuration"] == "candidate" and item["grading"]["status"] != "pass"
            ]
            record: dict[str, Any] = {
                "iteration": iteration,
                "design_id": design_id,
                "candidate_id": candidate_id,
                "baseline_id": baseline_id,
                "development_runs": len(development_results),
                "failed_candidate_development_runs": len(failed),
                "benchmark": str(iteration_root / "benchmark.json"),
            }
            history["iterations"].append(record)
            write_json(workspace / "history.json", history)

            critical_analysis = [
                item
                for item in benchmark.get("analysis", {}).get("findings", [])
                if item.get("severity") == "critical"
            ]
            if benchmark.get("analysis", {}).get("gate") == "fail" and critical_analysis:
                history["status"] = "benchmark_analysis_failed"
                record["stop_reason"] = "benchmark analyzer reported critical eval, budget, cost or overfit findings"
                break

            if failed:
                attributions = attribute_failures(adapter, failed, iteration)
                record["attributions"] = attributions
                if any(item.get("status") != "attributed" for item in attributions):
                    history["status"] = "attribution_inconclusive"
                    record["stop_reason"] = "at least one failure could not be attributed"
                    break
                primary_layer, selected_attributions, deferred_attributions = choose_primary_layer(attributions)
                record["primary_responsible_layer"] = primary_layer
                record["deferred_attributions"] = deferred_attributions
                if primary_layer in {"requirement", "scoring", "runtime"}:
                    history["status"] = f"{primary_layer}_revision_required"
                    record["stop_reason"] = {
                        "requirement": "requirement failure must return to requirement definition, not candidate editing",
                        "scoring": "scoring changes invalidate the frozen comparison; create a revised eval set",
                        "runtime": "runtime failure must be corrected in the execution environment, not hidden in Skill text",
                    }[primary_layer]
                    break
                if iteration >= max_iterations:
                    history["status"] = "max_iterations_reached"
                    record["stop_reason"] = "candidate still fails train/regression"
                    break
                next_candidate = workspace / "candidates" / f"iteration-{iteration + 1}"
                manifest = modify_candidate(
                    adapter,
                    candidate,
                    next_candidate,
                    selected_attributions,
                    development_cases,
                    iteration,
                    formal_hash,
                    formal_skill,
                    workspace / "metadata",
                    primary_layer,
                    design_id,
                )
                if baseline_path is not None and tree_hash(baseline_path) != baseline_id:
                    raise RuntimeError("baseline changed during modify")
                if eval_bundle_hash(eval_set, eval_root) != eval_set_id:
                    raise RuntimeError("frozen eval set or case inputs changed during modify")
                record["change_manifest"] = str(workspace / "metadata" / f"iteration-{iteration + 1}.json")
                record["modifier_status"] = manifest["modifier_status"]
                if manifest["modifier_status"] != "modified":
                    history["status"] = "modifier_stopped"
                    record["stop_reason"] = f"modifier returned {manifest['modifier_status']}"
                    break
                validation = validate_candidate(next_candidate, policy.get("candidate_checks", []))
                if any(path.startswith("scripts/") for path in manifest["changed_files"]) and not policy.get("candidate_checks"):
                    validation["valid"] = False
                    validation["errors"].append(
                        "candidate changed scripts but policy.candidate_checks is empty; real script execution is required"
                    )
                validation_path = workspace / "metadata" / f"iteration-{iteration + 1}-validation.json"
                write_json(validation_path, validation)
                record["candidate_validation"] = str(validation_path)
                if not validation["valid"]:
                    history["status"] = "candidate_validation_failed"
                    record["stop_reason"] = "modified candidate failed structural or executable checks"
                    break
                preflight = candidate_change_preflight(benchmark, eval_set, candidate, next_candidate)
                preflight_path = workspace / "metadata" / f"iteration-{iteration + 1}-preflight-analysis.json"
                write_json(preflight_path, preflight)
                record["candidate_preflight_analysis"] = str(preflight_path)
                if preflight["gate"] != "pass":
                    history["status"] = "candidate_preflight_failed"
                    record["stop_reason"] = "candidate has unresolved overfit or critical analyzer findings before re-evaluation"
                    break
                next_candidate_id = tree_hash(next_candidate)
                if design_report and auto_refresh_assurance:
                    refresh_root = workspace / "assurance" / f"iteration-{iteration + 1}"
                    refresh_report = refresh_assurance_evidence(
                        design_path=workspace / "design" / "skill-design.md",
                        approval_path=workspace / "design" / "design-approval.json",
                        traceability_source=workspace / "design" / "traceability.json",
                        candidate=next_candidate,
                        eval_path=frozen_eval,
                        output_dir=refresh_root,
                        adapter=adapter,
                        reviewer_id=str(args.assurance_reviewer_id),
                        modifier_id=args.modifier_id,
                        provider=args.assurance_provider,
                        iteration=iteration + 1,
                        changed_files=list(manifest.get("changed_files", [])),
                    )
                    write_json(refresh_root / "refresh-report.json", refresh_report)
                    record["assurance_refresh"] = str(refresh_root / "refresh-report.json")
                    if refresh_report.get("status") != "pass":
                        history["status"] = "assurance_refresh_failed"
                        record["stop_reason"] = "changed candidate did not obtain fresh traceability and independent conformance evidence"
                        break
                    next_assurance = {
                        "approval_id": refresh_report["approval_id"],
                        "traceability_id": refresh_report["traceability_id"],
                        "conformance_review_id": refresh_report["conformance_review_id"],
                        "implementation_binding_id": refresh_report["implementation_binding_id"],
                        "assurance_candidate_id": next_candidate_id,
                    }
                    assurance_by_candidate[next_candidate_id] = next_assurance
                    assurance_paths_by_candidate[next_candidate_id] = {
                        "approval_id": workspace / "design" / "design-approval.json",
                        "traceability_id": Path(refresh_report["paths"]["traceability_validation"]),
                        "conformance_review_id": Path(refresh_report["paths"]["conformance_review"]),
                    }
                    history["current_assurance"] = {"candidate_id": next_candidate_id, **next_assurance}
                else:
                    next_assurance = {
                        "approval_id": assurance_ids.get("approval_id"),
                        "traceability_id": None,
                        "conformance_review_id": None,
                        "implementation_binding_id": None,
                        "assurance_candidate_id": None,
                    }
                    assurance_by_candidate[next_candidate_id] = next_assurance
                previous_candidate = candidate
                candidate = next_candidate
                baseline_path = previous_candidate
                baseline_id = candidate_id
                write_json(workspace / "history.json", history)
                continue

            if not holdout_cases:
                history["status"] = "holdout_missing"
                record["stop_reason"] = "development cases pass, but no holdout exists"
                break
            holdout_results = run_split(
                adapter,
                holdout_cases,
                configurations,
                policy,
                eval_set_id,
                design_id,
                eval_root,
                run_root,
                iteration,
                args.workers,
            )
            verify_frozen_inputs(
                formal_skill,
                formal_hash,
                candidate,
                candidate_id,
                baseline_path,
                baseline_id,
                eval_set,
                eval_root,
                eval_set_id,
                design_id,
                candidate_assurance,
                assurance_paths_by_candidate.get(candidate_id),
            )
            blind_comparison = None
            if bool(policy.get("run_blind_comparison", False)) or bool(policy.get("require_blind_comparison", False)):
                blind_comparison = run_blind_comparisons(adapter, holdout_results, iteration, workspace)
            benchmark = write_benchmark(
                run_root,
                iteration_root,
                eval_set,
                baseline_path,
                candidate,
                blind_comparison,
            )
            benchmark["governance"] = governance
            write_json(iteration_root / "benchmark.json", benchmark)
            selection = finalise(workspace, iteration_root, benchmark, eval_set)
            record["holdout_runs"] = len(holdout_results)
            record["selection"] = str(iteration_root / "selection.json")
            record["decision"] = selection["decision"]
            history["status"] = selection["decision"]
            if selection["decision"] != "candidate_is_best":
                record["stop_reason"] = "holdout is never exposed to the modifier; add new training evidence before another optimization"
            break
        else:
            history["status"] = "max_iterations_reached"
    except Exception as exc:
        history["status"] = "error"
        history["error"] = str(exc)
        if isinstance(exc, AdapterCallError):
            history["failure_class"] = exc.failure_class
            history["retryable"] = exc.retryable
        history["finished_at"] = utc_now()
        write_json(workspace / "history.json", history)
        print(str(exc), file=sys.stderr)
        return 1

    history["finished_at"] = utc_now()
    history["formal_skill_untouched"] = tree_hash(formal_skill) == formal_hash
    history["recommended_candidate_path"] = str(candidate) if history["status"] == "candidate_is_best" else None
    write_json(workspace / "history.json", history)
    print(json.dumps({"status": history["status"], "workspace": str(workspace), "candidate": history["recommended_candidate_path"]}, ensure_ascii=False, indent=2))
    return 0 if history["status"] == "candidate_is_best" else 3


if __name__ == "__main__":
    sys.exit(main())
