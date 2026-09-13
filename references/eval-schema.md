# 标注与结果格式

脚本接受 JSON。路径相对 `eval-set.json` 所在目录解析。字段允许增加，但下面列出的必需字段和枚举必须保持稳定。

## eval-set.json

```json
{
  "schema_version": "2.0",
  "skill_name": "example-skill",
  "policy": {
    "max_iterations": 5,
    "default_repetitions": 1,
    "promotion": "manual",
    "min_train_delta": 0.0,
    "min_holdout_delta": 0.0,
    "max_regression_drop": 0.0,
    "max_candidate_stddev": 0.25,
    "require_effect_over_variance": true,
    "min_variance_pairs": 2,
    "min_effect_to_noise_ratio": 1.0,
    "require_sign_test": false,
    "min_non_tie_pairs": 5,
    "max_sign_test_pvalue": 0.05,
    "max_total_tokens": 100000,
    "max_candidate_tokens_ratio": 1.5,
    "run_blind_comparison": true,
    "require_blind_comparison": true,
    "min_blind_win_rate": 0.5,
    "fail_on_overfit_signal": true,
    "allow_unresolved_holdout": false,
    "require_identity_binding": true,
    "require_environment_binding": true,
    "require_design_binding": true,
    "require_design_review": true,
    "require_traceability": true,
    "require_conformance_review": true,
    "auto_refresh_assurance": true,
    "require_dataset_audit": true,
    "semantic_duplicate_threshold": 0.9,
    "require_grader_calibration": true,
    "require_professional_validation": false,
    "require_holdout_governance": true,
    "require_hard_holdout_isolation": false,
    "candidate_checks": [
      {"command": ["python", "scripts/self_test.py"], "timeout_seconds": 120, "expected_exit_code": 0}
    ]
  },
  "cases": [
    {
      "id": "case-001",
      "split": "train",
      "mode": "execution",
      "prompt": "完成一个真实用户任务",
      "files": ["files/input.json"],
      "risk": "high",
      "repetitions": 1,
      "tags": ["normal"],
      "oracle": {
        "expected_outcomes": ["应实现的业务结果"],
        "required_evidence": [
          {"statement": "必须使用的事实", "source": "input.json", "locator": "field.path"}
        ],
        "forbidden_claims": ["不得无证据声称的结论"],
        "allowed_uncertainty": ["缺少关键输入时允许不可判定"],
        "required_actions": ["必须给出的可执行行动"],
        "provenance": {
          "kind": "expert_review",
          "source": "review-record-17",
          "version": "2026-09-12",
          "reviewer": "domain-expert",
          "confidence": "confirmed"
        }
      },
      "expectations": [
        {
          "id": "evidence-grounded",
          "text": "结论由输入证据支持",
          "kind": "domain",
          "severity": "critical",
          "weight": 2.0
        },
        {
          "id": "artifact-created",
          "text": "生成结果文件",
          "kind": "deterministic",
          "severity": "critical",
          "weight": 1.0,
          "checker": {"type": "artifact_exists", "artifact": "final.md"}
        }
      ]
    }
  ]
}
```

枚举：

- `split`: `train`、`regression`、`holdout`；
- `mode`: `execution`（默认，显式使用候选完成任务）或 `trigger`（自然请求下观察是否自动加载）；
- `risk`: `low`、`medium`、`high`、`critical`；
- `provenance.kind`: `standard`、`expert_review`、`verified_case`、`observation`、`hypothesis`；
- `provenance.confidence`: `confirmed`、`high`、`medium`、`low`；
- `expectations.kind`: `deterministic`、`semantic`、`domain`；
- `expectations.severity`: `critical`、`major`、`minor`。

确定性 expectation 必须包含可执行 `checker`，支持 `execution_status`、`artifact_exists`、`artifact_text_contains`、`artifact_text_equals`、`artifact_text_not_contains`、`artifact_regex` 和 `artifact_json_path_equals`。确定性结果是评分事实，语义 Grader 不得覆盖。

成本字段可配置总预算以及 candidate/baseline 的 Token、耗时和工具调用比率。配置门禁但运行时没有返回测量值时，结果为不可判定，不得默认为零成本。启用方差门禁时需要成对重复观察；`lower_95` 是工程近似量，不应称为小样本统计显著。需要方向一致性的精确非参数门禁时启用 `require_sign_test`：只统计 holdout 成对差值中的胜负，平局不计，要求非平局对数达到 `min_non_tie_pairs` 且单侧精确符号检验 p 值不高于 `max_sign_test_pvalue`。启用盲比较时运行时还必须支持 `compare`。

新建或实质更新的专业 Skill 应同时启用四个 design assurance 字段。`require_design_review` 和 `require_traceability` 依赖 `require_design_binding`；`require_conformance_review` 依赖 `require_traceability`，校验器会拒绝不一致组合。控制器需要 ready design、版本审批、traceability validation 和 conformance review，并把相应标识写入候选元数据、逐次评分和 benchmark。任一标识缺失、混合或不再绑定最终 candidate/eval 时不得晋升。局部低风险修正可以不启用，但要记录设计边界未变化的依据。

`auto_refresh_assurance` 依赖 `require_conformance_review`。启用后，多轮控制器还需要追踪源和独立 reviewer；每次修改自动生成新 validation、conformance 与 implementation binding。四个 governance policy 字段要求 benchmark 中存在对应通过报告：数据集审计、grader 校准、真实专业验证及留出治理。`require_professional_validation` 只接受 `real_domain_claim_eligible: true`；构造套件不会通过此门禁。

每个案例至少要有一个 oracle 条目或 expectation。高风险、关键风险案例若真值只是 observation 或 hypothesis，校验器会警告；它们不能独立支持正式晋升。

`mode: trigger` 的案例必须在 `tags` 中且仅包含一个方向标签：`should-trigger` 或 `should-not-trigger`。执行适配器必须能观察真实加载行为；否则返回 blocked 或 inconclusive，不能用 description 文本匹配冒充触发成功。支持内置分层评分的适配器会把运行时观测与方向标签作为 critical 确定性结果。触发训练失败可以驱动候选 description 修改，触发留出案例仍不得暴露给修改器。

## grading.json

```json
{
  "schema_version": "2.0",
  "case_id": "case-001",
  "split": "train",
  "configuration": "candidate",
  "run_number": 1,
  "status": "fail",
  "score": 0.5,
  "expectations": [
    {
      "id": "evidence-grounded",
      "text": "结论由输入证据支持",
      "status": "fail",
      "severity": "critical",
      "weight": 2.0,
      "evidence": "输出未引用输入中的相关事实"
    }
  ],
  "critical_failures": ["evidence-grounded"],
  "claims": [],
  "eval_feedback": [],
  "failure_attribution": {
    "responsible_layer": "retrieval",
    "confidence": 0.8,
    "evidence": ["执行记录未检索对应资料"],
    "alternatives": ["knowledge", "instruction"],
    "discriminating_check": "确认资料存在并检查实际检索结果"
  },
  "metrics": {
    "duration_seconds": 20.1,
    "tokens": 3000,
    "tool_calls": 12,
    "errors": 0
  },
  "artifacts": ["outputs/report.md"],
  "candidate_id": "sha256-or-git-commit",
  "eval_set_id": "sha256-of-frozen-eval-set",
  "design_id": "sha256-of-ready-skill-design",
  "model_id": "executor-model-and-version",
  "environment_id": "frozen-runtime-and-tool-profile",
  "execution_independence": "verified"
}
```

`status` 使用 `pass`、`fail`、`inconclusive`、`blocked`、`invalid_case`。`score` 位于 0 到 1；blocked、inconclusive 和 invalid_case 不得偷偷按 0 分混入平均值，聚合结果分别计数。

## change-manifest.json

自动修改候选后另存变更记录，不放入目标 Skill 目录：

```json
{
  "iteration": 2,
  "design_id": "sha256-of-ready-skill-design",
  "parent_candidate_id": "candidate-v1",
  "new_candidate_id": "candidate-v2",
  "responsible_layers": ["instruction"],
  "source_cases": ["case-001"],
  "observed_failures": ["存在什么可观察失败"],
  "causal_evidence": ["为什么该层负责"],
  "changed_files": ["SKILL.md"],
  "change_summary": "完成的最小修改",
  "similar_defect_check": "同类缺陷检查结果",
  "opposite_case": "防止过修的相反案例",
  "holdout_exposed_to_modifier": false,
  "required_revalidation": ["case-001", "regression-*"],
  "status": "pending-eval"
}
```

`responsible_layers` 中的值只能是 `requirement`、`method`、`knowledge`、`retrieval`、`tool`、`runtime`、`instruction`、`scoring`。若包含 scoring，必须使所有旧评分失效并对候选和基线重评；若归因尚不可判定，不得生成修改清单。

执行器、评分器、归因器和修改器的逐任务输入输出见 [Agent 运行时适配协议](runtime-adapter.md)。执行 job 故意不包含 split、oracle 或 expectations；修改 job 故意不包含任何留出材料。

## selection.json

选择器输出：

```json
{
  "decision": "candidate_is_best",
  "requires_human_approval": true,
  "candidate": "candidate",
  "baseline": "baseline",
  "reasons": [],
  "failed_gates": [],
  "metrics": {}
}
```

`decision` 为 `candidate_is_best`、`keep_baseline` 或 `inconclusive`。选择结果不是发布授权。

## grader-calibration.json

校准输入使用 `schema_version: "1.0"` 和 `items`。每项包含 `id`、真实 `case`、既有 `execution` 以及只对校准控制器可见的 `gold`：`status`、期望项状态映射、critical failure ID 和可选 score。policy 可设置 `min_status_accuracy`、`min_expectation_accuracy`、`max_critical_false_negative_rate`、`max_score_mae`、`max_grader_disagreement_rate`、`max_status_accuracy_drop` 与 `max_critical_fn_rate_increase`。输出保存逐项差异、多个 grader 的分歧和相对基线漂移；grader 或评分规则版本变化后旧校准失效。

## professional-suite.json

专业套件使用 `schema_version: "1.0"`、`domain`、`validation_class`、policy、cases 和可选 results。真实案例必须给出 provenance kind/source/reviewers/adjudication、environment product_version/solver_type、failure_families、evidence_files 和 safety_expectations。results 用 `root_cause_certainty` 与 `root_cause_correct` 计算错误确定根因率。详细门禁见 [生产保证、恢复与治理](production-assurance.md)。
