# Anthropic skill-creator 集成

将 Anthropic `skill-creator` 视为可选外部提供者，而不是复制进本 Skill。`developing-skills` 保留专业方法、标签治理、八层归因、正文修改和候选晋升的控制权；外部提供者负责其成熟且可独立审计的通用能力。

## 责任边界

| 活动 | 主责任方 | 约束 |
|---|---|---|
| 通用 Skill 创作指导、宿主原生初始化与结构审查 | 宿主原生调用 Anthropic skill-creator | 只给已审批设计与候选，不给最终留出答案 |
| 标准格式校验 | Anthropic `quick_validate.py` | 是结构证据，不替代行为评测 |
| 打包 `.skill` | Anthropic `package_skill.py` | 只打包已选候选；不得等同发布 |
| 丰富 Eval Viewer | Anthropic `generate_review.py` | 先生成兼容工作区，保留状态降维说明 |
| Claude 原生 description 优化 | Anthropic `run_loop.py` | 只接触训练/回归触发案例与候选副本 |
| 可观察需求、专业方法与能力分层 | developing-skills | 不委托给通用格式工具 |
| 标注案例 Schema、五态评分与专业 Oracle | developing-skills | 五态不能被二元 PASS/FAIL 覆盖 |
| Skill 正文自动修改与八层归因 | developing-skills | 每轮一个主责任层 |
| 回归、最终留出与晋升决策 | developing-skills | 默认人工批准发布 |

Anthropic `skill-creator` 当前没有独立初始化脚本；“新建 Skill”由宿主通过其 Skill 指导完成。先用 `design_gate.py handoff` 生成绑定 design/approval 且不包含留出答案的交接单，再让宿主调用它；返回候选后执行本 Skill 的真实候选校验、追踪和 conformance 门禁。脚本级直接调用覆盖校验、打包、Viewer 和 description 优化。若当前宿主只能调用另一提供者的同名 `skill-creator`，必须记录实际提供者，不能称为 Anthropic。

## 可用性探测

安装本 Skill 的推荐 Anthropic provider 时先按 [推荐依赖安装](dependency-installation.md) 执行计划、确认、固定版本安装和校验。成功后适配器会从私有绑定发现 provider；也仍允许调用者通过 `--root` 或 `ANTHROPIC_SKILL_CREATOR_ROOT` 指向另一个经过审查的版本。

先执行：

```bash
python <developing-skills>/scripts/anthropic_skill_creator.py \
  --root <anthropic-skill-creator-root> detect
```

也可设置 `ANTHROPIC_SKILL_CREATOR_ROOT`。未显式配置时只检查当前项目及 `~/.claude/skills/skill-creator` 的精确候选路径，不递归扫描、不联网下载。一个目录只有同时包含 `SKILL.md`、`scripts/package_skill.py` 和 `eval-viewer/generate_review.py` 才会被识别为 Anthropic 提供者；各项脚本能力另行报告。

返回语义：

- `available`：包身份已识别；仍需看具体 capability；
- `unavailable`：包或对应脚本不存在，未尝试安装；
- `blocked`：包存在，但 Claude CLI 等运行依赖缺失；
- `failed`：官方脚本真实运行后非零退出、超时或输出不合法。

所有外部命令用参数数组执行，报告命令、退出码、stdout、stderr 与提供者内容标识。不得把 `unavailable` 自动改写成 `completed`。

## 直接调用

校验候选：

```bash
python <developing-skills>/scripts/anthropic_skill_creator.py \
  --root <creator-root> validate --skill <candidate>
```

打包已选候选：

```bash
python <developing-skills>/scripts/anthropic_skill_creator.py \
  --root <creator-root> package --skill <candidate> --output-dir <new-output-dir>
```

生成 Anthropic Viewer 静态页面：

```bash
python <developing-skills>/scripts/anthropic_skill_creator.py \
  --root <creator-root> viewer \
  --benchmark <developing-workspace>/benchmark.json \
  --eval-set <developing-workspace>/eval/eval-set.json \
  --workspace <new-compatibility-workspace> \
  --static <review.html> --skill-name <name>
```

适配器把 `candidate` 映射为 `with_skill`、`baseline` 映射为 `without_skill`，复制可观察产物并生成 Anthropic 的 `eval_metadata.json`、`grading.json` 和 benchmark 视图。原始配置名和五态状态保留在扩展字段；由于 Anthropic Viewer 的 expectation 是布尔值，`inconclusive`、`blocked` 与 `invalid_case` 只在 `original_status` 和 `summary.unresolved` 中表达。晋升仍读取 developing-skills 的原始 benchmark，绝不能读取降维后的 Viewer 数据。

使用 Claude 原生 description 优化：

```bash
python <developing-skills>/scripts/anthropic_skill_creator.py \
  --root <creator-root> description-optimize \
  --formal-skill <installed-skill> --candidate <isolated-candidate> \
  --eval-set <eval-set.json> --model <claude-model> \
  --results-dir <new-results-dir> --apply-to-candidate
```

适配器只导出 `mode: trigger` 且属于 train/regression 的案例给 Anthropic 内循环，明确排除最终 holdout。它要求正反触发案例齐备、正式版和候选互不包含、Claude CLI 可用；只把最佳单行 description 写入候选，并在前后核对正式 Skill 内容标识。之后必须回到 developing-skills 控制器，用未暴露留出集和实际加载证据完成最终触发评测。

## 版本漂移与失败处理

Anthropic 脚本不是 developing-skills 的内部 API。每次工作记录保存 `content_id`、命令和结果；升级提供者后，旧的脚本级验证只对旧内容标识有效。CLI 参数或 Viewer Schema 变化导致调用失败时，保留兼容工作区和错误证据，退回本 Skill 的静态报告或本环境原生校验，但必须明确标记 fallback 提供者与能力差异。

不得自动下载、安装、更新或发布 Anthropic 包。此类动作需要用户授权，并遵循其许可证与归属要求。
