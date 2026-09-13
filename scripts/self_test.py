#!/usr/bin/env python3
"""Deterministic self-tests for developing-skills infrastructure."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from anthropic_skill_creator import bound_dependency_root, export_viewer_workspace, inspect_root, tree_digest, trigger_eval_data
from audit_eval_set import audit as audit_eval_set
from build_promotion_packet import build as build_promotion_packet
from codeagent_adapter import run_discriminating_check
from design_gate import approve, bind, design_template, file_hash, tree_hash, validate_approval, validate_design
from generate_assurance_view import render as render_assurance_view
from holdout_vault import checkout as checkout_holdout, create_vault, status as holdout_status
from run_optimization import Adapter, AdapterCallError
from validate_professional_suite import validate_suite
from validate_traceability import validate_traceability
from select_candidate import decide, paired_delta_stats
from validate_eval_set import validate


HERE = Path(__file__).resolve().parent


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def minimal_eval(policy: dict | None = None) -> dict:
    return {
        "schema_version": "2.0",
        "skill_name": "demo",
        "policy": policy or {},
        "cases": [
            {
                "id": "train-1",
                "split": "train",
                "prompt": "do the work",
                "risk": "low",
                "tags": [],
                "oracle": {"expected_outcomes": ["done"], "provenance": {"kind": "expert_review", "source": "review", "confidence": "confirmed"}},
                "expectations": [],
            },
            {
                "id": "hold-1",
                "split": "holdout",
                "prompt": "do unseen work",
                "risk": "low",
                "tags": [],
                "oracle": {"expected_outcomes": ["done"], "provenance": {"kind": "expert_review", "source": "review", "confidence": "confirmed"}},
                "expectations": [],
            },
        ],
    }


def sign_benchmark() -> dict:
    runs = []
    case_summary = []
    for split, count in (("train", 1), ("holdout", 5)):
        for index in range(1, count + 1):
            case_id = f"{split}-{index}"
            for config, score in (("candidate", 1.0), ("baseline", 0.0)):
                runs.append({"case_id": case_id, "split": split, "configuration": config, "run_number": 1, "score": score})
                case_summary.append({"case_id": case_id, "split": split, "configuration": config, "runs": 1, "score": {"mean": score}})
    by_split = {
        "train": {"score": {"mean": 1.0}, "status_counts": {"pass": 1}},
        "holdout": {"score": {"mean": 1.0, "stddev": 0.0}, "status_counts": {"pass": 5}},
    }
    baseline_split = {
        "train": {"score": {"mean": 0.0}, "status_counts": {"fail": 1}},
        "holdout": {"score": {"mean": 0.0, "stddev": 0.0}, "status_counts": {"fail": 5}},
    }
    return {
        "configurations": {
            "candidate": {"critical_failure_runs": 0, "by_split": by_split},
            "baseline": {"critical_failure_runs": 0, "by_split": baseline_split},
        },
        "integrity": {"identity_bound": True, "environment_bound": True},
        "runs": runs,
        "case_summary": case_summary,
    }


def test_sign_gate_boundaries() -> None:
    benchmark = sign_benchmark()
    stats = paired_delta_stats(benchmark, "holdout", "candidate", "baseline")
    assert stats["wins"] == 5 and stats["one_sided_sign_pvalue"] == 0.03125
    for threshold, expected in ((0.031249, "inconclusive"), (0.03125, "candidate_is_best"), (0.031251, "candidate_is_best")):
        eval_set = minimal_eval({
            "require_sign_test": True,
            "min_non_tie_pairs": 5,
            "max_sign_test_pvalue": threshold,
            "require_identity_binding": True,
            "require_environment_binding": True,
        })
        assert decide(benchmark, eval_set, "candidate", "baseline")["decision"] == expected


def test_policy_validation_boundaries(root: Path) -> None:
    source = root / "eval.json"
    for value, valid in ((-0.000001, False), (0.0, True), (1.0, True), (1.000001, False)):
        data = minimal_eval({"max_sign_test_pvalue": value})
        source.write_text(json.dumps(data), encoding="utf-8")
        errors, _, _ = validate(data, source, False)
        assert (not errors) is valid, (value, errors)


def test_discriminating_checks(root: Path) -> None:
    skill = root / "skill"
    output = root / "output"
    inputs = root / "inputs"
    write(skill / "sample.py", "class Engine:\n    pass\n\ndef solve():\n    return 1\n")
    write(output / "record.json", '{"state":{"status":"ready"}}\n')
    write(inputs / "input.txt", "input")
    job = {"skill_path": str(skill), "output_dir": str(output), "case": {"files": [str(inputs / "input.txt")]}}
    assert run_discriminating_check(job, {"type": "text_regex", "root": "skill", "path": "sample.py", "pattern": "^def solve"})["observed"]
    assert run_discriminating_check(job, {"type": "json_path_equals", "root": "output", "path": "record.json", "json_path": "state.status", "equals": "ready"})["observed"]
    assert run_discriminating_check(job, {"type": "python_ast_symbol_exists", "root": "skill", "path": "sample.py", "symbol": "Engine", "symbol_kind": "class"})["observed"]
    assert run_discriminating_check(job, {"type": "file_exists", "root": "skill", "path": "../escape"})["status"] == "invalid"


def fake_anthropic_root(root: Path) -> Path:
    creator = root / "skill-creator"
    write(creator / "SKILL.md", "---\nname: skill-creator\ndescription: fake test provider\n---\n")
    write(creator / "scripts" / "__init__.py", "")
    write(creator / "scripts" / "quick_validate.py", "import sys\nprint('valid')\n")
    write(creator / "scripts" / "package_skill.py", "import pathlib,sys\nout=pathlib.Path(sys.argv[2]); out.mkdir(parents=True,exist_ok=True); (out/'demo.skill').write_text('ok')\n")
    write(creator / "scripts" / "run_loop.py", "print('{}')\n")
    write(creator / "eval-viewer" / "generate_review.py", "import pathlib,sys\np=pathlib.Path(sys.argv[sys.argv.index('--static')+1]); p.write_text('<html>review</html>')\n")
    write(creator / "LICENSE.txt", "Apache License\nVersion 2.0\n")
    return creator


def test_dependency_bootstrap(root: Path) -> None:
    source = fake_anthropic_root(root / "provider-source")
    ref = "1" * 40
    skill_root = root / "installed-developing-skills"; skill_root.mkdir()
    manifest = root / "dependency-manifest.json"
    manifest.write_text(json.dumps({
        "schema_version": "1.0", "dependencies": [{
            "id": "anthropic-skill-creator", "recommended": True, "default_selected": True,
            "source": {"repo": "anthropics/skills", "path": "skills/skill-creator", "ref": ref},
            "expected_content_id": tree_digest(source), "license": "Apache-2.0", "license_file": "LICENSE.txt",
            "required_capabilities": ["validate", "package", "description_optimize", "viewer"],
            "install": {"mode": "private-provider", "relative_root": "dependencies/anthropic-skill-creator"}
        }]
    }), encoding="utf-8")
    installer = root / "fake-installer.py"
    write(installer, f'''import argparse,shutil
p=argparse.ArgumentParser(); p.add_argument("--repo"); p.add_argument("--path"); p.add_argument("--ref"); p.add_argument("--dest"); p.add_argument("--name"); p.add_argument("--method"); a=p.parse_args()
shutil.copytree({str(source)!r}, a.dest + "/" + a.name)
print("installed fixture")
''')
    script = HERE / "bootstrap_dependencies.py"
    common = [sys.executable, str(script), "--skill-root", str(skill_root), "--manifest", str(manifest), "--installer", str(installer)]
    planned = subprocess.run([*common, "plan"], capture_output=True, text=True, check=False)
    assert planned.returncode == 0 and json.loads(planned.stdout)["confirmation_required"]
    unconfirmed = subprocess.run([*common, "install"], capture_output=True, text=True, check=False)
    assert unconfirmed.returncode == 3 and json.loads(unconfirmed.stdout)["status"] == "needs_confirmation"
    skipped = subprocess.run([*common, "install", "--without-anthropic"], capture_output=True, text=True, check=False)
    assert skipped.returncode == 0 and json.loads(skipped.stdout)["status"] == "skipped"
    installed = subprocess.run([*common, "install", "--confirm-external-install"], capture_output=True, text=True, check=False)
    assert installed.returncode == 0, installed.stderr + installed.stdout
    result = json.loads(installed.stdout)
    assert result["status"] == "installed" and result["installed"] and not result["global_skill_registration"]
    assert (skill_root / "dependencies" / "anthropic-skill-creator" / "installation.json").is_file()
    assert bound_dependency_root(skill_root) == Path(result["private_root"])
    moved_root = root / "moved-developing-skills"; shutil.copytree(skill_root, moved_root)
    assert bound_dependency_root(moved_root) == (moved_root / "dependencies" / "anthropic-skill-creator" / ref).resolve()
    repeated = subprocess.run([*common, "install"], capture_output=True, text=True, check=False)
    assert repeated.returncode == 0 and json.loads(repeated.stdout)["status"] == "already_installed"
    (Path(result["private_root"]) / "SKILL.md").write_text("tampered", encoding="utf-8")
    assert bound_dependency_root(skill_root) is None

    invalid_manifest = root / "invalid-dependency-manifest.json"
    invalid_data = json.loads(manifest.read_text(encoding="utf-8"))
    invalid_data["dependencies"][0]["expected_content_id"] = "0" * 64
    invalid_manifest.write_text(json.dumps(invalid_data), encoding="utf-8")
    invalid_root = root / "invalid-installed-developing-skills"; invalid_root.mkdir()
    invalid = subprocess.run([sys.executable, str(script), "install", "--skill-root", str(invalid_root), "--manifest", str(invalid_manifest), "--installer", str(installer), "--confirm-external-install"], capture_output=True, text=True, check=False)
    assert invalid.returncode == 1 and json.loads(invalid.stdout)["status"] == "failed"
    assert not (invalid_root / "dependencies" / "anthropic-skill-creator" / ref).exists()


def test_dependency_update_audit(root: Path) -> None:
    source = fake_anthropic_root(root / "dependency-update")
    manifest = root / "dependency-update-manifest.json"
    manifest.write_text(json.dumps({"dependencies": [{
        "id": "anthropic-skill-creator", "source": {"ref": "1" * 40},
        "expected_content_id": tree_digest(source), "license": "Apache-2.0", "license_file": "LICENSE.txt",
        "required_capabilities": ["validate", "package", "description_optimize", "viewer"],
    }]}), encoding="utf-8")
    report = root / "dependency-update-report.json"
    completed = subprocess.run([
        sys.executable, str(HERE / "audit_dependency_update.py"), "--manifest", str(manifest),
        "--candidate-root", str(source), "--candidate-ref", "2" * 40,
        "--current-root", str(source), "--compatibility-command", f"{sys.executable} -c pass", "--output", str(report),
    ], capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr + completed.stdout
    value = json.loads(report.read_text())
    assert value["status"] == "pass" and value["installs_or_updates_dependency"] is False and value["sbom"]


def ready_design_text(name: str, risk: str = "medium") -> str:
    text = design_template(name, "new", risk)
    text = text.replace("design_status: draft", "design_status: ready")
    text = re.sub(r"<!--[\s\S]*?-->", "明确、可执行、可验证的专业决定。", text)
    return text.replace("待定义的核心需求", "根据输入生成有证据的结论").replace("待定义的专业方法", "先核验证据再分层判断").replace("待定义的安全边界", "证据不足必须降级").replace("待定义的输出合同", "输出结论、证据与下一步").replace("待定义的能力依赖", "依赖已验证的本地输入")


def design_review(path: Path, report: dict) -> None:
    path.write_text(json.dumps({
        "schema_version": "1.0", "review_type": "design", "design_id": report["design_id"], "status": "pass",
        "reviewer_id": "independent-reviewer", "reviewer_independence": "verified",
        "dimensions": {name: {"status": "pass", "evidence": [f"verified {name} substance"]} for name in ("requirements", "method", "capability", "evaluation", "safety")},
        "findings": [], "required_changes": []
    }, ensure_ascii=False), encoding="utf-8")


def make_approval(design: Path, root: Path) -> tuple[dict, Path]:
    report = validate_design(design, True)
    review = root / f"{design.stem}-review.json"; design_review(review, report)
    approval_path = root / f"{design.stem}-approval.json"
    approve(report, review, approval_path, "test-user", "explicit-user-approval", "test-record")
    approval = validate_approval(report, approval_path)
    assert approval["valid"]
    return report, approval_path


def test_design_gate(root: Path) -> None:
    draft = root / "draft-design.md"
    draft.write_text(design_template("designed-skill", "new"), encoding="utf-8")
    draft_report = validate_design(draft)
    assert draft_report["valid"] and draft_report["warnings"]
    assert not validate_design(draft, True)["valid"]
    multilingual = root / "multilingual.md"
    multilingual.write_text(ready_design_text("designed-skill").replace("## DF-01 目标与成功标准", "## DF-01 Objective and success"), encoding="utf-8")
    assert validate_design(multilingual, True)["valid"]
    unsafe_depth = root / "unsafe-depth.md"
    unsafe_depth.write_text(ready_design_text("designed-skill", "high").replace("design_depth: high-assurance", "design_depth: standard"), encoding="utf-8")
    assert not validate_design(unsafe_depth, True)["valid"]
    design = root / "skill-design.md"
    design.write_text(ready_design_text("designed-skill"), encoding="utf-8")
    report, approval_path = make_approval(design, root)
    assert report["valid"] and report["ready"]
    candidate = root / "designed-skill"
    write(candidate / "SKILL.md", "---\nname: designed-skill\ndescription: test\n---\n")
    binding = bind(report, candidate, root / "binding.json", "test-provider", approval_path=approval_path)
    assert binding["design_id"] == report["design_id"] and binding["candidate_id"]
    design.write_text(ready_design_text("designed-skill") + "\nchanged\n", encoding="utf-8")
    changed = validate_design(design, True)
    assert changed["valid"] and not validate_approval(changed, approval_path)["valid"]
    wrong = root / "wrong"
    write(wrong / "SKILL.md", "---\nname: wrong\ndescription: test\n---\n")
    try:
        bind(report, wrong, root / "wrong-binding.json", "test-provider", approval_path=approval_path)
    except ValueError as exc:
        assert "does not match" in str(exc)
    else:
        raise AssertionError("design/candidate name mismatch was accepted")


def test_design_initialization(root: Path) -> None:
    design = root / "init-design.md"
    design.write_text(ready_design_text("initialized-skill"), encoding="utf-8")
    _, approval_path = make_approval(design, root)
    creator = root / "creator-with-init"
    write(creator / "scripts" / "init_skill.py", '''import argparse
from pathlib import Path
p=argparse.ArgumentParser(); p.add_argument("name"); p.add_argument("--path"); p.add_argument("--resources", default=""); a=p.parse_args()
target=Path(a.path)/a.name; target.mkdir(parents=True); (target/"SKILL.md").write_text(f"---\\nname: {a.name}\\ndescription: initialized\\n---\\n")
''')
    completed = subprocess.run([
        sys.executable, str(HERE / "design_gate.py"), "initialize", str(design), "--approval", str(approval_path),
        "--skill-creator-root", str(creator), "--output-directory", str(root / "initialized"),
        "--binding-output", str(root / "initialized-binding.json"),
    ], capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr + completed.stdout
    result = json.loads(completed.stdout)
    assert result["status"] == "completed" and result["binding"]["design_id"]


def test_design_assurance_and_policy_bypass(root: Path) -> None:
    case_root = root / "assurance"; case_root.mkdir()
    candidate = case_root / "demo"
    write(candidate / "SKILL.md", "---\nname: demo\ndescription: evidence-based demo\n---\n\nReturn an evidenced result.\n")
    design_path = case_root / "skill-design.md"
    design_path.write_text(ready_design_text("demo", "high"), encoding="utf-8")
    design, approval_path = make_approval(design_path, case_root)
    eval_data = minimal_eval({"require_design_binding": True, "require_design_review": True, "require_traceability": True, "require_conformance_review": True})
    for case in eval_data["cases"]:
        case["expectations"] = [{"id": "evidence", "text": "returns evidence", "kind": "semantic", "severity": "critical", "weight": 1.0}]
    eval_path = case_root / "eval.json"; eval_path.write_text(json.dumps(eval_data), encoding="utf-8")
    approval = validate_approval(design, approval_path)
    trace = {
        "schema_version": "1.0", "design_id": design["design_id"], "approval_id": approval["approval_id"],
        "candidate_id": tree_hash(candidate), "eval_set_id": file_hash(eval_path),
        "mappings": [{"design_item_id": item["id"], "implementation": [{"kind": "file", "path": "SKILL.md", "locator": item["id"]}], "evaluations": [{"case_id": "train-1", "expectation_ids": ["evidence"]}]} for item in design["design_items"]]
    }
    trace_path = case_root / "traceability.json"; trace_path.write_text(json.dumps(trace), encoding="utf-8")
    trace_report = validate_traceability(trace_path, design_path, approval_path, candidate, eval_path)
    assert trace_report["status"] == "pass", trace_report
    validation_path = case_root / "traceability-validation.json"; validation_path.write_text(json.dumps(trace_report), encoding="utf-8")
    conformance = {
        "schema_version": "1.0", "review_type": "conformance", "status": "pass", "design_id": design["design_id"],
        "approval_id": approval["approval_id"], "candidate_id": tree_hash(candidate), "eval_set_id": file_hash(eval_path),
        "reviewer_id": "independent-reviewer", "reviewer_independence": "verified",
        "dimensions": {name: {"status": "pass", "evidence": [f"verified {name}"]} for name in ("requirements", "method", "capability", "evaluation", "safety")},
        "findings": [], "required_changes": [], "controller_validation": {"valid": True, "errors": []}
    }
    conformance_path = case_root / "conformance.json"; conformance_path.write_text(json.dumps(conformance), encoding="utf-8")
    binding = bind(design, candidate, case_root / "implementation-binding.json", "test", approval_path=approval_path, stage="implementation", traceability_validation=validation_path, conformance_review=conformance_path)
    assert binding["stage"] == "implementation" and binding["traceability_id"]

    malformed = case_root / "malformed"; write(malformed / "SKILL.md", "---\nname: demo\ndescription: broken\n")
    try:
        bind(design, malformed, case_root / "bad-binding.json", "test", approval_path=approval_path)
    except ValueError as exc:
        assert "candidate validation failed" in str(exc)
    else:
        raise AssertionError("malformed candidate was bound")

    blocked_workspace = case_root / "blocked-workspace"
    completed = subprocess.run([sys.executable, str(HERE / "prepare_iteration.py"), "--skill", str(candidate), "--eval-set", str(eval_path), "--workspace", str(blocked_workspace), "--iteration", "1"], capture_output=True, text=True, check=False)
    assert completed.returncode != 0 and not blocked_workspace.exists()
    blocked_controller_workspace = case_root / "blocked-controller-workspace"
    completed = subprocess.run([sys.executable, str(HERE / "run_optimization.py"), "--skill", str(candidate), "--eval-set", str(eval_path), "--workspace", str(blocked_controller_workspace), "--adapter-command", "true"], capture_output=True, text=True, check=False)
    assert completed.returncode != 0 and not blocked_controller_workspace.exists()

    changed_path = case_root / "changed-design.md"
    changed_path.write_text(ready_design_text("demo", "high").replace("根据输入生成有证据的结论", "根据输入生成可追溯且有证据的结论"), encoding="utf-8")
    impact_path = case_root / "impact.json"
    completed = subprocess.run([sys.executable, str(HERE / "analyze_design_change.py"), str(design_path), str(changed_path), "--old-traceability", str(trace_path), "--output", str(impact_path)], capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr
    impact = json.loads(impact_path.read_text(encoding="utf-8"))
    assert impact["gate"] == "revalidation_required" and "REQ-001" in impact["changes"]["changed"]


def test_review_controller(root: Path) -> None:
    case_root = root / "review-controller"; case_root.mkdir()
    design_path = case_root / "design.md"; design_path.write_text(ready_design_text("reviewed-skill"), encoding="utf-8")
    adapter = case_root / "adapter.py"
    write(adapter, '''import json,sys
job=json.load(open(sys.argv[1], encoding="utf-8"))
value={"status":"pass","reviewer_id":job["reviewer_id"],"reviewer_independence":"verified","dimensions":{name:{"status":"pass","evidence":["specific evidence"]} for name in job["required_dimensions"]},"findings":[],"required_changes":[]}
json.dump(value,open(sys.argv[2],"w",encoding="utf-8"),ensure_ascii=False)
''')
    output = case_root / "review.json"
    completed = subprocess.run([sys.executable, str(HERE / "review_design.py"), "design", "--design", str(design_path), "--adapter-command", f"{sys.executable} {adapter}", "--reviewer-id", "reviewer", "--modifier-id", "author", "--output", str(output)], capture_output=True, text=True, check=False)
    assert completed.returncode == 0 and json.loads(output.read_text())["status"] == "pass", completed.stderr
    completed = subprocess.run([sys.executable, str(HERE / "review_design.py"), "design", "--design", str(design_path), "--adapter-command", f"{sys.executable} {adapter}", "--reviewer-id", "same", "--modifier-id", "same", "--output", str(case_root / "bad.json")], capture_output=True, text=True, check=False)
    assert completed.returncode != 0 and not (case_root / "bad.json").exists()


def test_anthropic_adapter(root: Path) -> None:
    creator = fake_anthropic_root(root)
    assert inspect_root(creator)["status"] == "available"
    skill = root / "candidate"
    write(skill / "SKILL.md", "---\nname: demo\ndescription: demo skill\n---\n")
    adapter = HERE / "anthropic_skill_creator.py"
    for command in (
        [sys.executable, str(adapter), "--root", str(creator), "validate", "--skill", str(skill)],
        [sys.executable, str(adapter), "--root", str(creator), "package", "--skill", str(skill), "--output-dir", str(root / "packages")],
    ):
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        assert completed.returncode == 0, completed.stderr + completed.stdout
    artifact = root / "artifact.md"
    artifact.write_text("answer", encoding="utf-8")
    benchmark = {
        "schema_version": "2.0",
        "runs": [{"case_id": "train-1", "configuration": "candidate", "run_number": 1, "status": "pass", "score": 1.0, "artifacts": [str(artifact)], "expectations": [{"id": "done", "text": "done", "status": "pass", "severity": "major", "evidence": "answer"}]}],
        "configurations": {"candidate": {"runs": 1, "determinate_pass_rate": 1.0, "status_counts": {"pass": 1}, "score": {"mean": 1.0}}},
    }
    benchmark_path = root / "benchmark.json"
    eval_path = root / "viewer-eval.json"
    benchmark_path.write_text(json.dumps(benchmark), encoding="utf-8")
    eval_path.write_text(json.dumps(minimal_eval()), encoding="utf-8")
    exported = export_viewer_workspace(benchmark_path, eval_path, root / "compat")
    assert exported["exported_runs"] == 1
    static = root / "review.html"
    completed = subprocess.run([
        sys.executable, str(adapter), "--root", str(creator), "viewer",
        "--benchmark", str(benchmark_path), "--eval-set", str(eval_path),
        "--workspace", str(root / "compat-cli"), "--static", str(static), "--skill-name", "demo",
    ], capture_output=True, text=True, check=False)
    assert completed.returncode == 0 and static.is_file(), completed.stderr + completed.stdout
    trigger, excluded = trigger_eval_data({"cases": [
        {"mode": "trigger", "split": "train", "prompt": "do it", "tags": ["should-trigger"]},
        {"mode": "trigger", "split": "regression", "prompt": "weather", "tags": ["should-not-trigger"]},
        {"mode": "trigger", "split": "holdout", "prompt": "hidden", "tags": ["should-trigger"]},
    ]})
    assert len(trigger) == 2 and excluded["holdout"] == 1


def test_dataset_holdout_and_professional_governance(root: Path) -> None:
    case_root = root / "治理 空间"; case_root.mkdir()
    eval_data = minimal_eval()
    eval_data["cases"][0]["prompt"] = "诊断 端口 模式 求解 失败"
    eval_data["cases"][1]["prompt"] = "请诊断端口模式求解失败"
    eval_path = case_root / "评测 集.json"; eval_path.write_text(json.dumps(eval_data, ensure_ascii=False), encoding="utf-8")
    report = audit_eval_set(eval_data, eval_path, threshold=0.6)
    assert report["status"] == "fail" and any(item["type"] == "semantic_holdout_leakage" for item in report["findings"])

    eval_data["cases"][1]["prompt"] = "检查网格资源异常"
    eval_path.write_text(json.dumps(eval_data, ensure_ascii=False), encoding="utf-8")
    vault = case_root / "vault"
    visible = case_root / "visible.json"
    created = create_vault(eval_path, vault, visible, 1)
    assert created["status"] == "sealed" and len(json.loads(visible.read_text())["cases"]) == 1
    token = vault / "token.txt"
    released = checkout_holdout(vault, token, "final_selection", "candidate-1", case_root / "checkout", visible)
    assert released["status"] == "released"
    assembled = json.loads(Path(released["eval_set"]).read_text())
    assert {case["split"] for case in assembled["cases"]} == {"train", "holdout"}
    state = holdout_status(vault, token)
    assert state["ledger_valid"] and state["remaining_final_accesses"] == 0
    try:
        checkout_holdout(vault, token, "final_selection", "candidate-2", case_root / "checkout-2")
    except ValueError as exc:
        assert "exhausted" in str(exc)
    else:
        raise AssertionError("holdout budget was bypassed")
    ledger = vault / "access-ledger.jsonl"
    ledger.write_text(ledger.read_text(encoding="utf-8").replace('"result": "success"', '"result": "tampered"', 1), encoding="utf-8")
    assert holdout_status(vault, token)["status"] == "tampered"

    evidence = case_root / "solver.log"; evidence.write_text("verified evidence", encoding="utf-8")
    suite = {
        "schema_version": "1.0", "domain": "hfss", "validation_class": "real",
        "policy": {"min_cases": 1, "min_independent_reviewers": 2, "max_false_certain_rate": 0.0, "min_observed_agreement": 1.0, "required_solver_types": ["DrivenModal"], "required_failure_families": ["ports"]},
        "cases": [{
            "case_id": "hfss-1", "provenance": {"kind": "expert_review", "source": "closed-ticket", "reviewers": ["expert-a", "expert-b"], "adjudication_status": "closed"},
            "environment": {"product_version": "2019R1", "solver_type": "DrivenModal"}, "failure_families": ["ports"],
            "evidence_files": ["solver.log"], "safety_expectations": ["do not invent evidence"],
            "expert_ratings": {"decisions": ["port", "port"]},
        }],
        "results": [{"case_id": "hfss-1", "root_cause_certainty": "certain", "root_cause_correct": True}],
    }
    professional = validate_suite(suite, case_root / "suite.json", True)
    assert professional["status"] == "pass" and professional["real_domain_claim_eligible"]
    suite["validation_class"] = "constructed"
    constructed = validate_suite(suite, case_root / "suite.json", False)
    assert not constructed["real_domain_claim_eligible"]


def test_grader_calibration(root: Path) -> None:
    case_root = root / "calibration"; case_root.mkdir()
    adapter = case_root / "adapter.py"
    write(adapter, '''import json,sys
job=json.load(open(sys.argv[1],encoding="utf-8")); expected=job["case"]["oracle"]["expected_outcomes"][0]
value={"job_id":job["job_id"],"status":expected,"score":1.0 if expected=="pass" else 0.0,"expectations":[{"id":"truth","status":expected,"severity":"critical","weight":1.0,"evidence":"golden artifact"}],"critical_failures":[] if expected=="pass" else ["truth"],"claims":[],"eval_feedback":[],"grader_id":"fixture-v1"}
json.dump(value,open(sys.argv[2],"w",encoding="utf-8"))
''')
    calibration = {
        "schema_version": "1.0", "policy": {"min_status_accuracy": 1.0, "min_expectation_accuracy": 1.0, "max_critical_false_negative_rate": 0.0, "max_score_mae": 0.0},
        "items": [
            {"id": "pass-case", "case": {"oracle": {"expected_outcomes": ["pass"]}}, "execution": {"status": "completed"}, "gold": {"status": "pass", "score": 1.0, "expectations": {"truth": "pass"}, "critical_failures": []}},
            {"id": "fail-case", "case": {"oracle": {"expected_outcomes": ["fail"]}}, "execution": {"status": "completed"}, "gold": {"status": "fail", "score": 0.0, "expectations": {"truth": "fail"}, "critical_failures": ["truth"]}},
        ],
    }
    calibration_path = case_root / "set.json"; calibration_path.write_text(json.dumps(calibration), encoding="utf-8")
    output = case_root / "report.json"
    completed = subprocess.run([sys.executable, str(HERE / "calibrate_grader.py"), str(calibration_path), "--adapter-command", f"{sys.executable} {adapter}", "--adapter-command", f"{sys.executable} {adapter}", "--output", str(output)], capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr + completed.stdout
    report = json.loads(output.read_text())
    assert report["status"] == "pass" and report["metrics"]["critical_false_negative_rate"] == 0.0 and report["inter_grader"]["graders"] == 2


def test_automatic_assurance_refresh_loop(root: Path) -> None:
    case_root = root / "automatic-assurance"; case_root.mkdir()
    candidate = case_root / "demo"
    write(candidate / "SKILL.md", "---\nname: demo\ndescription: evidence-based demo\n---\n\nReturn an evidenced result.\n")
    design_path = case_root / "skill-design.md"; design_path.write_text(ready_design_text("demo", "high"), encoding="utf-8")
    design, approval_path = make_approval(design_path, case_root)
    eval_data = minimal_eval({
        "max_iterations": 2, "require_design_binding": True, "require_design_review": True,
        "require_traceability": True, "require_conformance_review": True, "auto_refresh_assurance": True,
        "require_identity_binding": True, "require_environment_binding": True,
    })
    eval_data["cases"][0]["prompt"] = "produce initial evidence"
    eval_data["cases"][1]["prompt"] = "solve unseen boundary"
    for case in eval_data["cases"]:
        case["expectations"] = [{"id": "fixed", "text": "candidate implements the fix", "kind": "semantic", "severity": "critical", "weight": 1.0}]
    eval_path = case_root / "eval.json"; eval_path.write_text(json.dumps(eval_data), encoding="utf-8")
    approval = validate_approval(design, approval_path)
    trace = {
        "schema_version": "1.0", "design_id": design["design_id"], "approval_id": approval["approval_id"],
        "candidate_id": tree_hash(candidate), "eval_set_id": file_hash(eval_path),
        "mappings": [{"design_item_id": item["id"], "implementation": [{"kind": "file", "path": "SKILL.md", "locator": item["id"]}], "evaluations": [{"case_id": "train-1", "expectation_ids": ["fixed"]}]} for item in design["design_items"]],
    }
    trace_path = case_root / "traceability.json"; trace_path.write_text(json.dumps(trace), encoding="utf-8")
    validation = validate_traceability(trace_path, design_path, approval_path, candidate, eval_path)
    validation_path = case_root / "traceability-validation.json"; validation_path.write_text(json.dumps(validation), encoding="utf-8")
    conformance = {
        "schema_version": "1.0", "review_type": "conformance", "status": "pass", "design_id": design["design_id"], "approval_id": approval["approval_id"],
        "candidate_id": tree_hash(candidate), "eval_set_id": file_hash(eval_path), "reviewer_id": "reviewer", "reviewer_independence": "verified",
        "dimensions": {name: {"status": "pass", "evidence": ["initial evidence"]} for name in ("requirements", "method", "capability", "evaluation", "safety")},
        "findings": [], "required_changes": [], "controller_validation": {"valid": True, "errors": []},
    }
    conformance_path = case_root / "conformance.json"; conformance_path.write_text(json.dumps(conformance), encoding="utf-8")
    adapter = case_root / "adapter.py"
    write(adapter, '''import json,pathlib,shutil,sys
job=json.load(open(sys.argv[1],encoding="utf-8")); out=pathlib.Path(sys.argv[2]); out.parent.mkdir(parents=True,exist_ok=True); kind=job["job_type"]
if kind=="execute":
    target=pathlib.Path(job["output_dir"]); target.mkdir(parents=True,exist_ok=True); skill=pathlib.Path(job["skill_path"]) if job.get("skill_path") else None; fixed=bool(skill and "FIXED" in (skill/"SKILL.md").read_text(encoding="utf-8")); transcript=target/"transcript.md"; transcript.write_text("FIXED" if fixed else "BROKEN",encoding="utf-8"); value={"job_id":job["job_id"],"status":"completed","transcript_path":str(transcript),"artifacts":[str(transcript)],"metrics":{"duration_seconds":1,"tokens":10,"tool_calls":0,"errors":0},"model_id":"fixture","environment_id":"fixture-env","execution_independence":"verified"}
elif kind=="grade":
    fixed="FIXED" in pathlib.Path(job["execution"]["transcript_path"]).read_text(encoding="utf-8"); state="pass" if fixed else "fail"; value={"job_id":job["job_id"],"status":state,"score":1.0 if fixed else 0.0,"expectations":[{"id":"fixed","status":state,"severity":"critical","weight":1.0,"evidence":"transcript"}],"critical_failures":[] if fixed else ["fixed"],"claims":[],"eval_feedback":[]}
elif kind=="attribute": value={"job_id":job["job_id"],"status":"attributed","responsible_layer":"instruction","confidence":1.0,"observed_failure":"missing fix","evidence":["BROKEN transcript"],"alternatives":[],"discriminating_check":"inspect SKILL.md","recommended_change_target":"SKILL.md"}
elif kind=="modify":
    skill=pathlib.Path(job["target_skill_path"])/"SKILL.md"; skill.write_text(skill.read_text(encoding="utf-8")+"\\nFIXED\\n",encoding="utf-8"); value={"job_id":job["job_id"],"status":"modified","changed_files":["SKILL.md"],"change_summary":"add fix","similar_defect_check":"checked","opposite_case":"preserved","validation":["ok"]}
elif kind=="review_conformance": value={"job_id":job["job_id"],"status":"pass","reviewer_id":job["reviewer_id"],"reviewer_independence":"verified","dimensions":{name:{"status":"pass","evidence":["candidate inspected"]} for name in job["required_dimensions"]},"findings":[],"required_changes":[]}
else: value={"job_id":job["job_id"],"winner":"A","confidence":1.0,"evidence":["fixture"],"critical_difference":False}
json.dump(value,open(out,"w",encoding="utf-8"),ensure_ascii=False)
''')
    workspace = case_root / "workspace"
    completed = subprocess.run([
        sys.executable, str(HERE / "run_optimization.py"), "--skill", str(candidate), "--eval-set", str(eval_path), "--design", str(design_path), "--approval", str(approval_path),
        "--traceability", str(trace_path), "--traceability-validation", str(validation_path), "--conformance-review", str(conformance_path),
        "--assurance-reviewer-id", "reviewer", "--modifier-id", "modifier", "--workspace", str(workspace), "--adapter-command", f"{sys.executable} {adapter}", "--workers", "1",
    ], capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr + completed.stdout
    history = json.loads((workspace / "history.json").read_text())
    assert history["status"] == "candidate_is_best"
    latest = Path(history["recommended_candidate_path"])
    assert history["current_assurance"]["candidate_id"] == tree_hash(latest)
    assert history["current_assurance"]["implementation_binding_id"]
    packet = build_promotion_packet(workspace, latest, dataset_audit=None, grader_calibration=None, professional_validation=None, holdout_status=None)
    assert packet["status"] == "ready_for_human_review"
    html = render_assurance_view(workspace, packet)
    assert "Design → implementation → evaluation" in html and "ready_for_human_review" in html


def test_recovery_guard(root: Path) -> None:
    workspace = root / "recovery-source"
    write(workspace / "candidates" / "iteration-1" / "SKILL.md", "---\nname: demo\ndescription: demo\n---\n")
    write(workspace / "eval" / "eval-set.json", json.dumps(minimal_eval()))
    history = {"status": "error", "formal_skill_content_id": "missing", "eval_set_id": "eval", "iterations": [{"iteration": 1, "holdout_runs": 1}]}
    write(workspace / "history.json", json.dumps(history))
    completed = subprocess.run([sys.executable, str(HERE / "develop_skill.py"), "resume", "--workspace", str(workspace), "--successor-workspace", str(root / "successor"), "--adapter-command", "true"], capture_output=True, text=True, check=False)
    assert completed.returncode != 0 and "replacement eval set" in completed.stderr


def test_retry_classification(root: Path) -> None:
    case_root = root / "retry"; case_root.mkdir()
    adapter_path = case_root / "adapter.py"
    write(adapter_path, f'''import json,pathlib,sys
job=json.load(open(sys.argv[1],encoding="utf-8")); counter=pathlib.Path({str(case_root)!r})/(job["job_type"]+".count"); count=int(counter.read_text()) if counter.exists() else 0; counter.write_text(str(count+1))
if count==0: sys.exit(75)
json.dump({{"job_id":job["job_id"],"status":"pass"}},open(sys.argv[2],"w",encoding="utf-8"))
''')
    adapter = Adapter([sys.executable, str(adapter_path)], 10, case_root / "jobs", max_retries=2)
    response = adapter.call({"job_id": "grade:retry", "job_type": "grade"}, Path("grade"))
    assert response["status"] == "pass" and (case_root / "grade.count").read_text() == "2"
    try:
        adapter.call({"job_id": "modify:no-retry", "job_type": "modify"}, Path("modify"))
    except AdapterCallError as exc:
        assert exc.failure_class == "transient-exit-75" and not exc.retryable
    else:
        raise AssertionError("modify was retried despite mutation risk")
    assert (case_root / "modify.count").read_text() == "1"


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="developing-skills-self-test-") as temporary:
        root = Path(temporary)
        test_sign_gate_boundaries()
        test_policy_validation_boundaries(root)
        test_discriminating_checks(root)
        test_design_gate(root)
        test_design_initialization(root)
        test_design_assurance_and_policy_bypass(root)
        test_review_controller(root)
        test_anthropic_adapter(root)
        test_dependency_bootstrap(root)
        test_dependency_update_audit(root)
        test_dataset_holdout_and_professional_governance(root)
        test_grader_calibration(root)
        test_automatic_assurance_refresh_loop(root)
        test_recovery_guard(root)
        test_retry_classification(root)
    print("developing-skills self-test: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
