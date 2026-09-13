# Skill 设计保证框架（V2）

本框架把四类不同证据分开：结构有效、语义设计成立、实现可追踪、实现与设计一致。任何一种都不能替代另一种。

## 适用深度

| 变化/风险 | 深度 | 必需门禁 |
|---|---|---|
| `local` 且 `low/medium` | `lightweight` 或 `standard` | 结构；若影响关键项则升级 |
| `new/substantial` 且 `low/medium` | `standard` | 结构、独立设计评审、外部审批、追踪、实现一致性 |
| 任意 `high/critical` | `high-assurance` | 上述全部；必须包含 safety 设计项 |

新建或实质变化不能声明 `lightweight`；高/关键风险不能低于 `high-assurance`。这是确定性规则，不依赖标题语言。

## V2 产物与顺序

1. `skill-design.md`：用稳定的 `DF-01`…`DF-10` section ID 和 `REQ/MTH/SAFE/OUT/DEP` 设计项表达决定。
2. `design-review.json`：独立审查需求、方法、能力、评价、安全五维；结构检查不生成此结论。
3. `design-approval.json`：记录确认主体和依据，并精确绑定设计内容哈希。审批不写回设计正文。
4. 初始化候选：优先调用当前提供者 `skill-creator`；无脚本接口时生成 provider handoff。
5. `traceability.json` 与 validation：逐项绑定实现文件/外部能力和 Eval case/expectation。
6. `conformance-review.json`：独立检查实现是否兑现设计，不以关键词存在代替行为成立。
7. `design-binding.json`：`initialized` 只证明有效脚手架；`implementation` 还要求追踪与一致性审查通过。

设计、审批、评审、候选或 Eval 任一内容变化，相关哈希即变化。旧证据不得静默复用。

V1 把 `confirmed_*` 写在设计 frontmatter 中，无法防止“修改正文但沿用确认”的情况，因此不能通过 V2 初始化或绑定门禁。迁移时生成 V2 草稿、保留可追溯决定，重新做语义评审和外部审批；不要机械复制旧确认字段。

## 设计格式

```yaml
---
schema_version: "2.0"
skill_name: example-skill
design_status: draft
change_class: new
risk_level: high
design_depth: high-assurance
language: zh-CN
---
```

正文标题可以使用任意语言，但必须保留稳定 ID。`standard/high-assurance` 使用 `DF-01` 至 `DF-10`；轻量路径只要求 `DF-01/02/03/08/10`。正文另含一个 JSON code block，其根字段为 `design_items`。每项必须具有：

```json
{"id":"REQ-001","type":"requirement","text":"具体决定","criticality":"critical","source_ref":"来源","acceptance":"可检查条件"}
```

ID 与 type 必须匹配：`REQ=requirement`、`MTH=method`、`SAFE=safety`、`OUT=output`、`DEP=dependency`。`ready` 设计不能残留作者注释、TBD 或“待定义”。这只说明结构完整；真正的专业质量由独立设计评审裁决。

## 命令

生成风险自适应草稿并执行结构、语义、审批门禁：

```bash
python <developing-skills>/scripts/design_gate.py init \
  --skill-name <name> --change-class new --risk-level high \
  --language zh-CN --output <skill-design.md>
python <developing-skills>/scripts/design_gate.py validate <skill-design.md> --require-ready
python <developing-skills>/scripts/review_design.py design \
  --design <skill-design.md> --adapter-command '<review-adapter>' \
  --reviewer-id <independent-id> --modifier-id <author-id> \
  --output <design-review.json>
python <developing-skills>/scripts/design_gate.py approve <skill-design.md> \
  --review <design-review.json> --confirmed-by <identity> \
  --confirmation-basis explicit-user-approval --confirmation-ref <record> \
  --output <design-approval.json>
```

初始化或交接给仅能宿主调用的提供者：

```bash
python <developing-skills>/scripts/design_gate.py initialize <skill-design.md> \
  --approval <design-approval.json> --skill-creator-root <creator-root> \
  --output-directory <skills-parent> --resources scripts,references \
  --binding-output <records>/initialized-binding.json
python <developing-skills>/scripts/design_gate.py handoff <skill-design.md> \
  --approval <design-approval.json> --provider anthropic-skill-creator \
  --resources scripts,references --output <provider-handoff.json>
```

建立并验证追踪，执行一致性评审和实现绑定：

```bash
python <developing-skills>/scripts/validate_traceability.py init \
  --design <skill-design.md> --approval <design-approval.json> \
  --candidate <candidate> --eval-set <eval-set.json> --output <traceability.json>
python <developing-skills>/scripts/validate_traceability.py validate \
  --design <skill-design.md> --approval <design-approval.json> \
  --candidate <candidate> --eval-set <eval-set.json> \
  --traceability <traceability.json> --output <traceability-validation.json>
python <developing-skills>/scripts/review_design.py conformance \
  --design <skill-design.md> --approval <design-approval.json> \
  --candidate <candidate> --eval-set <eval-set.json> \
  --traceability-validation <traceability-validation.json> \
  --adapter-command '<review-adapter>' --reviewer-id <independent-id> \
  --modifier-id <author-id> --output <conformance-review.json>
python <developing-skills>/scripts/design_gate.py bind <skill-design.md> \
  --approval <design-approval.json> --candidate <candidate> \
  --provider <provider-version> --stage implementation \
  --traceability-validation <traceability-validation.json> \
  --conformance-review <conformance-review.json> \
  --output <records>/implementation-binding.json
```

比较设计变化：

```bash
python <developing-skills>/scripts/analyze_design_change.py \
  <old-design.md> <new-design.md> --old-traceability <traceability.json> \
  --output <impact.json>
```

输出列出新增、删除、变化设计项，受影响实现路径与 Eval case。删除关键项返回 `incompatible`；其他变化返回 `revalidation_required`。新设计必须重新审批、更新追踪、重跑相关回归/留出和一致性评审。

## 真实性约束

- 同一身份不能作为候选修改者和唯一语义审查者；无法隔离时标记 `unverified`，不能通过高保证门禁。
- 外部能力只有 `verified` 且具有 `evidence_ref` 才算实现覆盖。
- critical 设计项必须同时具备实现和 Eval expectation 映射。
- 自动优化后候选内容标识变化，旧 traceability/conformance 不再支持晋升。启用 `auto_refresh_assurance` 时，控制器保留已审批设计与追踪映射、重新验证实际文件和 expectation、调用独立 conformance reviewer 并生成新 implementation binding；否则只能继续探索，最终候选必须另行刷新证据。
- 绑定会真实校验 `SKILL.md` frontmatter、引用和脚本语法；提供者校验只能作为附加证据。
