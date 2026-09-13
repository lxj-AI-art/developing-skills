# CodeAgent 适配器配置

`scripts/codeagent_adapter.py` 把控制器的 job 转成可配置 CLI 调用，并内置确定性 checker、分层评分、八层归因后的安全区分性检查、责任层路径约束和盲比较。它不假设某个厂商 CLI；宿主需要把实际 CodeAgent 命令写成参数数组。

```json
{
  "model_id": "executor-model-version",
  "environment_id": "sandbox-and-tool-profile",
  "execution_independence": "verified",
  "timeout_seconds": 600,
  "execute_output_json": true,
  "commands": {
    "execute": ["codeagent", "run", "--cwd", "{cwd}", "--prompt-file", "{prompt_file}"],
    "trigger": ["codeagent", "run", "--isolated-discovery", "{skill_path}", "--prompt-file", "{prompt_file}"],
    "activation_probe": ["runtime-probe", "--run-dir", "{output_dir}"],
    "grade": ["codeagent", "run", "--fresh-context", "--prompt-file", "{prompt_file}"],
    "attribute": ["codeagent", "run", "--fresh-context", "--prompt-file", "{prompt_file}"],
    "modify": ["codeagent", "run", "--cwd", "{skill_path}", "--write", "--prompt-file", "{prompt_file}"],
    "compare": ["codeagent", "run", "--fresh-context", "--prompt-file", "{prompt_file}"],
    "review_design": ["codeagent", "run", "--fresh-context", "--prompt-file", "{prompt_file}"],
    "review_conformance": ["codeagent", "run", "--fresh-context", "--prompt-file", "{prompt_file}"]
  },
  "environment": {}
}
```

可用占位符：`{cwd}`、`{output_dir}`、`{skill_path}`、`{prompt_file}`、`{prompt}`、`{job_file}`。命令不经 shell 展开。没有 prompt 占位符时，prompt 从标准输入传入。

`execute_output_json: true` 时执行命令应返回：

```json
{"final": "给用户的最终文本", "metrics": {"tokens": 1200, "tool_calls": 2}}
```

否则标准输出整体作为最终产物。适配器总会记录命令退出码、耗时、stdout 和 stderr；只有实际运行时能证明独立上下文时，才能配置 `execution_independence: verified`。

运行命令：

```bash
python scripts/run_optimization.py \
  --skill <target-skill> --eval-set <eval-set.json> --workspace <new-workspace> \
  --adapter-command "python <developing-skills>/scripts/codeagent_adapter.py --config <config.json>"
```

## 角色与隔离

- `execute` 只接收自然任务、输入和候选路径，不接收标签。
- `grade` 在新上下文读取冻结标签和真实产物；内置确定性结果优先，语义/领域项才交给 CodeAgent。
- `attribute` 必须从八层中选一个主层。第一次不能区分时，可请求 `file_exists`、`text_contains` 或 `json_path_exists`；适配器在 skill、output 或 input 根内安全执行一次，再要求重新归因。
- `modify` 只编辑候选目录，并按责任层限制路径：method/instruction 可改 `SKILL.md` 与 `references/`，knowledge 只改 `references/`，retrieval 可改入口、参考和脚本，tool 只改 `scripts/`。requirement、runtime、scoring 不自动改候选；纯 trigger 迭代进一步限制为只改 frontmatter 的单行 `description`。
- `compare` 只看到随机编号 A/B 的复制产物；真实版本映射在裁决后才落盘。
- `review_design` 和 `review_conformance` 分别做设计语义与实现一致性审查；若未单独配置，可回退到只读 `role` 命令。控制器会拒绝 reviewer 与 modifier 使用相同身份，高保证任务不接受 `unverified` 独立性。
- `trigger` 必须配独立 `activation_probe`。probe 要返回运行时加载事件，不能从回答质量或关键词推断激活；内置 Grader 会把该事件与 `should-trigger`/`should-not-trigger` 标签做关键确定性核对。

宿主仍负责操作系统级沙箱、凭据、网络、模型配置和工具权限。适配器的哈希与路径检查用于发现违规，不代替强隔离。
