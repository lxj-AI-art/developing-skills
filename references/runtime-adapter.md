# Agent 运行时适配协议

`run_optimization.py` 不绑定某个模型、CLI 或 Agent 平台。调用方提供一个适配器命令；控制器每次在命令末尾追加 `<job.json> <response.json>`。适配器读取任务、完成对应动作并原子写入 JSON 响应。控制器使用参数数组启动进程，不经过 shell。

```bash
python <developing-skills>/scripts/run_optimization.py \
  --skill <target-skill> \
  --eval-set <eval-set.json> \
  --workspace <new-workspace> \
  --adapter-command "python /absolute/path/adapter.py"
```

适配器必须支持五种核心 `job_type`：`execute`、`grade`、`attribute`、`modify`、`compare`。启用高保证自动刷新时还必须支持 `review_conformance`；该任务读取设计、候选、Eval 和已验证追踪，输出五维独立审查，不得修改候选。执行和评分任务可以并行到达，适配器不得依赖调用顺序或共享可变会话。可直接使用 [CodeAgent 适配器](codeagent-adapter-config.md) 连接参数数组形式的 CLI；本轮没有增加新的平台原生适配器。

## execute

输入只包含自然任务、原始文件、Skill 副本路径、输出目录和运行身份；不含原案例 ID、`split`、oracle、expectations、归因、tags 或其他评分元数据。控制器用不可逆短哈希提供 `case.ref` 并构造路径，避免语义化 ID 泄漏答案。`skill_path` 为 `null` 表示无 Skill 基线。`case.mode` 默认为 `execution`；若为 `trigger`，适配器应把候选放入该次隔离运行的可发现位置，但不得显式指定或预加载它，再用自然 prompt 观察实际激活。

```json
{
  "job_type": "execute",
  "job_id": "execute:1:case-001:candidate:1",
  "case": {"ref": "a1b2c3d4e5f60708", "mode": "execution", "prompt": "用户任务", "files": [], "risk": "medium"},
  "configuration": "candidate",
  "skill_path": "/isolated/candidates/iteration-1",
  "candidate_id": "sha256",
  "eval_set_id": "sha256",
  "design_id": "sha256-of-ready-design-or-null",
  "approval_id": "sha256-of-version-bound-approval-or-null",
  "traceability_id": "sha256-of-passing-traceability-validation-or-null",
  "conformance_review_id": "sha256-of-passing-conformance-review-or-null",
  "run_number": 1,
  "output_dir": "/workspace/runs/...",
  "constraints": {"labels_hidden": true, "do_not_self_grade": true}
}
```

响应：

```json
{
  "job_id": "execute:1:case-001:candidate:1",
  "status": "completed",
  "transcript_path": "/workspace/runs/.../transcript.md",
  "artifacts": ["/workspace/runs/.../outputs/result.md"],
  "metrics": {"duration_seconds": 4.2, "tokens": 1200, "tool_calls": 2, "errors": 0},
  "model_id": "model-name-and-version",
  "environment_id": "runtime-tool-profile",
  "execution_independence": "verified"
}
```

这些设计保证标识只用于绑定执行依据，不能包含设计正文或留出答案；自动修改后的候选不得沿用旧 candidate_id 的追踪或一致性结论。`status` 为 `completed` 或 `blocked`。blocked 仍须给模型、环境和独立性身份，并可在 `error` 说明原因。`verified` 只用于确实有独立上下文或等价隔离的运行；仅靠提示要求不得填写 verified。

trigger 响应还必须给出实际加载观测，例如 `"activation": {"observed": true, "evidence": "runtime trace ..."}`。环境若不能观测加载，返回 blocked；不能以输出像是用了 Skill 或 description 含关键词作为加载证据。

## grade

输入包含冻结案例的 oracle、expectations 和已完成执行结果。评分器必须检查实际产物，不得修改候选。响应提供评分核心，控制器补齐案例、候选、环境和运行身份后写入 `grading.json`。

```json
{
  "job_id": "grade:1:case-001:candidate:1",
  "status": "fail",
  "score": 0.5,
  "expectations": [{"id": "safe", "status": "fail", "severity": "critical", "weight": 2, "evidence": "可观察证据"}],
  "critical_failures": ["safe"],
  "claims": [],
  "eval_feedback": []
}
```

状态只能是 `pass`、`fail`、`inconclusive`、`blocked` 或 `invalid_case`。不可判定状态的 `score` 应为 `null`。

## attribute

只对 candidate 的训练/回归失败调用。输入含案例标签、执行、评分、候选副本以及八层枚举；不得直接修改文件。

```json
{
  "job_id": "attribute:1:case-001:candidate:1",
  "status": "attributed",
  "responsible_layer": "instruction",
  "confidence": 0.9,
  "observed_failure": "危险操作被直接执行",
  "evidence": ["Skill 未定义该边界的行动"],
  "alternatives": ["method"],
  "discriminating_check": "检查资料是否已有明确规则"
}
```

无法区分时返回 `status: inconclusive`、0–1 的 `confidence` 和下一项区分性检查。内置安全检查支持 `file_exists`、`text_contains`、`text_regex`、`json_path_exists`、`json_path_equals` 和 `python_ast_symbol_exists`，且只能读取 skill/output/input 根内的相对路径；不执行归因器生成的任意命令。支持自动检查的适配器可安全执行后再次归因；仍不能闭合则停止修改。闭合结果还必须给 `recommended_change_target`。`requirement`、`runtime`、`scoring` 归因会停止候选正文修改；评分变化后必须重跑所有版本。

## modify

输入只包含训练/回归案例及已关闭归因，目标是下一轮候选副本。没有正式 Skill 路径，也没有留出案例、输出或分数。适配器只可修改 `target_skill_path`，优先处理一个主要责任层，并实际校验改动。

```json
{
  "job_id": "modify:1:to:2",
  "status": "modified",
  "changed_files": ["SKILL.md"],
  "change_summary": "补充危险操作的证据门禁",
  "similar_defect_check": "检查同类外部写入和删除分支",
  "opposite_case": "保留已获授权的安全只读请求",
  "validation": ["结构校验通过"]
}
```

也可返回 `no_change` 或 `blocked`。控制器会重新计算内容标识，并在每轮前后验证正式 Skill 的内容标识未变。运行适配器仍应由宿主沙箱限制写权限；内容标识校验是侦测，不是权限隔离。

## compare

输入包含冻结案例和随机标记为 A/B 的复制产物，不包含 candidate/baseline 身份。响应为 `winner: A|B|tie|inconclusive`、0–1 的 `confidence`、证据数组和是否存在关键差异。推荐同时返回 A/B 各自的 `content`、`structure` 评分、优缺点和逐 expectation 对照；控制器校验并保存这些可选字段，在裁决后才写入 A/B 映射，并汇总胜率与关键基线胜出数。

## review_conformance

输入包含已审批 design、精确 candidate、冻结 eval、traceability validation、控制器分配的 reviewer 身份和修改者身份。返回 `status`、`reviewer_id`、`reviewer_independence`，以及 requirements/method/capability/evaluation/safety 五个带证据维度。reviewer 必须不同于 modifier；高保证刷新只接受 `verified` 或 `human` 独立性。控制器补齐 candidate/eval/approval 标识并再次确定性验证。

## 故障分类与重试

适配器进程退出码 `75` 表示明确的临时失败；其他非零退出默认为永久失败，响应缺失、格式错误或身份不匹配属于协议失败。`--adapter-retries` 只重试 read-only 的 grade/attribute/compare/review job。`execute` 可能已产生外部副作用，`modify` 可能已改变候选，因此永不自动重试；应保留现场并使用带 lineage 的后继工作区恢复。

## 控制器停止语义

- 训练/回归失败 → 归因；归因闭合且属于可编辑层 → 只复制并修改该责任层的下一候选。
- 训练/回归全部通过 → 才运行留出集。
- 留出失败或选择不确定 → 停止，不把留出材料回传修改器；需要新增训练证据后开新工作区。
- `candidate_is_best` 只是推荐，正式发布仍需人工授权。
- 任一任务协议错误、适配器失败、正式 Skill 内容变化 → 保存 `history.json` 后停止。
