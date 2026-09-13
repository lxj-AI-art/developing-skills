# developing-skills

[中文](README.zh-CN.md) | English

[![CI](https://github.com/lxj-AI-art/developing-skills/actions/workflows/ci.yml/badge.svg)](https://github.com/lxj-AI-art/developing-skills/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue.svg)](.github/workflows/ci.yml)

An engineering workflow for creating and improving professional Agent Skills from requirements, domain material, execution records, and labeled evaluation cases.

developing-skills combines method design, version-bound design assurance, isolated candidate optimization, failure attribution, holdout governance, grader calibration, and human-controlled promotion. It is not merely a prompt template or a wrapper around another skill creator.

Current positioning: **high-assurance L2 release candidate**. It can autonomously modify and evaluate isolated candidate copies, while publication and replacement of a formal Skill remain human decisions.

## What it provides

| Area | Capability |
| --- | --- |
| Skill design | Turns requirements and domain evidence into observable outcomes, method decisions, capability assignments, safety boundaries, and Eval criteria |
| Design assurance | Stable design-item IDs, independent semantic review, external version-bound approval, traceability, conformance review, and change-impact analysis |
| Evaluation | Labeled-case schema, five-state outcomes, deterministic/domain/semantic grading, repeated runs, benchmark aggregation, and paired candidate comparison |
| Diagnosis | Attributes failures across requirement, method, knowledge, retrieval, tool, runtime, instruction, and scoring layers |
| Optimization | Modifies only isolated candidate copies, validates the candidate, reruns regression, refreshes assurance evidence, and selects the best supported version |
| Overfitting control | Training/regression/holdout separation, governed holdout vault, access budget, exposure invalidation, leakage and duplicate-case audits |
| Production governance | Resumable successor workspaces, immutable evidence, grader calibration, real-domain acceptance gates, assurance Viewer, and non-publishing promotion packets |
| Provider integration | Generic runtime-adapter protocol, CodeAgent adapter, and an optional pinned private Anthropic skill-creator provider |

## Operating model

The workflow scales with risk. A narrow, low-risk correction can use a lightweight path. New professional Skills and changes to methods, safety, outputs, or evaluation criteria use the full assurance path:

1. Define observable outcomes and non-goals.
2. Build an evidence-based method and assign capabilities.
3. Validate the structured design.
4. Obtain an independent design review.
5. Create an external approval bound to the exact design ID.
6. Initialize and implement an isolated candidate.
7. Map design items to implementation locations and Eval expectations.
8. Run independent conformance review and bind the exact candidate.
9. Execute, grade, attribute failures, and make the smallest responsible change.
10. Refresh traceability and conformance after every candidate change.
11. Run regression and governed holdout evaluation.
12. Generate a promotion packet for human approval.

Changing a design or candidate invalidates evidence bound to its previous content identity. The system does not relabel old evidence as current.

## Requirements

- Python 3.11 or 3.12
- A host that can load Agent Skills
- For automated behavioral evaluation: an adapter implementing the runtime protocol
- Git for the installation method below

Core deterministic scripts use the Python standard library. External Agent runtimes and the optional Anthropic provider are configured separately.

## Installation

### Linux and macOS

    git clone https://github.com/lxj-AI-art/developing-skills.git \
      "${CODEX_HOME:-$HOME/.codex}/skills/developing-skills"

### Windows PowerShell

    git clone https://github.com/lxj-AI-art/developing-skills.git "$HOME\.codex\skills\developing-skills"

Restart or refresh the host's Skill discovery after installation.

Validate the installed copy:

    python scripts/validate_candidate.py .
    python scripts/self_test.py

Expected final line:

    developing-skills self-test: PASS

## Invoke the Skill

In a compatible host, invoke it explicitly or let normal Skill discovery select it:

    Use $developing-skills to design a professional Skill from these
    requirements, then build labeled Evals and improve an isolated candidate.

The Skill supports four common starting modes:

- **Create:** start from business requirements.
- **Extract:** derive a reusable method from cases or demonstrations.
- **Improve:** diagnose and repair an existing Skill.
- **Labeled optimization:** evaluate and iteratively improve a Skill from cases with known outcomes.

## Quick start

All control functions are available through scripts/develop_skill.py.

### 1. Create a design

    python scripts/develop_skill.py design init \
      --skill-name example-skill \
      --change-class new \
      --risk-level high \
      --output work/skill-design.md

Complete the generated design, set it to ready, and validate it:

    python scripts/develop_skill.py design validate \
      work/skill-design.md --require-ready

For substantial or high-risk work, run an independent design review and create an external approval before initialization. See [Design framework and gates](references/design-framework.md).

### 2. Audit a labeled Eval set

    python scripts/develop_skill.py audit-data eval-set.json \
      --similarity-threshold 0.9 \
      --output work/dataset-audit.json

The audit checks schema quality, semantic near-duplicates, train/holdout leakage, conflicting labels, expectation coverage, provenance, and risk coverage.

### 3. Run an optimization loop

    python scripts/develop_skill.py run \
      --skill /absolute/path/to/formal-skill \
      --eval-set /absolute/path/to/eval-set.json \
      --workspace /absolute/path/to/new-workspace \
      --adapter-command "python /absolute/path/to/adapter.py" \
      --baseline-mode original

The controller appends job.json and response.json paths to the adapter command. The adapter must support execute, grade, attribute, modify, and compare. High-assurance automatic refresh additionally requires review_conformance.

The formal Skill is snapshotted and never modified by this command. Candidate edits happen only under the new workspace.

### 4. Inspect status or resume safely

    python scripts/develop_skill.py status --workspace work/run-001

    python scripts/develop_skill.py resume \
      --workspace work/run-001 \
      --successor-workspace work/run-002 \
      --adapter-command "python /absolute/path/to/adapter.py"

Resume always creates a successor workspace and records lineage. If the source run consumed holdout access, a replacement Eval set is required.

### 5. Prepare human promotion review

    python scripts/develop_skill.py promotion-packet \
      --workspace work/run-002 \
      --candidate work/run-002/candidates/iteration-2 \
      --output work/promotion-packet.json

    python scripts/develop_skill.py viewer \
      --workspace work/run-002 \
      --promotion-packet work/promotion-packet.json \
      --output work/assurance-view.html

The packet is identity-bound and non-publishing. It always requires human approval.

## High-assurance optimization

Provide exact design, approval, traceability, and conformance evidence:

    python scripts/develop_skill.py run \
      --skill /path/to/formal-skill \
      --design /path/to/skill-design.md \
      --approval /path/to/design-approval.json \
      --traceability /path/to/traceability.json \
      --traceability-validation /path/to/traceability-validation.json \
      --conformance-review /path/to/conformance-review.json \
      --assurance-reviewer-id independent-reviewer \
      --modifier-id candidate-modifier \
      --eval-set /path/to/eval-set.json \
      --workspace /path/to/new-workspace \
      --adapter-command "python /path/to/adapter.py"

After a modification, the controller rebuilds traceability validation, invokes an independent conformance review, creates a new implementation binding, and reruns the complete training/regression set before holdout access.

See [Production assurance, recovery, and governance](references/production-assurance.md).

## Unified command reference

| Command | Purpose |
| --- | --- |
| design | Initialize, validate, approve, bind, initialize, or hand off a version-bound design |
| run | Execute the isolated evaluation and optimization loop |
| assure | Refresh traceability and conformance for an exact changed candidate |
| audit-data | Detect duplicate cases, leakage, conflicts, and coverage gaps |
| holdout | Create, inspect, or check out from a governed local holdout vault |
| calibrate-grader | Compare grader results with expert golden decisions |
| validate-professional | Gate real-domain claims using expert-adjudicated cases |
| audit-dependency | Audit a staged optional-provider update without installing it |
| status | Inspect candidate identity, evidence consistency, and recovery readiness |
| resume | Create an evidence-preserving successor optimization run |
| promotion-packet | Build an identity-bound packet for human review; never publishes |
| viewer | Render a standalone HTML assurance view |

Use:

    python scripts/develop_skill.py --help
    python scripts/develop_skill.py <command> --help

## Runtime adapter

The optimizer is runtime-neutral. A caller supplies a command prefix, and the controller invokes it without a shell:

    <adapter-command> <job.json> <response.json>

The protocol separates execution from grading and hides labels from execution jobs. It records model, environment, candidate, Eval, design, approval, traceability, and conformance identities where applicable.

See:

- [Runtime adapter protocol](references/runtime-adapter.md)
- [CodeAgent adapter configuration](references/codeagent-adapter-config.md)

This release does not add first-class native adapters for Codex, Claude Code, OpenAI API, or Anthropic API. They can be connected through the protocol when an appropriate adapter is supplied.

## Optional Anthropic skill-creator provider

Anthropic skill-creator is recommended but not required. It is installed privately under dependencies/ so it does not compete with a host's global skill-creator.

Review the plan first:

    python scripts/bootstrap_dependencies.py plan

After the user explicitly approves the displayed source, immutable commit, license, and target:

    python scripts/bootstrap_dependencies.py install \
      --confirm-external-install

Or skip it:

    python scripts/bootstrap_dependencies.py install \
      --without-anthropic

Verify:

    python scripts/bootstrap_dependencies.py verify
    python scripts/anthropic_skill_creator.py detect

The manifest pins the official anthropics/skills repository, a full commit SHA, expected content hash, license, and required capabilities. Installed content is rehashed before use and is not updated automatically.

See [Recommended dependency installation](references/dependency-installation.md).

## Evidence and safety invariants

- Formal Skills are not overwritten during optimization.
- Workspaces and result records are append-oriented; retries do not overwrite unique evidence.
- Design approval is external and bound to the exact design ID.
- Traceability and conformance evidence are bound to the exact candidate ID and Eval-set ID.
- Holdout labels are not sent to the modifier.
- Exposed holdout cases are demoted to regression and cannot remain blind evidence.
- Automatic retries apply only to explicitly transient read-only jobs; execute and modify are not automatically retried.
- Generated promotion packets never publish or replace a Skill.
- External downloads require explicit user confirmation.

## Workspace outputs

A run records the frozen Eval, candidate generations, adapter jobs, executions, grading, attribution, assurance refreshes, benchmarks, selection, and history:

    <workspace>/
    ├── design/
    ├── eval/
    ├── baseline/
    ├── candidates/iteration-N/
    ├── jobs/iteration-N/
    ├── runs/iteration-N/
    ├── assurance/iteration-N/
    ├── iterations/iteration-N/
    ├── benchmark.json
    ├── selection.json
    └── history.json

Exact contents depend on the selected assurance and governance policy.

## Validation and CI

Every push and pull request runs the deterministic suite on:

- Ubuntu, macOS, and Windows
- Python 3.11 and 3.12

The workflow compiles all scripts, validates the Skill package, and runs scripts/self_test.py.

Local verification:

    python -m compileall -q scripts
    python scripts/validate_candidate.py .
    python scripts/self_test.py

## Scope and limitations

- L2 means autonomous candidate iteration in isolation, not autonomous production publishing.
- The local holdout vault provides access governance and tamper-evident auditing, not hard isolation from the same operating-system user.
- Real-domain quality claims require traceable, closed, expert-adjudicated cases. Constructed cases prove workflow behavior only.
- No real HFSS case corpus or expert business acceptance is bundled with this repository.
- An external adapter and its model/tool permissions determine whether behavioral evaluation can actually run.
- Structural validation and passing self-tests do not by themselves prove the quality of a newly developed domain Skill.

## Documentation

| Document | Use it for |
| --- | --- |
| [Skill instructions](SKILL.md) | Agent-facing workflow and routing |
| [Design framework and gates](references/design-framework.md) | Requirements, method, approval, traceability, and conformance |
| [Method and capability design](references/method-design.md) | Turning evidence and cases into a reusable decision method |
| [Eval schema](references/eval-schema.md) | Labeled-case and result contracts |
| [Evaluation and attribution](references/evaluation.md) | Execution, scoring, comparison, and evidence interpretation |
| [Labeled-case optimization](references/labeled-case-optimization.md) | Iterative optimization controller behavior |
| [Runtime adapter protocol](references/runtime-adapter.md) | Adapter jobs and responses |
| [Production assurance](references/production-assurance.md) | Assurance refresh, holdout, calibration, recovery, Viewer, and promotion |
| [Anthropic integration](references/anthropic-skill-creator-integration.md) | Provider capabilities and handoff behavior |
| [Dependency installation](references/dependency-installation.md) | Pinned private provider installation and audit |
| [Work record](references/work-record.md) | Compact evidence record for complex or multi-turn work |

## Contributing

Keep changes scoped to observable behavior. For script changes, add or update meaningful deterministic coverage and run the complete validation commands above. Do not commit dependencies/, generated workspaces, holdout tokens, provider downloads, or cache files.

## License

Licensed under the [Apache License 2.0](LICENSE).
