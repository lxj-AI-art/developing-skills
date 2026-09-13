# 推荐依赖安装

`developing-skills` 推荐使用 Anthropic `skill-creator` 的 Eval、Viewer、打包和 description 优化执行层，但它不是运行本 Skill 的强制依赖。安装采用私有 provider 目录，避免与宿主已有的全局 `skill-creator` 同名冲突。

## 默认行为

- Anthropic provider 默认被选择为推荐依赖；
- `plan` 和 `status` 不联网、不写入；
- 实际安装必须由用户看到来源、commit、许可证和目标目录后明确确认；
- 不确认时返回 `needs_confirmation`，不会偷偷下载；
- 用户可以选择 `--without-anthropic`，`developing-skills` 继续使用宿主原生能力；
- 不自动更新。升级时修改固定 commit 与预期内容哈希，重新审查和测试。

上游升级不得直接修改 manifest。先把候选版本下载到隔离目录，再执行：

```bash
python <developing-skills>/scripts/audit_dependency_update.py \
  --manifest <developing-skills>/references/recommended-dependencies.json \
  --candidate-root <staged-skill-creator> --candidate-ref <full-commit> \
  --current-root <current-provider-root> \
  --compatibility-command '<provider-compatibility-test>' \
  --output <dependency-update-audit.json>
```

审计输出能力识别、顶层 Python API 差异、许可证、symlink/网络/命令执行/删除相关发现、文件 SBOM 和真实兼容性命令结果。没有至少一条成功的兼容性命令不会提出 manifest 变更。它永不安装或改写 manifest；仍需人工审阅和明确安装确认。

安装清单位于 [recommended-dependencies.json](recommended-dependencies.json)。当前锁定：

- repository：`anthropics/skills`；
- path：`skills/skill-creator`；
- full commit：`34040c9c568585f6929bedeaad110ad08f079624`；
- expected content ID：`412558e2d4103a965f8bd319273797a4787f1d4635c2b16f1df0d77d7e0680f4`；
- license：Apache-2.0，安装内容必须包含 `LICENSE.txt`。

## 安装流程

先生成计划：

```bash
python <developing-skills>/scripts/bootstrap_dependencies.py plan
```

若尚未安装，向用户展示 JSON 中的 `source`、`private_root`、`license` 和 `confirmation_required`。得到明确同意后运行：

```bash
python <developing-skills>/scripts/bootstrap_dependencies.py install \
  --confirm-external-install
```

用户拒绝或安装时选择不带 Anthropic：

```bash
python <developing-skills>/scripts/bootstrap_dependencies.py install \
  --without-anthropic
```

核验现状：

```bash
python <developing-skills>/scripts/bootstrap_dependencies.py verify
python <developing-skills>/scripts/anthropic_skill_creator.py detect
```

成功后目录为：

```text
developing-skills/
└── dependencies/
    └── anthropic-skill-creator/
        ├── installation.json
        └── <full-commit>/
```

依赖目录不注册为全局 Skill；`anthropic_skill_creator.py` 读取 `installation.json` 并核对实际内容哈希后才加载。绑定路径相对 `developing-skills` 保存，因此整个 Skill 移动后仍可验证。

## 安全与失败语义

- 安装器复用当前 Codex 的 `skill-installer`，要求目标不存在并拒绝不安全链接；
- 安装后再次检查 provider 身份、四项能力、固定内容哈希和许可证文件；
- 新下载内容校验失败时删除该次新建的精确版本目录，不删除其他版本或正式 Skill；
- `blocked` 表示找不到安装器；`failed` 表示下载或验证失败；两者都不把 provider 声称为可用；
- 已安装且哈希一致时返回 `already_installed`，不会重复下载；
- 固定版本内容存在但绑定丢失时，经确认只重建绑定并返回 `bound_existing`，不重新下载；
- 内容被修改后，运行时绑定失效，必须重新安装或显式指定经过审查的 `--root`。

普通 Skill 文件夹没有通用的安装后钩子，因此“默认安装”由安装 Agent 在复制完 `developing-skills` 后调用上述计划与确认流程实现。若分发渠道不能执行安装器，只安装主 Skill，并在首次需要 Anthropic 能力时报告依赖未安装；不能绕过确认。
