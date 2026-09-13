#!/usr/bin/env python3
"""Build a non-publishing, identity-bound Skill promotion packet for human approval."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from design_gate import file_hash, tree_hash


def load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {path}: {exc}") from None
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def evidence_gate(path: Path | None, *, eligible_key: str | None = None) -> dict[str, Any]:
    if path is None:
        return {"provided": False, "status": "not_required"}
    value = load(path)
    passed = value.get("status") in {"pass", "valid"}
    if eligible_key is not None:
        passed = passed and value.get(eligible_key) is True
    return {"provided": True, "status": "pass" if passed else "fail", "artifact_id": file_hash(path), "path": str(path.resolve())}


def build(
    workspace: Path,
    candidate: Path,
    *,
    dataset_audit: Path | None,
    grader_calibration: Path | None,
    professional_validation: Path | None,
    holdout_status: Path | None,
) -> dict[str, Any]:
    history = load(workspace / "history.json")
    selection = load(workspace / "selection.json")
    candidate_id = tree_hash(candidate)
    expected = history.get("recommended_candidate_path")
    gates: dict[str, Any] = {
        "selection": {"status": "pass" if selection.get("decision") == "candidate_is_best" else "fail", "decision": selection.get("decision"), "selection_id": file_hash(workspace / "selection.json")},
        "candidate_identity": {"status": "pass" if expected and Path(expected).resolve() == candidate.resolve() else "fail", "candidate_id": candidate_id, "history_candidate": expected},
        "dataset_audit": evidence_gate(dataset_audit),
        "grader_calibration": evidence_gate(grader_calibration),
        "professional_validation": evidence_gate(professional_validation, eligible_key="real_domain_claim_eligible") if professional_validation else {"provided": False, "status": "not_required"},
        "holdout_governance": evidence_gate(holdout_status, eligible_key="holdout_eligible") if holdout_status else {"provided": False, "status": "not_required"},
    }
    assurance = history.get("current_assurance", {}) if isinstance(history.get("current_assurance"), dict) else {}
    assurance_match = all(assurance.get(key) for key in ("traceability_id", "conformance_review_id", "implementation_binding_id")) and assurance.get("candidate_id") == candidate_id
    if history.get("design_id"):
        gates["assurance"] = {"status": "pass" if assurance_match else "fail", **assurance}
    else:
        gates["assurance"] = {"status": "not_required"}
    failed = [name for name, value in gates.items() if value.get("status") == "fail"]
    return {
        "schema_version": "1.0",
        "status": "ready_for_human_review" if not failed else "blocked",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "publishes_or_overwrites_skill": False,
        "requires_human_approval": True,
        "workspace": str(workspace.resolve()),
        "candidate_path": str(candidate.resolve()),
        "candidate_id": candidate_id,
        "design_id": history.get("design_id"),
        "eval_set_id": history.get("eval_set_id"),
        "gates": gates,
        "failed_gates": failed,
        "limitations": ["This packet is a recommendation artifact, not deployment authorization."],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--dataset-audit", type=Path)
    parser.add_argument("--grader-calibration", type=Path)
    parser.add_argument("--professional-validation", type=Path)
    parser.add_argument("--holdout-status", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        history = load(args.workspace / "history.json")
        candidate = args.candidate or (Path(history["recommended_candidate_path"]) if history.get("recommended_candidate_path") else None)
        if candidate is None or not (candidate / "SKILL.md").is_file():
            raise ValueError("a valid recommended candidate is required")
        if args.output.exists():
            raise ValueError("refusing to overwrite output")
        packet = build(args.workspace.resolve(), candidate.resolve(), dataset_audit=args.dataset_audit, grader_calibration=args.grader_calibration, professional_validation=args.professional_validation, holdout_status=args.holdout_status)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(packet, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(packet, ensure_ascii=False, indent=2))
        return 0 if packet["status"] == "ready_for_human_review" else 1
    except (OSError, ValueError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
