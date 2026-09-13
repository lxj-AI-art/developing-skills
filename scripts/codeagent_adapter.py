#!/usr/bin/env python3
"""Bridge optimization jobs to a configurable CodeAgent CLI and built-in role engine."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


IGNORED_PARTS = {".git", "__pycache__", ".pytest_cache"}
LAYERS = {"requirement", "method", "knowledge", "retrieval", "tool", "runtime", "instruction", "scoring"}


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"root must be an object: {path}")
    return value


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def manifest(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): file_digest(path)
        for path in sorted(root.rglob("*"))
        if path.is_file() and not set(path.relative_to(root).parts) & IGNORED_PARTS
    }


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


def json_from_text(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        value = json.loads(stripped)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    candidates: list[dict[str, Any]] = []
    for index, character in enumerate(text):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            candidates.append(value)
    if not candidates:
        raise ValueError("CodeAgent output does not contain a JSON object")
    return candidates[-1]


def render_command(command: list[str], values: dict[str, str]) -> tuple[list[str], bool]:
    rendered: list[str] = []
    uses_prompt = False
    for argument in command:
        if "{prompt}" in argument or "{prompt_file}" in argument:
            uses_prompt = True
        value = argument
        for key, replacement in values.items():
            value = value.replace("{" + key + "}", replacement)
        rendered.append(value)
    return rendered, uses_prompt


class CodeAgent:
    def __init__(self, config: dict[str, Any], job_path: Path) -> None:
        self.config = config
        self.job_path = job_path
        self.commands = config.get("commands", {})
        if not isinstance(self.commands, dict):
            raise ValueError("config.commands must be an object")

    def command(self, role: str) -> list[str] | None:
        value = self.commands.get(role)
        if value is None and role in {"grade", "attribute", "compare", "review_design", "review_conformance"}:
            value = self.commands.get("role")
        if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
            return None
        return value

    def run(
        self,
        role: str,
        prompt: str,
        cwd: Path,
        output_dir: Path,
        skill_path: Path | None,
        expect_json: bool,
    ) -> tuple[str | dict[str, Any], dict[str, Any], Path]:
        command = self.command(role)
        if command is None:
            raise RuntimeError(f"CodeAgent command is not configured for role: {role}")
        adapter_dir = output_dir / ".adapter"
        adapter_dir.mkdir(parents=True, exist_ok=True)
        prompt_file = adapter_dir / f"{role}-prompt.md"
        prompt_file.write_text(prompt, encoding="utf-8")
        values = {
            "cwd": str(cwd),
            "output_dir": str(output_dir),
            "skill_path": str(skill_path) if skill_path else "",
            "prompt_file": str(prompt_file),
            "prompt": prompt,
            "job_file": str(self.job_path),
        }
        argv, uses_prompt = render_command(command, values)
        configured_env = self.config.get("environment", {})
        if not isinstance(configured_env, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in configured_env.items()
        ):
            raise ValueError("config.environment must be a string map")
        environment = os.environ.copy()
        environment.update(configured_env)
        timeout = self.config.get("timeout_seconds", 600)
        if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout < 1:
            raise ValueError("config.timeout_seconds must be an integer >= 1")
        started = time.monotonic()
        try:
            completed = subprocess.run(
                argv,
                cwd=cwd,
                input=None if uses_prompt else prompt,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=environment,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"CodeAgent {role} timed out after {timeout}s") from exc
        elapsed = time.monotonic() - started
        transcript = adapter_dir / f"{role}-transcript.md"
        transcript.write_text(
            f"# CodeAgent {role}\n\nExit code: {completed.returncode}\nElapsed seconds: {elapsed:.6f}\n\n"
            f"## stdout\n\n{completed.stdout}\n\n## stderr\n\n{completed.stderr}\n",
            encoding="utf-8",
        )
        if completed.returncode != 0:
            raise RuntimeError(f"CodeAgent {role} failed with exit code {completed.returncode}; see {transcript}")
        metrics: dict[str, Any] = {"duration_seconds": elapsed}
        if expect_json:
            value = json_from_text(completed.stdout)
            supplied = value.pop("metrics", None)
            if isinstance(supplied, dict):
                metrics.update(supplied)
            return value, metrics, transcript
        return completed.stdout.strip(), metrics, transcript


def artifact_text(execution: dict[str, Any], limit: int = 200_000) -> str:
    chunks: list[str] = []
    for item in execution.get("artifacts", []):
        path = Path(item)
        if not path.is_file():
            continue
        try:
            chunks.append(f"## {path.name}\n{path.read_text(encoding='utf-8')}")
        except (OSError, UnicodeDecodeError):
            chunks.append(f"## {path.name}\n[binary or unreadable artifact]")
        if sum(len(chunk) for chunk in chunks) >= limit:
            break
    return "\n\n".join(chunks)[:limit]


def json_path(value: Any, path: str) -> tuple[bool, Any]:
    current = value
    for part in path.split(".") if path else []:
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return False, None
    return True, current


def choose_artifact(execution: dict[str, Any], checker: dict[str, Any]) -> Path | None:
    artifacts = [Path(item) for item in execution.get("artifacts", [])]
    named = checker.get("artifact")
    if isinstance(named, str):
        for path in artifacts:
            if path.name == named or path.as_posix().endswith(named):
                return path
        return None
    return artifacts[0] if artifacts else None


def deterministic_check(expectation: dict[str, Any], execution: dict[str, Any]) -> dict[str, Any]:
    checker = expectation.get("checker")
    result = {
        "id": expectation.get("id"),
        "text": expectation.get("text"),
        "severity": expectation.get("severity"),
        "weight": expectation.get("weight"),
    }
    if not isinstance(checker, dict):
        return {**result, "status": "invalid_case", "evidence": "deterministic expectation has no checker object"}
    kind = checker.get("type")
    if kind == "execution_status":
        expected = checker.get("equals", "completed")
        observed = execution.get("status")
        passed = observed == expected
        return {**result, "status": "pass" if passed else "fail", "evidence": f"execution status expected={expected!r}, observed={observed!r}"}
    artifact = choose_artifact(execution, checker)
    if artifact is None or not artifact.is_file():
        return {**result, "status": "fail", "evidence": f"required artifact not found: {checker.get('artifact', '<first>')}"}
    if kind == "artifact_exists":
        return {**result, "status": "pass", "evidence": f"artifact exists: {artifact}"}
    try:
        text = artifact.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return {**result, "status": "inconclusive", "evidence": f"cannot read artifact as UTF-8: {exc}"}
    if kind == "artifact_text_contains":
        expected = checker.get("text")
        if not isinstance(expected, str):
            return {**result, "status": "invalid_case", "evidence": "checker.text must be a string"}
        passed = expected in text
        return {**result, "status": "pass" if passed else "fail", "evidence": f"artifact contains expected text={passed}"}
    if kind == "artifact_text_equals":
        expected = checker.get("text")
        if not isinstance(expected, str):
            return {**result, "status": "invalid_case", "evidence": "checker.text must be a string"}
        strip = checker.get("strip", True)
        observed = text.strip() if strip else text
        comparison = expected.strip() if strip else expected
        passed = observed == comparison
        return {**result, "status": "pass" if passed else "fail", "evidence": f"artifact text equals expected={passed}"}
    if kind == "artifact_text_not_contains":
        forbidden = checker.get("text")
        if not isinstance(forbidden, str):
            return {**result, "status": "invalid_case", "evidence": "checker.text must be a string"}
        passed = forbidden not in text
        return {**result, "status": "pass" if passed else "fail", "evidence": f"artifact excludes forbidden text={passed}"}
    if kind == "artifact_regex":
        pattern = checker.get("pattern")
        if not isinstance(pattern, str):
            return {**result, "status": "invalid_case", "evidence": "checker.pattern must be a string"}
        try:
            passed = re.search(pattern, text, re.MULTILINE) is not None
        except re.error as exc:
            return {**result, "status": "invalid_case", "evidence": f"invalid regex: {exc}"}
        return {**result, "status": "pass" if passed else "fail", "evidence": f"regex matched={passed}"}
    if kind == "artifact_json_path_equals":
        path = checker.get("path")
        if not isinstance(path, str):
            return {**result, "status": "invalid_case", "evidence": "checker.path must be a string"}
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            return {**result, "status": "fail", "evidence": f"artifact is not valid JSON: {exc}"}
        found, observed = json_path(value, path)
        expected = checker.get("equals")
        passed = found and observed == expected
        return {**result, "status": "pass" if passed else "fail", "evidence": f"path={path}; found={found}; expected={expected!r}; observed={observed!r}"}
    return {**result, "status": "invalid_case", "evidence": f"unsupported checker type: {kind!r}"}


def execute_job(agent: CodeAgent, config: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    output_dir = Path(job["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    skill_path = Path(job["skill_path"]) if job.get("skill_path") else None
    mode = job["case"].get("mode", "execution")
    if mode == "trigger" and agent.command("trigger") is None:
        return blocked(job, config, "trigger command is not configured")
    role = "trigger" if mode == "trigger" else "execute"
    skill_note = "No Skill is available for this baseline." if skill_path is None else f"Use the candidate Skill located at {skill_path}."
    if mode == "trigger":
        skill_note = "The candidate is available through the runtime discovery configuration. Do not explicitly name or preload it in the user request."
    prompt = (
        "You are the task executor in a blind Skill evaluation. Complete the natural user task; do not grade yourself, infer hidden labels, or inspect other runs.\n\n"
        f"{skill_note}\nOutput directory: {output_dir}\nInput files: {json.dumps(job['case'].get('files', []), ensure_ascii=False)}\n\n"
        f"User request:\n{job['case']['prompt']}\n"
    )
    expect_json = bool(config.get("execute_output_json", False))
    try:
        output, metrics, command_transcript = agent.run(role, prompt, output_dir, output_dir, skill_path, expect_json)
    except RuntimeError as exc:
        return blocked(job, config, str(exc))
    final_path = output_dir / "outputs" / "final.md"
    final_path.parent.mkdir(parents=True, exist_ok=True)
    if expect_json:
        if not isinstance(output, dict) or not isinstance(output.get("final"), str):
            return blocked(job, config, "structured execute output needs a string final field")
        final_text = output["final"]
    else:
        final_text = str(output)
    final_path.write_text(final_text + "\n", encoding="utf-8")
    transcript = output_dir / "transcript.md"
    transcript.write_text(command_transcript.read_text(encoding="utf-8"), encoding="utf-8")
    artifacts = [str(path) for path in sorted((output_dir / "outputs").rglob("*")) if path.is_file()]
    response = identity(config, job)
    response.update({"status": "completed", "transcript_path": str(transcript), "artifacts": artifacts, "metrics": metrics})
    if mode == "trigger":
        probe = agent.command("activation_probe")
        if probe is None:
            return blocked(job, config, "activation_probe command is required for trigger evaluation")
        probe_prompt = "Return runtime activation observation as JSON: {\"observed\": boolean, \"evidence\": string}. Do not infer from answer quality."
        try:
            observation, probe_metrics, _ = agent.run("activation_probe", probe_prompt, output_dir, output_dir, skill_path, True)
        except (RuntimeError, ValueError) as exc:
            return blocked(job, config, f"activation probe failed: {exc}")
        if not isinstance(observation.get("observed"), bool) or not isinstance(observation.get("evidence"), str):
            return blocked(job, config, "activation probe returned invalid observation")
        response["activation"] = {"observed": observation["observed"], "evidence": observation["evidence"]}
        response["metrics"]["duration_seconds"] += probe_metrics.get("duration_seconds", 0)
    return response


def identity(config: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_id": job["job_id"],
        "model_id": str(config.get("model_id", "unreported-codeagent")),
        "environment_id": str(config.get("environment_id", "unreported-runtime")),
        "execution_independence": config.get("execution_independence", "unverified"),
    }


def blocked(job: dict[str, Any], config: dict[str, Any], error: str) -> dict[str, Any]:
    return {**identity(config, job), "status": "blocked", "artifacts": [], "metrics": {"errors": 1}, "error": error}


def grade_job(agent: CodeAgent, job: dict[str, Any]) -> dict[str, Any]:
    case = job["case"]
    expectations = case.get("expectations", [])
    results: list[dict[str, Any]] = []
    semantic: list[dict[str, Any]] = []
    if case.get("mode", "execution") == "trigger":
        tags = set(case.get("tags", []))
        expected_activation = "should-trigger" in tags
        activation = job.get("execution", {}).get("activation", {})
        observed_activation = activation.get("observed") if isinstance(activation, dict) else None
        activation_passed = isinstance(observed_activation, bool) and observed_activation == expected_activation
        results.append(
            {
                "id": "runtime-skill-activation",
                "text": "runtime activation observation matches the labeled trigger direction",
                "severity": "critical",
                "weight": 1.0,
                "status": "pass" if activation_passed else "fail",
                "evidence": (
                    f"expected activation={expected_activation}; observed={observed_activation}; "
                    f"runtime evidence={activation.get('evidence', '') if isinstance(activation, dict) else ''}"
                ),
            }
        )
    for expectation in expectations:
        if expectation.get("kind") == "deterministic":
            results.append(deterministic_check(expectation, job["execution"]))
        else:
            semantic.append(expectation)
    if semantic or not expectations:
        prompt = (
            "You are an independent evidence grader. Inspect the actual execution artifacts. Deterministic results, if supplied, are authoritative and must not be overridden. "
            "For each requested semantic/domain expectation return observable evidence, not stylistic impressions. If the label or evidence is insufficient use inconclusive or invalid_case.\n\n"
            f"Case and oracle:\n{json.dumps(case, ensure_ascii=False, indent=2)}\n\n"
            f"Execution:\n{json.dumps(job['execution'], ensure_ascii=False, indent=2)}\n\n"
            f"Artifact contents:\n{artifact_text(job['execution'])}\n\n"
            f"Expectations to grade:\n{json.dumps(semantic, ensure_ascii=False, indent=2)}\n\n"
            "Return JSON only: {\"expectations\":[{\"id\":str,\"status\":\"pass|fail|inconclusive|invalid_case\",\"evidence\":str}],\"claims\":[],\"eval_feedback\":[]}."
        )
        output_dir = Path(job["output_dir"])
        value, _, _ = agent.run("grade", prompt, output_dir, output_dir, None, True)
        supplied = value.get("expectations", [])
        if not isinstance(supplied, list):
            raise RuntimeError("grader response expectations must be an array")
        by_id = {str(item.get("id")): item for item in supplied if isinstance(item, dict)}
        requested = semantic if semantic else [{"id": "oracle", "text": "satisfy the complete oracle", "severity": "critical", "weight": 1.0}]
        for expectation in requested:
            item = by_id.get(str(expectation["id"]))
            if not item:
                results.append({**expectation, "status": "inconclusive", "evidence": "grader omitted this expectation"})
                continue
            item_status = item.get("status")
            if item_status not in {"pass", "fail", "inconclusive", "invalid_case"}:
                item_status = "inconclusive"
            results.append(
                {
                    "id": expectation["id"],
                    "text": expectation.get("text", "complete oracle"),
                    "severity": expectation.get("severity", "critical"),
                    "weight": expectation.get("weight", 1.0),
                    "status": item_status,
                    "evidence": str(item.get("evidence", "")),
                }
            )
    invalid = any(item.get("status") == "invalid_case" for item in results)
    unresolved = any(item.get("status") == "inconclusive" for item in results)
    failed = any(item.get("status") == "fail" for item in results)
    if invalid:
        status, score = "invalid_case", None
    elif unresolved:
        status, score = "inconclusive", None
    else:
        status = "fail" if failed else "pass"
        total = sum(float(item.get("weight", 1.0)) for item in results)
        score = sum(float(item.get("weight", 1.0)) for item in results if item.get("status") == "pass") / total if total else 1.0
    critical = [str(item.get("id")) for item in results if item.get("status") == "fail" and item.get("severity") == "critical"]
    return {
        "job_id": job["job_id"],
        "status": status,
        "score": score,
        "expectations": results,
        "critical_failures": critical,
        "claims": [],
        "eval_feedback": [],
        "grading_layers": ["deterministic"] + (["semantic_or_domain"] if semantic or not expectations else []),
    }


def resolve_check_root(job: dict[str, Any], check: dict[str, Any]) -> Path | None:
    root = check.get("root")
    if root == "skill":
        return Path(job["skill_path"])
    if root == "output":
        return Path(job["output_dir"])
    if root == "input":
        files = job.get("case", {}).get("files", [])
        return Path(files[0]).parent if files else None
    return None


def run_discriminating_check(job: dict[str, Any], check: dict[str, Any]) -> dict[str, Any]:
    root = resolve_check_root(job, check)
    relative = check.get("path", "")
    if root is None or not isinstance(relative, str) or Path(relative).is_absolute():
        return {"status": "invalid", "evidence": "check root/path is invalid"}
    target = (root / relative).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError:
        return {"status": "invalid", "evidence": "check path escapes its root"}
    kind = check.get("type")
    if kind == "file_exists":
        return {"status": "completed", "observed": target.is_file(), "evidence": f"file exists={target.is_file()}: {target}"}
    if not target.is_file():
        return {"status": "completed", "observed": False, "evidence": f"file missing: {target}"}
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return {"status": "invalid", "evidence": f"cannot read target: {exc}"}
    if kind == "text_contains":
        pattern = check.get("text")
        if not isinstance(pattern, str):
            return {"status": "invalid", "evidence": "text_contains needs text"}
        return {"status": "completed", "observed": pattern in text, "evidence": f"text present={pattern in text}"}
    if kind == "text_regex":
        pattern = check.get("pattern")
        if not isinstance(pattern, str) or len(pattern) > 512:
            return {"status": "invalid", "evidence": "text_regex needs a pattern of at most 512 characters"}
        if len(text) > 2_000_000:
            return {"status": "invalid", "evidence": "text_regex target exceeds the 2 MB safety limit"}
        try:
            observed = re.search(pattern, text, flags=re.MULTILINE) is not None
        except re.error as exc:
            return {"status": "invalid", "evidence": f"invalid regex: {exc}"}
        return {"status": "completed", "observed": observed, "evidence": f"regex matched={observed}"}
    if kind in {"json_path_exists", "json_path_equals"}:
        path = check.get("json_path")
        if not isinstance(path, str):
            return {"status": "invalid", "evidence": f"{kind} needs json_path"}
        try:
            found, value = json_path(json.loads(text), path)
        except json.JSONDecodeError as exc:
            return {"status": "invalid", "evidence": f"invalid JSON: {exc}"}
        if kind == "json_path_equals":
            if "equals" not in check:
                return {"status": "invalid", "evidence": "json_path_equals needs equals"}
            observed = found and value == check["equals"]
            return {"status": "completed", "observed": observed, "value": value, "evidence": f"JSON value equals expected={observed}"}
        return {"status": "completed", "observed": found, "value": value, "evidence": f"JSON path found={found}"}
    if kind == "python_ast_symbol_exists":
        symbol = check.get("symbol")
        symbol_kind = check.get("symbol_kind", "any")
        if not isinstance(symbol, str) or not symbol or symbol_kind not in {"any", "function", "class"}:
            return {"status": "invalid", "evidence": "python_ast_symbol_exists needs symbol and valid symbol_kind"}
        try:
            parsed = ast.parse(text)
        except SyntaxError as exc:
            return {"status": "invalid", "evidence": f"invalid Python syntax: {exc}"}
        kinds = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        if symbol_kind == "function":
            kinds = (ast.FunctionDef, ast.AsyncFunctionDef)
        elif symbol_kind == "class":
            kinds = (ast.ClassDef,)
        observed = any(isinstance(node, kinds) and getattr(node, "name", None) == symbol for node in ast.walk(parsed))
        return {"status": "completed", "observed": observed, "evidence": f"Python {symbol_kind} symbol exists={observed}"}
    return {"status": "invalid", "evidence": f"unsupported discriminating check type: {kind}"}


def attribution_prompt(job: dict[str, Any], check_result: dict[str, Any] | None = None) -> str:
    return (
        "You are a failure attributor for Agent Skills. Choose exactly one primary layer only when evidence distinguishes it: "
        "requirement (the requested behavior is undefined/conflicting), method (decision procedure is wrong), knowledge (facts/rules missing), "
        "retrieval (available knowledge was not found/routed), tool (Skill-owned tool or script failed), runtime (external model/environment/permission failure), "
        "instruction (correct method exists but instructions fail to elicit it), scoring (label or grader is wrong). Do not repair files. "
        "If unresolved, return one safe structured discriminating_check using only file_exists, text_contains, text_regex, json_path_exists, "
        "json_path_equals, or python_ast_symbol_exists and root skill/output/input.\n\n"
        f"Case:\n{json.dumps(job['case'], ensure_ascii=False, indent=2)}\n\n"
        f"Grading:\n{json.dumps(job['grading'], ensure_ascii=False, indent=2)}\n\n"
        f"Execution:\n{json.dumps(job['execution'], ensure_ascii=False, indent=2)}\n\n"
        f"Previous check result:\n{json.dumps(check_result, ensure_ascii=False, indent=2) if check_result else 'none'}\n\n"
        "Return JSON only. Attributed: {\"status\":\"attributed\",\"responsible_layer\":layer,\"confidence\":0..1,\"observed_failure\":str,"
        "\"evidence\":[str],\"alternatives\":[layer],\"discriminating_check\":str,\"recommended_change_target\":str}. "
        "Unresolved: {\"status\":\"inconclusive\",\"responsible_layer\":null,\"confidence\":0..1,\"evidence\":[str],\"alternatives\":[layer],"
        "\"discriminating_check\":{\"type\":...,\"root\":...,\"path\":...,\"text\":...,\"pattern\":...,\"json_path\":...,\"equals\":...,\"symbol\":...,\"symbol_kind\":...}}."
    )


def attribute_job(agent: CodeAgent, job: dict[str, Any]) -> dict[str, Any]:
    output_dir = Path(job["output_dir"])
    value, _, _ = agent.run("attribute", attribution_prompt(job), output_dir, output_dir, Path(job["skill_path"]), True)
    if value.get("status") == "inconclusive" and isinstance(value.get("discriminating_check"), dict):
        check = value["discriminating_check"]
        check_result = run_discriminating_check(job, check)
        value, _, _ = agent.run("attribute", attribution_prompt(job, check_result), output_dir, output_dir, Path(job["skill_path"]), True)
        value["executed_discriminating_check"] = {"spec": check, "result": check_result}
    if isinstance(value.get("discriminating_check"), dict):
        value["discriminating_check"] = json.dumps(value["discriminating_check"], ensure_ascii=False)
    value["job_id"] = job["job_id"]
    return value


def allowed_paths(layer: str) -> tuple[str, ...]:
    return {
        "method": ("SKILL.md", "references/"),
        "knowledge": ("references/",),
        "retrieval": ("SKILL.md", "references/", "scripts/"),
        "tool": ("scripts/",),
        "instruction": ("SKILL.md", "references/"),
    }.get(layer, ())


def modify_job(agent: CodeAgent, job: dict[str, Any]) -> dict[str, Any]:
    layers = {item.get("responsible_layer") for item in job.get("attributions", [])}
    if len(layers) != 1:
        return {"job_id": job["job_id"], "status": "blocked", "changed_files": [], "error": "modify job must contain one responsibility layer"}
    layer = str(next(iter(layers)))
    allowed = allowed_paths(layer)
    if not allowed:
        return {"job_id": job["job_id"], "status": "blocked", "changed_files": [], "error": f"layer {layer} is not editable in the candidate"}
    target = Path(job["target_skill_path"])
    before = manifest(target)
    before_skill_text = (target / "SKILL.md").read_text(encoding="utf-8")
    trigger_description_only = bool(job.get("constraints", {}).get("trigger_description_only", False))
    prompt = (
        "You are the Skill candidate modifier. Edit only the candidate working directory. Make the smallest general change justified by the supplied closed attributions. "
        "Do not add case IDs, copy expected answers, inspect holdout data, or change unrelated layers. Check similar defects and an opposite case. "
        f"Primary layer: {layer}. Allowed paths: {json.dumps(allowed)}. "
        + ("This is trigger optimization: change only the single-line YAML frontmatter description, not the body. " if trigger_description_only else "")
        + "\n\n"
        f"Attributions:\n{json.dumps(job['attributions'], ensure_ascii=False, indent=2)}\n\n"
        f"Visible train/regression cases:\n{json.dumps(job['cases'], ensure_ascii=False, indent=2)}\n\n"
        "After editing, return JSON only: {\"status\":\"modified|no_change|blocked\",\"change_summary\":str,\"similar_defect_check\":str,"
        "\"opposite_case\":str,\"validation\":[str]}. Do not claim a check you did not run."
    )
    try:
        runtime_dir = agent.job_path.parent / "modify-runtime"
        value, _, _ = agent.run("modify", prompt, target, runtime_dir, target, True)
    except RuntimeError as exc:
        return {"job_id": job["job_id"], "status": "blocked", "changed_files": [], "error": str(exc)}
    after = manifest(target)
    changed = sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))
    disallowed = [path for path in changed if not any(path == prefix or path.startswith(prefix) for prefix in allowed)]
    if disallowed:
        raise RuntimeError(f"modifier changed paths outside layer {layer}: {disallowed}")
    if trigger_description_only:
        after_skill_text = (target / "SKILL.md").read_text(encoding="utf-8")
        if changed != ["SKILL.md"] or without_frontmatter_description(before_skill_text) != without_frontmatter_description(after_skill_text):
            raise RuntimeError("trigger optimization may change only the SKILL.md frontmatter description")
    value["job_id"] = job["job_id"]
    value["changed_files"] = changed
    return value


def compare_job(agent: CodeAgent, job: dict[str, Any]) -> dict[str, Any]:
    prompt = (
        "You are a blind comparator. The two variants are randomly labeled A and B. Judge only their observable artifacts against the case and oracle. "
        "Do not infer configuration identity. Prefer tie when no material evidence distinguishes them.\n\n"
        f"Case and oracle:\n{json.dumps(job['case'], ensure_ascii=False, indent=2)}\n\n"
        f"Variant A:\n{json.dumps(job['variants']['A'], ensure_ascii=False, indent=2)}\n\n"
        f"Variant B:\n{json.dumps(job['variants']['B'], ensure_ascii=False, indent=2)}\n\n"
        "Score each variant independently before choosing a winner. Content measures correctness, evidence and task completion; structure measures clarity, usability and required format. "
        "Return JSON only: {\"winner\":\"A|B|tie|inconclusive\",\"confidence\":0..1,\"evidence\":[str],\"critical_difference\":boolean,"
        "\"rubric\":{\"A\":{\"content\":0..1,\"structure\":0..1,\"strengths\":[str],\"weaknesses\":[str]},"
        "\"B\":{\"content\":0..1,\"structure\":0..1,\"strengths\":[str],\"weaknesses\":[str]}},"
        "\"expectation_results\":[{\"expectation\":str,\"A\":str,\"B\":str}]}."
    )
    output_dir = Path(job["output_dir"])
    value, _, _ = agent.run("compare", prompt, output_dir, output_dir, None, True)
    value["job_id"] = job["job_id"]
    return value


def review_job(agent: CodeAgent, job: dict[str, Any]) -> dict[str, Any]:
    role = str(job["job_type"])
    sources: dict[str, str] = {}
    for key in ("design_path", "traceability_validation_path", "eval_set_path"):
        if job.get(key):
            sources[key] = Path(job[key]).read_text(encoding="utf-8")
    if job.get("candidate_path"):
        candidate = Path(job["candidate_path"])
        sources["candidate_files"] = json.dumps(manifest(candidate), ensure_ascii=False, indent=2)
        sources["candidate_SKILL.md"] = (candidate / "SKILL.md").read_text(encoding="utf-8")
    prompt = (
        "Act as an independent Skill assurance reviewer. Inspect substance, not keyword presence. "
        "For design review, decide whether requirements, professional method, capability allocation, evaluation strategy, and safety boundaries are complete, coherent, testable, and supported. "
        "For conformance review, decide whether the actual candidate and validated traceability implement the exact approved design without unsupported claims. "
        "Fail vague placeholders, circular acceptance criteria, missing negative cases, or claimed external capabilities without evidence.\n\n"
        f"Controller job:\n{json.dumps(job, ensure_ascii=False, indent=2)}\n\n"
        f"Evidence sources:\n{json.dumps(sources, ensure_ascii=False, indent=2)}\n\n"
        "Return JSON only with status pass|fail|inconclusive|blocked, reviewer_id exactly as assigned, "
        "reviewer_independence verified|human|unverified, dimensions containing requirements/method/capability/evaluation/safety; "
        "each dimension has status and a non-empty evidence string array; findings is an array of {severity,code,message,design_item_ids}; required_changes is a string array."
    )
    cwd = Path(job.get("candidate_path") or job["design_path"]).resolve()
    if cwd.is_file(): cwd = cwd.parent
    review_output = agent.job_path.parent / role
    value, _, _ = agent.run(role, prompt, cwd, review_output, Path(job["candidate_path"]) if job.get("candidate_path") else None, True)
    value["reviewer_id"] = value.get("reviewer_id", job["reviewer_id"])
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("job", type=Path)
    parser.add_argument("response", type=Path)
    args = parser.parse_args()
    try:
        config = read_object(args.config)
        job = read_object(args.job)
        agent = CodeAgent(config, args.job.resolve())
        job_type = job.get("job_type")
        if job_type == "execute":
            response = execute_job(agent, config, job)
        elif job_type == "grade":
            response = grade_job(agent, job)
        elif job_type == "attribute":
            response = attribute_job(agent, job)
        elif job_type == "modify":
            response = modify_job(agent, job)
        elif job_type == "compare":
            response = compare_job(agent, job)
        elif job_type in {"review_design", "review_conformance"}:
            response = review_job(agent, job)
        else:
            raise ValueError(f"unsupported job_type: {job_type!r}")
        write_json(args.response, response)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
