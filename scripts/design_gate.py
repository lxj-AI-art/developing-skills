#!/usr/bin/env python3
"""Version-bound design, approval, initialization, binding, and provider handoff gates."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


IGNORED = {".git", "__pycache__", ".pytest_cache"}
SECTION_IDS = tuple(f"DF-{index:02d}" for index in range(1, 11))
DEPTH_SECTIONS = {
    "lightweight": {"DF-01", "DF-02", "DF-03", "DF-08", "DF-10"},
    "standard": set(SECTION_IDS),
    "high-assurance": set(SECTION_IDS),
}
PREFIX_TYPES = {"REQ": "requirement", "MTH": "method", "SAFE": "safety", "OUT": "output", "DEP": "dependency"}
NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
DEPTH_TYPES = {
    "lightweight": {"requirement", "output"},
    "standard": {"requirement", "method", "output", "dependency"},
    "high-assurance": set(PREFIX_TYPES.values()),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file() and not set(p.relative_to(root).parts) & IGNORED):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(file_hash(path).encode())
        digest.update(b"\0")
    return digest.hexdigest()


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise ValueError(f"refusing to overwrite existing artifact: {path}")
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def frontmatter(text: str) -> tuple[dict[str, str], str, list[str]]:
    errors: list[str] = []
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text, ["design must start with YAML frontmatter"]
    try:
        end = next(i for i, line in enumerate(lines[1:], 1) if line.strip() == "---")
    except StopIteration:
        return {}, text, ["design frontmatter is not closed"]
    values: dict[str, str] = {}
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            errors.append(f"unsupported frontmatter line: {line}")
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        if key in values:
            errors.append(f"duplicate frontmatter key: {key}")
        values[key] = value.strip().strip("\"'")
    return values, "\n".join(lines[end + 1 :]), errors


def json_blocks(body: str) -> list[Any]:
    values: list[Any] = []
    for raw in re.findall(r"```json\s*\n([\s\S]*?)\n```", body, flags=re.IGNORECASE):
        try:
            values.append(json.loads(raw))
        except json.JSONDecodeError:
            pass
    return values


def design_template(name: str, change_class: str = "new", risk_level: str = "medium", design_depth: str | None = None, language: str = "zh-CN") -> str:
    depth = design_depth or ("high-assurance" if risk_level in {"high", "critical"} else "standard")
    return f'''---
schema_version: "2.0"
skill_name: "{name}"
design_status: draft
change_class: {change_class}
risk_level: {risk_level}
design_depth: {depth}
language: "{language}"
---

# Skill design: {name}

## DF-01 目标与成功标准

<!-- 明确目标、可观察成功条件和非目标。 -->

## DF-02 用户、场景与触发边界

<!-- 谁在何时使用；正反触发和不触发样例。 -->

## DF-03 输入、输出与证据合同

<!-- 输入质量、输出结构、证据、不确定性和阻断状态。 -->

## DF-04 专业方法与决策路径

<!-- 可执行步骤、分支条件、停止条件和复核点。 -->

## DF-05 能力分配与依赖

<!-- Skill、脚本、工具、外部 Skill 与人工分别负责什么。 -->

## DF-06 知识、检索与版本

<!-- 知识来源、检索策略、时效性和版本边界。 -->

## DF-07 安全、权限与失败策略

<!-- 风险、禁止动作、最小权限、降级和恢复。 -->

## DF-08 评价、Oracle 与验收

<!-- 案例、断言、关键门禁、回归与留出策略。 -->

## DF-09 可观测性与运行记录

<!-- 记录身份、环境、工具调用、成本和不可观测项。 -->

## DF-10 开放问题与已定决策

<!-- ready 前解决关键开放问题，并记录决定依据。 -->

## 结构化设计项

```json
{{
  "design_items": [
    {{"id": "REQ-001", "type": "requirement", "text": "待定义的核心需求", "criticality": "critical", "source_ref": "用户确认", "acceptance": "可执行的验收条件"}},
    {{"id": "MTH-001", "type": "method", "text": "待定义的专业方法", "criticality": "major", "source_ref": "专业方法", "acceptance": "可观察的方法结果"}},
    {{"id": "SAFE-001", "type": "safety", "text": "待定义的安全边界", "criticality": "critical", "source_ref": "风险分析", "acceptance": "禁止项和降级行为可验证"}},
    {{"id": "OUT-001", "type": "output", "text": "待定义的输出合同", "criticality": "major", "source_ref": "用户确认", "acceptance": "输出字段和证据可检查"}},
    {{"id": "DEP-001", "type": "dependency", "text": "待定义的能力依赖", "criticality": "major", "source_ref": "能力分配", "acceptance": "依赖存在且失败可识别"}}
  ]
}}
```
'''


def validate_design(path: Path, require_ready: bool = False) -> dict[str, Any]:
    path = path.resolve()
    errors: list[str] = []
    warnings: list[str] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return {"schema_version": "2.0", "valid": False, "errors": [str(exc)], "warnings": [], "design_id": None}
    meta, body, parse_errors = frontmatter(text)
    errors.extend(parse_errors)
    required = ("schema_version", "skill_name", "design_status", "change_class", "risk_level", "design_depth", "language")
    for key in required:
        if not meta.get(key):
            errors.append(f"frontmatter needs non-empty {key}")
    if meta.get("skill_name") and not NAME_PATTERN.fullmatch(meta["skill_name"]):
        errors.append("skill_name must use lowercase letters, digits and hyphens, max 64 characters")
    if meta.get("schema_version") != "2.0":
        errors.append("schema_version must be 2.0")
    status = meta.get("design_status")
    if status not in {"draft", "ready"}:
        errors.append("design_status must be draft or ready")
    change = meta.get("change_class")
    risk = meta.get("risk_level")
    depth = meta.get("design_depth")
    if change not in {"new", "substantial", "local"}:
        errors.append("change_class must be new, substantial, or local")
    if risk not in {"low", "medium", "high", "critical"}:
        errors.append("risk_level must be low, medium, high, or critical")
    if depth not in DEPTH_SECTIONS:
        errors.append("design_depth must be lightweight, standard, or high-assurance")
    if change in {"new", "substantial"} and depth == "lightweight":
        errors.append("new/substantial work cannot use lightweight design")
    if risk in {"high", "critical"} and depth != "high-assurance":
        errors.append("high/critical risk requires high-assurance design")
    section_matches = re.findall(r"^##\s+(DF-\d{2})\b", body, flags=re.MULTILINE)
    duplicates = sorted({item for item in section_matches if section_matches.count(item) > 1})
    if duplicates:
        errors.append(f"duplicate stable sections: {duplicates}")
    required_sections = DEPTH_SECTIONS.get(depth, set())
    missing_sections = sorted(required_sections - set(section_matches))
    if missing_sections:
        errors.append(f"missing stable sections for {depth}: {missing_sections}")
    manifest = next((block for block in json_blocks(body) if isinstance(block, dict) and "design_items" in block), None)
    items = manifest.get("design_items") if isinstance(manifest, dict) else None
    if not isinstance(items, list) or not items:
        errors.append("one JSON block must contain a non-empty design_items array")
        items = []
    seen: set[str] = set()
    present_types: set[str] = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            errors.append(f"design_items[{index}] must be an object")
            continue
        for field in ("id", "type", "text", "criticality", "source_ref", "acceptance"):
            if not isinstance(item.get(field), str) or not item[field].strip():
                errors.append(f"design_items[{index}].{field} must be non-empty text")
        item_id = item.get("id", "")
        item_type = item.get("type", "")
        match = re.fullmatch(r"(REQ|MTH|SAFE|OUT|DEP)-\d{3}", item_id)
        if not match:
            errors.append(f"design_items[{index}].id has invalid stable ID")
        elif PREFIX_TYPES[match.group(1)] != item_type:
            errors.append(f"{item_id} prefix does not match type {item_type}")
        if item_id in seen:
            errors.append(f"duplicate design item ID: {item_id}")
        seen.add(item_id)
        present_types.add(item_type)
        if item.get("criticality") not in {"critical", "major", "minor"}:
            errors.append(f"{item_id or index} has invalid criticality")
        unresolved = any(token in str(item.get(field, "")) for field in ("text", "source_ref", "acceptance") for token in ("待定义", "TBD", "TODO"))
        if unresolved:
            (errors if status == "ready" else warnings).append(f"{item_id or index} is unresolved")
    missing_types = sorted(DEPTH_TYPES.get(depth, set()) - present_types)
    if missing_types:
        errors.append(f"design_items missing types required by {depth}: {missing_types}")
    if "<!--" in body:
        (errors if status == "ready" else warnings).append("design still contains unresolved authoring comments")
    if require_ready and status != "ready":
        errors.append("design_status must be ready")
    design_id = file_hash(path) if path.is_file() else None
    return {
        "schema_version": "2.0", "valid": not errors, "ready": status == "ready", "design_id": design_id,
        "skill_name": meta.get("skill_name"), "change_class": change, "risk_level": risk, "design_depth": depth,
        "design_items": items, "errors": errors, "warnings": warnings, "path": str(path),
    }


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def review_required(report: dict[str, Any]) -> bool:
    return report.get("change_class") in {"new", "substantial"} or report.get("risk_level") in {"high", "critical"} or report.get("design_depth") == "high-assurance"


def validate_review(report: dict[str, Any], review_path: Path) -> dict[str, Any]:
    review = load_json(review_path)
    errors: list[str] = []
    if review.get("review_type") != "design":
        errors.append("review_type must be design")
    if review.get("design_id") != report.get("design_id"):
        errors.append("design review is not bound to this exact design_id")
    if review.get("status") != "pass":
        errors.append("design review must pass")
    if review.get("reviewer_independence") not in {"verified", "human"}:
        errors.append("design review independence must be verified or human")
    dimensions = review.get("dimensions")
    for name in ("requirements", "method", "capability", "evaluation", "safety"):
        if not isinstance(dimensions, dict) or not isinstance(dimensions.get(name), dict) or dimensions[name].get("status") != "pass" or not dimensions[name].get("evidence"):
            errors.append(f"design review dimension {name} must pass with evidence")
    if any(item.get("severity") == "critical" for item in review.get("findings", []) if isinstance(item, dict)):
        errors.append("design review has unresolved critical findings")
    return {"valid": not errors, "errors": errors, "review": review, "review_id": file_hash(review_path.resolve())}


def approve(report: dict[str, Any], review_path: Path | None, output: Path, confirmed_by: str, basis: str, reference: str) -> dict[str, Any]:
    if not report.get("valid") or not report.get("ready"):
        raise ValueError("only a structurally valid ready design can be approved")
    review = None
    if review_path:
        review = validate_review(report, review_path.resolve())
        if not review["valid"]:
            raise ValueError("; ".join(review["errors"]))
    if review_required(report) and review is None:
        raise ValueError("this design requires an independent semantic review before approval")
    artifact = {
        "schema_version": "1.0", "status": "approved", "design_id": report["design_id"],
        "skill_name": report["skill_name"], "confirmed_by": confirmed_by, "confirmed_at": utc_now(),
        "confirmation_basis": basis, "confirmation_ref": reference,
        "review_path": str(review_path.resolve()) if review_path else None,
        "review_artifact_id": review["review_id"] if review else None,
    }
    write_json(output.resolve(), artifact)
    artifact["approval_id"] = file_hash(output.resolve())
    return artifact


def validate_approval(report: dict[str, Any], approval_path: Path, require_review_artifact: bool = True) -> dict[str, Any]:
    approval_path = approval_path.resolve()
    errors: list[str] = []
    try:
        value = load_json(approval_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {"valid": False, "errors": [str(exc)], "approval_id": None}
    if value.get("status") != "approved":
        errors.append("approval status must be approved")
    if value.get("design_id") != report.get("design_id"):
        errors.append("approval does not bind this exact design version")
    for key in ("confirmed_by", "confirmed_at", "confirmation_basis", "confirmation_ref"):
        if not isinstance(value.get(key), str) or not value[key].strip():
            errors.append(f"approval needs non-empty {key}")
    try:
        confirmed_at = datetime.fromisoformat(str(value.get("confirmed_at", "")).replace("Z", "+00:00"))
        if confirmed_at.tzinfo is None:
            raise ValueError
    except ValueError:
        errors.append("approval confirmed_at must be an ISO-8601 timestamp with timezone")
    if value.get("confirmation_basis") not in {"explicit-user-approval", "prior-recorded-decision", "authorized-autonomous"}:
        errors.append("invalid confirmation_basis")
    if review_required(report) and require_review_artifact:
        raw = value.get("review_path")
        if not raw:
            errors.append("approval is missing required design review")
        else:
            review_path = Path(raw)
            try:
                review = validate_review(report, review_path)
                if not review["valid"]:
                    errors.extend(review["errors"])
                if value.get("review_artifact_id") != review.get("review_id"):
                    errors.append("approved review artifact changed")
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                errors.append(f"cannot validate approved review: {exc}")
    return {"valid": not errors, "errors": errors, "approval": value, "approval_id": file_hash(approval_path)}


def skill_name(candidate: Path) -> str | None:
    try:
        text = (candidate / "SKILL.md").read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r"^name:\s*[\"']?([^\n\"']+)", text, flags=re.MULTILINE)
    return match.group(1).strip() if match else None


def validate_candidate_external(candidate: Path) -> dict[str, Any]:
    script = Path(__file__).with_name("validate_candidate.py")
    run = subprocess.run([sys.executable, str(script), str(candidate.resolve())], capture_output=True, text=True, check=False)
    try:
        report = json.loads(run.stdout)
    except json.JSONDecodeError:
        raise ValueError(f"candidate validator failed: {run.stderr or run.stdout}")
    if run.returncode or not report.get("valid"):
        raise ValueError("candidate validation failed: " + "; ".join(report.get("errors", [])))
    return report


def ensure_binding_match(report: dict[str, Any], candidate: Path) -> None:
    actual = skill_name(candidate.resolve())
    if actual != report.get("skill_name"):
        raise ValueError(f"design skill_name {report.get('skill_name')} does not match candidate {actual}")


def validate_conformance(report: dict[str, Any], approval: dict[str, Any], candidate: Path, conformance_path: Path) -> dict[str, Any]:
    value = load_json(conformance_path.resolve())
    errors: list[str] = []
    expected = {"review_type": "conformance", "design_id": report["design_id"], "candidate_id": tree_hash(candidate.resolve())}
    for key, wanted in expected.items():
        if value.get(key) != wanted:
            errors.append(f"conformance {key} mismatch")
    if value.get("approval_id") != approval.get("approval_id"):
        errors.append("conformance approval_id mismatch")
    if value.get("status") != "pass":
        errors.append("conformance review must pass")
    if value.get("reviewer_independence") not in {"verified", "human"}:
        errors.append("conformance reviewer must be independent")
    dimensions = value.get("dimensions")
    for name in ("requirements", "method", "capability", "evaluation", "safety"):
        dimension = dimensions.get(name) if isinstance(dimensions, dict) else None
        if not isinstance(dimension, dict) or dimension.get("status") != "pass" or not isinstance(dimension.get("evidence"), list) or not dimension["evidence"]:
            errors.append(f"conformance dimension {name} must pass with evidence")
    if any(item.get("severity") == "critical" for item in value.get("findings", []) if isinstance(item, dict)):
        errors.append("conformance review has unresolved critical findings")
    controller = value.get("controller_validation")
    if not isinstance(controller, dict) or controller.get("valid") is not True:
        errors.append("conformance review lacks passing controller validation")
    return {"valid": not errors, "errors": errors, "conformance_id": file_hash(conformance_path.resolve()), "review": value}


def bind(report: dict[str, Any], candidate: Path, output: Path, provider: str, *, approval_path: Path | None = None, stage: str = "initialized", traceability_validation: Path | None = None, conformance_review: Path | None = None) -> dict[str, Any]:
    if not report.get("valid") or not report.get("ready"):
        raise ValueError("binding requires a valid ready design")
    if approval_path is None:
        raise ValueError("binding requires an external approval artifact")
    approval = validate_approval(report, approval_path)
    if not approval["valid"]:
        raise ValueError("; ".join(approval["errors"]))
    ensure_binding_match(report, candidate)
    candidate_report = validate_candidate_external(candidate)
    value: dict[str, Any] = {
        "schema_version": "2.0", "stage": stage, "created_at": utc_now(), "provider": provider,
        "design_id": report["design_id"], "approval_id": approval["approval_id"],
        "candidate_id": candidate_report["skill_id"], "candidate_path": str(candidate.resolve()),
    }
    if stage == "implementation":
        if not traceability_validation or not conformance_review:
            raise ValueError("implementation binding requires traceability validation and conformance review")
        trace = load_json(traceability_validation.resolve())
        if trace.get("status") != "pass" or trace.get("design_id") != report["design_id"] or trace.get("candidate_id") != candidate_report["skill_id"]:
            raise ValueError("traceability validation does not pass for this exact design and candidate")
        conf = validate_conformance(report, approval, candidate, conformance_review)
        if not conf["valid"]:
            raise ValueError("; ".join(conf["errors"]))
        value.update({"traceability_id": file_hash(traceability_validation.resolve()), "conformance_review_id": conf["conformance_id"]})
    write_json(output.resolve(), value)
    return value


def freeze_approved_design(design: Path, approval_path: Path, destination: Path, candidate: Path | None = None) -> dict[str, Any]:
    report = validate_design(design, True)
    if not report["valid"]:
        raise ValueError("; ".join(report["errors"]))
    approval = validate_approval(report, approval_path)
    if not approval["valid"]:
        raise ValueError("; ".join(approval["errors"]))
    if candidate:
        ensure_binding_match(report, candidate)
    destination.parent.mkdir(parents=True, exist_ok=True)
    approval_destination = destination.parent / "design-approval.json"
    for source, target in ((design.resolve(), destination), (approval_path.resolve(), approval_destination)):
        if target.exists() and file_hash(target) != file_hash(source):
            raise ValueError(f"workspace already contains a different frozen artifact: {target}")
        if not target.exists():
            shutil.copy2(source, target)
    review_source = approval["approval"].get("review_path")
    if review_source:
        review_source_path = Path(review_source).resolve()
        review_destination = destination.parent / "design-review.json"
        if file_hash(review_source_path) != approval["approval"].get("review_artifact_id"):
            raise ValueError("approved review artifact changed before freezing")
        if review_destination.exists() and file_hash(review_destination) != file_hash(review_source_path):
            raise ValueError("workspace already contains a different frozen design review")
        if not review_destination.exists():
            shutil.copy2(review_source_path, review_destination)
    report["approval_id"] = approval["approval_id"]
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    init = sub.add_parser("init")
    init.add_argument("--skill-name", required=True); init.add_argument("--change-class", choices=("new", "substantial", "local"), default="new")
    init.add_argument("--risk-level", choices=("low", "medium", "high", "critical"), default="medium")
    init.add_argument("--design-depth", choices=tuple(DEPTH_SECTIONS)); init.add_argument("--language", default="zh-CN"); init.add_argument("--output", required=True, type=Path)
    check = sub.add_parser("validate"); check.add_argument("design", type=Path); check.add_argument("--require-ready", action="store_true"); check.add_argument("--approval", type=Path); check.add_argument("--output", type=Path)
    approval_cmd = sub.add_parser("approve"); approval_cmd.add_argument("design", type=Path); approval_cmd.add_argument("--review", type=Path)
    approval_cmd.add_argument("--confirmed-by", required=True); approval_cmd.add_argument("--confirmation-basis", required=True, choices=("explicit-user-approval", "prior-recorded-decision", "authorized-autonomous")); approval_cmd.add_argument("--confirmation-ref", required=True); approval_cmd.add_argument("--output", required=True, type=Path)
    bind_cmd = sub.add_parser("bind"); bind_cmd.add_argument("design", type=Path); bind_cmd.add_argument("--approval", required=True, type=Path); bind_cmd.add_argument("--candidate", required=True, type=Path); bind_cmd.add_argument("--provider", required=True); bind_cmd.add_argument("--stage", choices=("initialized", "implementation"), default="initialized"); bind_cmd.add_argument("--traceability-validation", type=Path); bind_cmd.add_argument("--conformance-review", type=Path); bind_cmd.add_argument("--output", required=True, type=Path)
    initialize = sub.add_parser("initialize"); initialize.add_argument("design", type=Path); initialize.add_argument("--approval", required=True, type=Path); initialize.add_argument("--skill-creator-root", required=True, type=Path); initialize.add_argument("--output-directory", required=True, type=Path); initialize.add_argument("--resources", default=""); initialize.add_argument("--binding-output", required=True, type=Path)
    handoff = sub.add_parser("handoff"); handoff.add_argument("design", type=Path); handoff.add_argument("--approval", required=True, type=Path); handoff.add_argument("--provider", required=True); handoff.add_argument("--resources", default=""); handoff.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.action == "init":
            if not NAME_PATTERN.fullmatch(args.skill_name): raise ValueError("skill name must use lowercase letters, digits and hyphens, max 64 characters")
            if args.output.exists(): raise ValueError("refusing to overwrite existing design")
            args.output.parent.mkdir(parents=True, exist_ok=True); args.output.write_text(design_template(args.skill_name, args.change_class, args.risk_level, args.design_depth, args.language), encoding="utf-8"); result = {"status": "created", "path": str(args.output.resolve())}
        elif args.action == "validate":
            result = validate_design(args.design, args.require_ready)
            if args.approval:
                approval_result = validate_approval(result, args.approval); result["approval"] = approval_result; result["valid"] = result["valid"] and approval_result["valid"]
            if args.output:
                write_json(args.output, result)
            print(json.dumps(result, ensure_ascii=False, indent=2)); return 0 if result["valid"] else 1
        elif args.action == "approve":
            result = approve(validate_design(args.design, True), args.review, args.output, args.confirmed_by, args.confirmation_basis, args.confirmation_ref)
        elif args.action == "bind":
            result = bind(validate_design(args.design, True), args.candidate, args.output, args.provider, approval_path=args.approval, stage=args.stage, traceability_validation=args.traceability_validation, conformance_review=args.conformance_review)
        elif args.action == "initialize":
            report = validate_design(args.design, True); approval = validate_approval(report, args.approval)
            if not report["valid"] or not approval["valid"]: raise ValueError("design or approval gate failed")
            script = args.skill_creator_root.resolve() / "scripts" / "init_skill.py"
            if not script.is_file(): raise ValueError("selected skill-creator has no initializer; create a provider handoff instead")
            command = [sys.executable, str(script), report["skill_name"], "--path", str(args.output_directory.resolve())]
            if args.resources: command += ["--resources", args.resources]
            run = subprocess.run(command, capture_output=True, text=True, check=False)
            if run.returncode: raise ValueError(f"initializer failed: {run.stderr or run.stdout}")
            candidate = args.output_directory.resolve() / report["skill_name"]
            binding = bind(report, candidate, args.binding_output, f"skill-creator:{args.skill_creator_root.resolve()}", approval_path=args.approval)
            result = {"status": "completed", "candidate": str(candidate), "binding": binding, "provider_stdout": run.stdout[-4000:]}
        else:
            report = validate_design(args.design, True); approval = validate_approval(report, args.approval)
            if not report["valid"] or not approval["valid"]: raise ValueError("design or approval gate failed")
            result = {"schema_version": "1.0", "status": "ready", "provider": args.provider, "design_id": report["design_id"], "approval_id": approval["approval_id"], "skill_name": report["skill_name"], "resources": [x for x in args.resources.split(",") if x], "instruction": "Initialize from the approved design; return candidate path, provider version, validation evidence, and immutable candidate identity. Do not receive holdout cases or hidden answers."}
            write_json(args.output.resolve(), result)
        print(json.dumps(result, ensure_ascii=False, indent=2)); return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr); return 2


if __name__ == "__main__":
    sys.exit(main())
