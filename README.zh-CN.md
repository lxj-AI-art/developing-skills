# developing-skills

中文 | [English](README.md)

[![CI](https://github.com/lxj-AI-art/developing-skills/actions/workflows/ci.yml/badge.svg)](https://github.com/lxj-AI-art/developing-skills/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue.svg)](.github/workflows/ci.yml)

一套从需求、专业资料、执行记录和已标注评测案例出发，创建并持续改进专业 Agent Skill 的工程化工作流。

developing-skills 将专业方法设计、版本绑定的设计保证、隔离候选优化、失败归因、留出集治理、Grader 校准和人工晋升控制组合在一起。它不是简单的提示词模板，也不只是对其他 skill-creator 的包装。

当前定位：**高保证 L2 Release Candidate**。系统可以自主修改和评测隔离的候选副本，但正式 Skill 的发布和替换始终由人决定。

## 核心能力

| 领域 | 能力 |
| --- | --- |
| Skill 设计 | 将需求和领域证据转化为可观察结果、方法决策、能力分配、安全边界及 Eval 判据 |
| 设计保证 | 稳定设计项 ID、独立语义评审、外部版本绑定审批、追踪矩阵、一致性评审及变更影响分析 |
| 评测 | 标注案例 Schema、五态结果、确定性/领域/语义评分、重复运行、Benchmark 聚合及候选配对比较 |
| 问题归因 | 从 requirement、method、knowledge、retrieval、tool、runtime、instruction、scoring 八层定位失败责任 |
| 自动优化 | 只修改隔离候选，执行候选校验、完整回归、保证刷新并选择证据支持的最佳版本 |
| 防过拟合 | train/regression/holdout 分离、留出 Vault、访问预算、暴露失效、泄漏和重复案例审计 |
| 生产治理 | 后继工作区恢复、不可变证据、Grader 校准、真实专业验收门禁、保证 Viewer 和非发布型晋升包 |
| Provider 集成 | 通用运行时适配协议、CodeAgent 适配器及固定版本的私有 Anthropic skill-creator 可选依赖 |

## 工作模型

工作流根据风险选择深度。影响明确的低风险局部修正可走轻量路径；新建专业 Skill，或修改方法、安全、输出及 Eval 判据时，使用完整保证路径：

1. 定义可观察结果和非目标。
2. 建立基于证据的方法并分配能力。
3. 校验结构化设计。
4. 由独立身份完成设计语义评审。
5. 生成精确绑定 design ID 的外部审批。
6. 初始化并实现隔离候选。
7. 将设计项映射到实现位置和 Eval expectation。
8. 完成独立一致性评审并绑定精确候选。
9. 执行、评分、归因并实施最小责任修改。
10. 每次候选变化后刷新追踪与一致性证据。
11. 执行完整回归和受治理的留出评测。
12. 生成人工审批使用的晋升包。

设计或候选内容改变后，绑定旧内容身份的证据会失效。系统不会把旧证据重新贴到新版本上。

## 环境要求

- Python 3.11 或 3.12
- 能够加载 Agent Skill 的宿主
- 自动行为评测需要实现运行时协议的适配器
- 使用下述安装方式时需要 Git

核心确定性脚本只依赖 Python 标准库。外部 Agent 运行时和 Anthropic 可选 provider 需要单独配置。

## 安装

### Linux 与 macOS

    git clone https://github.com/lxj-AI-art/developing-skills.git \
      "${CODEX_HOME:-$HOME/.codex}/skills/developing-skills"

### Windows PowerShell

    git clone https://github.com/lxj-AI-art/developing-skills.git "$HOME\.codex\skills\developing-skills"

安装后重新启动宿主，或刷新 Skill 发现。

校验安装结果：

    python scripts/validate_candidate.py .
    python scripts/self_test.py

最后应输出：

    developing-skills self-test: PASS

## 调用 Skill

在兼容宿主中可显式调用，也可由正常的 Skill 发现机制选择：

    请使用 $developing-skills，根据这些需求设计一个专业 Skill，
    然后构建标注 Eval 并改进隔离候选。

支持四种常见起点：

- **新建：** 从业务需求开始。
- **提炼：** 从案例或演示中提取可复用方法。
- **改进：** 诊断并修复已有 Skill。
- **标注优化：** 根据带真值的案例评测并迭代优化 Skill。

## 快速开始

所有主要控制能力均可通过 scripts/develop_skill.py 使用。

### 1. 创建设计

    python scripts/develop_skill.py design init \
      --skill-name example-skill \
      --change-class new \
      --risk-level high \
      --output work/skill-design.md

完成生成的设计并将其状态设为 ready，然后执行校验：

    python scripts/develop_skill.py design validate \
      work/skill-design.md --require-ready

实质性或高风险任务应在初始化前完成独立设计评审并生成外部 approval。详见[设计框架与门禁](references/design-framework.md)。

### 2. 审计标注 Eval 集

    python scripts/develop_skill.py audit-data eval-set.json \
      --similarity-threshold 0.9 \
      --output work/dataset-audit.json

审计会检查 Schema 质量、语义近重复、train/holdout 泄漏、标签冲突、expectation 覆盖、来源和风险覆盖。

### 3. 运行优化循环

    python scripts/develop_skill.py run \
      --skill /absolute/path/to/formal-skill \
      --eval-set /absolute/path/to/eval-set.json \
      --workspace /absolute/path/to/new-workspace \
      --adapter-command "python /absolute/path/to/adapter.py" \
      --baseline-mode original

控制器会在适配器命令后追加 job.json 与 response.json。适配器必须支持 execute、grade、attribute、modify 和 compare；高保证自动刷新还需要支持 review_conformance。

该命令只会快照正式 Skill，不会修改正式版本。所有候选修改均发生在新工作区中。

### 4. 查看状态或安全恢复

    python scripts/develop_skill.py status --workspace work/run-001

    python scripts/develop_skill.py resume \
      --workspace work/run-001 \
      --successor-workspace work/run-002 \
      --adapter-command "python /absolute/path/to/adapter.py"

恢复始终创建后继工作区并记录 lineage。若源运行已消耗留出访问次数，则必须提供新的 Eval 集。

### 5. 准备人工晋升审查

    python scripts/develop_skill.py promotion-packet \
      --workspace work/run-002 \
      --candidate work/run-002/candidates/iteration-2 \
      --output work/promotion-packet.json

    python scripts/develop_skill.py viewer \
      --workspace work/run-002 \
      --promotion-packet work/promotion-packet.json \
      --output work/assurance-view.html

晋升包精确绑定候选身份，并且不执行发布，始终要求人工批准。

## 高保证优化

向控制器提供精确的设计、审批、追踪与一致性证据：

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

候选修改后，控制器会重建追踪校验、调用独立 conformance reviewer、生成新的 implementation binding，并在访问留出集之前重新运行完整训练/回归集。

详见[生产保证、恢复与治理](references/production-assurance.md)。

## 统一命令入口

| 命令 | 用途 |
| --- | --- |
| design | 初始化、校验、审批、绑定、创建或移交版本绑定的设计 |
| run | 执行隔离的评测与优化循环 |
| assure | 为精确的已变更候选刷新追踪与一致性证据 |
| audit-data | 检查重复案例、泄漏、冲突和覆盖缺口 |
| holdout | 创建、检查或从本地受治理留出 Vault 中签出 |
| calibrate-grader | 将 Grader 结果与专家 golden 裁决比较 |
| validate-professional | 使用专家裁决案例控制真实领域质量声明 |
| audit-dependency | 在不安装的情况下审计候选依赖升级 |
| status | 检查候选身份、证据一致性和恢复条件 |
| resume | 创建保留证据的后继优化运行 |
| promotion-packet | 生成人工审查包，不执行发布 |
| viewer | 生成独立 HTML 保证视图 |

帮助命令：

    python scripts/develop_skill.py --help
    python scripts/develop_skill.py <command> --help

## 运行时适配协议

优化器不绑定特定运行时。调用方提供命令前缀，控制器不经过 shell 调用：

    <adapter-command> <job.json> <response.json>

协议将执行与评分隔离，不向 execute job 暴露标签；适用时记录模型、环境、候选、Eval、设计、审批、追踪和一致性身份。

相关文档：

- [运行时适配协议](references/runtime-adapter.md)
- [CodeAgent 适配器配置](references/codeagent-adapter-config.md)

本版本没有增加 Codex、Claude Code、OpenAI API 或 Anthropic API 的一等原生适配器；提供相应适配器后，可通过通用协议连接。

## Anthropic skill-creator 可选依赖

Anthropic skill-creator 是推荐依赖，但不是运行主 Skill 的强制依赖。它安装在 dependencies/ 私有目录中，不会与宿主的全局 skill-creator 竞争。

先查看计划：

    python scripts/bootstrap_dependencies.py plan

向用户展示来源、固定 commit、许可证和安装目标并获得明确批准后执行：

    python scripts/bootstrap_dependencies.py install \
      --confirm-external-install

也可以跳过：

    python scripts/bootstrap_dependencies.py install \
      --without-anthropic

验证状态：

    python scripts/bootstrap_dependencies.py verify
    python scripts/anthropic_skill_creator.py detect

清单会锁定官方 anthropics/skills 仓库、完整 commit SHA、预期内容哈希、许可证及所需能力。每次加载前重新计算安装内容哈希，并且不会自动跟随上游更新。

详见[推荐依赖安装](references/dependency-installation.md)。

## 证据与安全不变量

- 优化过程不覆盖正式 Skill。
- 工作区和结果记录以追加为主，重试不覆盖唯一原始证据。
- 设计审批位于设计正文之外，并绑定精确 design ID。
- 追踪和一致性证据绑定精确 candidate ID 与 Eval-set ID。
- 修改器不会收到留出标签。
- 暴露过的留出案例自动降级为 regression，不能继续作为盲测证据。
- 自动重试只用于明确的临时性只读任务；execute 和 modify 不自动重试。
- 晋升包不会发布或替换 Skill。
- 外部下载必须获得用户明确确认。

## 工作区产物

一次运行会保存冻结 Eval、候选版本、适配器任务、执行、评分、归因、保证刷新、Benchmark、选择结果和历史：

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

实际内容取决于启用的保证和治理策略。

## 验证与 CI

每次 push 和 pull request 都会运行以下矩阵：

- Ubuntu、macOS、Windows
- Python 3.11、3.12

工作流会编译全部脚本、校验 Skill 包并执行 scripts/self_test.py。

本地验证：

    python -m compileall -q scripts
    python scripts/validate_candidate.py .
    python scripts/self_test.py

## 范围与限制

- L2 表示可以在隔离环境中自主迭代候选，不表示可以自主发布到生产。
- 本地留出 Vault 提供访问治理与防篡改审计，不等于同一操作系统用户之间的强隔离。
- 真实领域质量声明需要可追溯、已关闭并经专家裁决的案例；构造案例只能证明流程行为。
- 仓库不包含真实 HFSS 案例集或专家业务验收结论。
- 行为评测能否执行，取决于外部适配器及其模型和工具权限。
- 结构校验和自测通过不能单独证明新开发的专业 Skill 具备业务质量。

## 文档导航

| 文档 | 用途 |
| --- | --- |
| [Skill 指令](SKILL.md) | Agent 使用的工作流与路由 |
| [设计框架与门禁](references/design-framework.md) | 需求、方法、审批、追踪与一致性 |
| [方法与能力设计](references/method-design.md) | 从证据和案例推导可复用决策方法 |
| [Eval Schema](references/eval-schema.md) | 标注案例及结果合同 |
| [评测与归因](references/evaluation.md) | 执行、评分、比较和证据解释 |
| [标注案例自动优化](references/labeled-case-optimization.md) | 迭代优化控制器行为 |
| [运行时适配协议](references/runtime-adapter.md) | 适配器任务和响应 |
| [生产保证](references/production-assurance.md) | 保证刷新、留出、校准、恢复、Viewer 与晋升 |
| [Anthropic 集成](references/anthropic-skill-creator-integration.md) | Provider 能力及 handoff |
| [依赖安装](references/dependency-installation.md) | 固定版本私有 provider 的安装与审计 |
| [工作记录](references/work-record.md) | 复杂或跨轮任务的紧凑证据记录 |

## 参与贡献

改动应对应可观察行为。修改脚本时，请增加或更新有意义的确定性测试，并运行上述完整校验。不要提交 dependencies/、生成的工作区、留出 token、下载的 provider 或缓存文件。

## 许可证

本项目使用 [Apache License 2.0](LICENSE)。
