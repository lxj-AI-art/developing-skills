# 生产保证、恢复与治理

用于高保证标注优化、候选晋升准备、执行中断恢复或真实专业案例验收。它不授权自动发布，也不把本地目录约束称为系统级隔离。

## 候选变更后的保证刷新

启用 `require_traceability`、`require_conformance_review` 和 `auto_refresh_assurance` 时，控制器需要追踪源与独立 reviewer：

```bash
python <developing-skills>/scripts/run_optimization.py \
  --skill <formal-skill> --design <skill-design.md> --approval <design-approval.json> \
  --traceability <traceability.json> \
  --traceability-validation <traceability-validation.json> \
  --conformance-review <conformance-review.json> \
  --assurance-reviewer-id <independent-reviewer> --modifier-id <modifier> \
  --eval-set <eval-set.json> --workspace <workspace> \
  --adapter-command '<adapter-command>'
```

候选每次改变后，控制器保留追踪映射、更新精确身份、重新检查所有真实路径和 Eval expectation，再调用 `review_conformance`。通过后生成新的 validation、conformance、implementation binding 和 change impact。旧候选证据保留；新候选不继承其标识。训练与回归仍完整重跑，避免把旧执行结果重新绑定到新内容。

若只需单独刷新：

```bash
python <developing-skills>/scripts/refresh_assurance.py \
  --design <design> --approval <approval> --traceability <traceability> \
  --candidate <candidate> --eval-set <eval-set> --output-dir <new-dir> \
  --adapter-command '<adapter>' --reviewer-id <reviewer> --modifier-id <modifier>
```

## 留出集治理

`holdout_vault.py` 把留出案例与可见开发集拆开，使用授权 token、HMAC 链式账本、用途和最终选择访问预算治理访问：

```bash
python <developing-skills>/scripts/holdout_vault.py create \
  --eval-set <full-eval.json> --vault <private-vault> \
  --visible-eval <train-regression.json> --max-final-accesses 1
python <developing-skills>/scripts/holdout_vault.py checkout \
  --vault <private-vault> --token-file <private-vault/token.txt> \
  --purpose final_selection --candidate-id <candidate-id> \
  --visible-eval <train-regression.json> --output <new-checkout>
python <developing-skills>/scripts/holdout_vault.py status \
  --vault <private-vault> --token-file <private-vault/token.txt> \
  --output <holdout-status.json>
```

带 `--visible-eval` 时，checkout 在新目录中组装一次性完整 Eval，重写可见与留出输入路径并写入 ledger entry。`modifier_debug` 会永久记录暴露，此后该 vault 不再支持最终选择。重复最终访问超过预算会被拒绝。token 文件不得放入目标 Skill、共享工作区或发送给修改器。

本地 vault 是治理与可审计隔离，不是跨进程的强安全边界；同一操作系统用户仍可能主动扫描文件。需要强隔离时，由独立服务或沙箱持有 vault/token，只向控制器返回最终执行结果和签名状态，并在状态证据中给出 `hard_isolation: true`。`require_hard_holdout_isolation` 会拒绝本地 vault，不能用文字声明绕过。

## Grader 校准

校准集保存原始 case、execution 和人工 golden 评分。适配器只看到评分任务，不看到 `gold`：

```bash
python <developing-skills>/scripts/calibrate_grader.py <calibration-set.json> \
  --adapter-command '<adapter-command>' --output <grader-calibration.json>
```

重复 `--adapter-command` 可校准多个独立 grader，并计算状态分歧率；`--baseline-report` 检查状态准确率下降和 critical 漏判率上升。报告至少计算状态准确率、expectation 准确率、critical false-negative rate 和 score MAE。高风险晋升建议将 `max_critical_false_negative_rate` 设为 0；grader、模型或评分提示改变后重新校准。校准失败不能用增加 Skill 专用口令修复，应修 grader 或评分定义，并重评候选和基线。

## 专业案例验收

真实领域质量声明需要独立于 Eval 通过率的准入报告：

```bash
python <developing-skills>/scripts/validate_professional_suite.py \
  <professional-suite.json> --check-files --output <professional-validation.json>
```

`validation_class: real` 要求可追溯专家/已关闭案例、独立 reviewer、已关闭裁决、产品版本、solver 类型、证据文件和安全断言。报告计算专家观察一致率和错误确定根因率。`constructed` 只能证明流程，会明确输出 `real_domain_claim_eligible: false`。

HFSS 套件至少按求解类型、软件版本、失败家族、缺失/冲突证据和错误确定性风险组织。没有用户提供的真实封闭案例与专家裁决时，只能交付框架，不能声称已完成 HFSS 业务质量验证。

## 数据集生命周期

在优化前运行：

```bash
python <developing-skills>/scripts/audit_eval_set.py <eval-set.json> \
  --similarity-threshold 0.9 --benchmark <benchmark.json> \
  --output <dataset-audit.json>
```

审计检查语义近重复、跨留出泄漏、相似输入的标签冲突、expectation 覆盖、风险与 provenance；带 benchmark 时还按 case/tag 聚合候选失败，给出补充反例或独立来源案例的 active-learning 建议。critical 泄漏或冲突必须先修数据；暴露过的 holdout 转 regression，新补 holdout 重新密封并产生新 vault ID。不要把重复运行或近重复案例当作独立覆盖。

## 恢复、Viewer 与晋升包

统一入口：

```bash
python <developing-skills>/scripts/develop_skill.py status --workspace <workspace>
python <developing-skills>/scripts/develop_skill.py resume \
  --workspace <failed-workspace> --successor-workspace <new-workspace> \
  --adapter-command '<adapter-command>'
```

同一入口还提供 `design`、`run`、`assure`、`audit-data`、`holdout`、`calibrate-grader`、`validate-professional`、`audit-dependency`、`promotion-packet` 和 `viewer`，各子命令直接沿用对应脚本参数，不另外复制业务逻辑。

恢复始终创建后继工作区并写 `recovery-lineage.json`，不覆盖原始证据。源工作区已经执行留出时，必须提供新的 `--replacement-eval-set`；设计保证模式还要提供与新 Eval 精确绑定的 `--replacement-traceability`、`--replacement-traceability-validation` 和 `--replacement-conformance-review`。这是防止通过反复恢复消耗同一留出集或沿用旧 Eval 证据。`--adapter-retries` 只作用于返回临时失败的只读 grade/attribute/compare/review job；execute 和 modify 不自动重试。

选择通过后只生成非发布型晋升包和统一 Viewer：

```bash
python <developing-skills>/scripts/develop_skill.py promotion-packet \
  --workspace <workspace> --candidate <candidate> --output <promotion-packet.json>
python <developing-skills>/scripts/develop_skill.py viewer \
  --workspace <workspace> --promotion-packet <promotion-packet.json> \
  --output <assurance-view.html>
```

晋升包精确绑定候选、selection、设计保证及可选数据、grader、专业和留出治理报告；它始终声明 `requires_human_approval: true`，不会复制或覆盖正式 Skill。
