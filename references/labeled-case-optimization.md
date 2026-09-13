# 标注案例自动优化

用于以已标注案例自动分析、修改和重测目标 Skill。默认能力等级为 L2：系统可在隔离工作区内迭代候选并推荐最佳版本，但不得自动覆盖已安装或正式版本。

## 进入条件

先确认目标 Skill、基线版本、案例文件、可用运行环境和预算。将原始案例规范化为 [标注与结果格式](eval-schema.md)，再运行：

```bash
python <developing-skills>/scripts/validate_eval_set.py <eval-set.json> --check-files
```

存在 `invalid_case`、关键真值来源不明、必要文件缺失或评分规则会实质改变结论时，先修案例；不能用自动修改 Skill 来迎合不可靠标签。构造案例可验证流程，不得称为真实业务验证。

运行环境已有可调用 Agent 时，为它实现 [运行时适配协议](runtime-adapter.md)，再由控制器完成整个 L2 闭环：

```bash
python <developing-skills>/scripts/run_optimization.py \
  --skill <target-skill> --design <ready-skill-design.md> \
  --approval <design-approval.json> \
  --traceability <traceability.json> \
  --traceability-validation <traceability-validation.json> \
  --conformance-review <conformance-review.json> \
  --assurance-reviewer-id <independent-reviewer> \
  --modifier-id <modifier-identity> \
  --eval-set <eval-set.json> \
  --workspace <new-workspace> \
  --adapter-command "<adapter-command>"
```

控制器会冻结已审批设计及保证产物、案例及其文件，并行执行成对任务、分层评分、自动区分检查与八层归因、按责任层复制并修改下一候选、真实校验修改后的 Skill、自动刷新追踪/一致性/实现绑定、完整重跑训练/回归，最后才运行留出集、盲比较和选择门禁。design assurance policy 字段按依赖关系强制执行；启用自动刷新时还必须提供追踪源和不同于修改者的 reviewer。旧保证留在原候选，新证据精确绑定新的 candidate_id。

数据集语义泄漏、grader 校准、专业验证、留出访问预算、故障恢复和 promotion packet 见 [生产保证、恢复与治理](production-assurance.md)。这些治理报告可通过 policy 的 `require_dataset_audit`、`require_grader_calibration`、`require_professional_validation` 和 `require_holdout_governance` 变成晋升门禁。

## 隔离与可见性

每次优化先快照正式版本，只修改 `candidates/iteration-N/` 中的副本。执行器看任务、输入和候选，不看标准答案；评分器在执行完成后看标签和产物；修改器只看训练集、回归集的失败与归因，不看留出案例、留出输出或留出分数。选择器可以读取全部最终评分，但不修改候选。

若运行环境不能提供独立 Agent 或等价上下文隔离，允许完成自查，但将 `execution_independence` 记为 `unverified`，不得把留出集称为盲测。共享目录约束不是系统隔离。

## 工作区

```text
<workspace>/
├── design/skill-design.md
├── eval/eval-set.json
├── eval/<case-inputs>
├── baseline/
├── candidates/iteration-N/
├── metadata/iteration-N.json
├── jobs/iteration-N/<phase>/*.job.json
├── runs/iteration-N/<case-id>/<configuration>/run-N/
│   ├── transcript.md
│   ├── outputs/
│   ├── execution.json
│   └── grading.json
├── iterations/iteration-N/{benchmark,selection,report}.*
├── benchmark.json
├── selection.json
└── history.json
```

所有结果绑定设计标识、候选内容标识、案例集标识、模型、工具与环境。失败重跑写新目录，不覆盖唯一原始证据。

## 自动循环

1. **冻结。** 保存目标、正式基线、候选、案例、判据、环境、预算和授权。新建 Skill 的基线为无 Skill；改进已有 Skill 的基线为原始版或当前最佳版。可用下面的脚本创建不可覆盖的候选和案例快照：

   ```bash
   python <developing-skills>/scripts/prepare_iteration.py \
     --skill <target-skill> --eval-set <eval-set.json> \
     --workspace <workspace> --iteration 1 --baseline-mode original
   ```
2. **成对执行。** 对同一案例同轮启动候选和基线。两组使用相同任务、原始文件、模型、工具权限和可比预算。确定性任务通常一次；模型行为或已观察到波动的案例按配置重复。
3. **分层评分。** 先运行确定性 checker，再进行领域规则检查，最后才用独立语义评分。状态只能是 `pass`、`fail`、`inconclusive`、`blocked`、`invalid_case`。评分器逐项给证据，同时指出无区分度、可被表面迎合或遗漏关键结果的断言。
4. **八层归因。** 对失败依次判断 requirement、method、knowledge、retrieval、tool、runtime、instruction、scoring。输出观察、证据、责任层、置信度、候选原因和区分性检查。不能区分时先执行最小检查，不立即修改。
5. **生成最小修改。** 修改器只处理训练/回归失败，并在同轮多个失败中选择置信度最高的一个主责任层；其他层延后。保留修改前快照，记录改动目标、因果依据、同类缺陷范围和防止过修的相反案例。控制器核对实际变更路径，拒绝跨层改动和案例专用口令。修改完成后重新计算内容标识；评分记录不得沿用修改前标识。
6. **验证候选。** 先用 `validate_candidate.py` 做 frontmatter、引用和 Python 语法检查，再执行 `policy.candidate_checks`。如果修改了 `scripts/` 却没有真实 candidate check，控制器拒绝继续。检查命令不得修改候选。随后重新运行完整训练/回归集；通过的候选成为下一轮 current-best 基线，稳定后才运行留出集。
7. **聚合与选择。** 运行：

   ```bash
   python <developing-skills>/scripts/aggregate_benchmark.py <runs-dir> --output <benchmark.json>
   python <developing-skills>/scripts/analyze_benchmark.py <benchmark.json> <eval-set.json> --parent <parent> --candidate <candidate> --output <analysis.json>
   python <developing-skills>/scripts/select_candidate.py <benchmark.json> <eval-set.json> --output <selection.json>
   python <developing-skills>/scripts/generate_eval_report.py <benchmark.json> --selection <selection.json> --output <report.html>
   ```

   分析器检查 Eval 可执行性、无区分度断言、高方差、成本、预算以及案例 ID/特征文本过拟合。静态报告并排展示成对版本、分析发现、盲比较、逐次运行和指标，并可收集审阅意见下载为 `feedback.json`。反馈进入下一轮前先归因；用户意见不是自动替换专业真值的标签。

   若 Anthropic `skill-creator` 可用，可按 [集成说明](anthropic-skill-creator-integration.md) 将同一原始 benchmark 转成只读兼容工作区并调用其 Viewer。丰富 Viewer 只改善审阅界面，不参与晋升计算；原始五态评分和版本绑定仍是唯一选择依据。

8. **继续或停止。** 接受的候选成为下一轮基线；被拒绝的候选保留证据但不晋升。达到最大轮数、预算、连续两轮无可判定提升、只剩环境阻断或需要新专业真值时停止。

## 角色契约

### 执行器

接收候选路径、自然任务、输入文件和输出目录。不得接收标签、作者诊断、拟议修改或其他组结果。实际使用候选完成任务，保存最终产物、可观察执行记录、错误与指标；不评价自身是否通过。

### 评分器

接收冻结的标签、执行记录和产物。先检查真实文件与副作用，再判定每项 expectation。不能从“提到了关键词”推断任务已完成；专业断言需要标签所指向的可靠来源。输出必须符合 `grading.json` 契约。

### 归因器

接收评分失败、候选、执行记录和能力依赖状态。不得直接修改文件。只有证据能区分时才给主责任层；否则给 `inconclusive` 和下一项区分性检查。评分标准错误时归因到 scoring，并使已有比较结果失效。

### 修改器

只接收训练/回归失败及已关闭的归因。修改候选副本，不接触正式版和留出材料。每轮优先修改一个责任层；工具或运行时失败不得用提示词掩盖，知识缺失不得无依据写成方法规则。

## 晋升门禁

选择脚本只提供推荐，正式晋升仍需用户授权。候选至少满足：

- 没有新的 critical expectation 失败；
- 回归集没有相对基线下降；
- 训练和留出平均得分达到配置的最小增益；
- 留出集没有 blocked、inconclusive 或 invalid_case，除非策略显式允许；
- 波动未超过策略阈值；未超过阈值不等于统计显著，只支持本轮工程选择；
- 权限、安全、工具副作用和成本约束没有退化。
- 配置方差门禁时，成对提升超过本轮观测噪声；样本不足只能判为不可判定；
- 配置盲比较时，随机 A/B 裁决达到最低胜率且无 critical 基线胜出；
- 分析器没有关键预算、评测质量或明显案例专用过拟合发现。

看过并用于修改的留出案例立即转为 regression；若仍需泛化结论，必须补充新的未见案例。评分规则或真值改变后，所有候选与基线都要按新规则重评。

## 触发优化

Skill 正文优化与自动触发优化分开。触发案例设 `mode: trigger`，并在 tags 中标 `should-trigger` 或 `should-not-trigger`；同一套控制器仍负责训练、留出隔离和选择。执行适配器把候选放到隔离运行的可发现位置，但用户请求不显式指定 Skill；响应必须提供实际加载观测。只有运行环境能观察加载行为时才执行自动触发优化，否则返回 blocked。纯触发失败按训练集归因后只能改写 frontmatter 的单行 `description`，控制器拒绝同时改正文；最终只按未暴露留出集选择。

Anthropic 提供者与 Claude CLI 可用时，可把训练/回归触发案例交给其 `run_loop.py` 做内层 description 搜索。通过 `anthropic_skill_creator.py description-optimize` 调用，适配器会排除最终 holdout，只改隔离候选并核对正式版本未变。优化结果不能直接晋升；仍需本控制器用真实加载观测、回归集和未暴露留出集评判。
