# {{PROJECT_NAME}} 自动化开发工作流

这是一个可复制到任意 Git 项目的 Codex 工作流骨架。它把项目记忆保存在版本化文件中，按任务临时创建专业 Agent，并用风险分级质量门控制返工与人工升级。

默认策略是安全半自动：交互式 Codex 负责 Commander 会话，Builder 是唯一写实现的角色，Explorer 与 Reviewer 只读；工具不会自动 commit、push，也不会循环调用 `codex exec`。

## 快速开始

从本模板仓库初始化一个目标项目：

```bash
python3 scripts/autodev.py init /path/to/target --name my-project
```

初始化会先完成全部冲突预检。默认只要发现一个冲突就不会写入任何文件；`--merge` 只补充缺失文件、绝不覆盖已有内容，并以退出码 `2` 列出需要人工合并的文件。`Auto_Dev.md` 不会被复制，已有 `README.md` 不会被覆盖。

进入目标项目后：

1. 填写 [PROJECT.md](PROJECT.md)、[需求](docs/REQUIREMENTS.md)、[架构](docs/ARCHITECTURE.md) 和 [路线图](.agent/ROADMAP.md) 中的必填占位符。
2. 把 `.agent/STATE.json` 的 `project_status` 改为 `ACTIVE`，更新 `next_action`、`next_owner` 和 `updated_at`。
3. 执行 `python3 scripts/autodev.py validate --ready`。
4. 创建任务：

   ```bash
   python3 scripts/autodev.py new-task \
     --id TASK-001 \
     --title "实现第一个垂直切片" \
     --risk MEDIUM \
     --requirements REQ-001
   ```

5. 生成并复制 Commander 提示：

   ```bash
   python3 scripts/autodev.py prompt
   ```

6. 在项目根目录启动交互式 Codex，将提示交给 Commander。

## 常用命令

```text
python3 scripts/autodev.py init TARGET --name NAME [--merge]
python3 scripts/autodev.py doctor
python3 scripts/autodev.py validate [--ready] [--json]
python3 scripts/autodev.py status [--json]
python3 scripts/autodev.py new-task --id TASK-NNN --title TITLE --risk LEVEL [--requirements IDS]
python3 scripts/autodev.py prompt
```

退出码固定为：`0` 成功，`1` 参数或验证失败，`2` 非破坏性冲突或项目尚未 ready。

## 三种质量模式

- `BUILD`：快速交付可工作的纵向切片。阻止功能缺失、核心测试失败、硬约束违反、明显回归和架构死路；次要问题进入 `.agent/DEBT.md`。
- `INTEGRATION`：在 BUILD 基础上检查接口、跨模块行为与回归。
- `HARDENING`：执行完整边界、性能、文档、运维准备和债务清理。

LOW/MEDIUM 风险通常由 Builder 自检和 Commander 增量检查完成。HIGH 风险、架构变更、共享数据结构、里程碑集成以及 HARDENING 必须使用独立 Reviewer。

## Checkpoint 与会话轮换

每个里程碑结束或发生重大架构变更时：

1. 运行必要测试并记录证据。
2. 更新 `STATE.json`、`ROADMAP.md`、相关 ADR 和 `HANDOFF.md`。
3. 确认 `last_good_commit` 指向已知良好提交（本工具不会替你提交）。
4. 结束已膨胀的 Commander 会话；新会话只加载当前状态、当前 Task、相关需求/架构和 diff。

## BLOCKED 处理

只有缺少产品决策、外部权限/凭据、不可恢复的环境问题或预算耗尽时才进入 `BLOCKED`。此时 `blocker` 必须具体，`next_owner` 必须是 `HUMAN`，并在 `next_action` 中写明解除阻塞所需的最小动作。不要用 BLOCKED 代替普通失败或待返工。

## 可选 Stop Hook

Hook 默认不加载。审查后显式启用：

```bash
cp .codex/hooks.example.json .codex/hooks.json
```

随后在 Codex 中使用 `/hooks` 检查并信任它。该 Stop Hook 只运行只读状态校验；状态无效时最多请求一次修正，并尊重 `stop_hook_active` 防止循环。它不会因为项目仍为 `ACTIVE` 就自动续跑，也不会修改文件。

## 故障排查

- `doctor`：检查 Python、Git、Codex CLI、TOML、目录和状态契约。
- `validate`：报告 schema 与跨字段错误；加 `--json` 可供脚本消费。
- `validate --ready` 退出 `2`：补齐契约占位符，并让状态离开 `BOOTSTRAP`。
- `new-task` 拒绝创建：ID 必须匹配 `TASK-\d{3,}`，且同名文件不能已存在。
- Codex 配置问题：运行 `codex --strict-config -C . doctor --summary`。
- 测试：运行 `python3 -m unittest discover -s tests`。

完整运行规则见 [docs/WORKFLOW.md](docs/WORKFLOW.md)。Codex 的行为基础来自官方文档：[AGENTS.md](https://learn.chatgpt.com/docs/agent-configuration/agents-md)、[Subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents)、[Hooks](https://learn.chatgpt.com/docs/hooks) 和[非交互模式](https://learn.chatgpt.com/docs/non-interactive-mode)。`codex exec --json` 会产生 JSONL 事件流，但在 V1 中仅作为未来的显式无人值守增强层。
